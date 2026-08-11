"""
Extract Prey (2017) CryEngine indoor.dat Brush instances.

This is a companion to extract_prey_terrain_fixed_v9.py. Its output deliberately
uses the same core column names as the terrain CSV so the same resolver and
Blender import logic can handle both sources:

    MeshIndex, MeshPath
    MinX..MaxZ
    M00..M22, PosX..PosZ
    Quality

Input may be:
- one indoor.dat file;
- one level directory;
- a root directory containing many extracted levels.

For each indoor.dat, the script expects the matching terrain.dat beside it,
usually:

    <Level>/terrain/indoor.dat
    <Level>/terrain/terrain.dat

Outputs beside indoor.dat:
- indoor_instances_all.csv
- indoor_instances_strong.csv
- indoor_areas_fixed.csv

Observed Prey 2017 format:
- VisArea manager version 6
- SVisAreaChunk size 260 bytes
- Octree node header size 32 bytes
- Brush render-node type 1
- Prey Brush record size 104 bytes

Only Python's standard library is required.
"""

from __future__ import annotations

import argparse
import csv
import math
import struct
from collections import Counter
from pathlib import Path
from typing import Iterable


PATH_SLOT_SIZE = 0x100
VEGETATION_MODEL_RECORD_SIZE = 0x168
MATERIAL_SLOT_SIZE = 0x80

VISAREA_MANAGER_HEADER_SIZE = 20
VISAREA_CHUNK_SIZE = 260
OCTREE_NODE_HEADER_SIZE = 32
PREY_BRUSH_RECORD_SIZE = 104

EXPECTED_MANAGER_VERSION = 6
EXPECTED_VISAREA_CHUNK_VERSION = 2
EXPECTED_OCTREE_VERSION = 7
RENDER_NODE_BRUSH = 1


# ---------------------------------------------------------------------------
# Primitive readers
# ---------------------------------------------------------------------------

def u16(data: bytes, off: int) -> int | None:
    if off < 0 or off + 2 > len(data):
        return None
    return struct.unpack_from("<H", data, off)[0]


def i16(data: bytes, off: int) -> int | None:
    if off < 0 or off + 2 > len(data):
        return None
    return struct.unpack_from("<h", data, off)[0]


def u32(data: bytes, off: int) -> int | None:
    if off < 0 or off + 4 > len(data):
        return None
    return struct.unpack_from("<I", data, off)[0]


def i32(data: bytes, off: int) -> int | None:
    if off < 0 or off + 4 > len(data):
        return None
    return struct.unpack_from("<i", data, off)[0]


def f32(data: bytes, off: int) -> float | None:
    if off < 0 or off + 4 > len(data):
        return None
    return struct.unpack_from("<f", data, off)[0]


def read_zstring(data: bytes, off: int, max_len: int) -> str:
    if off < 0 or off >= len(data):
        return ""
    chunk = data[off : min(off + max_len, len(data))]
    zero = chunk.find(b"\x00")
    if zero >= 0:
        chunk = chunk[:zero]
    return chunk.decode("utf-8", errors="replace")


def all_finite(values: Iterable[float]) -> bool:
    return all(math.isfinite(value) for value in values)


# ---------------------------------------------------------------------------
# terrain.dat common CGF/material tables
# ---------------------------------------------------------------------------

def is_cgf_path(value: str) -> bool:
    if not value:
        return False
    low = value.lower()
    return (
        ".cgf" in low
        and len(value) < 240
        and (
            low.startswith("%level%/")
            or low.startswith("objects/")
            or low.startswith("objects\\")
            or low.startswith("brush/")
            or low.startswith("brush\\")
        )
    )


def find_cgf_string_starts(data: bytes) -> list[int]:
    starts: set[int] = set()
    lower = data.lower()
    pos = 0

    while True:
        idx = lower.find(b".cgf", pos)
        if idx < 0:
            break

        search_start = max(0, idx - 240)
        start = data.rfind(b"\x00", search_start, idx)
        start = search_start if start < 0 else start + 1

        value = read_zstring(data, start, PATH_SLOT_SIZE)
        if is_cgf_path(value):
            starts.add(start)

        pos = idx + 4

    return sorted(starts)


