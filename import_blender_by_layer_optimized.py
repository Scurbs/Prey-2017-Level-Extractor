"""
Prey (2017) combined terrain + indoor Brush importer for Blender.

OPTIMIZED GEOMETRY-NODES VERSION
================================

Instead of creating one Blender Object/Empty per CSV placement, this importer:

1. Imports every unique USD asset once into an unlinked source collection.
2. Creates one prototype Empty per unique asset in an unlinked prototype collection.
3. Stores all placements for a Layer / SourceType as POINTS in a mesh.
4. Uses one shared Geometry Nodes group to instance the correct prototype on each point.

Typical result:
    36,000 CSV placements
        -> ~36,000 points
        -> only ~one visible Blender object per Layer/Indoor/Outdoor group

This greatly reduces Outliner/depsgraph/object-management overhead while preserving
real instancing of the source geometry.

Trade-off:
- Individual placements are no longer separate selectable Blender objects.
- Per-placement metadata is stored as point attributes instead of Object properties.
- Matrices that cannot be represented accurately as Location/Rotation/Scale are
  automatically kept as normal collection-instance objects as a correctness fallback.
"""

from __future__ import annotations

import csv
import hashlib
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

import bpy
from mathutils import Matrix


# ============================================================================
# CONFIGURATION
# ============================================================================

#Path to your prey_import_combined.csv
CSV_PATH = Path(
    r"...\prey_import_combined.csv"
)


ROOT_COLLECTION_NAME = "Prey_Combined"
LAYERS_COLLECTION_NAME = "Layers"
SOURCE_COLLECTION_PREFIX = "__PREY_SOURCE_PATH__"
PROTOTYPE_COLLECTION_NAME = "__PREY_GN_PROTOTYPES__"
GN_GROUP_NAME = "__PREY_GN_INSTANCER__"
FALLBACK_PREFIX = "__PREY_MATRIX_FALLBACK__"

DELETE_PREVIOUS_IMPORT = True

# Create Indoor / Outdoor children under every Layer_XXX collection.
SPLIT_BY_SOURCE_TYPE = True

# Store useful numeric metadata on the placement points.
ADD_POINT_METADATA = True

# If the original 4x4 matrix cannot be reconstructed from location/rotation/scale
# within this tolerance, keep that placement as a normal Empty collection instance.
# CryEngine brush transforms normally should be TRS, so fallbacks should be rare/zero.
TRS_MATRIX_TOLERANCE = 1.0e-4

# USD import options.
IMPORT_MATERIALS = True
IMPORT_CURVES = True

# Optional filters:
SOURCE_TYPES: set[str] | None = None       # Example: {"Indoor"}
AREA_NAMES: set[str] | None = None         # Example: {"visarea_reactor"}
MODEL_PATH_CONTAINS = ""
MAX_INSTANCES = 0                          # 0 = unlimited


# ============================================================================
# GENERAL HELPERS
# ============================================================================

def safe_name(value: str, maximum: int = 56) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value))
    return cleaned[:maximum] or "unnamed"


def canonical_asset_key(resolved_usd: str) -> str:
    """Use the resolved absolute file path as the cache key."""
    path = Path(resolved_usd).expanduser()
    try:
        path = path.resolve(strict=False)
    except OSError:
        pass
    return path.as_posix().casefold()


def source_collection_name(asset_key: str, usd_path: Path) -> str:
    digest = hashlib.sha1(asset_key.encode("utf-8")).hexdigest()[:12]
    return (
        f"{SOURCE_COLLECTION_PREFIX}{digest}_"
        f"{safe_name(usd_path.stem, 36)}"
    )


def cry_matrix(row: dict[str, str]) -> Matrix:
    return Matrix((
        (
            float(row["M00"]),
            float(row["M01"]),
            float(row["M02"]),
            float(row["PosX"]),
        ),
        (
            float(row["M10"]),
            float(row["M11"]),
            float(row["M12"]),
            float(row["PosY"]),
        ),
        (
            float(row["M20"]),
            float(row["M21"]),
            float(row["M22"]),
            float(row["PosZ"]),
        ),
        (0.0, 0.0, 0.0, 1.0),
    ))


def reconstruct_trs(location, rotation_quaternion, scale) -> Matrix:
    """Build a 4x4 transform without relying on Matrix.LocRotScale."""
    translation = Matrix.Translation(location)
    rotation = rotation_quaternion.to_matrix().to_4x4()
    scaling = Matrix.Diagonal((scale.x, scale.y, scale.z, 1.0))
    return translation @ rotation @ scaling


