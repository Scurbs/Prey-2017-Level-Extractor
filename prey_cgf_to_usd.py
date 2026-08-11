#!/usr/bin/env python3
"""
Prey (2017) static CGF -> USDA converter wrapper with explicit node-transform
handling.

The script uses Markemp's cgf-converter for the difficult CGF/material decoding,
then rewrites only the USDA node-transform representation. It supports:

    --node-transform preserve
    --node-transform ignore
    --node-transform auto

Auto mode can use either:
1. a Prey-specific repeated/non-affine node-matrix heuristic; or
2. level-assisted AABB validation from a JSON sidecar or command-line values.

Only Python's standard library is required.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Sequence


NUMBER_RE = re.compile(
    r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
)
XFORM_RE = re.compile(r'\bdef\s+Xform\s+"([^"]+)"')
MATRIX_RE = re.compile(r"\bmatrix4d\s+xformOp:transform\s*=")
POINTS_RE = re.compile(
    r"\b(?:point3f|float3|double3)\s*\[\]\s+points\s*="
)
EXTENT_RE = re.compile(
    r"\b(?:float3|double3|point3f)\s*\[\]\s+extent\s*="
)

PHYSICS_MARKERS = (
    "physics",
    "collision",
    "$physics",
    "physproxy",
    "proxy_collision",
)

IDENTITY_4X4: tuple[float, ...] = (
    1.0, 0.0, 0.0, 0.0,
    0.0, 1.0, 0.0, 0.0,
    0.0, 0.0, 1.0, 0.0,
    0.0, 0.0, 0.0, 1.0,
)


class ConversionError(RuntimeError):
    """Raised for a conversion or USDA parsing failure."""


@dataclass(frozen=True)
class Aabb:
    minimum: tuple[float, float, float]
    maximum: tuple[float, float, float]

    @property
    def size(self) -> tuple[float, float, float]:
        return tuple(b - a for a, b in zip(self.minimum, self.maximum))

    @property
    def center(self) -> tuple[float, float, float]:
        return tuple((a + b) * 0.5 for a, b in zip(self.minimum, self.maximum))

    def to_json(self) -> dict[str, list[float]]:
        return {
            "min": list(self.minimum),
            "max": list(self.maximum),
        }


@dataclass
class BoundsAccumulator:
    minimum: list[float] = field(
        default_factory=lambda: [math.inf, math.inf, math.inf]
    )
    maximum: list[float] = field(
        default_factory=lambda: [-math.inf, -math.inf, -math.inf]
    )
    count: int = 0

    def add(self, point: Sequence[float]) -> None:
        if len(point) != 3 or not all(math.isfinite(v) for v in point):
            raise ConversionError(f"Invalid point: {point!r}")
        for axis in range(3):
            self.minimum[axis] = min(self.minimum[axis], float(point[axis]))
            self.maximum[axis] = max(self.maximum[axis], float(point[axis]))
        self.count += 1

    def finish(self) -> Aabb:
        if self.count == 0:
            raise ConversionError("No mesh points were found for AABB validation")
        return Aabb(tuple(self.minimum), tuple(self.maximum))


@dataclass
class XformBlock:
    name: str
    declaration_start: int
    open_brace: int
    close_brace: int
    parent_index: int | None = None
    matrix_index: int | None = None
    contains_mesh: bool = False

    @property
    def body_start(self) -> int:
        return self.open_brace + 1

    @property
    def body_end(self) -> int:
        return self.close_brace

    @property
    def is_physics(self) -> bool:
        lower = self.name.casefold()
        return any(marker in lower for marker in PHYSICS_MARKERS)


@dataclass
class MatrixOccurrence:
    attribute_start: int
    expression_start: int
    expression_end: int  # exclusive
    values: tuple[float, ...]
    block_index: int | None

    @property
    def translation(self) -> tuple[float, float, float]:
        return self.values[12], self.values[13], self.values[14]

    @property
    def m44(self) -> float:
        return self.values[15]


@dataclass(frozen=True)
class ValidationInput:
    instance_matrix: tuple[tuple[float, float, float, float], ...]
    expected_aabb: Aabb


@dataclass
class Decision:
    requested_mode: str
    effective_mode: str
    reason: str
    heuristic_conditions: dict[str, bool | int | float | str] = field(
        default_factory=dict
    )
    ignore_error: float | None = None
    preserve_error: float | None = None
    candidate_ignore: Aabb | None = None
    candidate_preserve: Aabb | None = None
    expected_aabb: Aabb | None = None

    def to_json(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "requested_mode": self.requested_mode,
            "effective_mode": self.effective_mode,
            "reason": self.reason,
            "heuristic_conditions": self.heuristic_conditions,
        }
        if self.ignore_error is not None:
            payload["ignore_error"] = self.ignore_error
        if self.preserve_error is not None:
            payload["preserve_error"] = self.preserve_error
        if self.candidate_ignore is not None:
            payload["candidate_ignore"] = self.candidate_ignore.to_json()
        if self.candidate_preserve is not None:
            payload["candidate_preserve"] = self.candidate_preserve.to_json()
        if self.expected_aabb is not None:
            payload["expected_aabb"] = self.expected_aabb.to_json()
        return payload


@dataclass
class UsdaDocument:
    text: str
    blocks: list[XformBlock]
    matrices: list[MatrixOccurrence]


# ---------------------------------------------------------------------------
# Generic text scanning
# ---------------------------------------------------------------------------


def _find_next_nonspace(text: str, start: int) -> int:
    while start < len(text) and text[start].isspace():
        start += 1
    return start


def _find_open_brace(text: str, start: int) -> int:
    """Find an opening brace outside quoted strings and comments."""
    i = start
    in_string = False
    escaped = False
    while i < len(text):
        char = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            i += 1
            continue
        if char == '"':
            in_string = True
            i += 1
            continue
        if char == "#":
            newline = text.find("\n", i)
            return -1 if newline < 0 else _find_open_brace(text, newline + 1)
        if char == "{":
            return i
        # A new declaration before a brace means the match was malformed.
        if char == "\n" and text.startswith("def ", _find_next_nonspace(text, i + 1)):
            return -1
        i += 1
    return -1


def _find_matching_delimiter(
    text: str,
    start: int,
    opening: str,
    closing: str,
) -> int:
    if start < 0 or start >= len(text) or text[start] != opening:
        raise ConversionError(
            f"Expected {opening!r} at text offset {start}, got "
            f"{text[start:start + 1]!r}"
        )

    depth = 0
    in_string = False
    escaped = False
    i = start
    while i < len(text):
        char = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            i += 1
            continue
        if char == '"':
            in_string = True
        elif char == "#":
            newline = text.find("\n", i)
            if newline < 0:
                break
            i = newline
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return i
            if depth < 0:
                break
        i += 1
    raise ConversionError(
        f"Unbalanced {opening}{closing} expression beginning at offset {start}"
    )


def _smallest_containing_block(
    blocks: Sequence[XformBlock],
    offset: int,
) -> int | None:
    candidates = [
        (block.close_brace - block.open_brace, index)
        for index, block in enumerate(blocks)
        if block.open_brace < offset < block.close_brace
    ]
    return min(candidates)[1] if candidates else None


def _line_indent(text: str, offset: int) -> str:
    line_start = text.rfind("\n", 0, offset) + 1
    match = re.match(r"[ \t]*", text[line_start:offset])
    return match.group(0) if match else ""


# ---------------------------------------------------------------------------
# USDA parsing
# ---------------------------------------------------------------------------


def parse_matrix_values(expression: str) -> tuple[float, ...]:
    numbers = tuple(float(value) for value in NUMBER_RE.findall(expression))
    if len(numbers) != 16:
        raise ConversionError(
            f"Expected 16 values in matrix4d, found {len(numbers)}: "
            f"{expression[:200]!r}"
        )
    if not all(math.isfinite(value) for value in numbers):
        raise ConversionError("Matrix contains NaN or infinity")
    return numbers


def parse_usda(text: str) -> UsdaDocument:
    blocks: list[XformBlock] = []
    for match in XFORM_RE.finditer(text):
        open_brace = _find_open_brace(text, match.end())
        if open_brace < 0:
            continue
        close_brace = _find_matching_delimiter(text, open_brace, "{", "}")
        blocks.append(
            XformBlock(
                name=match.group(1),
                declaration_start=match.start(),
                open_brace=open_brace,
                close_brace=close_brace,
            )
        )

    for index, block in enumerate(blocks):
        parents = [
            (candidate.close_brace - candidate.open_brace, candidate_index)
            for candidate_index, candidate in enumerate(blocks)
            if candidate_index != index
            and candidate.open_brace < block.open_brace
            and candidate.close_brace > block.close_brace
        ]
        block.parent_index = min(parents)[1] if parents else None
        block.contains_mesh = bool(
            re.search(r'\bdef\s+Mesh\s+"', text[block.body_start:block.body_end])
        )

    matrices: list[MatrixOccurrence] = []
    for match in MATRIX_RE.finditer(text):
        expression_start = text.find("(", match.end())
        if expression_start < 0:
            raise ConversionError(
                f"Missing matrix expression after offset {match.start()}"
            )
        expression_close = _find_matching_delimiter(
            text, expression_start, "(", ")"
        )
        occurrence = MatrixOccurrence(
            attribute_start=match.start(),
            expression_start=expression_start,
            expression_end=expression_close + 1,
            values=parse_matrix_values(
                text[expression_start:expression_close + 1]
            ),
            block_index=_smallest_containing_block(blocks, match.start()),
        )
        matrix_index = len(matrices)
        matrices.append(occurrence)
        if occurrence.block_index is not None:
            block = blocks[occurrence.block_index]
            # The first xformOp on the smallest Xform is its node transform.
            if block.matrix_index is None:
                block.matrix_index = matrix_index

    if not matrices:
        raise ConversionError("No matrix4d xformOp:transform attributes found")

    return UsdaDocument(text=text, blocks=blocks, matrices=matrices)


# ---------------------------------------------------------------------------
# Matrix and AABB math
# ---------------------------------------------------------------------------


def nearly_equal(a: float, b: float, tolerance: float) -> bool:
    return math.isclose(a, b, rel_tol=0.0, abs_tol=tolerance)


def vectors_nearly_equal(
    first: Sequence[float],
    second: Sequence[float],
    tolerance: float,
) -> bool:
    return len(first) == len(second) and all(
        nearly_equal(a, b, tolerance) for a, b in zip(first, second)
    )


def normalize_affine_matrix(values: Sequence[float]) -> tuple[float, ...]:
    if len(values) != 16:
        raise ConversionError("A 4x4 matrix must contain 16 values")
    result = list(float(value) for value in values)
    # System.Numerics / USD row-vector layout: translation is M41..M43.
    result[3] = 0.0
    result[7] = 0.0
    result[11] = 0.0
    result[15] = 1.0
    return tuple(result)


def apply_usd_row_matrix(
    point: Sequence[float],
    matrix: Sequence[float],
) -> tuple[float, float, float]:
    """Apply a row-vector USD/System.Numerics Matrix4x4."""
    x, y, z = point
    return (
        x * matrix[0] + y * matrix[4] + z * matrix[8] + matrix[12],
        x * matrix[1] + y * matrix[5] + z * matrix[9] + matrix[13],
        x * matrix[2] + y * matrix[6] + z * matrix[10] + matrix[14],
    )


def apply_instance_matrix(
    point: Sequence[float],
    matrix: Sequence[Sequence[float]],
) -> tuple[float, float, float]:
    """Apply CryEngine Matrix34 represented as three conventional rows."""
    x, y, z = point
    return (
        matrix[0][0] * x + matrix[0][1] * y + matrix[0][2] * z + matrix[0][3],
        matrix[1][0] * x + matrix[1][1] * y + matrix[1][2] * z + matrix[1][3],
        matrix[2][0] * x + matrix[2][1] * y + matrix[2][2] * z + matrix[2][3],
    )


def vector_distance(first: Sequence[float], second: Sequence[float]) -> float:
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(first, second)))


def aabb_error(candidate: Aabb, expected: Aabb) -> float:
    return vector_distance(candidate.minimum, expected.minimum) + vector_distance(
        candidate.maximum, expected.maximum
    )


def extent_corners(extent: Aabb) -> Iterator[tuple[float, float, float]]:
    for x in (extent.minimum[0], extent.maximum[0]):
        for y in (extent.minimum[1], extent.maximum[1]):
            for z in (extent.minimum[2], extent.maximum[2]):
                yield x, y, z


# ---------------------------------------------------------------------------
# Geometry extraction for level-assisted validation
# ---------------------------------------------------------------------------


def _parse_vector_tuples(array_text: str) -> Iterator[tuple[float, float, float]]:
    for tuple_match in re.finditer(r"\(([^()]*)\)", array_text):
        numbers = NUMBER_RE.findall(tuple_match.group(1))
        if len(numbers) < 3:
            continue
        point = tuple(float(value) for value in numbers[:3])
        if not all(math.isfinite(value) for value in point):
            raise ConversionError("Mesh point array contains NaN or infinity")
        yield point  # type: ignore[misc]


def _matrix_for_offset(document: UsdaDocument, offset: int) -> tuple[float, ...]:
    block_index = _smallest_containing_block(document.blocks, offset)
    while block_index is not None:
        block = document.blocks[block_index]
        if block.matrix_index is not None:
            return document.matrices[block.matrix_index].values
        block_index = block.parent_index
    return IDENTITY_4X4


def _block_is_physics_for_offset(document: UsdaDocument, offset: int) -> bool:
    block_index = _smallest_containing_block(document.blocks, offset)
    while block_index is not None:
        block = document.blocks[block_index]
        if block.is_physics:
            return True
        block_index = block.parent_index
    return False


def iter_render_geometry(
    document: UsdaDocument,
) -> Iterator[tuple[tuple[float, float, float], tuple[float, ...]]]:
    """Yield raw render points with the active node matrix that owns them."""
    point_arrays_found = 0
    for match in POINTS_RE.finditer(document.text):
        if _block_is_physics_for_offset(document, match.start()):
            continue
        bracket = document.text.find("[", match.end())
        if bracket < 0:
            continue
        close = _find_matching_delimiter(document.text, bracket, "[", "]")
        matrix = _matrix_for_offset(document, match.start())
        yielded = False
        for point in _parse_vector_tuples(document.text[bracket:close + 1]):
            yielded = True
            yield point, matrix
        if yielded:
            point_arrays_found += 1

    if point_arrays_found:
        return

    # Fallback for minimal/debug USDA files that contain only extents.
    for match in EXTENT_RE.finditer(document.text):
        if _block_is_physics_for_offset(document, match.start()):
            continue
        bracket = document.text.find("[", match.end())
        if bracket < 0:
            continue
        close = _find_matching_delimiter(document.text, bracket, "[", "]")
        vectors = list(_parse_vector_tuples(document.text[bracket:close + 1]))
        if len(vectors) < 2:
            continue
        extent = Aabb(vectors[0], vectors[1])
        matrix = _matrix_for_offset(document, match.start())
        for corner in extent_corners(extent):
            yield corner, matrix


def calculate_validation_candidates(
    document: UsdaDocument,
    validation: ValidationInput,
) -> tuple[Aabb, Aabb, int]:
    ignored = BoundsAccumulator()
    preserved = BoundsAccumulator()

    for raw_point, raw_node_matrix in iter_render_geometry(document):
        ignored.add(
            apply_instance_matrix(raw_point, validation.instance_matrix)
        )
        node_point = apply_usd_row_matrix(
            raw_point,
            normalize_affine_matrix(raw_node_matrix),
        )
        preserved.add(
            apply_instance_matrix(node_point, validation.instance_matrix)
        )

    if ignored.count == 0:
        raise ConversionError(
            "Could not find render mesh points or extents in the generated USDA"
        )
    return ignored.finish(), preserved.finish(), ignored.count


# ---------------------------------------------------------------------------
# Transform policy
# ---------------------------------------------------------------------------


def classify_auto_heuristic(
    document: UsdaDocument,
    source_suffix: str,
    tolerance: float,
) -> tuple[bool, dict[str, bool | int | float | str]]:
    matrices = document.matrices
    matrix_blocks = [
        document.blocks[item.block_index]
        for item in matrices
        if item.block_index is not None
    ]

    parents = {
        block.parent_index
        for block in matrix_blocks
    }
    translations = [item.translation for item in matrices]
    first_translation = translations[0]

    conditions: dict[str, bool | int | float | str] = {
        "static_cgf": source_suffix.casefold() == ".cgf",
        "node_count": len(matrices),
        "all_m44_zero": all(nearly_equal(item.m44, 0.0, tolerance) for item in matrices),
        "shared_translation": all(
            vectors_nearly_equal(value, first_translation, tolerance)
            for value in translations[1:]
        ),
        "flat_hierarchy": len(parents) <= 1,
        "has_render_node": any(
            block.contains_mesh and not block.is_physics for block in matrix_blocks
        ),
        "has_physics_node": any(block.is_physics for block in matrix_blocks),
        "translation_x": first_translation[0],
        "translation_y": first_translation[1],
        "translation_z": first_translation[2],
    }

    suspicious = all(
        bool(conditions[name])
        for name in (
            "static_cgf",
            "all_m44_zero",
            "shared_translation",
            "flat_hierarchy",
            "has_render_node",
            "has_physics_node",
        )
    )
    return suspicious, conditions


def choose_decision(
    document: UsdaDocument,
    requested_mode: str,
    source_suffix: str,
    validation: ValidationInput | None,
    matrix_tolerance: float,
    aabb_ambiguity: float,
) -> Decision:
    if requested_mode == "ignore":
        return Decision(
            requested_mode=requested_mode,
            effective_mode="ignore",
            reason="ignored_by_user",
        )

    if requested_mode == "preserve":
        return Decision(
            requested_mode=requested_mode,
            effective_mode="preserve",
            reason="preserved_by_user_normalized_affine",
        )

    suspicious, conditions = classify_auto_heuristic(
        document, source_suffix, matrix_tolerance
    )

    if validation is not None:
        ignored_bounds, preserved_bounds, point_count = (
            calculate_validation_candidates(document, validation)
        )
        ignore_error = aabb_error(ignored_bounds, validation.expected_aabb)
        preserve_error = aabb_error(preserved_bounds, validation.expected_aabb)
        difference = abs(ignore_error - preserve_error)
        conditions["validation_point_count"] = point_count
        conditions["aabb_error_difference"] = difference

        if difference > aabb_ambiguity:
            if ignore_error < preserve_error:
                return Decision(
                    requested_mode=requested_mode,
                    effective_mode="ignore",
                    reason="ignored_level_aabb_match",
                    heuristic_conditions=conditions,
                    ignore_error=ignore_error,
                    preserve_error=preserve_error,
                    candidate_ignore=ignored_bounds,
                    candidate_preserve=preserved_bounds,
                    expected_aabb=validation.expected_aabb,
                )
            return Decision(
                requested_mode=requested_mode,
                effective_mode="preserve",
                reason="preserved_level_aabb_match",
                heuristic_conditions=conditions,
                ignore_error=ignore_error,
                preserve_error=preserve_error,
                candidate_ignore=ignored_bounds,
                candidate_preserve=preserved_bounds,
                expected_aabb=validation.expected_aabb,
            )

        conditions["aabb_validation_ambiguous"] = True

    if suspicious:
        return Decision(
            requested_mode=requested_mode,
            effective_mode="ignore",
            reason="ignored_static_cgf_shared_non_affine_transform",
            heuristic_conditions=conditions,
        )

    return Decision(
        requested_mode=requested_mode,
        effective_mode="preserve",
        reason="preserved_auto_no_legacy_signature",
        heuristic_conditions=conditions,
    )


# ---------------------------------------------------------------------------
# USDA writing
# ---------------------------------------------------------------------------


def _format_number(value: float) -> str:
    if nearly_equal(value, 0.0, 1e-15):
        value = 0.0
    if nearly_equal(value, round(value), 1e-12):
        return str(int(round(value)))
    return format(value, ".10g")


def format_matrix(values: Sequence[float]) -> str:
    rows = []
    for row in range(4):
        start = row * 4
        rows.append(
            "(" + ", ".join(_format_number(v) for v in values[start:start + 4]) + ")"
        )
    return "( " + ", ".join(rows) + " )"


def format_raw_array(values: Sequence[float]) -> str:
    return "[" + ", ".join(_format_number(value) for value in values) + "]"


def escape_usda_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def rewrite_usda(
    document: UsdaDocument,
    decision: Decision,
    source_path: Path,
) -> str:
    if "cryengine:transformDecision" in document.text:
        raise ConversionError(
            "This USDA already contains transform-decision metadata. "
            "Use an unmodified converter output as input."
        )

    replacements: list[tuple[int, int, str]] = []
    for occurrence in document.matrices:
        if decision.effective_mode == "ignore":
            active = IDENTITY_4X4
        elif decision.effective_mode == "preserve":
            active = normalize_affine_matrix(occurrence.values)
        else:
            raise ConversionError(
                f"Unsupported effective mode {decision.effective_mode!r}"
            )

        indent = _line_indent(document.text, occurrence.attribute_start)
        metadata = (
            "\n"
            f"{indent}custom double[] cryengine:originalNodeMatrixRaw = "
            f"{format_raw_array(occurrence.values)}\n"
            f"{indent}custom string cryengine:transformDecision = "
            f'"{escape_usda_string(decision.reason)}"\n'
            f"{indent}custom string cryengine:nodeTransformMode = "
            f'"{escape_usda_string(decision.requested_mode)}"\n'
            f"{indent}custom string cryengine:sourceAsset = "
            f'"{escape_usda_string(source_path.as_posix())}"'
        )
        replacements.append(
            (
                occurrence.expression_start,
                occurrence.expression_end,
                format_matrix(active) + metadata,
            )
        )

    output = document.text
    for start, end, replacement in sorted(replacements, reverse=True):
        output = output[:start] + replacement + output[end:]
    return output


# ---------------------------------------------------------------------------
# Validation input parsing
# ---------------------------------------------------------------------------


def _float_sequence(value: object, expected: int, label: str) -> tuple[float, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != expected:
        raise ConversionError(f"{label} must contain exactly {expected} numbers")
    result = tuple(float(item) for item in value)
    if not all(math.isfinite(item) for item in result):
        raise ConversionError(f"{label} contains NaN or infinity")
    return result


def parse_instance_matrix(value: object) -> tuple[tuple[float, float, float, float], ...]:
    if not isinstance(value, (list, tuple)) or len(value) not in (3, 4):
        raise ConversionError("instance_matrix must be a 3x4 or 4x4 array")
    rows = tuple(_float_sequence(row, 4, "instance_matrix row") for row in value)
    if len(rows) == 4 and not vectors_nearly_equal(rows[3], (0, 0, 0, 1), 1e-5):
        raise ConversionError("The fourth instance_matrix row must be [0,0,0,1]")
    return rows[:3]  # type: ignore[return-value]


def parse_expected_aabb(value: object) -> Aabb:
    if isinstance(value, dict):
        minimum = _float_sequence(value.get("min"), 3, "expected_aabb.min")
        maximum = _float_sequence(value.get("max"), 3, "expected_aabb.max")
    elif isinstance(value, (list, tuple)) and len(value) == 6:
        numbers = _float_sequence(value, 6, "expected_aabb")
        minimum, maximum = numbers[:3], numbers[3:]
    else:
        raise ConversionError(
            "expected_aabb must be {\"min\":[x,y,z],\"max\":[x,y,z]} "
            "or six numbers"
        )
    if any(a > b for a, b in zip(minimum, maximum)):
        raise ConversionError("expected_aabb minimum exceeds maximum")
    return Aabb(minimum, maximum)  # type: ignore[arg-type]


def parse_matrix_cli(value: str) -> tuple[tuple[float, float, float, float], ...]:
    rows = [row.strip() for row in value.split(";") if row.strip()]
    parsed = [[float(item.strip()) for item in row.split(",")] for row in rows]
    return parse_instance_matrix(parsed)


def parse_aabb_cli(value: str) -> Aabb:
    numbers = [float(item.strip()) for item in value.split(",")]
    return parse_expected_aabb(numbers)


def load_validation(args: argparse.Namespace) -> ValidationInput | None:
    payload: dict[str, object] = {}
    if args.validation_sidecar:
        sidecar_path = Path(args.validation_sidecar).expanduser().resolve()
        try:
            payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConversionError(f"Could not read validation sidecar: {exc}") from exc
        if not isinstance(payload, dict):
            raise ConversionError("Validation sidecar root must be a JSON object")

    matrix_value: object | None = payload.get("instance_matrix")
    aabb_value: object | None = payload.get("expected_aabb")

    if args.instance_matrix:
        matrix_value = parse_matrix_cli(args.instance_matrix)
    if args.expected_aabb:
        aabb_value = parse_aabb_cli(args.expected_aabb)

    if matrix_value is None and aabb_value is None:
        return None
    if matrix_value is None or aabb_value is None:
        raise ConversionError(
            "AABB validation requires both instance_matrix and expected_aabb"
        )

    instance = (
        matrix_value
        if isinstance(matrix_value, tuple)
        else parse_instance_matrix(matrix_value)
    )
    expected = aabb_value if isinstance(aabb_value, Aabb) else parse_expected_aabb(aabb_value)
    return ValidationInput(instance, expected)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Upstream converter invocation
# ---------------------------------------------------------------------------


def locate_generated_usda(directory: Path, input_stem: str) -> Path:
    candidates = list(directory.rglob("*.usda"))
    if not candidates:
        raise ConversionError(
            f"cgf-converter completed but wrote no USDA file under {directory}"
        )

    exact = [path for path in candidates if path.stem.casefold() == input_stem.casefold()]
    pool = exact or candidates
    return max(pool, key=lambda path: path.stat().st_mtime_ns)


def run_upstream_converter(
    input_path: Path,
    converter: str,
    objectdir: Path | None,
    converter_args: Sequence[str],
) -> tuple[str, list[str]]:
    with tempfile.TemporaryDirectory(prefix="prey-cgf-usd-") as temp_name:
        temp_dir = Path(temp_name)
        command = [converter, str(input_path), "-usda", "-out", str(temp_dir)]
        if objectdir is not None:
            command.extend(["-objectdir", str(objectdir)])
        command.extend(converter_args)

        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            raise ConversionError(
                f"Could not start cgf-converter {converter!r}: {exc}"
            ) from exc

        if completed.returncode != 0:
            message = (
                f"cgf-converter failed with exit code {completed.returncode}\n"
                f"Command: {subprocess.list2cmdline(command)}\n"
                f"STDOUT:\n{completed.stdout}\n"
                f"STDERR:\n{completed.stderr}"
            )
            raise ConversionError(message)

        generated = locate_generated_usda(temp_dir, input_path.stem)
        try:
            text = generated.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise ConversionError(f"Could not read generated USDA: {exc}") from exc
        log_lines = [line for line in (completed.stdout + completed.stderr).splitlines() if line]
        return text, log_lines


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Convert a Prey (2017) static CGF to USDA while explicitly deciding "
            "whether legacy CGF node transforms are active."
        )
    )
    parser.add_argument("input", help="Input .cgf or existing textual .usda file")
    parser.add_argument("output", help="Output .usda path")
    parser.add_argument(
        "--node-transform",
        choices=("auto", "preserve", "ignore"),
        default="auto",
        help="Node-transform policy (default: auto)",
    )
    parser.add_argument(
        "--converter",
        default=os.environ.get("CGF_CONVERTER", "cgf-converter"),
        help="Path/name of Markemp cgf-converter executable",
    )
    parser.add_argument(
        "--objectdir",
        help="Extracted Prey data root passed to cgf-converter -objectdir",
    )
    parser.add_argument(
        "--converter-arg",
        action="append",
        default=[],
        help="Additional argument passed through to cgf-converter; repeatable",
    )
    parser.add_argument(
        "--validation-sidecar",
        help="JSON containing instance_matrix and expected_aabb",
    )
    parser.add_argument(
        "--instance-matrix",
        help=(
            "CryEngine Matrix34 as comma-separated rows separated by semicolons, "
            "e.g. '0,-1,0,623.6;1,0,0,1181.55;0,0,1,83.1'"
        ),
    )
    parser.add_argument(
        "--expected-aabb",
        help="Expected minx,miny,minz,maxx,maxy,maxz",
    )
    parser.add_argument(
        "--matrix-tolerance",
        type=float,
        default=1e-4,
        help="Tolerance for the repeated legacy-matrix heuristic",
    )
    parser.add_argument(
        "--aabb-ambiguity",
        type=float,
        default=0.01,
        help="Minimum error difference required for AABB validation to decide",
    )
    parser.add_argument(
        "--report",
        help="Optional JSON diagnostic report path (default: output + .report.json)",
    )
    parser.add_argument(
        "--no-report",
        action="store_true",
        help="Do not write a diagnostic JSON report",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing output/report",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        input_path = Path(args.input).expanduser().resolve()
        output_path = Path(args.output).expanduser().resolve()
        if not input_path.is_file():
            raise ConversionError(f"Input file not found: {input_path}")
        if output_path.suffix.casefold() != ".usda":
            raise ConversionError(
                "Output must use the textual .usda extension; binary USDC cannot "
                "be safely rewritten by this standard-library tool"
            )
        if output_path.exists() and not args.force:
            raise ConversionError(
                f"Output already exists: {output_path} (use --force to overwrite)"
            )
        if args.matrix_tolerance <= 0 or args.aabb_ambiguity < 0:
            raise ConversionError("Tolerances must be positive/non-negative")

        objectdir = Path(args.objectdir).expanduser().resolve() if args.objectdir else None
        if objectdir is not None and not objectdir.is_dir():
            raise ConversionError(f"objectdir is not a directory: {objectdir}")

        validation = load_validation(args)
        converter_log: list[str] = []
        if input_path.suffix.casefold() == ".usda":
            source_text = input_path.read_text(encoding="utf-8-sig")
            source_suffix = ".cgf"  # Existing USDA is treated as static-CGF output.
        elif input_path.suffix.casefold() == ".cgf":
            source_text, converter_log = run_upstream_converter(
                input_path,
                args.converter,
                objectdir,
                args.converter_arg,
            )
            source_suffix = input_path.suffix
        else:
            raise ConversionError("Input must be .cgf or textual .usda")

        document = parse_usda(source_text)
        decision = choose_decision(
            document=document,
            requested_mode=args.node_transform,
            source_suffix=source_suffix,
            validation=validation,
            matrix_tolerance=args.matrix_tolerance,
            aabb_ambiguity=args.aabb_ambiguity,
        )
        rewritten = rewrite_usda(document, decision, input_path)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rewritten, encoding="utf-8", newline="\n")

        report_path: Path | None = None
        if not args.no_report:
            report_path = (
                Path(args.report).expanduser().resolve()
                if args.report
                else output_path.with_suffix(output_path.suffix + ".report.json")
            )
            if report_path.exists() and not args.force:
                output_path.unlink(missing_ok=True)
                raise ConversionError(
                    f"Report already exists: {report_path} (use --force to overwrite)"
                )
            report = {
                "input": str(input_path),
                "output": str(output_path),
                "matrix_count": len(document.matrices),
                "xform_count": len(document.blocks),
                "converter_log": converter_log,
                "decision": decision.to_json(),
                "matrices": [
                    {
                        "node": (
                            document.blocks[item.block_index].name
                            if item.block_index is not None
                            else None
                        ),
                        "translation": list(item.translation),
                        "m44": item.m44,
                        "raw": list(item.values),
                    }
                    for item in document.matrices
                ],
            }
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

        print(f"Input:      {input_path}")
        print(f"Output:     {output_path}")
        print(f"Requested:  {decision.requested_mode}")
        print(f"Decision:   {decision.effective_mode.upper()}")
        print(f"Reason:     {decision.reason}")
        print(f"Node Xforms:{len(document.matrices):>6}")
        if decision.ignore_error is not None:
            print(f"AABB ignore error:   {decision.ignore_error:.9f}")
            print(f"AABB preserve error: {decision.preserve_error:.9f}")
        if report_path is not None:
            print(f"Report:     {report_path}")
        return 0

    except (ConversionError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