def detect_path_table(data: bytes) -> tuple[int | None, list[str], int]:
    
    best_start: int | None = None
    best_paths: list[str] = []
    best_good = -1

    for start in find_cgf_string_starts(data):
        first = read_zstring(data, start, PATH_SLOT_SIZE)
        if not is_cgf_path(first):
            continue

        paths: list[str] = []
        good = 0
        misses = 0
        pos = start

        while pos + PATH_SLOT_SIZE <= len(data):
            value = read_zstring(data, pos, PATH_SLOT_SIZE)
            if is_cgf_path(value):
                good += 1
                misses = 0
                paths.append(value)
            else:
                paths.append(value)
                misses += 1
                if misses >= 8:
                    break
            pos += PATH_SLOT_SIZE

        while paths and not is_cgf_path(paths[-1]):
            paths.pop()

        if good > best_good:
            best_good = good
            best_start = start
            best_paths = paths

    return best_start, best_paths, best_good


def parse_common_tables(terrain_data: bytes) -> tuple[list[str], list[str], int]:
    """
    Parse the shared StatObj/material tables from terrain.dat.

    Prey can place a vegetation-model table before the StatObj table:

        +0x20  uint32 vegetation_model_count
        +0x24  vegetation_model_count records of 0x168 bytes
        then   uint32 statobj_model_count
        then   statobj_model_count x 0x100-byte path slots
        then   uint32 material_count
        then   material_count x 0x80-byte material slots

    Empty StatObj slots are preserved because indoor Brush MeshIndex values
    index the complete table.
    """
    vegetation_count = u32(terrain_data, 0x20)

    if vegetation_count is not None and 0 <= vegetation_count <= 65535:
        model_count_offset = (
            0x24 + vegetation_count * VEGETATION_MODEL_RECORD_SIZE
        )
        model_count = u32(terrain_data, model_count_offset)
        table_offset = model_count_offset + 4

        if (
            model_count is not None
            and 0 < model_count <= 65535
            and table_offset + model_count * PATH_SLOT_SIZE + 4
            <= len(terrain_data)
        ):
            models = [
                read_zstring(
                    terrain_data,
                    table_offset + index * PATH_SLOT_SIZE,
                    PATH_SLOT_SIZE,
                )
                for index in range(model_count)
            ]

            material_count_offset = (
                table_offset + model_count * PATH_SLOT_SIZE
            )
            material_count = u32(
                terrain_data,
                material_count_offset,
            )

            if (
                material_count is not None
                and 0 <= material_count <= 65535
                and material_count_offset
                    + 4
                    + material_count * MATERIAL_SLOT_SIZE
                    <= len(terrain_data)
            ):
                material_table_offset = material_count_offset + 4
                materials = [
                    read_zstring(
                        terrain_data,
                        material_table_offset
                            + index * MATERIAL_SLOT_SIZE,
                        MATERIAL_SLOT_SIZE,
                    )
                    for index in range(material_count)
                ]

                valid_paths = sum(is_cgf_path(path) for path in models)
                if valid_paths > 0:
                    return models, materials, table_offset

    # Conservative fallback for a still-unknown file variant.
    detected_offset, detected_paths, _score = detect_path_table(terrain_data)
    if detected_offset is None or not detected_paths:
        raise ValueError("Could not resolve the terrain.dat CGF path table")

    print(
        "[WARN] Structured model table parsing failed; using detected CGF "
        "table without guaranteed reserved-slot preservation."
    )
    return detected_paths, [], detected_offset


# ---------------------------------------------------------------------------
# indoor.dat area and octree parsing
# ---------------------------------------------------------------------------

