"""
Prey (2017) combined terrain + indoor Brush importer for Blender.

This version groups every placed instance by CryEngine LayerId:

Prey_Combined
└── Layers
    ├── Layer_002
    │   ├── Indoor
    │   └── Outdoor
    ├── Layer_012
    │   ├── Indoor
    │   └── Outdoor
    └── Layer_Unknown

You can toggle each Layer_XXX collection directly in Blender's Outliner.

Important:
- Imported USD assets are cached by absolute ResolvedUSD path.
- Reusable source collections are not linked into the scene.
- Each placed instance is linked to exactly one layer collection.
- Indoor rows use the CSV LayerId column.
- Outdoor rows decode LayerId from the lower 16 bits of Flags1 when the
  LayerId column is empty.
"""

from __future__ import annotations

import csv
import hashlib
import re
import time
from collections import Counter
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


ROOT_COLLECTION_NAME = "Prey_by_Layers"
LAYERS_COLLECTION_NAME = "Layers"
SOURCE_COLLECTION_PREFIX = "__PREY_SOURCE_PATH__"

DELETE_PREVIOUS_IMPORT = True
ADD_CUSTOM_PROPERTIES = True

# Create Indoor / Outdoor children under every Layer_XXX collection.
SPLIT_BY_SOURCE_TYPE = True

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
    """
    Use the resolved absolute file path as the cache key.

    casefold() is intentional because this workflow is Windows-based and
    CryEngine paths may contain inconsistent casing.
    """
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


def parse_int_auto(value: object) -> int | None:
    """
    Parse decimal, 0x-prefixed hexadecimal, or simple float-like integers.
    """
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
    Indoor:
        Uses LayerId directly.

    Outdoor:
        If LayerId is empty, decode the lower 16 bits of Flags1.
    """
    direct_layer = parse_int_auto(row.get("LayerId", ""))
    if direct_layer is not None:
        return direct_layer

    source_type = str(row.get("SourceType", "")).strip().casefold()
    flags1 = parse_int_auto(row.get("Flags1", ""))

    if source_type == "outdoor" and flags1 is not None:
        return flags1 & 0xFFFF

    # Also allow the fallback for rows where SourceType is missing but Flags1
    # exists. This keeps the importer tolerant of older combined CSV files.
    if not source_type and flags1 is not None:
        return flags1 & 0xFFFF

    return None


def layer_collection_name(layer_id: int | None) -> str:
    if layer_id is None:
        return "Layer_Unknown"
    return f"Layer_{layer_id:03d}"


# ============================================================================
# COLLECTION MANAGEMENT
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

    for collection in list(bpy.data.collections):
        if collection.name.startswith(SOURCE_COLLECTION_PREFIX):
            remove_collection_recursive(collection)


def new_linked_collection(
    name: str,
    parent: bpy.types.Collection,
) -> bpy.types.Collection:
    collection = bpy.data.collections.new(name)
    parent.children.link(collection)
    return collection


def get_or_create_child_collection(
    parent: bpy.types.Collection,
    name: str,
) -> bpy.types.Collection:
    for child in parent.children:
        if child.name == name:
            return child
    return new_linked_collection(name, parent)


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

            # Calculate once and keep it available during placement.
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
        import_curves=True,
        import_lights=False,
        import_materials=True,
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

    return source


def apply_properties(
    obj: bpy.types.Object,
    row: dict[str, str],
    asset_key: str,
    layer_id: int | None,
) -> None:
    if not ADD_CUSTOM_PROPERTIES:
        return

    obj["prey_asset_key"] = asset_key
    obj["prey_resolved_usd"] = row.get("ResolvedUSD", "")
    obj["prey_mesh_path"] = row.get("MeshPath", "")
    obj["prey_mesh_index"] = int(row.get("MeshIndex", "0") or 0)
    obj["prey_source_type"] = row.get("SourceType", "")
    obj["prey_area_name"] = row.get("AreaName", "")
    obj["prey_record_offset"] = row.get("OffsetHex", "")
    obj["prey_level_name"] = row.get("LevelName", "")
    obj["prey_layer_id"] = -1 if layer_id is None else layer_id


# ============================================================================
# LAYER HIERARCHY
# ============================================================================

def build_layer_targets(
    rows: list[dict[str, str]],
    layers_root: bpy.types.Collection,
) -> dict[tuple[int | None, str], bpy.types.Collection]:
    """
    Pre-create sorted Layer_XXX collections so the Outliner remains tidy.
    """
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

    targets: dict[
        tuple[int | None, str],
        bpy.types.Collection,
    ] = {}

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
            sources[asset_key] = import_usd_source(
                usd_path,
                asset_key,
            )
        except Exception as exc:
            failed_assets[asset_key] = str(exc)
            print(f"[Prey] FAILED {usd_path}: {exc}")

        if number == 1 or number % 25 == 0:
            print(
                f"[Prey] Imported unique assets "
                f"{number}/{len(first_row_by_asset)}"
            )

    placed = 0
    skipped = 0
    source_counts = Counter()
    layer_counts = Counter()

    for serial, row in enumerate(rows, start=1):
        asset_key = canonical_asset_key(row["ResolvedUSD"])
        source = sources.get(asset_key)

        if source is None:
            skipped += 1
            continue

        source_type = (
            str(row.get("SourceType", "")).strip()
            or "Unknown"
        )
        raw_layer = row.get("_CalculatedLayerId", "")
        layer_id = None if raw_layer == "" else int(raw_layer)

        if SPLIT_BY_SOURCE_TYPE:
            target = layer_targets[(layer_id, source_type)]
        else:
            target = layer_targets[(layer_id, "")]

        mesh_stem = Path(row.get("MeshPath", "asset")).stem
        offset = str(row.get("OffsetHex", "")).replace("0x", "")

        obj = bpy.data.objects.new(
            (
                f"{safe_name(source_type, 12)}_"
                f"L{layer_id if layer_id is not None else 'Unknown'}_"
                f"{serial:06d}_"
                f"{safe_name(mesh_stem, 30)}_"
                f"{offset}"
            ),
            None,
        )
        obj.instance_type = "COLLECTION"
        obj.instance_collection = source
        obj.empty_display_type = "PLAIN_AXES"
        obj.empty_display_size = 0.2
        obj.matrix_world = cry_matrix(row)

        target.objects.link(obj)

        apply_properties(
            obj,
            row,
            asset_key,
            layer_id,
        )

        placed += 1
        source_counts[source_type] += 1
        layer_counts[layer_collection_name(layer_id)] += 1

        if serial == 1 or serial % 500 == 0:
            print(f"[Prey] Placed {serial}/{len(rows)} rows")

    elapsed = time.perf_counter() - started

    print("")
    print("[Prey] Finished")
    print(f"  CSV rows:             {len(rows):,}")
    print(f"  Unique path assets:   {len(first_row_by_asset):,}")
    print(f"  Imported assets:      {len(sources):,}")
    print(f"  Failed assets:        {len(failed_assets):,}")
    print(f"  Placed instances:     {placed:,}")
    print(f"  Skipped instances:    {skipped:,}")
    print(f"  Layer collections:    {len(layer_counts):,}")
    print(f"  Runtime:              {elapsed:.1f} seconds")

    print("")
    print("  Source types:")
    for source_type, count in sorted(source_counts.items()):
        print(f"    {source_type}: {count:,}")

    print("")
    print("  Layers:")
    for layer_name, count in sorted(layer_counts.items()):
        print(f"    {layer_name}: {count:,}")


if __name__ == "__main__":
    main()