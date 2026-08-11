import argparse
import csv
import math
import struct
from pathlib import Path


PATH_SLOT_SIZE = 0x100
VEGETATION_MODEL_RECORD_SIZE = 0x168


def u32(data, off):
    if off < 0 or off + 4 > len(data):
        return None
    return struct.unpack_from("<I", data, off)[0]


def f32(data, off):
    if off < 0 or off + 4 > len(data):
        return None
    return struct.unpack_from("<f", data, off)[0]


def read_zstring(data, off, max_len=0x100):
    if off < 0 or off >= len(data):
        return ""
    chunk = data[off:min(off + max_len, len(data))]
    zero = chunk.find(b"\x00")
    if zero >= 0:
        chunk = chunk[:zero]
    return chunk.decode("ascii", errors="ignore")


def is_cgf_path(s):
    if not s:
        return False
    low = s.lower()
    return (
        ".cgf" in low
        and len(s) < 240
        and (
            low.startswith("%level%/")
            or low.startswith("objects/")
            or low.startswith("objects\\")
            or low.startswith("brush/")
            or low.startswith("brush\\")
        )
    )


def find_cgf_string_starts(data):
    starts = set()
    lower = data.lower()
    pos = 0
    while True:
        idx = lower.find(b".cgf", pos)
        if idx < 0:
            break
        search_start = max(0, idx - 240)
        start = data.rfind(b"\x00", search_start, idx)
        if start < 0:
            start = search_start
        else:
            start += 1
        s = read_zstring(data, start, PATH_SLOT_SIZE)
        if is_cgf_path(s):
            starts.add(start)
        pos = idx + 4
    return sorted(starts)



def read_full_header_path_table(data):
    """
    Read Prey's complete terrain StatObj table while preserving model indices.

    Prey terrain.dat can contain a vegetation-model definition table before the
    StatObj table.

    Header layout observed in Prey 2017:

        +0x20  uint32 vegetation_model_count
        +0x24  vegetation_model_count records, each 0x168 bytes
               (the record begins with/contains the vegetation CGF path)
        then   uint32 statobj_model_count
        then   statobj_model_count fixed 0x100-byte path slots
        then   uint32 material_count
        then   material_count fixed 0x80-byte material slots

    When vegetation_model_count == 0, statobj_model_count is therefore at
    0x24 and the StatObj table starts at 0x28. That is the layout seen in many
    Prey levels and was the only layout understood by v10/v11.

    Arboretum has vegetation_model_count == 15, so its StatObj count is at
    0x153C and its StatObj table starts at 0x1540.
    """
    vegetation_count = u32(data, 0x20)
    if vegetation_count is None or not (0 <= vegetation_count <= 0xFFFF):
        return None, [], 0

    count_off = 0x24 + vegetation_count * VEGETATION_MODEL_RECORD_SIZE
    count = u32(data, count_off)
    table_off = count_off + 4

    if count is None or not (0 < count <= 0xFFFF):
        return None, [], 0

    table_end = table_off + count * PATH_SLOT_SIZE
    if table_end > len(data):
        return None, [], 0

    paths = [
        read_zstring(data, table_off + i * PATH_SLOT_SIZE, PATH_SLOT_SIZE)
        for i in range(count)
    ]
    valid = sum(1 for path in paths if is_cgf_path(path))

    if valid == 0:
        return None, [], 0

    return table_off, paths, valid


def detect_path_table(data):
    string_starts = find_cgf_string_starts(data)
    best_start = None
    best_paths = []
    best_good = -1
    for start in string_starts:
        first = read_zstring(data, start, PATH_SLOT_SIZE)
        if not is_cgf_path(first):
            continue
        paths = []
        good = 0
        misses = 0
        pos = start
        while pos + PATH_SLOT_SIZE <= len(data):
            s = read_zstring(data, pos, PATH_SLOT_SIZE)
            if is_cgf_path(s):
                good += 1
                misses = 0
                paths.append(s)
            else:
                paths.append(s)
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