def parse_area(
    data: bytes,
    offset: int,
    category: str,
    index: int,
) -> tuple[dict, int]:
    if offset + VISAREA_CHUNK_SIZE + 4 > len(data):
        raise ValueError(f"Truncated SVisAreaChunk at 0x{offset:X}")

    start = offset
    chunk_version = i32(data, offset)
    if chunk_version is None:
        raise ValueError(f"Missing VisArea chunk version at 0x{offset:X}")

    box_area = struct.unpack_from("<6f", data, offset + 4)
    box_statics = struct.unpack_from("<6f", data, offset + 28)
    name = read_zstring(data, offset + 52, 32)
    object_tree_size = i32(data, offset + 84)
    connections = struct.unpack_from("<30i", data, offset + 88)
    flags = u32(data, offset + 208)
    portal_blending = f32(data, offset + 212)
    connection_normals = struct.unpack_from("<6f", data, offset + 216)
    height = f32(data, offset + 240)
    ambient = struct.unpack_from("<3f", data, offset + 244)
    view_distance_ratio = f32(data, offset + 256)

    if object_tree_size is None or object_tree_size < 0:
        raise ValueError(f"Invalid object tree size at 0x{offset + 84:X}")

    offset += VISAREA_CHUNK_SIZE
    point_count = i32(data, offset)
    offset += 4

    if point_count is None or point_count < 0:
        raise ValueError(f"Invalid shape point count at 0x{offset - 4:X}")
    if offset + point_count * 12 > len(data):
        raise ValueError(f"Shape points exceed file at 0x{offset:X}")

    points = [
        struct.unpack_from("<3f", data, offset + point_index * 12)
        for point_index in range(point_count)
    ]
    offset += point_count * 12

    tree_start = offset
    tree_end = tree_start + object_tree_size
    if tree_end > len(data):
        raise ValueError(
            f"Object tree for {name!r} ends beyond the file: 0x{tree_end:X}"
        )

    area = {
        "Category": category,
        "Index": index,
        "FileOffset": start,
        "ChunkVersion": chunk_version,
        "BoxArea": box_area,
        "BoxStatics": box_statics,
        "Name": name,
        "ObjectTreeSize": object_tree_size,
        "Connections": connections,
        "Flags": flags or 0,
        "PortalBlending": portal_blending if portal_blending is not None else 0.0,
        "ConnectionNormals": connection_normals,
        "Height": height if height is not None else 0.0,
        "Ambient": ambient,
        "ViewDistanceRatio": (
            view_distance_ratio if view_distance_ratio is not None else 0.0
        ),
        "PointCount": point_count,
        "Points": points,
        "TreeStart": tree_start,
        "TreeEnd": tree_end,
    }
    return area, tree_end


def parse_octree_nodes(data: bytes, start: int, end: int) -> list[dict]:
    nodes: list[dict] = []

    def parse_node(offset: int, depth: int) -> int:
        if offset + OCTREE_NODE_HEADER_SIZE > end:
            raise ValueError(f"Truncated octree node at 0x{offset:X}")

        version, child_mask_signed = struct.unpack_from("<hh", data, offset)
        child_mask = child_mask_signed & 0xFFFF
        node_box = struct.unpack_from("<6f", data, offset + 4)
        object_block_size = i32(data, offset + 28)

        if object_block_size is None or object_block_size < 0:
            raise ValueError(f"Invalid object block size at 0x{offset + 28:X}")

        object_start = offset + OCTREE_NODE_HEADER_SIZE
        object_end = object_start + object_block_size
        if object_end > end:
            raise ValueError(
                f"Object block at 0x{offset:X} ends beyond its area tree"
            )

        node = {
            "FileOffset": offset,
            "Version": version,
            "ChildMask": child_mask,
            "Box": node_box,
            "ObjectBlockSize": object_block_size,
            "ObjectStart": object_start,
            "ObjectEnd": object_end,
            "Depth": depth,
        }
        nodes.append(node)

        cursor = object_end
        for bit in range(8):
            if child_mask & (1 << bit):
                cursor = parse_node(cursor, depth + 1)

        node["End"] = cursor
        return cursor

    final_offset = parse_node(start, 0) if start < end else start
    if final_offset != end:
        raise ValueError(
            f"Octree ended at 0x{final_offset:X}; expected 0x{end:X}"
        )

    return nodes

