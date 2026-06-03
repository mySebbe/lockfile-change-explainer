import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lockfile_change_explainer import compare_lockfiles, format_text_report, main


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
                    "--output",
                    str(out_path),
                ]
            )

            self.assertEqual(0, code)
            payload = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual("a", payload["changed"][0]["name"])
            self.assertEqual("high", payload["changed"][0]["risk"])


if __name__ == "__main__":
    unittest.main()