def looks_like_aabb(data, off):
    vals = [f32(data, off + i * 4) for i in range(6)]
    if any(v is None for v in vals):
        return False
    if not all(math.isfinite(v) for v in vals):
        return False
    if any(abs(v) > 100000 for v in vals):
        return False
    minx, miny, minz, maxx, maxy, maxz = vals
    if minx > maxx or miny > maxy or minz > maxz:
        return False
    size = abs(maxx - minx) + abs(maxy - miny) + abs(maxz - minz)
    return size >= 0.001


def pos_inside_aabb(px, py, pz, minx, miny, minz, maxx, maxy, maxz, margin=12.0):
    return (
        minx - margin <= px <= maxx + margin
        and miny - margin <= py <= maxy + margin
        and minz - margin <= pz <= maxz + margin
    )


def flags_look_valid(flags1):
    return flags1 is not None and (flags1 & 0xFF000000) == 0x01000000


def parse_matrix(data, off):
    return {
        "M00": f32(data, off + 0),
        "M01": f32(data, off + 4),
        "M02": f32(data, off + 8),
        "PosX": f32(data, off + 12),
        "M10": f32(data, off + 16),
        "M11": f32(data, off + 20),
        "M12": f32(data, off + 24),
        "PosY": f32(data, off + 28),
        "M20": f32(data, off + 32),
        "M21": f32(data, off + 36),
        "M22": f32(data, off + 40),
        "PosZ": f32(data, off + 44),
    }


def matrix_values_valid(m):
    vals = list(m.values())
    if any(v is None for v in vals):
        return False
    if not all(math.isfinite(v) for v in vals):
        return False
    return not any(abs(v) > 100000 for v in vals)


def matrix_basis_reasonable(m):
    """Reject obvious random floats while allowing scaled/rotated CryEngine brushes."""
    rows = [
        (m["M00"], m["M01"], m["M02"]),
        (m["M10"], m["M11"], m["M12"]),
        (m["M20"], m["M21"], m["M22"]),
    ]
    lengths = [math.sqrt(sum(v * v for v in row)) for row in rows]
    if any(not math.isfinite(x) for x in lengths):
        return False
    # Most records are unit or modestly scaled. Keep this permissive for scaled meshes.
    return all(0.0001 <= x <= 1000.0 for x in lengths)


def object_header_looks_valid(data, off):
    w0 = u32(data, off + 0x00)
    w1 = u32(data, off + 0x04)
    w2 = u32(data, off + 0x08)
    w3 = u32(data, off + 0x0C)
    if None in (w0, w1, w2, w3):
        return False
    if w2 != 0 or w3 != 1:
        return False
    if not (w0 < 10 or w0 == 0x00020000):
        return False
    if not (w1 == 0xFFFFFFFF or w1 <= 200):
        return False
    return True


def grid_header_looks_valid(data, off):
    """
    Some terrain.dat files store the same instance payload after a cell/grid style
    header. The first two words decode as world/grid floats, +0x08 is a small
    byte-ish offset/count, and +0x0C is still 1.
    """
    w2 = u32(data, off + 0x08)
    w3 = u32(data, off + 0x0C)
    x = f32(data, off + 0x00)
    y = f32(data, off + 0x04)
    if w2 is None or w3 is None or x is None or y is None:
        return False
    if w3 != 1:
        return False
    if not (0 < w2 < 0x20000):
        return False
    if not (math.isfinite(x) and math.isfinite(y)):
        return False
    if abs(x) > 100000 or abs(y) > 100000:
        return False
    return True


def layout_name(data, off):
    w0 = u32(data, off + 0x00)
    w1 = u32(data, off + 0x04)
    w2 = u32(data, off + 0x08)
    w3 = u32(data, off + 0x0C)
    if w0 == 0 and w1 == 0xFFFFFFFF and w2 == 0 and w3 == 1:
        return "primary"
    if object_header_looks_valid(data, off):
        return "linked"
    if grid_header_looks_valid(data, off):
        return "grid"
    return "payload"


