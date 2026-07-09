# lockfile-change-explainer

`lockfile-change-explainer` is a stdlib-first Python CLI that compares two lockfiles and explains dependency additions, removals, and version changes.

Supported v0.1 inputs:

- `package-lock.json` using `packages` or top-level `dependencies`
- requirements-style files such as `requirements.txt`
- simple pylock/TOML files with package entries containing `name` and `version`

## 0.1.2 Highlights

- CLI output can now be filtered with `--min-risk low|medium|high`.
- Maintainers can focus large dependency diffs on high-risk changes first.

## Usage

```bash
python -m lockfile_change_explainer --old old-package-lock.json --new package-lock.json --format package-lock
python -m lockfile_change_explainer --old old.txt --new new.txt --format requirements --json
python -m lockfile_change_explainer --old old.txt --new new.txt --format requirements --min-risk high
python -m lockfile_change_explainer --old old.txt --new new.txt --format requirements --json --fail-on-risk high
python -m lockfile_change_explainer --old old.txt --new new.txt --max-input-bytes 5242880
```

Risk hints are heuristic. Major version changes are marked high risk, minor changes medium risk, patch changes low risk, and added or removed dependencies medium risk.

## Bounded input and CI checks

Each input file is checked with `stat` before reading and is read with a `max+1` byte guard to catch growth during the read. The default limit is 10 MiB per file and can be changed with `--max-input-bytes`. The library API applies the same default to in-memory text.

JSON and TOML inputs are also limited to 64 nesting levels and 100,000 structure items. These limits are deliberately conservative for hostile lockfiles and are applied before the full JSON/TOML object is constructed where possible.

Repeated package names are reported in text output under `Security findings`. They are included in JSON as `duplicate_package_names`, `duplicate_package_names_by_file`, and `risk_summary`. A duplicate name is treated as high risk because different entries can make dependency resolution ambiguous. For CI, `--fail-on-risk low|medium|high` exits with status 1 when the unfiltered comparison reaches that threshold; output is still written before the exit.

The JSON report keeps the existing `added`, `removed`, `changed`, `unchanged_count`, and `lockfile_format` fields and adds this stable summary shape:

```json
{
  "duplicate_package_names": [],
  "duplicate_package_names_by_file": {"old": [], "new": []},
  "risk_summary": {
    "max_risk": "none",
    "counts": {"low": 0, "medium": 0, "high": 0},
    "has_findings": false,
    "has_duplicates": false,
    "duplicate_package_count": 0,
    "duplicate_package_names": [],
    "duplicate_package_names_by_file": {"old": [], "new": []},
    "fail_on_risk": "high",
    "threshold_exceeded": false
  }
}
```

The security review for this change is in [`docs/SECURITY_REVIEW_2026-07.md`](docs/SECURITY_REVIEW_2026-07.md).

## Development

```bash
python -m unittest discover -s tests
```