# ---------------------------------------------------------------------------
# Brush extraction
# ---------------------------------------------------------------------------

def brush_record_is_valid(
    data: bytes,
    position: int,
    node: dict,
    model_count: int,
) -> bool:
    if position + PREY_BRUSH_RECORD_SIZE > node["ObjectEnd"]:
        return False

    if u32(data, position) != RENDER_NODE_BRUSH:
        return False

    bbox = struct.unpack_from("<6f", data, position + 4)
    if not all_finite(bbox) or any(abs(value) > 100000 for value in bbox):
        return False

    minx, miny, minz, maxx, maxy, maxz = bbox
    if minx > maxx or miny > maxy or minz > maxz:
        return False

    mesh_index = u16(data, position + 36)
    if mesh_index is None or mesh_index >= model_count:
        return False

    matrix = struct.unpack_from("<12f", data, position + 44)
    if not all_finite(matrix) or any(abs(value) > 100000 for value in matrix):
        return False

    rows = (matrix[0:3], matrix[4:7], matrix[8:11])
    row_lengths = [math.sqrt(sum(value * value for value in row)) for row in rows]
    if any(length < 0.0001 or length > 1000.0 for length in row_lengths):
        return False

    # The instance AABB should overlap its containing octree node AABB.
    node_box = node["Box"]
    bbox_min = bbox[:3]
    bbox_max = bbox[3:]
    for axis in range(3):
        if (
            bbox_max[axis] < node_box[axis] - 0.001
            or bbox_min[axis] > node_box[axis + 3] + 0.001
        ):
            return False

    return True


def parse_brush_record(
    data: bytes,
    position: int,
    area: dict,
    node: dict,
    models: list[str],
    materials: list[str],
) -> dict:
    bbox = struct.unpack_from("<6f", data, position + 4)
    matrix = struct.unpack_from("<12f", data, position + 44)
    mesh_index = u16(data, position + 36)
    material_index = i32(data, position + 96)

    assert mesh_index is not None
    assert material_index is not None

    material_path = ""
    if 0 <= material_index < len(materials):
        material_path = materials[material_index]

    return {
        # Terrain-compatible core fields.
        "SourceType": "Indoor",
        "OffsetHex": f"0x{position:X}",
        "Layout": "indoor",
        "Header0": "",
        "Header1": "",
        "Header2": "",
        "Header3": "",
        "Quality": "strong",
        "MeshIndex": mesh_index,
        "MeshPath": models[mesh_index],
        "Packed": "",
        "PackedHigh": "",
        "Flags1": f"0x{(u32(data, position + 32) or 0):08X}",
        "Flags2": "",
        "MinX": bbox[0],
        "MinY": bbox[1],
        "MinZ": bbox[2],
        "MaxX": bbox[3],
        "MaxY": bbox[4],
        "MaxZ": bbox[5],
        "M00": matrix[0],
        "M01": matrix[1],
        "M02": matrix[2],
        "PosX": matrix[3],
        "M10": matrix[4],
        "M11": matrix[5],
        "M12": matrix[6],
        "PosY": matrix[7],
        "M20": matrix[8],
        "M21": matrix[9],
        "M22": matrix[10],
        "PosZ": matrix[11],
        # Indoor-specific metadata.
        "AreaCategory": area["Category"],
        "AreaIndex": area["Index"],
        "AreaName": area["Name"],
        "NodeDepth": node["Depth"],
        "NodeOffsetHex": f"0x{node['FileOffset']:X}",
        "LayerId": u16(data, position + 28) or 0,
        "ShadowLodBias": struct.unpack_from("<b", data, position + 30)[0],
        "CommonDummy": data[position + 31],
        "RenderFlags": f"0x{(u32(data, position + 32) or 0):08X}",
        "ViewDistanceRatio": data[position + 38],
        "LodRatio": data[position + 39],
        "ArkaneCommonU32": f"0x{(u32(data, position + 40) or 0):08X}",
        "CollisionClassIndex": i16(data, position + 92) or 0,
        "BrushFlags": f"0x{(u16(data, position + 94) or 0):04X}",
        "MaterialIndex": material_index,
        "MaterialPath": material_path,
        "MaterialLayers": i32(data, position + 100) or 0,
        "NodeMinX": node["Box"][0],
        "NodeMinY": node["Box"][1],
        "NodeMinZ": node["Box"][2],
        "NodeMaxX": node["Box"][3],
        "NodeMaxY": node["Box"][4],
        "NodeMaxZ": node["Box"][5],
    }


