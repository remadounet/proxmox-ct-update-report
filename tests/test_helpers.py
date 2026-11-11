"""Unit tests for the pure helper functions of check-ct-updates.py."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "check_ct_updates",
    Path(__file__).resolve().parent.parent / "check-ct-updates.py",
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["check_ct_updates"] = _mod
_spec.loader.exec_module(_mod)

from check_ct_updates import (  # noqa: E402,F401
    filter_container_ids,
    parse_apk_updates,
    parse_apt_updates,
    parse_ct_selection,
    parse_dnf_query_output,
    remove_auto_report_block,
    replace_auto_report_block,
    split_apk_name_version,
)


class TestParseAptUpdates(unittest.TestCase):
    def test_basic(self):
        output = (
            "Listing...\n"
            "openssl/stable 3.0.11-1 amd64 [upgradable from: 3.0.10-1]\n"
            "curl/stable 8.5.0-2 amd64 [upgradable from: 8.5.0-1]\n"
        )
        packages = parse_apt_updates(output)
        self.assertEqual(
            packages,
            [
                {"package": "openssl", "candidate": "3.0.11-1"},
                {"package": "curl", "candidate": "8.5.0-2"},
            ],
        )

    def test_empty(self):
        self.assertEqual(parse_apt_updates("Listing...\n"), [])


class TestParseDnfQueryOutput(unittest.TestCase):
    def test_normal_lines(self):
        output = "openssl.x86_64|1:3.0.11-1.fc39\ncurl.x86_64|8.5.0-2.fc39\n"
        packages = parse_dnf_query_output(output)
        self.assertEqual(len(packages), 2)
        self.assertIn({"package": "openssl.x86_64", "candidate": "1:3.0.11-1.fc39"}, packages)

    def test_dnf5_escaped_newlines(self):
        output = "openssl.x86_64|1:3.0.11\\ncurl.x86_64|8.5.0\\n"
        packages = parse_dnf_query_output(output)
        self.assertEqual(len(packages), 2)

    def test_deduplication(self):
        output = "a.x86_64|1.0\na.x86_64|1.0\n"
        self.assertEqual(len(parse_dnf_query_output(output)), 1)


class TestApkHelpers(unittest.TestCase):
    def test_split_name_version(self):
        self.assertEqual(split_apk_name_version("openssl-3.3.1-r0"), ("openssl", "3.3.1-r0"))
        self.assertEqual(split_apk_name_version("py3-requests-2.31.0-r1"), ("py3-requests", "2.31.0-r1"))
        self.assertIsNone(split_apk_name_version("nonsense"))
        self.assertIsNone(split_apk_name_version(""))

    def test_parse_apk_updates(self):
        output = (
            "WARNING: opening /etc/apk/repositories\n"
            "openssl-3.3.1-r0 x86_64 {main} [upgradable from: openssl-3.3.0-r0]\n"
            "py3-requests-2.31.0-r1 x86_64 {main} [upgradable from: py3-requests-2.31.0-r0]\n"
        )
        packages = parse_apk_updates(output)
        self.assertEqual(len(packages), 2)
        self.assertEqual(packages[0]["package"], "openssl")
        self.assertEqual(packages[0]["candidate"], "3.3.1-r0")
        self.assertEqual(packages[0]["installed"], "3.3.0-r0")
        self.assertEqual(packages[1]["package"], "py3-requests")

    def test_parse_apk_updates_empty(self):
        self.assertEqual(parse_apk_updates("OK: 100 distinct packages available\n"), [])


class TestContainerSelection(unittest.TestCase):
    def test_parse_selection(self):
        self.assertEqual(parse_ct_selection("100,105-110,200"), {100, 105, 106, 107, 108, 109, 110, 200})

    def test_parse_selection_invalid(self):
        with self.assertRaises(ValueError):
            parse_ct_selection("110-105")
        with self.assertRaises(ValueError):
            parse_ct_selection("abc")

    def test_filter(self):
        ids = [100, 101, 102, 103]
        self.assertEqual(filter_container_ids(ids, "100,102", None), [100, 102])
        self.assertEqual(filter_container_ids(ids, None, "101,103"), [100, 102])
        self.assertEqual(filter_container_ids(ids, "100,999", None), [100])


class TestReportMarkers(unittest.TestCase):
    def test_remove_keeps_personal_notes(self):
        description = "My notes\n\n<!-- BEGIN AUTO UPDATE REPORT -->\nreport\n<!-- END AUTO UPDATE REPORT -->"
        self.assertEqual(remove_auto_report_block(description), "My notes")

    def test_legacy_markers(self):
        description = "Notes\n\n--- BEGIN AUTO UPDATE REPORT ---\nreport\n--- END AUTO UPDATE REPORT ---"
        self.assertEqual(remove_auto_report_block(description), "Notes")

    def test_replace_preserves_notes(self):
        description = "My notes\n\n<!-- BEGIN AUTO UPDATE REPORT -->\nold\n<!-- END AUTO UPDATE REPORT -->"
        new = replace_auto_report_block(description, "NEW")
        self.assertTrue(new.startswith("My notes"))
        self.assertIn("NEW", new)
        self.assertNotIn("old", new)


if __name__ == "__main__":
    unittest.main()