def extract_instances(data, mesh_paths):
    rows = []
    payload_count = 0
    aabb_count = 0
    packed_count = 0
    matrix_count = 0

    for off in range(0, len(data) - 0x80, 4):
        packed = u32(data, off + 0x30)
        if packed is None:
            continue

        # IMPORTANT (v11):
        # +0x30 is not a 0x6464 "record marker".
        #
        #   low 16 bits  = StatObj/model table index
        #   byte +0x32   = view-distance ratio
        #   byte +0x33   = LOD ratio
        #
        # 0x64/0x64 simply means 100/100. Exterior Talos-I shell/LOD
        # objects commonly use 255, 225, 175, 150, 140, 100, 96, 75, 60...
        # Requiring 0x6464 therefore discards valid large structures.
        mesh_index = packed & 0xFFFF
        if mesh_index >= len(mesh_paths):
            continue

        mesh_path = mesh_paths[mesh_index]
        if not is_cgf_path(mesh_path):
            continue

        payload_count += 1

        # Without the old 0x6464 shortcut we require a valid Prey object
        # header. This prevents arbitrary 4-byte-aligned data from being
        # mistaken for a Brush record.
        if not (object_header_looks_valid(data, off) or grid_header_looks_valid(data, off)):
            continue

        aabb_off = off + 0x10
        if not looks_like_aabb(data, aabb_off):
            continue
        aabb_count += 1

        common1 = u32(data, off + 0x28)
        render_flags = u32(data, off + 0x2C)
        if not flags_look_valid(common1):
            continue
        packed_count += 1

        matrix = parse_matrix(data, off + 0x38)
        if not matrix_values_valid(matrix) or not matrix_basis_reasonable(matrix):
            continue
        matrix_count += 1

        minx = f32(data, aabb_off + 0)
        miny = f32(data, aabb_off + 4)
        minz = f32(data, aabb_off + 8)
        maxx = f32(data, aabb_off + 12)
        maxy = f32(data, aabb_off + 16)
        maxz = f32(data, aabb_off + 20)

        pos_good = pos_inside_aabb(
            matrix["PosX"], matrix["PosY"], matrix["PosZ"],
            minx, miny, minz, maxx, maxy, maxz,
        )

        w0 = u32(data, off + 0x00)
        w1 = u32(data, off + 0x04)
        w2 = u32(data, off + 0x08)
        w3 = u32(data, off + 0x0C)

        # Prey v9 terrain Brush common data at +0x28:
        #   uint16 LayerId
        #   int8   ShadowLodBias
        #   uint8  common/dummy byte (normally 1)
        layer_id = common1 & 0xFFFF
        shadow_lod_bias_raw = (common1 >> 16) & 0xFF
        shadow_lod_bias = shadow_lod_bias_raw - 256 if shadow_lod_bias_raw >= 128 else shadow_lod_bias_raw
        common_dummy = (common1 >> 24) & 0xFF

        # Two bytes immediately following the model index.
        view_dist_ratio = (packed >> 16) & 0xFF
        lod_ratio = (packed >> 24) & 0xFF

        row = {
            "OffsetHex": f"0x{off:X}",
            "Layout": layout_name(data, off),
            "Header0": f"0x{w0:08X}",
            "Header1": f"0x{w1:08X}",
            "Header2": f"0x{w2:08X}",
            "Header3": f"0x{w3:08X}",

            # A pivot may legitimately lie outside a huge static object's AABB.
            # Structural validation, not pivot containment, determines whether
            # this is a real Brush.
            "Quality": "strong",
            "PivotInsideAABB": pos_good,

            "MeshIndex": mesh_index,
            "MeshPath": mesh_path,

            "Packed": f"0x{packed:08X}",
            "PackedHigh": f"0x{packed >> 16:04X}",
            "ViewDistRatio": view_dist_ratio,
            "LodRatio": lod_ratio,

            # Keep old names for compatibility, but also expose decoded names.
            "Flags1": f"0x{common1:08X}",
            "Flags2": f"0x{render_flags:08X}" if render_flags is not None else "",
            "LayerId": layer_id,
            "ShadowLodBias": shadow_lod_bias,
            "CommonDummy": common_dummy,
            "RenderFlags": f"0x{render_flags:08X}" if render_flags is not None else "",

            "MinX": minx,
            "MinY": miny,
            "MinZ": minz,
            "MaxX": maxx,
            "MaxY": maxy,
            "MaxZ": maxz,
            **matrix,
        }
        rows.append(row)

    stats = {
        "payload_candidates": payload_count,
        "aabb_matches": aabb_count,
        "packed_flag_matches": packed_count,
        "matrix_matches": matrix_count,
        "total_results": len(rows),
        "strong_results": sum(1 for r in rows if r["Quality"] == "strong"),
        "review_results": sum(1 for r in rows if not r.get("PivotInsideAABB", True)),
    }
    return rows, stats