def matrix_max_abs_difference(a: Matrix, b: Matrix) -> float:
    return max(
        abs(a[row][column] - b[row][column])
        for row in range(4)
        for column in range(4)
    )


def decompose_for_geometry_nodes(matrix: Matrix):
    """
    Convert the original matrix to Geometry Nodes point attributes.

    Returns:
        (location_xyz, rotation_euler_xyz, scale_xyz, error)
    """
    location, rotation, scale = matrix.decompose()
    rebuilt = reconstruct_trs(location, rotation, scale)
    error = matrix_max_abs_difference(matrix, rebuilt)
    euler = rotation.to_euler("XYZ")

    return (
        (location.x, location.y, location.z),
        (euler.x, euler.y, euler.z),
        (scale.x, scale.y, scale.z),
        error,
    )


def parse_int_auto(value: object) -> int | None:
    """Parse decimal, 0x-prefixed hexadecimal, or simple float-like integers."""
    text = str(value or "").strip()
    if not text:
        return None

    try:
        return int(text, 0)
    except ValueError:
        pass

    try:
        return int(float(text))
    except ValueError:
        pass

    if re.fullmatch(r"[0-9A-Fa-f]+", text):
        return int(text, 16)

    return None


def get_layer_id(row: dict[str, str]) -> int | None:
    """
    Indoor: use LayerId directly.
    Outdoor: if LayerId is empty, decode the lower 16 bits of Flags1.
    """
    direct_layer = parse_int_auto(row.get("LayerId", ""))
    if direct_layer is not None:
        return direct_layer

    source_type = str(row.get("SourceType", "")).strip().casefold()
    flags1 = parse_int_auto(row.get("Flags1", ""))

    if source_type == "outdoor" and flags1 is not None:
        return flags1 & 0xFFFF

    if not source_type and flags1 is not None:
        return flags1 & 0xFFFF

    return None


def layer_collection_name(layer_id: int | None) -> str:
    if layer_id is None:
        return "Layer_Unknown"
    return f"Layer_{layer_id:03d}"


# ============================================================================
# COLLECTION / CLEANUP MANAGEMENT
# ============================================================================

def remove_collection_recursive(collection: bpy.types.Collection) -> None:
    for child in list(collection.children):
        remove_collection_recursive(child)

    for obj in list(collection.objects):
        bpy.data.objects.remove(obj, do_unlink=True)

    bpy.data.collections.remove(collection)


def clear_previous_import() -> None:
    root = bpy.data.collections.get(ROOT_COLLECTION_NAME)
    if root is not None:
        remove_collection_recursive(root)

    prototype_collection = bpy.data.collections.get(PROTOTYPE_COLLECTION_NAME)
    if prototype_collection is not None:
        remove_collection_recursive(prototype_collection)

    for collection in list(bpy.data.collections):
        if collection.name.startswith(SOURCE_COLLECTION_PREFIX):
            remove_collection_recursive(collection)

    for node_group in list(bpy.data.node_groups):
        if node_group.name.startswith(GN_GROUP_NAME):
            bpy.data.node_groups.remove(node_group)

    # Remove old point meshes created by this script if they became orphaned.
    for mesh in list(bpy.data.meshes):
        if mesh.name.startswith("PreyPoints_") and mesh.users == 0:
            bpy.data.meshes.remove(mesh)


def new_linked_collection(
    name: str,
    parent: bpy.types.Collection,
) -> bpy.types.Collection:
    collection = bpy.data.collections.new(name)
    parent.children.link(collection)
    return collection


# ============================================================================
# CSV
# ============================================================================

def read_rows() -> list[dict[str, str]]:
    if not CSV_PATH.is_file():
        raise FileNotFoundError(f"CSV not found: {CSV_PATH}")

    required = {
        "ResolvedUSD",
        "MeshPath",
        "MeshIndex",
        "M00", "M01", "M02", "PosX",
        "M10", "M11", "M12", "PosY",
        "M20", "M21", "M22", "PosZ",
    }

    rows: list[dict[str, str]] = []
    contains = MODEL_PATH_CONTAINS.casefold().strip()

    with CSV_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = required.difference(reader.fieldnames or [])
        if missing:
            raise ValueError(
                "CSV is missing columns: " + ", ".join(sorted(missing))
            )

        for source_row in reader:
            row = dict(source_row)

            source_type = str(row.get("SourceType", "")).strip()
            area_name = str(row.get("AreaName", "")).strip()
            mesh_path = str(row.get("MeshPath", ""))
            resolved = str(row.get("ResolvedUSD", "")).strip()

            if SOURCE_TYPES is not None and source_type not in SOURCE_TYPES:
                continue
            if AREA_NAMES is not None and area_name not in AREA_NAMES:
                continue
            if contains and contains not in mesh_path.casefold():
                continue
            if not resolved:
                continue

            layer_id = get_layer_id(row)
            row["_CalculatedLayerId"] = (
                "" if layer_id is None else str(layer_id)
            )

            rows.append(row)

            if MAX_INSTANCES > 0 and len(rows) >= MAX_INSTANCES:
                break

    return rows


