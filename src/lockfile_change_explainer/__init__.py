"""Compare lockfiles and explain dependency changes."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ._version import __version__


@dataclass(frozen=True)
class DependencyChange:
    name: str
    old_version: str | None
    new_version: str | None
    risk: str
    hint: str


@dataclass(frozen=True)
class LockfileComparison:
    added: list[DependencyChange]
    removed: list[DependencyChange]
    changed: list[DependencyChange]
    unchanged_count: int
    lockfile_format: str
    duplicate_package_names: list[str] = field(default_factory=list)
    duplicate_package_names_by_file: dict[str, list[str]] = field(default_factory=dict)


class InputLimitError(ValueError):
    """Raised when an input exceeds a configured safety limit."""


DEFAULT_MAX_INPUT_BYTES = 10 * 1024 * 1024
MAX_JSON_DEPTH = 64
MAX_TOML_DEPTH = 64
MAX_STRUCTURE_ITEMS = 100_000
RISK_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3}
_RISK_LEVELS = ("low", "medium", "high")
_JSON_NUMBER_RE = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?")


@dataclass(frozen=True)
class _ParsedLockfile:
    dependencies: dict[str, str]
    duplicate_package_names: list[str]


class _JsonObject(dict[str, object]):
    """JSON object that retains duplicate keys for package-map inspection."""

    def __init__(self, pairs: Iterable[tuple[str, object]] = ()) -> None:
        super().__init__()
        self.duplicate_keys: list[str] = []
        for key, value in pairs:
            if key in self:
                self.duplicate_keys.append(key)
            self[key] = value


def compare_lockfiles(
    old_text: str,
    new_text: str,
    lockfile_format: str = "auto",
    max_input_bytes: int | None = DEFAULT_MAX_INPUT_BYTES,
) -> LockfileComparison:
    """Return added, removed, and changed dependency entries."""
    _validate_text_size(old_text, "old", max_input_bytes)
    _validate_text_size(new_text, "new", max_input_bytes)
    resolved_format = _resolve_format(old_text, new_text, lockfile_format)
    old_parsed = _parse_lockfile(old_text, resolved_format)
    new_parsed = _parse_lockfile(new_text, resolved_format)
    old_deps = old_parsed.dependencies
    new_deps = new_parsed.dependencies

    added = [
        DependencyChange(name, None, new_deps[name], "medium", "new dependency expands install surface")
        for name in sorted(new_deps.keys() - old_deps.keys())
    ]
    removed = [
        DependencyChange(name, old_deps[name], None, "medium", "removed dependency may break imports or builds")
        for name in sorted(old_deps.keys() - new_deps.keys())
    ]
    changed = []
    for name in sorted(old_deps.keys() & new_deps.keys()):
        old_version = old_deps[name]
        new_version = new_deps[name]
        if old_version != new_version:
            risk, hint = _version_risk(old_version, new_version)
            changed.append(DependencyChange(name, old_version, new_version, risk, hint))

    unchanged_count = sum(1 for name in old_deps.keys() & new_deps.keys() if old_deps[name] == new_deps[name])
    duplicate_by_file = {
        "old": list(old_parsed.duplicate_package_names),
        "new": list(new_parsed.duplicate_package_names),
    }
    duplicate_names = sorted(set(old_parsed.duplicate_package_names) | set(new_parsed.duplicate_package_names))
    return LockfileComparison(
        added,
        removed,
        changed,
        unchanged_count,
        resolved_format,
        duplicate_names,
        duplicate_by_file,
    )


def filter_by_min_risk(result: LockfileComparison, minimum_risk: str) -> LockfileComparison:
    """Return a comparison containing only changes at or above the requested risk."""
    threshold = RISK_ORDER[minimum_risk]

    def keep(change: DependencyChange) -> bool:
        return RISK_ORDER.get(change.risk, 0) >= threshold

    return LockfileComparison(
        added=[item for item in result.added if keep(item)],
        removed=[item for item in result.removed if keep(item)],
        changed=[item for item in result.changed if keep(item)],
        unchanged_count=result.unchanged_count,
        lockfile_format=result.lockfile_format,
        duplicate_package_names=list(result.duplicate_package_names),
        duplicate_package_names_by_file={
            key: list(value) for key, value in result.duplicate_package_names_by_file.items()
        },
    )


def format_text_report(result: LockfileComparison) -> str:
    lines = [
        f"Lockfile format: {result.lockfile_format}",
        f"Summary: +{len(result.added)} -{len(result.removed)} ~{len(result.changed)} unchanged={result.unchanged_count}",
        "",
    ]
    _append_section(lines, "Added", result.added)
    _append_section(lines, "Removed", result.removed)
    _append_section(lines, "Changed", result.changed)
    _append_security_findings(lines, result)
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Explain dependency changes between two lockfiles.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--old", required=True, help="Path to the old lockfile")
    parser.add_argument("--new", required=True, help="Path to the new lockfile")
    parser.add_argument(
        "--format",
        choices=["auto", "package-lock", "requirements", "pylock"],
        default="auto",
        help="Lockfile format to parse",
    )
    parser.add_argument(
        "--min-risk",
        choices=["low", "medium", "high"],
        default="low",
        help="Only include dependency changes at or above this risk level.",
    )
    parser.add_argument(
        "--fail-on-risk",
        choices=["low", "medium", "high"],
        help="Exit with status 1 when the comparison reaches this risk level.",
    )
    parser.add_argument(
        "--max-input-bytes",
        type=_positive_int,
        default=DEFAULT_MAX_INPUT_BYTES,
        help=f"Maximum UTF-8 bytes allowed for each input file (default: {DEFAULT_MAX_INPUT_BYTES}).",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument("--output", help="Write output to a file instead of stdout")
    args = parser.parse_args(argv)

    try:
        old_text = _read_text_limited(Path(args.old), args.max_input_bytes)
        new_text = _read_text_limited(Path(args.new), args.max_input_bytes)
        comparison = compare_lockfiles(old_text, new_text, args.format, args.max_input_bytes)
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2

    result = filter_by_min_risk(comparison, args.min_risk)
    output = (
        json.dumps(
            _comparison_to_dict(result, comparison, args.fail_on_risk), indent=2, sort_keys=True
        )
        + "\n"
        if args.json
        else format_text_report(result)
    )

    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
    else:
        sys.stdout.write(output)
    return 1 if risk_summary(comparison, args.fail_on_risk)["threshold_exceeded"] else 0


def _append_section(lines: list[str], title: str, changes: Iterable[DependencyChange]) -> None:
    rows = list(changes)
    lines.append(f"{title}:")
    if not rows:
        lines.append("  none")
    for change in rows:
        if change.old_version and change.new_version:
            version_text = f"{change.old_version} -> {change.new_version}"
        else:
            version_text = change.new_version or change.old_version or "unknown"
        lines.append(f"  - {change.name} {version_text} [{change.risk}] {change.hint}")
    lines.append("")


def _append_security_findings(lines: list[str], result: LockfileComparison) -> None:
    if not result.duplicate_package_names:
        return

    lines.append("Security findings:")
    lines.append("  Duplicate package names:")
    by_file = result.duplicate_package_names_by_file
    for name in result.duplicate_package_names:
        locations = [label for label in ("old", "new") if name in by_file.get(label, [])]
        location_text = ", ".join(locations)
        lines.append(
            f"  - {name} ({location_text}) [high] duplicate entries can make dependency resolution ambiguous"
        )
    lines.append("")


def risk_summary(result: LockfileComparison, fail_on_risk: str | None = None) -> dict[str, object]:
    """Return a stable JSON-friendly risk and security summary."""
    if fail_on_risk is not None and fail_on_risk not in RISK_ORDER:
        raise ValueError(f"unknown risk threshold: {fail_on_risk}")

    counts = {level: 0 for level in _RISK_LEVELS}
    max_risk = "none"
    for change in (*result.added, *result.removed, *result.changed):
        if change.risk in counts:
            counts[change.risk] += 1
            if RISK_ORDER[change.risk] > RISK_ORDER[max_risk]:
                max_risk = change.risk

    if result.duplicate_package_names:
        max_risk = "high"

    threshold_exceeded = (
        fail_on_risk is not None and RISK_ORDER[max_risk] >= RISK_ORDER[fail_on_risk]
    )
    return {
        "max_risk": max_risk,
        "counts": counts,
        "has_findings": bool(
            result.added or result.removed or result.changed or result.duplicate_package_names
        ),
        "has_duplicates": bool(result.duplicate_package_names),
        "duplicate_package_count": len(result.duplicate_package_names),
        "duplicate_package_names": list(result.duplicate_package_names),
        "duplicate_package_names_by_file": {
            key: list(value) for key, value in result.duplicate_package_names_by_file.items()
        },
        "fail_on_risk": fail_on_risk,
        "threshold_exceeded": threshold_exceeded,
    }


def _comparison_to_dict(
    result: LockfileComparison,
    risk_result: LockfileComparison | None = None,
    fail_on_risk: str | None = None,
) -> dict[str, object]:
    return {
        "lockfile_format": result.lockfile_format,
        "added": [asdict(item) for item in result.added],
        "removed": [asdict(item) for item in result.removed],
        "changed": [asdict(item) for item in result.changed],
        "unchanged_count": result.unchanged_count,
        "duplicate_package_names": list(result.duplicate_package_names),
        "duplicate_package_names_by_file": {
            key: list(value) for key, value in result.duplicate_package_names_by_file.items()
        },
        "risk_summary": risk_summary(risk_result or result, fail_on_risk),
    }


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _read_text_limited(path: Path, max_bytes: int) -> str:
    """Check the file size before reading and detect growth during the read."""
    if max_bytes <= 0:
        raise ValueError("max_input_bytes must be positive")
    file_size = path.stat().st_size
    if file_size > max_bytes:
        raise InputLimitError(
            f"{path} is {file_size} bytes; maximum allowed is {max_bytes} bytes"
        )

    with path.open("rb") as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise InputLimitError(
            f"{path} grew beyond the maximum allowed size of {max_bytes} bytes while reading"
        )
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path} is not valid UTF-8") from exc


def _validate_text_size(text: str, label: str, max_bytes: int | None) -> None:
    if max_bytes is None:
        return
    if max_bytes <= 0:
        raise ValueError("max_input_bytes must be positive")
    size = len(text.encode("utf-8"))
    if size > max_bytes:
        raise InputLimitError(f"{label} input is {size} bytes; maximum allowed is {max_bytes} bytes")


def _resolve_format(old_text: str, new_text: str, requested: str) -> str:
    if requested != "auto":
        return (
            "requirements"
            if requested == "pylock" and not (_looks_like_toml(old_text) or _looks_like_toml(new_text))
            else requested
        )
    if old_text.lstrip().startswith("{") or new_text.lstrip().startswith("{"):
        return "package-lock"
    if _looks_like_toml(old_text) or _looks_like_toml(new_text):
        return "pylock"
    return "requirements"


def _parse_lockfile(text: str, lockfile_format: str) -> _ParsedLockfile:
    if lockfile_format == "package-lock":
        return _parse_package_lock(text)
    if lockfile_format == "pylock":
        return _parse_pylock(text)
    return _parse_requirements(text)


def _parse_package_lock(text: str) -> _ParsedLockfile:
    _scan_json_limits(text)
    try:
        data = json.loads(text, object_pairs_hook=_JsonObject)
    except RecursionError as exc:
        raise InputLimitError(f"JSON nesting exceeds the maximum depth of {MAX_JSON_DEPTH}") from exc
    if not isinstance(data, dict):
        raise ValueError("package-lock JSON root must be an object")

    deps: dict[str, str] = {}
    duplicate_names: set[str] = set()
    seen_names: set[str] = set()
    packages = data.get("packages", {})
    dependencies = data.get("dependencies", {})
    if not isinstance(packages, dict):
        raise ValueError("package-lock 'packages' must be an object")
    if not isinstance(dependencies, dict):
        raise ValueError("package-lock 'dependencies' must be an object")

    for key in packages:
        name = _package_lock_name(str(key))
        if name:
            _mark_duplicate(name, seen_names, duplicate_names)
    if isinstance(packages, _JsonObject):
        for key in packages.duplicate_keys:
            name = _package_lock_name(key)
            if name:
                duplicate_names.add(name)

    for key, value in packages.items():
        name = _package_lock_name(str(key))
        if not name:
            continue
        version = str(value.get("version", "")).strip() if isinstance(value, dict) else ""
        if version:
            deps[name] = version

    for name, value in dependencies.items():
        package_name = str(name).strip()
        if not package_name:
            continue
        _mark_duplicate(package_name, seen_names, duplicate_names)
        if isinstance(dependencies, _JsonObject) and name in dependencies.duplicate_keys:
            duplicate_names.add(package_name)
        version = str(value.get("version", "")).strip() if isinstance(value, dict) else str(value).strip()
        if version:
            deps.setdefault(package_name, version)
    return _ParsedLockfile(deps, sorted(duplicate_names))


_REQ_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)(?:\[[^\]]+\])?\s*(?:==|===|>=|<=|~=|>|<)\s*([^;#\s]+)")


def _parse_requirements(text: str) -> _ParsedLockfile:
    deps: dict[str, str] = {}
    duplicate_names: set[str] = set()
    seen_names: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith(("-r ", "--")):
            continue
        match = _REQ_RE.match(stripped)
        if match:
            name = _normalize_name(match.group(1))
            _mark_duplicate(name, seen_names, duplicate_names)
            deps[name] = match.group(2).strip()
    return _ParsedLockfile(deps, sorted(duplicate_names))


def _parse_pylock(text: str) -> _ParsedLockfile:
    _scan_toml_limits(text)
    try:
        data = tomllib.loads(text)
    except RecursionError as exc:
        raise InputLimitError(f"TOML nesting exceeds the maximum depth of {MAX_TOML_DEPTH}") from exc
    _validate_structure_limits(data, MAX_TOML_DEPTH, MAX_STRUCTURE_ITEMS)
    if not isinstance(data, dict):
        raise ValueError("pylock TOML root must be a table")

    packages = data.get("packages", data.get("package", []))
    deps: dict[str, str] = {}
    duplicate_names: set[str] = set()
    seen_names: set[str] = set()
    if isinstance(packages, list):
        for item in packages:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            name = _normalize_name(str(item["name"]))
            _mark_duplicate(name, seen_names, duplicate_names)
            if item.get("version"):
                deps[name] = str(item["version"])
    return _ParsedLockfile(deps, sorted(duplicate_names))


def _mark_duplicate(name: str, seen_names: set[str], duplicate_names: set[str]) -> None:
    if name in seen_names:
        duplicate_names.add(name)
    seen_names.add(name)


def _package_lock_name(key: str) -> str:
    if not key:
        return ""
    return key.removeprefix("node_modules/").strip()


def _scan_json_limits(text: str) -> None:
    """Walk JSON syntax without allocating objects before enforcing limits."""
    item_count = 0

    def skip_whitespace(position: int) -> int:
        while position < len(text) and text[position] in " \t\r\n":
            position += 1
        return position

    def check_item() -> None:
        nonlocal item_count
        item_count += 1
        if item_count > MAX_STRUCTURE_ITEMS:
            raise InputLimitError(
                f"JSON contains more than the maximum of {MAX_STRUCTURE_ITEMS} items"
            )

    def parse_value(position: int, depth: int) -> int:
        position = skip_whitespace(position)
        if position >= len(text):
            return position
        char = text[position]
        if char == "{":
            return parse_object(position, depth + 1)
        if char == "[":
            return parse_array(position, depth + 1)
        if char == '"':
            _, end = json.decoder.scanstring(text, position + 1, True)
            return end
        for literal in ("true", "false", "null"):
            if text.startswith(literal, position):
                return position + len(literal)
        number = _JSON_NUMBER_RE.match(text, position)
        return number.end() if number else position + 1

    def parse_object(position: int, depth: int) -> int:
        if depth > MAX_JSON_DEPTH:
            raise InputLimitError(f"JSON nesting exceeds the maximum depth of {MAX_JSON_DEPTH}")
        position = skip_whitespace(position + 1)
        if position < len(text) and text[position] == "}":
            return position + 1
        while position < len(text):
            if text[position] != '"':
                return position + 1
            _, position = json.decoder.scanstring(text, position + 1, True)
            position = skip_whitespace(position)
            if position >= len(text) or text[position] != ":":
                return position + 1
            check_item()
            position = parse_value(position + 1, depth)
            position = skip_whitespace(position)
            if position < len(text) and text[position] == "}":
                return position + 1
            if position >= len(text) or text[position] != ",":
                return position + 1
            position = skip_whitespace(position + 1)
        return position

    def parse_array(position: int, depth: int) -> int:
        if depth > MAX_JSON_DEPTH:
            raise InputLimitError(f"JSON nesting exceeds the maximum depth of {MAX_JSON_DEPTH}")
        position = skip_whitespace(position + 1)
        if position < len(text) and text[position] == "]":
            return position + 1
        while position < len(text):
            check_item()
            position = parse_value(position, depth)
            position = skip_whitespace(position)
            if position < len(text) and text[position] == "]":
                return position + 1
            if position >= len(text) or text[position] != ",":
                return position + 1
            position = skip_whitespace(position + 1)
        return position

    parse_value(0, 0)


def _scan_toml_limits(text: str) -> None:
    """Preflight TOML bracket depth and item separators before parsing."""
    depth = 0
    item_count = 0
    position = 0
    line_start = True
    in_basic_string = False
    in_literal_string = False

    def check_item() -> None:
        nonlocal item_count
        item_count += 1
        if item_count > MAX_STRUCTURE_ITEMS:
            raise InputLimitError(
                f"TOML contains more than the maximum of {MAX_STRUCTURE_ITEMS} items"
            )

    while position < len(text):
        if in_basic_string:
            if text.startswith('"""', position):
                in_basic_string = False
                position += 3
            elif text[position] == "\\":
                position += 2
            else:
                position += 1
            continue
        if in_literal_string:
            if text.startswith("'''", position):
                in_literal_string = False
                position += 3
            else:
                position += 1
            continue

        char = text[position]
        if char in "\r\n":
            line_start = True
            position += 1
            continue
        if char in " \t":
            position += 1
            continue
        if char == "#":
            newline = text.find("\n", position)
            position = len(text) if newline == -1 else newline
            continue
        if text.startswith('"""', position):
            in_basic_string = True
            position += 3
            line_start = False
            continue
        if text.startswith("'''", position):
            in_literal_string = True
            position += 3
            line_start = False
            continue
        if char == '"':
            in_basic_string = True
            position += 1
            line_start = False
            continue
        if char == "'":
            in_literal_string = True
            position += 1
            line_start = False
            continue
        if line_start and char == "[":
            check_item()
        if char in "[{":
            depth += 1
            if depth > MAX_TOML_DEPTH:
                raise InputLimitError(f"TOML nesting exceeds the maximum depth of {MAX_TOML_DEPTH}")
        elif char in "]}":
            if depth:
                depth -= 1
        elif char in "=,":
            check_item()
        line_start = False
        position += 1


