# Security Review: Bounded Lockfile Inputs (2026-07)

## Executive summary

This review covers the lockfile parsing and reporting paths in `lockfile-change-explainer`. The main risk was resource exhaustion or ambiguous dependency interpretation when a CI job processes a hostile or malformed lockfile. The implementation now bounds file reads, bounds JSON/TOML structure complexity, detects duplicate package names, and exposes a machine-readable risk decision for CI.

The tool remains a heuristic comparison utility. It does not verify lockfile signatures, package authenticity, resolver behavior, or the security of the packages being compared.

## Scope and threat model

The inputs are treated as untrusted text supplied by a local checkout, pull request, artifact, or CI job. An attacker may provide:

- very large files or files that grow while they are being read;
- deeply nested JSON/TOML intended to exhaust parser recursion;
- very large arrays, objects, or TOML tables intended to consume memory and CPU;
- duplicate package names with different versions intended to make a report or resolver decision ambiguous;
- malformed or non-UTF-8 input intended to produce uncontrolled failures.

The comparison code does not execute lockfile content, import package names, resolve URLs, or invoke package managers.

## Implemented controls

### Input size

`--max-input-bytes` applies independently to `--old` and `--new`. The default is 10 MiB per file. The CLI checks `Path.stat().st_size` before opening either file, then reads at most one byte beyond the configured limit to detect a file that grew after the preflight check. Bytes are decoded as strict UTF-8 only after the bounded read.

The public `compare_lockfiles` function applies the same default to already-loaded text so callers do not accidentally bypass the parser budget. Callers that already enforce a different budget can pass an explicit `max_input_bytes` value or `None` when the text is trusted and bounded elsewhere.

### JSON limits

JSON is scanned before `json.loads` to enforce a maximum nesting depth of 64 and a maximum of 100,000 object members or array values. JSON object construction retains duplicate keys so package-map duplicates are not silently lost. Invalid JSON is still rejected by the standard library parser.

### TOML limits

TOML is preflight-scanned for bracket depth and item separators before `tomllib.loads`. The decoded object is then walked iteratively with the same maximum nesting depth of 64 and 100,000-item budget. The iterative validation avoids adding another recursive walk over attacker-controlled structure.

### Duplicate package names

Requirements entries are normalized before duplicate detection. Package-lock entries are checked across the `packages` and top-level `dependencies` maps, including duplicate JSON object keys. Pylock entries are normalized and checked across the package table list. Duplicate names remain visible in text reports and are included in both the top-level JSON fields and `risk_summary`.

Duplicate package names are classified as high risk because an ambiguous package set can hide a version discrepancy or make downstream dependency resolution differ from the report.

The implementation anchors for these controls are `src/lockfile_change_explainer/__init__.py:68` for the bounded comparison entry point, `:301` for pre-read file-size enforcement, `:355` for package-lock duplicate handling, `:408` and `:424` for requirements and pylock duplicate handling, and `:461` and `:539` for JSON and TOML preflight limits.

### CI decision output

JSON reports keep the existing comparison fields and add:

- `duplicate_package_names` and `duplicate_package_names_by_file`;
- `risk_summary.max_risk`, `risk_summary.counts`, duplicate metadata, and `threshold_exceeded`;
- the selected `fail_on_risk` threshold.

`--fail-on-risk low|medium|high` evaluates the complete comparison before `--min-risk` filtering. A threshold breach writes the requested report and returns exit status 1. Input or parsing failures return exit status 2.

## Compatibility and residual risk

- Normal text output is unchanged when no duplicate package names are present.
- Existing JSON comparison fields remain available; the new fields are additive.
- Risk values are heuristic and do not replace package vulnerability scanning or review of release notes.
- The default limits may reject unusually large but legitimate lockfiles; CI can raise the byte budget explicitly after reviewing the trust boundary.
- The tool does not resolve or fetch dependencies, so it cannot prove that a reported version is available, authentic, or free of known vulnerabilities.

## Verification

The regression suite covers oversized old and new files, JSON/TOML nesting limits, structure-item limits, duplicate detection for all supported formats, JSON risk summaries, and `--fail-on-risk`.

- `python -m unittest discover -s tests -v`: exit 0, 14 tests passed.
- `python -m ruff check .`: exit 0.
- `python -m bandit -r src -ll`: exit 0, no issues identified.
- `python -m pip_audit`: exit 0, no known vulnerabilities; `smolagents==1.27.0.dev0` was skipped because it is not available on PyPI.
- `python -m build --sdist --wheel`: exit 0; sdist and wheel built successfully.
- `git diff --check`: exit 0.
