# Changelog

All notable changes to `lockfile-change-explainer` will be documented in this file.

The format is based on Keep a Changelog, and this project uses semantic versioning.

## [Unreleased]

- No unreleased changes yet.

## [0.1.2] - 2026-07-06

- Updated GitHub Actions workflow dependencies to current major versions.
- Modernized package license metadata to avoid current Setuptools deprecation warnings.
- Added `--min-risk` filtering for text and JSON reports.
- Exposed `filter_by_min_risk` for library users building dependency review workflows.

## [0.1.1] - 2026-06-17

- Classified semantic-version downgrades as high risk before normal major/minor/patch handling.
- Added a downgrade-specific risk hint.
- Fixed GitHub Actions workflow pins to supported action versions.

## [0.1.0] - 2026-06-03

- Initial open-source release with CLI, examples, tests, GitHub workflows, security policy, and contributor docs.
