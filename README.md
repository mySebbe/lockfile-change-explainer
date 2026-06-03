# lockfile-change-explainer

`lockfile-change-explainer` is a stdlib-first Python CLI that compares two lockfiles and explains dependency additions, removals, and version changes.

Supported v0.1 inputs:

- `package-lock.json` using `packages` or top-level `dependencies`
- requirements-style files such as `requirements.txt`
- simple pylock/TOML files with package entries containing `name` and `version`

## Usage

```bash
python -m lockfile_change_explainer --old old-package-lock.json --new package-lock.json --format package-lock
python -m lockfile_change_explainer --old old.txt --new new.txt --format requirements --json
```

Risk hints are heuristic. Major version changes are marked high risk, minor changes medium risk, patch changes low risk, and added or removed dependencies medium risk.

## Development

```bash
python -m unittest discover -s tests
```