def parse_indoor_and_terrain(
    indoor_path: Path,
    terrain_path: Path,
) -> tuple[dict, list[dict], list[dict]]:
    indoor_data = indoor_path.read_bytes()
    terrain_data = terrain_path.read_bytes()
    models, materials, table_offset = parse_common_tables(terrain_data)

    if len(indoor_data) < VISAREA_MANAGER_HEADER_SIZE:
        raise ValueError("indoor.dat is too small")

    (
        version,
        dummy,
        serialization_flags,
        flags2,
        declared_size,
        visarea_count,
        portal_count,
        occlusion_count,
    ) = struct.unpack_from("<4B4I", indoor_data, 0)

    if declared_size != len(indoor_data):
        raise ValueError(
            f"Header says {declared_size} bytes, file has {len(indoor_data)}"
        )

    areas: list[dict] = []
    cursor = VISAREA_MANAGER_HEADER_SIZE

    for category, count in (
        ("visarea", visarea_count),
        ("portal", portal_count),
        ("occlusion", occlusion_count),
    ):
        for index in range(count):
            area, cursor = parse_area(indoor_data, cursor, category, index)
            area["Nodes"] = parse_octree_nodes(
                indoor_data,
                area["TreeStart"],
                area["TreeEnd"],
            )
            areas.append(area)

    if cursor != len(indoor_data):
        raise ValueError(
            f"Area parser ended at 0x{cursor:X}; file ends at "
            f"0x{len(indoor_data):X}"
        )

    brushes: list[dict] = []
    seen_offsets: set[int] = set()

    for area in areas:
        for node in area["Nodes"]:
            # Other render-node types have different record sizes. Scanning the
            # node's own object block at 4-byte alignment is robust for Prey,
            # while the structural checks reject non-Brush data.
            last_start = node["ObjectEnd"] - PREY_BRUSH_RECORD_SIZE
            for position in range(node["ObjectStart"], last_start + 1, 4):
                if position in seen_offsets:
                    continue
                if brush_record_is_valid(
                    indoor_data,
                    position,
                    node,
                    len(models),
                ):
                    seen_offsets.add(position)
                    brushes.append(
                        parse_brush_record(
                            indoor_data,
                            position,
                            area,
                            node,
                            models,
                            materials,
                        )
                    )

    metadata = {
        "ManagerVersion": version,
        "Dummy": dummy,
        "SerializationFlags": serialization_flags,
        "Flags2": flags2,
        "DeclaredSize": declared_size,
        "VisAreaCount": visarea_count,
        "PortalCount": portal_count,
        "OcclusionCount": occlusion_count,
        "ModelCount": len(models),
        "MaterialCount": len(materials),
        "ModelTableOffset": table_offset,
    }
    return metadata, areas, brushes


# ---------------------------------------------------------------------------
# CSV writing
# ---------------------------------------------------------------------------

INSTANCE_FIELDS = [
    "SourceType",
    "OffsetHex",
    "Layout",
    "Header0",
    "Header1",
    "Header2",
    "Header3",
    "Quality",
    "MeshIndex",
    "MeshPath",
    "Packed",
    "PackedHigh",
    "Flags1",
    "Flags2",
    "MinX",
    "MinY",
    "MinZ",
    "MaxX",
    "MaxY",
    "MaxZ",
    "M00",
    "M01",
    "M02",
    "PosX",
    "M10",
    "M11",
    "M12",
    "PosY",
    "M20",
    "M21",
    "M22",
    "PosZ",
    "AreaCategory",
    "AreaIndex",
    "AreaName",
    "NodeDepth",
    "NodeOffsetHex",
    "LayerId",
    "ShadowLodBias",
    "CommonDummy",
    "RenderFlags",
    "ViewDistanceRatio",
    "LodRatio",
    "ArkaneCommonU32",
    "CollisionClassIndex",
    "BrushFlags",
    "MaterialIndex",
    "MaterialPath",
    "MaterialLayers",
    "NodeMinX",
    "NodeMinY",
    "NodeMinZ",
    "NodeMaxX",
    "NodeMaxY",
    "NodeMaxZ",
]