# ============================================================================
# USD IMPORT
# ============================================================================

def import_usd_source(
    usd_path: Path,
    asset_key: str,
) -> bpy.types.Collection:
    before_objects = set(bpy.data.objects)
    before_collections = set(bpy.data.collections)

    result = bpy.ops.wm.usd_import(
        filepath=str(usd_path),
        import_cameras=False,
        import_curves=IMPORT_CURVES,
        import_lights=False,
        import_materials=IMPORT_MATERIALS,
        import_meshes=True,
        import_volumes=False,
        relative_path=True,
        set_frame_range=False,
        validate_meshes=False,
    )

    if "FINISHED" not in result:
        raise RuntimeError(f"Blender failed to import: {usd_path}")

    new_objects = [
        obj for obj in bpy.data.objects
        if obj not in before_objects
    ]
    new_collections = [
        collection for collection in bpy.data.collections
        if collection not in before_collections
    ]

    if not new_objects:
        raise RuntimeError(f"USD import produced no objects: {usd_path}")

    source = bpy.data.collections.new(
        source_collection_name(asset_key, usd_path)
    )

    # Preserve parenting and transforms; only change collection membership.
    for obj in new_objects:
        for collection in list(obj.users_collection):
            collection.objects.unlink(obj)
        source.objects.link(obj)

    # Remove empty collections created by the USD importer.
    changed = True
    while changed:
        changed = False
        for collection in list(new_collections):
            if collection.name not in bpy.data.collections:
                continue
            if collection.objects or collection.children:
                continue
            bpy.data.collections.remove(collection)
            changed = True

    source["prey_asset_key"] = asset_key
    source["prey_resolved_usd"] = str(usd_path)

    # Intentionally remains unlinked from the scene.
    return source


# ============================================================================
# LAYER HIERARCHY
# ============================================================================

def build_layer_targets(
    rows: list[dict[str, str]],
    layers_root: bpy.types.Collection,
) -> dict[tuple[int | None, str], bpy.types.Collection]:
    source_types_by_layer: dict[int | None, set[str]] = {}

    for row in rows:
        raw_layer = row.get("_CalculatedLayerId", "")
        layer_id = None if raw_layer == "" else int(raw_layer)
        source_type = str(row.get("SourceType", "")).strip() or "Unknown"
        source_types_by_layer.setdefault(layer_id, set()).add(source_type)

    ordered_layers = sorted(
        source_types_by_layer,
        key=lambda value: (value is None, value if value is not None else 0),
    )

    targets: dict[tuple[int | None, str], bpy.types.Collection] = {}

    for layer_id in ordered_layers:
        layer_collection = new_linked_collection(
            layer_collection_name(layer_id),
            layers_root,
        )
        layer_collection["prey_layer_id"] = (
            -1 if layer_id is None else layer_id
        )

        if SPLIT_BY_SOURCE_TYPE:
            for source_type in sorted(source_types_by_layer[layer_id]):
                target = new_linked_collection(
                    safe_name(source_type),
                    layer_collection,
                )
                targets[(layer_id, source_type)] = target
        else:
            targets[(layer_id, "")] = layer_collection

    return targets


# ============================================================================
# GEOMETRY NODES PROTOTYPES
# ============================================================================

