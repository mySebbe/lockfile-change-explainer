import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lockfile_change_explainer import (
    MAX_JSON_DEPTH,
    MAX_TOML_DEPTH,
    InputLimitError,
    compare_lockfiles,
    filter_by_min_risk,
    format_text_report,
    main,
)


class LockfileChangeExplainerTests(unittest.TestCase):
    def test_compares_package_lock_added_removed_changed_with_risk_hints(self):
        old = json.dumps(
            {
                "packages": {
                    "node_modules/left-pad": {"version": "1.1.0"},
                    "node_modules/lodash": {"version": "4.17.20"},
                    "node_modules/old-only": {"version": "0.1.0"},
                }
            }
        )
        new = json.dumps(
            {
                "packages": {
                    "node_modules/left-pad": {"version": "2.0.0"},
                    "node_modules/lodash": {"version": "4.17.20"},
                    "node_modules/new-only": {"version": "0.2.0"},
                }
            }
        )

        result = compare_lockfiles(old, new, lockfile_format="package-lock")

        self.assertEqual(["new-only"], [item.name for item in result.added])
        self.assertEqual(["old-only"], [item.name for item in result.removed])
        self.assertEqual("left-pad", result.changed[0].name)
        self.assertEqual("1.1.0", result.changed[0].old_version)
        self.assertEqual("2.0.0", result.changed[0].new_version)
        self.assertEqual("high", result.changed[0].risk)

    def test_parses_requirements_style_comments_and_extras(self):
        old = "requests[security]==2.31.0\n# ignored\nurllib3==1.26.18\n"
        new = "requests[security]==2.32.0\nrich==13.7.1\n"

        result = compare_lockfiles(old, new, lockfile_format="requirements")

        self.assertEqual(["rich"], [item.name for item in result.added])
        self.assertEqual(["urllib3"], [item.name for item in result.removed])
        self.assertEqual("requests", result.changed[0].name)
        self.assertEqual("medium", result.changed[0].risk)

    def test_text_report_contains_sections_and_hints(self):
        result = compare_lockfiles("a==1.0.0\n", "a==1.0.1\nb==1.0.0\n", lockfile_format="requirements")

        report = format_text_report(result)

        self.assertIn("Added", report)
        self.assertIn("b 1.0.0", report)
        self.assertIn("Changed", report)
        self.assertIn("a 1.0.0 -> 1.0.1", report)
        self.assertIn("low", report)

    def test_downgrade_is_high_risk_with_specific_hint(self):
        result = compare_lockfiles("a==1.4.0\n", "a==1.3.9\n", lockfile_format="requirements")

        self.assertEqual("high", result.changed[0].risk)
        self.assertIn("downgraded", result.changed[0].hint)

    def test_filter_by_min_risk_keeps_only_high_risk_changes(self):
        result = compare_lockfiles(
            "major==1.0.0\nminor==1.0.0\npatch==1.0.0\n",
            "major==2.0.0\nminor==1.1.0\npatch==1.0.1\n",
            lockfile_format="requirements",
        )

        filtered = filter_by_min_risk(result, "high")

        self.assertEqual(["major"], [item.name for item in filtered.changed])

    def test_cli_emits_json_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_path = Path(tmp) / "old.txt"
            new_path = Path(tmp) / "new.txt"
            out_path = Path(tmp) / "out.json"
            old_path.write_text("a==1.0.0\n", encoding="utf-8")
            new_path.write_text("a==2.0.0\n", encoding="utf-8")

            code = main(
                [
                    "--old",
                    str(old_path),
                    "--new",
                    str(new_path),
                    "--format",
                    "requirements",
                    "--json",
                    "--min-risk",
                    "high",
                    "--output",
                    str(out_path),
                ]
            )

            self.assertEqual(0, code)
            payload = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual("a", payload["changed"][0]["name"])
            self.assertEqual("high", payload["changed"][0]["risk"])

    def test_duplicate_requirements_are_reported_without_changing_last_value_behavior(self):
        result = compare_lockfiles(
            "Foo==1.0.0\nfoo==1.1.0\n",
            "foo==1.1.0\n",
            lockfile_format="requirements",
        )

        self.assertEqual(["foo"], result.duplicate_package_names)
        self.assertEqual(1, result.unchanged_count)
        self.assertIn("Duplicate package names", format_text_report(result))
        self.assertEqual(["foo"], result.duplicate_package_names_by_file["old"])

    def test_package_lock_duplicates_across_package_maps_are_reported(self):
        lockfile = json.dumps(
            {
                "packages": {"node_modules/foo": {"version": "1.0.0"}},
                "dependencies": {"foo": {"version": "1.0.0"}},
            }
        )

        result = compare_lockfiles(lockfile, lockfile, lockfile_format="package-lock")

        self.assertEqual(["foo"], result.duplicate_package_names)
        self.assertEqual(["foo"], result.duplicate_package_names_by_file["old"])
        self.assertEqual(["foo"], result.duplicate_package_names_by_file["new"])

    def test_json_report_surfaces_duplicate_package_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_path = Path(tmp) / "old.txt"
            new_path = Path(tmp) / "new.txt"
            out_path = Path(tmp) / "out.json"
            old_path.write_text("Foo==1.0.0\nfoo==1.1.0\n", encoding="utf-8")
            new_path.write_text("foo==1.1.0\n", encoding="utf-8")

            code = main(
                [
                    "--old",
                    str(old_path),
                    "--new",
                    str(new_path),
                    "--format",
                    "requirements",
                    "--json",
                    "--output",
                    str(out_path),
                ]
            )

            payload = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(0, code)
            self.assertEqual(["foo"], payload["duplicate_package_names"])
            self.assertEqual(["foo"], payload["risk_summary"]["duplicate_package_names"])
            self.assertTrue(payload["risk_summary"]["has_duplicates"])

    def test_pylock_duplicate_names_are_normalized_and_reported(self):
        lockfile = (
            '[[packages]]\nname = "Foo-Bar"\nversion = "1.0.0"\n\n'
            '[[packages]]\nname = "foo_bar"\nversion = "1.1.0"\n'
        )

        result = compare_lockfiles(lockfile, lockfile, lockfile_format="pylock")

        self.assertEqual(["foo-bar"], result.duplicate_package_names)

    def test_cli_rejects_oversized_old_and_new_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_path = Path(tmp) / "old.txt"
            new_path = Path(tmp) / "new.txt"
            old_path.write_text("a==1.0.0\n", encoding="utf-8")
            new_path.write_text("a==1.0.0\n", encoding="utf-8")

            old_path.write_bytes(b"123456")
            old_error = io.StringIO()
            with redirect_stderr(old_error):
                old_code = main(
                    [
                        "--old",
                        str(old_path),
                        "--new",
                        str(new_path),
                        "--format",
                        "requirements",
                        "--max-input-bytes",
                        "5",
                    ]
                )
            self.assertEqual(2, old_code)
            self.assertIn("old.txt", old_error.getvalue())

            old_path.write_text("a==1", encoding="utf-8")
            new_path.write_bytes(b"123456")
            new_error = io.StringIO()
            with redirect_stderr(new_error):
                new_code = main(
                    [
                        "--old",
                        str(old_path),
                        "--new",
                        str(new_path),
                        "--format",
                        "requirements",
                        "--max-input-bytes",
                        "5",
                    ]
                )
            self.assertEqual(2, new_code)
            self.assertIn("new.txt", new_error.getvalue())

    def test_json_and_toml_nesting_limits_reject_hostile_input(self):
        json_nested = "[" * (MAX_JSON_DEPTH + 1) + "0" + "]" * (MAX_JSON_DEPTH + 1)
        toml_nested = "packages = " + "[" * (MAX_TOML_DEPTH + 1) + "0" + "]" * (MAX_TOML_DEPTH + 1)

        with self.assertRaises(InputLimitError):
            compare_lockfiles("{}", json_nested, lockfile_format="package-lock")
        with self.assertRaises(InputLimitError):
            compare_lockfiles("packages = []", toml_nested, lockfile_format="pylock")

    def test_structure_item_limit_is_enforced_before_json_parse(self):
        with patch("lockfile_change_explainer.MAX_STRUCTURE_ITEMS", 2):
            with self.assertRaises(InputLimitError):
                compare_lockfiles("{}", '{"a": 1, "b": 2, "c": 3}', lockfile_format="package-lock")

    def test_cli_risk_summary_and_fail_on_risk_are_machine_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_path = Path(tmp) / "old.txt"
            new_path = Path(tmp) / "new.txt"
            out_path = Path(tmp) / "out.json"
            old_path.write_text("a==1.0.0\n", encoding="utf-8")
            new_path.write_text("a==2.0.0\n", encoding="utf-8")

            code = main(
                [
                    "--old",
                    str(old_path),
                    "--new",
                    str(new_path),
                    "--format",
                    "requirements",
                    "--json",
                    "--fail-on-risk",
                    "high",
                    "--output",
                    str(out_path),
                ]
            )

            payload = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(1, code)
            self.assertEqual("high", payload["risk_summary"]["max_risk"])
            self.assertEqual(1, payload["risk_summary"]["counts"]["high"])
            self.assertTrue(payload["risk_summary"]["threshold_exceeded"])


if __name__ == "__main__":
    unittest.main()