def _validate_structure_limits(value: object, max_depth: int, max_items: int) -> None:
    stack: list[tuple[object, int]] = [(value, 0)]
    item_count = 0
    while stack:
        current, depth = stack.pop()
        if not isinstance(current, (dict, list)):
            continue
        current_depth = depth + 1
        if current_depth > max_depth:
            raise InputLimitError(f"TOML nesting exceeds the maximum depth of {max_depth}")
        children = current.values() if isinstance(current, dict) else current
        for child in children:
            item_count += 1
            if item_count > max_items:
                raise InputLimitError(f"TOML contains more than the maximum of {max_items} items")
            stack.append((child, current_depth))


def _looks_like_toml(text: str) -> bool:
    return "[[packages]]" in text or "[[package]]" in text or "packages = [" in text


def _normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _version_risk(old_version: str, new_version: str) -> tuple[str, str]:
    old_parts = _semver_parts(old_version)
    new_parts = _semver_parts(new_version)
    if old_parts and new_parts:
        if new_parts < old_parts:
            return "high", "version was downgraded; verify compatibility, security fixes, and resolver constraints"
        if new_parts[0] != old_parts[0]:
            return "high", "major version changed; review breaking changes and transitive impact"
        if len(new_parts) > 1 and len(old_parts) > 1 and new_parts[1] != old_parts[1]:
            return "medium", "minor version changed; check release notes and feature flags"
        return "low", "patch-level change; still verify security and regression notes"
    return "medium", "non-semver version changed; inspect changelog manually"


def _semver_parts(version: str) -> tuple[int, ...] | None:
    match = re.match(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", version)
    if not match:
        return None
    return tuple(int(part) for part in match.groups(default="0"))


__all__ = [
    "__version__",
    "DEFAULT_MAX_INPUT_BYTES",
    "InputLimitError",
    "MAX_JSON_DEPTH",
    "MAX_TOML_DEPTH",
    "MAX_STRUCTURE_ITEMS",
    "DependencyChange",
    "LockfileComparison",
    "compare_lockfiles",
    "filter_by_min_risk",
    "format_text_report",
    "risk_summary",
    "main",
]
