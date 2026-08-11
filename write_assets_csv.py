#!/usr/bin/env python3
"""
Resolve and combine Prey terrain + indoor Brush CSVs for Blender.

This script leaves the proven extraction scripts independent:

    terrain.dat -> terrain_instances_strong.csv
    indoor.dat  -> indoor_instances_strong.csv

It then resolves both sources against:
- each level's own root for %level%/Brush/... assets;
- one global MODEL_BASE for Objects/... assets.

For every level it writes beside terrain.dat:
- prey_import_terrain.csv           (terrain only)
- prey_import_indoor.csv     (indoor only)
- prey_import_combined.csv             (terrain + indoor)
- missing_prey_import_assets.txt

Input may be one level folder, one terrain folder, or a root containing many
levels. Only Python's standard library is required.
"""

from __future__ import annotations

import argparse
import csv
from collections import OrderedDict
from pathlib import Path


TERRAIN_CSV_NAME = (
    "terrain_instances_strong.csv",

)
INDOOR_CSV_NAME = (
    "indoor_instances_strong.csv",

)

TERRAIN_RESOLVED_NAME = "prey_import_terrain.csv"
INDOOR_RESOLVED_NAME = "prey_import_indoor.csv"
COMBINED_RESOLVED_NAME = "prey_import_combined.csv"
MISSING_REPORT_NAME = "missing_prey_import_assets.txt"

USD_EXTENSIONS = (".usda", ".usd", ".usdc")

def find_terrain_csv(directory: Path) -> Path | None:
    """Prefer corrected v12 terrain CSV, with v10/v9 as legacy fallbacks."""
    for name in TERRAIN_CSV_NAME:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def find_indoor_csv(directory: Path) -> Path | None:
    """Prefer corrected v3 indoor CSV, with v1 only as a legacy fallback."""
    for name in INDOOR_CSV_NAME:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


COMMON_FIRST_FIELDS = [
    "SourceType",
    "LevelName",
    "AreaCategory",
    "AreaIndex",
    "AreaName",
    "OffsetHex",
    "Layout",
    "Quality",
    "MeshIndex",
    "MeshPath",
    "SourceGroup",
    "ResolvedUSD",
    "Exists",
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
]


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def normalized_model_path(value: str) -> str:
    return value.replace("\\", "/").strip()


def source_group(model_path: str) -> str:
    lower = normalized_model_path(model_path).lower()

    if lower.startswith("%level%/brush/") or lower.startswith("brush/"):
        return "Brush"
    if lower.startswith("objects/environment/architecture/"):
        return "Architecture"
    if lower.startswith("objects/environment/props/"):
        return "Props"
    if lower.startswith("objects/characters/"):
        return "Characters"
    if lower.startswith("objects/arkeffects/"):
        return "Effects"
    return "Other"


def case_insensitive_child(root: Path, relative: str) -> Path | None:
    """Fallback for inconsistent CryEngine casing; cached by the caller."""
    current = root

    for component in Path(relative.replace("\\", "/")).parts:
        direct = current / component
        if direct.exists():
            current = direct
            continue

        if not current.is_dir():
            return None

        wanted = component.casefold()
        try:
            match = next(
                (
                    child
                    for child in current.iterdir()
                    if child.name.casefold() == wanted
                ),
                None,
            )
        except OSError:
            return None

        if match is None:
            return None
        current = match

    return current


