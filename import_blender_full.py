"""
Blender USD import helper.

This script reads a CSV of resolved USD file paths, imports every unique
USD asset once, and then creates a collection-instance empty for each CSV row
using the row's transform matrix.

Note: USD import is expensive and this script can take a long time to run
when many unique USD files are imported or the scene becomes large.
"""

import bpy
import csv
from pathlib import Path
from mathutils import Matrix

#Path to your prey_import_combined.csv
CSV_PATH = Path(
    r"...\prey_import_combined.csv"
)

# If True, old collections gets deleted
DELETE_OLD_IMPORT_COLLECTIONS = True

INSTANCE_COLLECTION_NAME = "Prey"
SOURCE_ROOT_NAME = "_Prey_USD_Sources_Unlinked"

def delete_collection_recursive(name):
    col = bpy.data.collections.get(name)
    if not col:
        return

    def delete_col(c):
        for child in list(c.children):
            delete_col(child)

        for obj in list(c.objects):
            bpy.data.objects.remove(obj, do_unlink=True)

        bpy.data.collections.remove(c)

    delete_col(col)

def get_or_create_scene_collection(name):
    col = bpy.data.collections.get(name)

    if col is None:
        col = bpy.data.collections.new(name)
        bpy.context.scene.collection.children.link(col)

    return col

def get_or_create_unlinked_collection(name):
    col = bpy.data.collections.get(name)

    if col is None:
        col = bpy.data.collections.new(name)

    return col

def import_usd_file(filepath: Path):
    before = set(bpy.data.objects)

    try:
        bpy.ops.wm.usd_import(filepath=str(filepath))
    except Exception:
        bpy.ops.import_scene.usd(filepath=str(filepath))

    after = set(bpy.data.objects)
    imported = list(after - before)

    return imported


def move_objects_to_collection(objects, collection):
    for obj in objects:
        for old_col in list(obj.users_collection):
            old_col.objects.unlink(obj)

        collection.objects.link(obj)

def row_to_matrix(row):
    """
    Matrix from terrain.dat:

    M00 M01 M02 PosX
    M10 M11 M12 PosY
    M20 M21 M22 PosZ
    0   0   0   1
    """

    return Matrix((
        (float(row["M00"]), float(row["M01"]), float(row["M02"]), float(row["PosX"])),
        (float(row["M10"]), float(row["M11"]), float(row["M12"]), float(row["PosY"])),
        (float(row["M20"]), float(row["M21"]), float(row["M22"]), float(row["PosZ"])),
        (0.0,              0.0,              0.0,              1.0),
    ))

def create_collection_instance(collection, matrix, name, target_collection):
    empty = bpy.data.objects.new(name, None)
    empty.empty_display_type = "PLAIN_AXES"
    empty.instance_type = "COLLECTION"
    empty.instance_collection = collection
    empty.matrix_world = matrix

    target_collection.objects.link(empty)

    return empty

def safe_name(text):
    bad = '<>:"/\\|?*'
    out = text

    for ch in bad:
        out = out.replace(ch, "_")

    return out[:80]

# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------

if DELETE_OLD_IMPORT_COLLECTIONS:
    delete_collection_recursive(INSTANCE_COLLECTION_NAME)
    delete_collection_recursive(SOURCE_ROOT_NAME)

source_root = get_or_create_unlinked_collection(SOURCE_ROOT_NAME)
instances_root = get_or_create_scene_collection(INSTANCE_COLLECTION_NAME)

asset_cache = {}
created = 0
skipped = 0
missing = []

group_collections = {}

with CSV_PATH.open("r", encoding="utf-8-sig", newline="") as csv_file:
    reader = csv.DictReader(csv_file)

    for idx, row in enumerate(reader):
        usd_text = row.get("ResolvedUSD", "").strip()

        if not usd_text:
            skipped += 1
            missing.append(row.get("MeshPath", ""))
            continue

        usd_path = Path(usd_text)

        if not usd_path.exists():
            skipped += 1
            missing.append(row.get("MeshPath", ""))
            continue

        group = row.get("SourceGroup", "Imported")

        if group not in group_collections:
            group_col = bpy.data.collections.new(group)
            instances_root.children.link(group_col)
            group_collections[group] = group_col

        target_col = group_collections[group]

        cache_key = str(usd_path).lower()

        if cache_key not in asset_cache:
            print(f"Importing USD: {usd_path}")

            imported_objects = import_usd_file(usd_path)

            if not imported_objects:
                print(f"WARNING: no objects imported from {usd_path}")
                skipped += 1
                missing.append(row.get("MeshPath", ""))
                continue

            asset_col_name = f"SRC_{len(asset_cache):05d}_{safe_name(usd_path.stem)}"
            asset_col = bpy.data.collections.new(asset_col_name)

            source_root.children.link(asset_col)

            move_objects_to_collection(imported_objects, asset_col)

            asset_cache[cache_key] = asset_col

        matrix = row_to_matrix(row)

        offset = row.get("OffsetHex", f"row_{idx}")
        mesh_index = row.get("MeshIndex", "")
        inst_name = f"{group}_{idx:06d}_{mesh_index}_{safe_name(usd_path.stem)}_{offset}"

        create_collection_instance(
            asset_cache[cache_key],
            matrix,
            inst_name,
            target_col,
        )

        created += 1

print("Done.")
print(f"Created instances: {created}")
print(f"Unique USD assets imported: {len(asset_cache)}")
print(f"Skipped/missing rows: {skipped}")

if missing:
    missing_file = CSV_PATH.parent / "missing_usd_imports_from_blender.txt"

    with missing_file.open("w", encoding="utf-8") as f:
        for item in sorted(set(missing)):
            f.write(item + "\n")

    print(f"Wrote missing list: {missing_file}")