AREA_FIELDS = [
    "Category",
    "Index",
    "Name",
    "FileOffsetHex",
    "ChunkVersion",
    "ObjectTreeSize",
    "OctreeNodeCount",
    "MaxOctreeDepth",
    "BrushCount",
    "UniqueBrushModels",
    "PointCount",
    "FlagsHex",
    "Height",
    "PortalBlending",
    "ViewDistanceRatio",
    "AreaMinX",
    "AreaMinY",
    "AreaMinZ",
    "AreaMaxX",
    "AreaMaxY",
    "AreaMaxZ",
    "StaticsMinX",
    "StaticsMinY",
    "StaticsMinZ",
    "StaticsMaxX",
    "StaticsMaxY",
    "StaticsMaxZ",
    "AmbientR",
    "AmbientG",
    "AmbientB",
    "Connections",
]


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fieldnames,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)


def area_rows(areas: list[dict], brushes: list[dict]) -> list[dict]:
    brush_counts = Counter(
        (brush["AreaCategory"], int(brush["AreaIndex"])) for brush in brushes
    )
    model_sets: dict[tuple[str, int], set[int]] = {}
    for brush in brushes:
        key = (brush["AreaCategory"], int(brush["AreaIndex"]))
        model_sets.setdefault(key, set()).add(int(brush["MeshIndex"]))

    output: list[dict] = []
    for area in areas:
        key = (area["Category"], area["Index"])
        box_area = area["BoxArea"]
        box_statics = area["BoxStatics"]
        output.append({
            "Category": area["Category"],
            "Index": area["Index"],
            "Name": area["Name"],
            "FileOffsetHex": f"0x{area['FileOffset']:X}",
            "ChunkVersion": area["ChunkVersion"],
            "ObjectTreeSize": area["ObjectTreeSize"],
            "OctreeNodeCount": len(area["Nodes"]),
            "MaxOctreeDepth": max(
                (node["Depth"] for node in area["Nodes"]),
                default=0,
            ),
            "BrushCount": brush_counts[key],
            "UniqueBrushModels": len(model_sets.get(key, set())),
            "PointCount": area["PointCount"],
            "FlagsHex": f"0x{area['Flags']:08X}",
            "Height": area["Height"],
            "PortalBlending": area["PortalBlending"],
            "ViewDistanceRatio": area["ViewDistanceRatio"],
            "AreaMinX": box_area[0],
            "AreaMinY": box_area[1],
            "AreaMinZ": box_area[2],
            "AreaMaxX": box_area[3],
            "AreaMaxY": box_area[4],
            "AreaMaxZ": box_area[5],
            "StaticsMinX": box_statics[0],
            "StaticsMinY": box_statics[1],
            "StaticsMinZ": box_statics[2],
            "StaticsMaxX": box_statics[3],
            "StaticsMaxY": box_statics[4],
            "StaticsMaxZ": box_statics[5],
            "AmbientR": area["Ambient"][0],
            "AmbientG": area["Ambient"][1],
            "AmbientB": area["Ambient"][2],
            "Connections": ";".join(
                str(value) for value in area["Connections"] if value >= 0
            ),
        })

    return output


# ---------------------------------------------------------------------------
# Recursive processing
# ---------------------------------------------------------------------------