def resolve_mesh_path(
    mesh_path: str,
    level_root: Path,
    model_base: Path,
    cache: dict[tuple[str, str], Path | None],
) -> Path | None:
    normalized = normalized_model_path(mesh_path)
    lower = normalized.lower()

    cache_key = (str(level_root), lower)
    if cache_key in cache:
        return cache[cache_key]

    if lower.startswith("%level%/"):
        relative = normalized[len("%level%/") :]
        base = level_root
    elif lower.startswith("brush/"):
        relative = normalized
        base = level_root
    else:
        relative = normalized
        base = model_base

    relative_path = Path(*relative.split("/"))
    relative_without_extension = relative_path.with_suffix("")

    for extension in USD_EXTENSIONS:
        candidate_relative = relative_without_extension.with_suffix(extension)
        candidate = base / candidate_relative

        if candidate.is_file():
            cache[cache_key] = candidate
            return candidate

        resolved = case_insensitive_child(base, str(candidate_relative))
        if resolved is not None and resolved.is_file():
            cache[cache_key] = resolved
            return resolved

    cache[cache_key] = None
    return None


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return [dict(row) for row in reader]


def ordered_fieldnames(rows: list[dict[str, object]]) -> list[str]:
    seen: OrderedDict[str, None] = OrderedDict()

    for field in COMMON_FIRST_FIELDS:
        seen[field] = None

    for row in rows:
        for field in row:
            seen.setdefault(field, None)

    return list(seen)


def write_rows(path: Path, rows: list[dict[str, object]]) -> None:
    fieldnames = ordered_fieldnames(rows)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fieldnames,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)


def normalize_row(
    row: dict[str, str],
    source_type: str,
    level_name: str,
) -> dict[str, object]:
    output: dict[str, object] = dict(row)
    output["SourceType"] = source_type
    output["LevelName"] = level_name

    if source_type == "Outdoor":
        output.setdefault("AreaCategory", "")
        output.setdefault("AreaIndex", "")
        output.setdefault("AreaName", "")
    else:
        output.setdefault("AreaCategory", row.get("AreaCategory", ""))
        output.setdefault("AreaIndex", row.get("AreaIndex", ""))
        output.setdefault("AreaName", row.get("AreaName", ""))

    return output


def resolve_rows(
    source_rows: list[dict[str, str]],
    source_type: str,
    level_name: str,
    level_root: Path,
    model_base: Path,
    cache: dict[tuple[str, str], Path | None],
) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    resolved_rows: list[dict[str, object]] = []
    missing_rows: list[dict[str, str]] = []

    for row in source_rows:
        quality = row.get("Quality", "strong").strip().lower()
        if quality and quality not in {"strong", "review"}:
            continue

        mesh_path = row.get("MeshPath", "").strip()
        if not mesh_path:
            continue

        resolved = resolve_mesh_path(mesh_path, level_root, model_base, cache)
        if resolved is None:
            missing_rows.append({
                "SourceType": source_type,
                "MeshPath": mesh_path,
            })
            continue

        output = normalize_row(row, source_type, level_name)
        output["SourceGroup"] = source_group(mesh_path)
        output["ResolvedUSD"] = str(resolved)
        output["Exists"] = True
        resolved_rows.append(output)

    return resolved_rows, missing_rows


# ---------------------------------------------------------------------------
# Recursive level discovery
# ---------------------------------------------------------------------------

def find_terrain_dirs(input_path: Path) -> list[Path]:
    if not input_path.exists():
        raise FileNotFoundError(input_path)

    if input_path.is_file():
        if input_path.name not in {*TERRAIN_CSV_NAME, *INDOOR_CSV_NAME}:
            raise ValueError(
                f"Input file must be one of {TERRAIN_CSV_NAME} or {INDOOR_CSV_NAME}"
            )
        return [input_path.parent]

    candidates: set[Path] = set()

    # Direct terrain directory or level directory.
    for directory in (input_path, input_path / "terrain"):
        if (
            find_terrain_csv(directory) is not None
            or find_indoor_csv(directory) is not None
        ):
            candidates.add(directory)

    # Multi-level root.
    for csv_name in (*TERRAIN_CSV_NAME, *INDOOR_CSV_NAME):
        for csv_path in input_path.rglob(csv_name):
            candidates.add(csv_path.parent)

    return sorted(candidates)