def create_prototype_collection(
    sources: dict[str, bpy.types.Collection],
    asset_order: list[str],
) -> tuple[bpy.types.Collection, dict[str, int]]:
    """
    Build an UNLINKED collection containing one identity-transform Empty per asset.

    Collection Info + Separate Children turns these into a pickable instance list.
    """
    prototype_collection = bpy.data.collections.new(PROTOTYPE_COLLECTION_NAME)
    index_by_asset: dict[str, int] = {}

    for asset_key in asset_order:
        source = sources.get(asset_key)
        if source is None:
            continue

        index = len(index_by_asset)
        index_by_asset[asset_key] = index

        usd_path = Path(str(source.get("prey_resolved_usd", "asset")))
        prototype = bpy.data.objects.new(
            f"Prototype_{index:05d}_{safe_name(usd_path.stem, 40)}",
            None,
        )
        prototype.instance_type = "COLLECTION"
        prototype.instance_collection = source
        prototype.empty_display_type = "PLAIN_AXES"
        prototype.empty_display_size = 0.1
        prototype["prey_prototype_index"] = index
        prototype["prey_asset_key"] = asset_key

        prototype_collection.objects.link(prototype)

    return prototype_collection, index_by_asset


def add_geometry_socket(node_group, *, name: str, in_out: str) -> None:
    """Support Blender 4.x/5.x interface API and older fallback API."""
    if hasattr(node_group, "interface"):
        node_group.interface.new_socket(
            name=name,
            in_out=in_out,
            socket_type="NodeSocketGeometry",
        )
    else:
        if in_out == "INPUT":
            node_group.inputs.new("NodeSocketGeometry", name)
        else:
            node_group.outputs.new("NodeSocketGeometry", name)


def make_named_attribute_node(nodes, data_type: str, attribute_name: str, label: str):
    node = nodes.new("GeometryNodeInputNamedAttribute")
    node.data_type = data_type
    node.label = label
    node.inputs["Name"].default_value = attribute_name
    return node


def create_geometry_nodes_group(
    prototype_collection: bpy.types.Collection,
) -> bpy.types.GeometryNodeTree:
    node_group = bpy.data.node_groups.new(GN_GROUP_NAME, "GeometryNodeTree")

    add_geometry_socket(node_group, name="Geometry", in_out="INPUT")
    add_geometry_socket(node_group, name="Geometry", in_out="OUTPUT")

    nodes = node_group.nodes
    links = node_group.links

    group_in = nodes.new("NodeGroupInput")
    group_in.location = (-650, 80)

    group_out = nodes.new("NodeGroupOutput")
    group_out.location = (450, 80)

    collection_info = nodes.new("GeometryNodeCollectionInfo")
    collection_info.location = (-650, -180)
    collection_info.inputs["Collection"].default_value = prototype_collection

    # We need the collection's child objects as a list of separately pickable instances.
    if "Separate Children" in collection_info.inputs:
        collection_info.inputs["Separate Children"].default_value = True
    if "Reset Children" in collection_info.inputs:
        collection_info.inputs["Reset Children"].default_value = True

    try:
        collection_info.transform_space = "ORIGINAL"
    except Exception:
        pass

    attr_index = make_named_attribute_node(
        nodes,
        "INT",
        "prey_instance_index",
        "Prototype Index",
    )
    attr_index.location = (-420, -280)

    attr_rotation = make_named_attribute_node(
        nodes,
        "FLOAT_VECTOR",
        "prey_rotation",
        "Rotation",
    )
    attr_rotation.location = (-420, -400)

    attr_scale = make_named_attribute_node(
        nodes,
        "FLOAT_VECTOR",
        "prey_scale",
        "Scale",
    )
    attr_scale.location = (-420, -520)

    instance_on_points = nodes.new("GeometryNodeInstanceOnPoints")
    instance_on_points.location = (80, 60)
    instance_on_points.inputs["Pick Instance"].default_value = True

    links.new(group_in.outputs["Geometry"], instance_on_points.inputs["Points"])
    links.new(collection_info.outputs["Instances"], instance_on_points.inputs["Instance"])
    links.new(attr_index.outputs["Attribute"], instance_on_points.inputs["Instance Index"])
    links.new(attr_rotation.outputs["Attribute"], instance_on_points.inputs["Rotation"])
    links.new(attr_scale.outputs["Attribute"], instance_on_points.inputs["Scale"])
    links.new(instance_on_points.outputs["Instances"], group_out.inputs["Geometry"])

    return node_group


# ============================================================================
# POINT-CLOUD / ATTRIBUTE CREATION
# ============================================================================

def create_int_point_attribute(mesh, name: str, values: list[int]) -> None:
    attribute = mesh.attributes.new(name=name, type="INT", domain="POINT")
    if values:
        attribute.data.foreach_set("value", values)


def create_vector_point_attribute(
    mesh,
    name: str,
    values: list[tuple[float, float, float]],
) -> None:
    attribute = mesh.attributes.new(name=name, type="FLOAT_VECTOR", domain="POINT")
    if values:
        flat = [component for vector in values for component in vector]
        attribute.data.foreach_set("vector", flat)