def find_indoor_files(input_path: Path) -> list[Path]:
    if input_path.is_file():
        if input_path.name.lower() != "indoor.dat":
            raise ValueError("Input file must be named indoor.dat")
        return [input_path]

    if not input_path.is_dir():
        raise FileNotFoundError(input_path)

    return sorted(
        path
        for path in input_path.rglob("*")
        if path.is_file() and path.name.lower() == "indoor.dat"
    )


def process_file(indoor_path: Path) -> bool:
    terrain_path = indoor_path.with_name("terrain.dat")
    if not terrain_path.is_file():
        print(f"[SKIP] {indoor_path}")
        print(f"  Matching terrain.dat not found: {terrain_path}")
        return False

    try:
        metadata, areas, brushes = parse_indoor_and_terrain(
            indoor_path,
            terrain_path,
        )
    except Exception as exc:
        print(f"[FAIL] {indoor_path}")
        print(f"  {type(exc).__name__}: {exc}")
        return False

    out_dir = indoor_path.parent
    all_path = out_dir / "indoor_instances_allcsv"
    strong_path = out_dir / "indoor_instances_strong.csv"
    areas_path = out_dir / "indoor_areas.csv"

    write_csv(all_path, brushes, INSTANCE_FIELDS)
    # At present every emitted row has passed the full structural validation.
    write_csv(strong_path, brushes, INSTANCE_FIELDS)
    write_csv(areas_path, area_rows(areas, brushes), AREA_FIELDS)

    unique_models = len({int(row["MeshIndex"]) for row in brushes})
    octree_nodes = sum(len(area["Nodes"]) for area in areas)
    chunk_versions = sorted({int(area["ChunkVersion"]) for area in areas})
    octree_versions = sorted(
        {int(node["Version"]) for area in areas for node in area["Nodes"]}
    )

    print(f"[OK] {indoor_path}")
    print(f"  Matching terrain.dat: {terrain_path}")
    print(f"  File size: {metadata['DeclaredSize']}")
    print(f"  Manager version: {metadata['ManagerVersion']}")
    print(f"  VisArea chunk versions: {chunk_versions}")
    print(f"  Octree node versions: {octree_versions}")
    print(f"  VisAreas: {metadata['VisAreaCount']}")
    print(f"  Portals: {metadata['PortalCount']}")
    print(f"  Occlusion areas: {metadata['OcclusionCount']}")
    print(f"  Octree nodes: {octree_nodes}")
    print(f"  Shared CGF paths: {metadata['ModelCount']}")
    print(f"  Shared material paths: {metadata['MaterialCount']}")
    print(f"  Brush instances: {len(brushes)}")
    print(f"  Unique Brush models: {unique_models}")
    print(f"  Wrote: {strong_path}")
    print(f"  Wrote: {areas_path}")
    print()

    # Warnings only: keep files usable if another Prey map varies slightly.
    if metadata["ManagerVersion"] != EXPECTED_MANAGER_VERSION:
        print(
            f"  WARNING: expected manager version {EXPECTED_MANAGER_VERSION}, "
            f"got {metadata['ManagerVersion']}"
        )
    if chunk_versions and chunk_versions != [EXPECTED_VISAREA_CHUNK_VERSION]:
        print(
            f"  WARNING: expected VisArea chunk version "
            f"{EXPECTED_VISAREA_CHUNK_VERSION}, got {chunk_versions}"
        )
    if octree_versions and octree_versions != [EXPECTED_OCTREE_VERSION]:
        print(
            f"  WARNING: expected octree version {EXPECTED_OCTREE_VERSION}, "
            f"got {octree_versions}"
        )

    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract Prey 2017 indoor.dat Brush instances recursively"
    )
    parser.add_argument(
        "input",
        help="Path to indoor.dat, one level folder, or a levels root folder",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    try:
        indoor_files = find_indoor_files(input_path)
    except Exception as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1) from exc

    if not indoor_files:
        print("No indoor.dat files found.")
        return

    succeeded = 0
    for indoor_path in indoor_files:
        if process_file(indoor_path):
            succeeded += 1

    print(f"Processed {succeeded}/{len(indoor_files)} indoor.dat files successfully.")


if __name__ == "__main__":
    main()