def infer_level_root(terrain_dir: Path) -> Path:
    # Normal extracted layout: <level>/terrain/*.csv
    if terrain_dir.name.lower() == "terrain":
        return terrain_dir.parent
    return terrain_dir


def process_level(
    terrain_dir: Path,
    model_base: Path,
    cache: dict[tuple[str, str], Path | None],
) -> bool:
    level_root = infer_level_root(terrain_dir)
    level_name = level_root.name

    terrain_csv = find_terrain_csv(terrain_dir)
    indoor_csv = find_indoor_csv(terrain_dir)

    terrain_source_rows = (
        read_rows(terrain_csv)
        if terrain_csv is not None and terrain_csv.is_file()
        else []
    )
    indoor_source_rows = (
        read_rows(indoor_csv)
        if indoor_csv is not None and indoor_csv.is_file()
        else []
    )

    terrain_rows, terrain_missing = resolve_rows(
        terrain_source_rows,
        "Outdoor",
        level_name,
        level_root,
        model_base,
        cache,
    )
    indoor_rows, indoor_missing = resolve_rows(
        indoor_source_rows,
        "Indoor",
        level_name,
        level_root,
        model_base,
        cache,
    )

    if terrain_csv is not None and terrain_csv.is_file():
        write_rows(terrain_dir / TERRAIN_RESOLVED_NAME, terrain_rows)
    if indoor_csv is not None and indoor_csv.is_file():
        write_rows(terrain_dir / INDOOR_RESOLVED_NAME, indoor_rows)

    combined_rows = terrain_rows + indoor_rows
    write_rows(terrain_dir / COMBINED_RESOLVED_NAME, combined_rows)

    missing = terrain_missing + indoor_missing
    unique_missing = sorted(
        {(item["SourceType"], item["MeshPath"]) for item in missing},
        key=lambda item: (item[0], item[1].casefold()),
    )

    report_path = terrain_dir / MISSING_REPORT_NAME
    with report_path.open("w", encoding="utf-8") as handle:
        handle.write("SourceType | MeshPath\n")
        handle.write("=====================\n")
        for source_type, mesh_path in unique_missing:
            handle.write(f"{source_type} | {mesh_path}\n")

    print(f"[OK] {level_root}")
    print(f"  Terrain source rows: {len(terrain_source_rows)}")
    print(f"  Terrain resolved: {len(terrain_rows)}")
    print(f"  Indoor source rows: {len(indoor_source_rows)}")
    print(f"  Indoor resolved: {len(indoor_rows)}")
    print(f"  Combined Blender rows: {len(combined_rows)}")
    print(f"  Missing unique assets: {len(unique_missing)}")
    print(f"  Wrote: {terrain_dir / COMBINED_RESOLVED_NAME}")
    print()

    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Resolve and combine Prey terrain/indoor CSVs recursively"
    )
    parser.add_argument(
        "input",
        help="One level folder, terrain folder, CSV, or multi-level root",
    )
    parser.add_argument(
        "--model-base",
        required=True,
        help="Global converted model root containing Objects/...",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    model_base = Path(args.model_base)

    if not model_base.is_dir():
        print(f"ERROR: MODEL_BASE is not a directory: {model_base}")
        raise SystemExit(1)

    try:
        terrain_dirs = find_terrain_dirs(input_path)
    except Exception as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1) from exc

    if not terrain_dirs:
        print("No terrain/indoor instance CSV files found.")
        return

    cache: dict[tuple[str, str], Path | None] = {}
    succeeded = 0
    for terrain_dir in terrain_dirs:
        try:
            if process_level(terrain_dir, model_base, cache):
                succeeded += 1
        except Exception as exc:
            print(f"[FAIL] {terrain_dir}")
            print(f"  {type(exc).__name__}: {exc}")
            print()

    print(f"Processed {succeeded}/{len(terrain_dirs)} levels successfully.")


if __name__ == "__main__":
    main()