def create_instancer_object(
    *,
    name: str,
    target_collection: bpy.types.Collection,
    node_group: bpy.types.GeometryNodeTree,
    placements: list[dict],
    layer_id: int | None,
    source_type: str,
) -> bpy.types.Object:
    mesh = bpy.data.meshes.new(f"PreyPoints_{name}")

    locations = [placement["location"] for placement in placements]
    rotations = [placement["rotation"] for placement in placements]
    scales = [placement["scale"] for placement in placements]
    prototype_indices = [placement["prototype_index"] for placement in placements]

    mesh.from_pydata(locations, [], [])

    create_int_point_attribute(mesh, "prey_instance_index", prototype_indices)
    create_vector_point_attribute(mesh, "prey_rotation", rotations)
    create_vector_point_attribute(mesh, "prey_scale", scales)

    if ADD_POINT_METADATA:
        create_int_point_attribute(
            mesh,
            "prey_csv_row",
            [placement["csv_row"] for placement in placements],
        )
        create_int_point_attribute(
            mesh,
            "prey_mesh_index",
            [placement["mesh_index"] for placement in placements],
        )

    mesh.update()

    obj = bpy.data.objects.new(name, mesh)
    target_collection.objects.link(obj)

    modifier = obj.modifiers.new(name="Prey Instances", type="NODES")
    modifier.node_group = node_group

    obj["prey_instance_count"] = len(placements)
    obj["prey_layer_id"] = -1 if layer_id is None else layer_id
    obj["prey_source_type"] = source_type

    return obj


def create_fallback_instance(
    *,
    source: bpy.types.Collection,
    matrix: Matrix,
    target: bpy.types.Collection,
    serial: int,
    row: dict[str, str],
    layer_id: int | None,
) -> bpy.types.Object:
    """Exact 4x4-matrix fallback for unusual non-TRS/sheared transforms."""
    source_type = str(row.get("SourceType", "")).strip() or "Unknown"
    mesh_stem = Path(row.get("MeshPath", "asset")).stem

    obj = bpy.data.objects.new(
        (
            f"{FALLBACK_PREFIX}"
            f"{safe_name(source_type, 12)}_"
            f"L{layer_id if layer_id is not None else 'Unknown'}_"
            f"{serial:06d}_{safe_name(mesh_stem, 30)}"
        ),
        None,
    )
    obj.instance_type = "COLLECTION"
    obj.instance_collection = source
    obj.empty_display_type = "PLAIN_AXES"
    obj.empty_display_size = 0.2
    obj.matrix_world = matrix
    obj["prey_csv_row"] = serial

    target.objects.link(obj)
    return obj


# ============================================================================
# MAIN
# ============================================================================

