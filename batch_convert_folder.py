"""Recursively convert a folder tree of Prey static CGF files to USDA.

The output directory mirrors the input directory structure. Each asset is
converted in a separate subprocess through ``prey_cgf_to_usd.py`` so one bad
file cannot terminate the whole batch.

Only Python's standard library is required.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence


@dataclass
class Result:
    input: str
    output: str
    status: str
    seconds: float
    returncode: int | None = None
    validation_sidecar: str | None = None
    error_log: str | None = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Recursively convert all static .cgf files below INPUT_ROOT and "
            "mirror the folder structure below OUTPUT_ROOT."
        )
    )
    parser.add_argument("input_root", help="Folder containing CGF files")
    parser.add_argument("output_root", help="Separate destination folder")
    parser.add_argument(
        "--converter-script",
        help=(
            "Path to prey_cgf_to_usd.py; default is the file beside this "
            "batch script"
        ),
    )
    parser.add_argument(
        "--converter",
        default=os.environ.get("CGF_CONVERTER", "cgf-converter"),
        help="Path/name of Markemp cgf-converter executable",
    )
    parser.add_argument(
        "--objectdir",
        help=(
            "Extracted Prey data root passed to cgf-converter. Normally this "
            "is the directory that directly contains Objects and Textures."
        ),
    )
    parser.add_argument(
        "--node-transform",
        choices=("auto", "preserve", "ignore"),
        default="auto",
        help="Transform policy for every asset (default: auto)",
    )
    parser.add_argument(
        "--converter-arg",
        action="append",
        default=[],
        help="Additional argument passed to cgf-converter; repeatable",
    )
    parser.add_argument(
        "--validation-root",
        help=(
            "Optional root containing per-asset validation sidecars. For "
            "foo/bar.cgf, the expected sidecar is "
            "VALIDATION_ROOT/foo/bar.validation.json"
        ),
    )
    parser.add_argument(
        "--write-reports",
        action="store_true",
        help="Keep one .report.json file beside every output USDA",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite completed USDA files instead of resuming/skipping",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List planned conversions without starting the converter",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Convert at most this many discovered assets (useful for testing)",
    )
    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="Stop the batch after the first failed asset",
    )
    return parser


def discover_cgf_files(root: Path) -> list[Path]:
    # Path.rglob('*.cgf') is case-sensitive on some platforms.
    return sorted(
        (path for path in root.rglob("*") if path.is_file() and path.suffix.casefold() == ".cgf"),
        key=lambda path: str(path.relative_to(root)).casefold(),
    )


def sidecar_for(validation_root: Path | None, relative_cgf: Path) -> Path | None:
    if validation_root is None:
        return None
    candidate = validation_root / relative_cgf.with_suffix(".validation.json")
    return candidate if candidate.is_file() else None


def log_path_for(log_root: Path, relative_cgf: Path) -> Path:
    return log_root / relative_cgf.with_suffix(".log.txt")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    input_root = Path(args.input_root).expanduser().resolve()
    output_root = Path(args.output_root).expanduser().resolve()
    script_path = (
        Path(args.converter_script).expanduser().resolve()
        if args.converter_script
        else Path(__file__).with_name("prey_cgf_to_usd.py").resolve()
    )
    objectdir = Path(args.objectdir).expanduser().resolve() if args.objectdir else None
    validation_root = (
        Path(args.validation_root).expanduser().resolve()
        if args.validation_root
        else None
    )

    if not input_root.is_dir():
        print(f"ERROR: Input root is not a directory: {input_root}", file=sys.stderr)
        return 2
    if input_root == output_root:
        print("ERROR: INPUT_ROOT and OUTPUT_ROOT must be different.", file=sys.stderr)
        return 2
    if not script_path.is_file():
        print(f"ERROR: Converter script not found: {script_path}", file=sys.stderr)
        return 2
    if objectdir is not None and not objectdir.is_dir():
        print(f"ERROR: objectdir is not a directory: {objectdir}", file=sys.stderr)
        return 2
    if validation_root is not None and not validation_root.is_dir():
        print(
            f"ERROR: validation root is not a directory: {validation_root}",
            file=sys.stderr,
        )
        return 2
    if args.limit is not None and args.limit <= 0:
        print("ERROR: --limit must be greater than zero.", file=sys.stderr)
        return 2

    files = discover_cgf_files(input_root)
    if args.limit is not None:
        files = files[: args.limit]

    output_root.mkdir(parents=True, exist_ok=True)
    log_root = output_root / "_batch_logs"
    summary_path = output_root / "batch_summary.json"

    print(f"Input root : {input_root}")
    print(f"Output root: {output_root}")
    print(f"Assets     : {len(files)}")
    print(f"Policy     : {args.node_transform}")
    print()

    results: list[Result] = []
    batch_started = time.perf_counter()

    try:
        for index, input_path in enumerate(files, start=1):
            relative = input_path.relative_to(input_root)
            output_path = output_root / relative.with_suffix(".usda")
            sidecar = sidecar_for(validation_root, relative)
            prefix = f"[{index}/{len(files)}]"

            if output_path.exists() and not args.overwrite:
                print(f"{prefix} SKIP  {relative}")
                results.append(
                    Result(
                        input=str(input_path),
                        output=str(output_path),
                        status="skipped_existing",
                        seconds=0.0,
                        validation_sidecar=str(sidecar) if sidecar else None,
                    )
                )
                continue

            print(f"{prefix} CONVERT {relative}")
            if args.dry_run:
                results.append(
                    Result(
                        input=str(input_path),
                        output=str(output_path),
                        status="dry_run",
                        seconds=0.0,
                        validation_sidecar=str(sidecar) if sidecar else None,
                    )
                )
                continue

            output_path.parent.mkdir(parents=True, exist_ok=True)
            command = [
                sys.executable,
                str(script_path),
                str(input_path),
                str(output_path),
                "--converter",
                args.converter,
                "--node-transform",
                args.node_transform,
            ]
            if objectdir is not None:
                command.extend(["--objectdir", str(objectdir)])
            for converter_arg in args.converter_arg:
                command.extend(["--converter-arg", converter_arg])
            if sidecar is not None:
                command.extend(["--validation-sidecar", str(sidecar)])
            if not args.write_reports:
                command.append("--no-report")
            if args.overwrite:
                command.append("--force")

            started = time.perf_counter()
            completed = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            elapsed = time.perf_counter() - started

            if completed.returncode == 0:
                print(f"{prefix} OK    {relative} ({elapsed:.2f}s)")
                results.append(
                    Result(
                        input=str(input_path),
                        output=str(output_path),
                        status="converted",
                        seconds=elapsed,
                        returncode=0,
                        validation_sidecar=str(sidecar) if sidecar else None,
                    )
                )
                continue

            log_path = log_path_for(log_root, relative)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(
                "COMMAND:\n"
                + subprocess.list2cmdline(command)
                + "\n\nSTDOUT:\n"
                + completed.stdout
                + "\n\nSTDERR:\n"
                + completed.stderr,
                encoding="utf-8",
                newline="\n",
            )
            print(
                f"{prefix} FAIL  {relative} ({elapsed:.2f}s) -> {log_path}",
                file=sys.stderr,
            )
            results.append(
                Result(
                    input=str(input_path),
                    output=str(output_path),
                    status="failed",
                    seconds=elapsed,
                    returncode=completed.returncode,
                    validation_sidecar=str(sidecar) if sidecar else None,
                    error_log=str(log_path),
                )
            )
            if args.stop_on_error:
                break

    except KeyboardInterrupt:
        print("\nBatch interrupted by user.", file=sys.stderr)

    total_seconds = time.perf_counter() - batch_started
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1

    summary = {
        "input_root": str(input_root),
        "output_root": str(output_root),
        "converter_script": str(script_path),
        "converter": args.converter,
        "objectdir": str(objectdir) if objectdir else None,
        "node_transform": args.node_transform,
        "total_discovered": len(files),
        "total_processed": len(results),
        "counts": counts,
        "seconds": total_seconds,
        "results": [asdict(result) for result in results],
    }
    summary_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    print()
    print("Finished")
    print(f"Converted: {counts.get('converted', 0)}")
    print(f"Skipped  : {counts.get('skipped_existing', 0)}")
    print(f"Failed   : {counts.get('failed', 0)}")
    print(f"Summary  : {summary_path}")

    return 1 if counts.get("failed", 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())
