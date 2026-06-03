"""Compare lockfiles and explain dependency changes."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from collections.abc import Iterable
from dataclasses import asdict, dataclass
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


def compare_lockfiles(old_text: str, new_text: str, lockfile_format: str = "auto") -> LockfileComparison:
    """Return added, removed, and changed dependency entries."""
    resolved_format = _resolve_format(old_text, new_text, lockfile_format)
    old_deps = _parse_lockfile(old_text, resolved_format)
    new_deps = _parse_lockfile(new_text, resolved_format)

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
    return LockfileComparison(added, removed, changed, unchanged_count, resolved_format)


def format_text_report(result: LockfileComparison) -> str:
    lines = [
        f"Lockfile format: {result.lockfile_format}",
        f"Summary: +{len(result.added)} -{len(result.removed)} ~{len(result.changed)} unchanged={result.unchanged_count}",
        "",
    ]
    _append_section(lines, "Added", result.added)
    _append_section(lines, "Removed", result.removed)
    _append_section(lines, "Changed", result.changed)
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
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument("--output", help="Write output to a file instead of stdout")
    args = parser.parse_args(argv)

    old_text = Path(args.old).read_text(encoding="utf-8")
    new_text = Path(args.new).read_text(encoding="utf-8")
    result = compare_lockfiles(old_text, new_text, args.format)
    output = json.dumps(_comparison_to_dict(result), indent=2, sort_keys=True) + "\n" if args.json else format_text_report(result)

    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
    else:
        sys.stdout.write(output)
    return 0


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


def _comparison_to_dict(result: LockfileComparison) -> dict[str, object]:
    return {
        "lockfile_format": result.lockfile_format,
        "added": [asdict(item) for item in result.added],
        "removed": [asdict(item) for item in result.removed],
        "changed": [asdict(item) for item in result.changed],
        "unchanged_count": result.unchanged_count,
    }


def _resolve_format(old_text: str, new_text: str, requested: str) -> str:
    if requested != "auto":
        return "requirements" if requested == "pylock" and not _looks_like_toml(old_text + new_text) else requested
    combined = old_text.lstrip() + "\n" + new_text.lstrip()
    if combined.lstrip().startswith("{"):
        return "package-lock"
    if _looks_like_toml(combined):
        return "pylock"
    return "requirements"


def _parse_lockfile(text: str, lockfile_format: str) -> dict[str, str]:
    if lockfile_format == "package-lock":
        return _parse_package_lock(text)
    if lockfile_format == "pylock":
        return _parse_pylock(text)
    return _parse_requirements(text)


def _parse_package_lock(text: str) -> dict[str, str]:
    data = json.loads(text)
    deps: dict[str, str] = {}
    for key, value in data.get("packages", {}).items():
        if not key or key == "":
            continue
        name = key.removeprefix("node_modules/").strip()
        version = str(value.get("version", "")).strip()
        if name and version:
            deps[name] = version
    for name, value in data.get("dependencies", {}).items():
        version = str(value.get("version", "")).strip() if isinstance(value, dict) else str(value).strip()
        if name and version:
            deps.setdefault(name, version)
    return deps


_REQ_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)(?:\[[^\]]+\])?\s*(?:==|===|>=|<=|~=|>|<)\s*([^;#\s]+)")


def _parse_requirements(text: str) -> dict[str, str]:
    deps: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith(("-r ", "--")):
            continue
        match = _REQ_RE.match(stripped)
        if match:
            deps[_normalize_name(match.group(1))] = match.group(2).strip()
    return deps


def _parse_pylock(text: str) -> dict[str, str]:
    data = tomllib.loads(text)
    packages = data.get("packages", data.get("package", []))
    deps: dict[str, str] = {}
    if isinstance(packages, list):
        for item in packages:
            if isinstance(item, dict) and item.get("name") and item.get("version"):
                deps[_normalize_name(str(item["name"]))] = str(item["version"])
    return deps


def _looks_like_toml(text: str) -> bool:
    return "[[packages]]" in text or "[[package]]" in text or "packages = [" in text


def _normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _version_risk(old_version: str, new_version: str) -> tuple[str, str]:
    old_parts = _semver_parts(old_version)
    new_parts = _semver_parts(new_version)
    if old_parts and new_parts:
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


__all__ = ["__version__", "DependencyChange", "LockfileComparison", "compare_lockfiles", "format_text_report", "main"]