def main() -> None:
    started = time.perf_counter()
    rows = read_rows()

    if not rows:
        raise RuntimeError("No rows match the current filters.")

    if DELETE_PREVIOUS_IMPORT:
        clear_previous_import()

    root = new_linked_collection(
        ROOT_COLLECTION_NAME,
        bpy.context.scene.collection,
    )
    layers_root = new_linked_collection(
        LAYERS_COLLECTION_NAME,
        root,
    )

    layer_targets = build_layer_targets(rows, layers_root)

    # Cache by resolved file path, never by MeshIndex.
    first_row_by_asset: dict[str, dict[str, str]] = {}
    for row in rows:
        key = canonical_asset_key(row["ResolvedUSD"])
        first_row_by_asset.setdefault(key, row)

    sources: dict[str, bpy.types.Collection] = {}
    failed_assets: dict[str, str] = {}

    print(
        f"[Prey] {len(rows):,} placements, "
        f"{len(first_row_by_asset):,} unique resolved assets"
    )

    for number, (asset_key, row) in enumerate(
        first_row_by_asset.items(),
        start=1,
    ):
        usd_path = Path(row["ResolvedUSD"])

        if not usd_path.is_file():
            failed_assets[asset_key] = f"File not found: {usd_path}"
            continue

        try:
            sources[asset_key] = import_usd_source(usd_path, asset_key)
        except Exception as exc:
            failed_assets[asset_key] = str(exc)
            print(f"[Prey] FAILED {usd_path}: {exc}")

        if number == 1 or number % 25 == 0:
            print(
                f"[Prey] Imported unique assets "
                f"{number}/{len(first_row_by_asset)}"
            )

    asset_order = list(first_row_by_asset.keys())
    prototype_collection, prototype_index_by_asset = create_prototype_collection(
        sources,
        asset_order,
    )
    node_group = create_geometry_nodes_group(prototype_collection)

    # One point-cloud group per Layer/SourceType target.
    placement_groups: dict[tuple[int | None, str], list[dict]] = defaultdict(list)

    placed = 0
    skipped = 0
    fallback_count = 0
    max_trs_error = 0.0
    source_counts = Counter()
    layer_counts = Counter()

    for serial, row in enumerate(rows, start=1):
        asset_key = canonical_asset_key(row["ResolvedUSD"])
        source = sources.get(asset_key)
        prototype_index = prototype_index_by_asset.get(asset_key)

        if source is None or prototype_index is None:
            skipped += 1
            continue

        source_type = str(row.get("SourceType", "")).strip() or "Unknown"
        raw_layer = row.get("_CalculatedLayerId", "")
        layer_id = None if raw_layer == "" else int(raw_layer)

        target_key = (
            (layer_id, source_type)
            if SPLIT_BY_SOURCE_TYPE
            else (layer_id, "")
        )
        target = layer_targets[target_key]

        matrix = cry_matrix(row)
        location, rotation, scale, trs_error = decompose_for_geometry_nodes(matrix)
        max_trs_error = max(max_trs_error, trs_error)

        if trs_error > TRS_MATRIX_TOLERANCE:
            create_fallback_instance(
                source=source,
                matrix=matrix,
                target=target,
                serial=serial,
                row=row,
                layer_id=layer_id,
            )
            fallback_count += 1
        else:
            try:
                mesh_index = int(float(row.get("MeshIndex", "0") or 0))
            except ValueError:
                mesh_index = 0

            placement_groups[target_key].append({
                "location": location,
                "rotation": rotation,
                "scale": scale,
                "prototype_index": prototype_index,
                "csv_row": serial,
                "mesh_index": mesh_index,
            })

        placed += 1
        source_counts[source_type] += 1
        layer_counts[layer_collection_name(layer_id)] += 1

        if serial == 1 or serial % 5000 == 0:
            print(f"[Prey] Prepared {serial}/{len(rows)} rows")

    instancer_objects = 0

    ordered_group_keys = sorted(
        placement_groups,
        key=lambda key: (
            key[0] is None,
            key[0] if key[0] is not None else 0,
            key[1],
        ),
    )

    for target_key in ordered_group_keys:
        layer_id, group_source_type = target_key
        placements = placement_groups[target_key]
        target = layer_targets[target_key]

        display_source_type = group_source_type if SPLIT_BY_SOURCE_TYPE else "Combined"
        name = (
            f"PreyInstancer_"
            f"{layer_collection_name(layer_id)}_"
            f"{safe_name(display_source_type, 20)}"
        )

        create_instancer_object(
            name=name,
            target_collection=target,
            node_group=node_group,
            placements=placements,
            layer_id=layer_id,
            source_type=display_source_type,
        )
        instancer_objects += 1

    elapsed = time.perf_counter() - started

    print("")
    print("[Prey] Finished - Geometry Nodes optimized")
    print(f"  CSV rows:                  {len(rows):,}")
    print(f"  Unique path assets:        {len(first_row_by_asset):,}")
    print(f"  Imported assets:           {len(sources):,}")
    print(f"  Prototype objects:         {len(prototype_index_by_asset):,}")
    print(f"  Failed assets:             {len(failed_assets):,}")
    print(f"  Placements represented:    {placed:,}")
    print(f"  Skipped placements:        {skipped:,}")
    print(f"  Visible instancer objects: {instancer_objects:,}")
    print(f"  Matrix fallback objects:   {fallback_count:,}")
    print(f"  Max TRS matrix error:      {max_trs_error:.8g}")
    print(f"  Layer collections:         {len(layer_counts):,}")
    print(f"  Runtime:                   {elapsed:.1f} seconds")

    print("")
    print("  Source types:")
    for source_type, count in sorted(source_counts.items()):
        print(f"    {source_type}: {count:,}")

    print("")
    print("  Layers:")
    for layer_name, count in sorted(layer_counts.items()):
        print(f"    {layer_name}: {count:,}")

    if fallback_count:
        print("")
        print(
            "  NOTE: Matrix fallback objects preserve exact transforms for rows "
            "that contained shear/non-TRS matrices."
        )


if __name__ == "__main__":
    main()