def write_csv(path, rows):
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = []
    seen = set()
    for row in rows:
        for key in row.keys():
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def process_file(terrain_path):
    data = terrain_path.read_bytes()
    header_size = u32(data, 0x04)
    table_off, mesh_paths, score = read_full_header_path_table(data)
    if table_off is None:
        print(
            f"[WARN] {terrain_path}: header-declared path table invalid; "
            "falling back to readable-path detection without guaranteed "
            "reserved-slot preservation."
        )
        table_off, mesh_paths, score = detect_path_table(data)

    if table_off is None:
        print(f"[FAIL] {terrain_path}")
        print("  Could not resolve CGF path table")
        return

    instances, stats = extract_instances(data, mesh_paths)
    strong = [r for r in instances if r["Quality"] == "strong"]
    out_dir = terrain_path.parent

    path_rows = [
        {
            "Index": i,
            "OffsetHex": f"0x{table_off + i * PATH_SLOT_SIZE:X}",
            "Path": p,
            "LooksLikeCgfPath": is_cgf_path(p),
        }
        for i, p in enumerate(mesh_paths)
    ]

    write_csv(out_dir / "terrain_cgf_paths.csv", path_rows)
    write_csv(out_dir / "terrain_instances_all.csv", instances)
    write_csv(out_dir / "terrain_instances_strong.csv", strong)

    print(f"[OK] {terrain_path}")
    print(f"  File size: {len(data)}")
    print(f"  Header file size: {header_size}")
    vegetation_count = u32(data, 0x20)
    statobj_count_off = (
        0x24 + vegetation_count * VEGETATION_MODEL_RECORD_SIZE
        if vegetation_count is not None else None
    )

    print(f"  Vegetation model definitions: {vegetation_count}")
    if statobj_count_off is not None:
        print(f"  StatObj count offset: 0x{statobj_count_off:X}")
    print(f"  Model table offset: 0x{table_off:X}")
    print(f"  Header model slots: {len(mesh_paths)}")
    print(f"  Valid CGF paths: {sum(1 for p in mesh_paths if is_cgf_path(p))}")
    print(f"  Payload candidates: {stats['payload_candidates']}")
    print(f"  AABB matches: {stats['aabb_matches']}")
    print(f"  Packed/flag matches: {stats['packed_flag_matches']}")
    print(f"  Matrix matches: {stats['matrix_matches']}")
    print(f"  Total results: {stats['total_results']}")
    print(f"  Strong results: {stats['strong_results']}")
    print(f"  Review results: {stats['review_results']}")
    print(f"  Wrote: {out_dir / 'terrain_instances_all_fixed_v12.csv'}")
    print()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", help="Path to terrain.dat or folder containing terrain.dat files")
    args = parser.parse_args()
    input_path = Path(args.input)
    if input_path.is_dir():
        files = list(input_path.rglob("terrain.dat"))
        if not files:
            print("No terrain.dat files found.")
            return
        for file in files:
            process_file(file)
    else:
        process_file(input_path)


if __name__ == "__main__":
    main()
