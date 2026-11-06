"""Focused tests for read-only creator note detail extraction."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


VENDOR_SCRIPTS = Path(__file__).resolve().parent / "vendor" / "xiaohongshu-skills" / "scripts"
sys.path.insert(0, str(VENDOR_SCRIPTS))
from xhs import creator  # noqa: E402


class FakePage:
    def __init__(self, manager_notes, details):
        self.manager_notes = manager_notes
        self.details = details
        self.current_id = ""
        self.navigations = []

    def navigate(self, url):
        self.navigations.append(url)
        self.current_id = url.split("id=", 1)[1].split("&", 1)[0] if "id=" in url else ""

    def wait_for_load(self, timeout=0):
        return None

    def evaluate(self, expression):
        if expression == creator._EXTRACT_NOTES_JS:
            return json.dumps({"count": len(self.manager_notes), "notes": self.manager_notes})
        if expression == creator._EXTRACT_UPDATE_NOTE_JS:
            value = self.details[self.current_id]
            if isinstance(value, Exception):
                raise value
            return value
        return None


class CreatorProfileTests(unittest.TestCase):
    def test_recent_published_notes_are_enriched_and_failures_are_explicit(self):
        notes = [
            {"note_id": "a" * 24, "title": "old pinned", "time": "2026-01-01 12:00", "scheduled": False},
            {"note_id": "b" * 24, "title": "scheduled", "time": "2026-11-07 12:00", "scheduled": True},
            {"note_id": "c" * 24, "title": "new", "time": "2026-11-06 12:00", "scheduled": False},
            {"note_id": "d" * 24, "title": "failed", "time": "2026-11-05 12:00", "scheduled": False},
        ]
        details = {
            "a" * 24: {"ready": True, "title": "old pinned", "content": "old body", "tags": ["old"]},
            "c" * 24: {"ready": True, "title": "new", "content": "real body", "tags": ["topic"]},
            "d" * 24: RuntimeError("detail unavailable"),
        }
        page = FakePage(notes, details)
        with patch.object(creator.time, "sleep"):
            result = creator.get_creator_notes(page, max_count=3, scroll_rounds=0)
        self.assertTrue(result["success"])
        self.assertEqual([note["note_id"] for note in result["notes"]], ["c" * 24, "d" * 24, "a" * 24])
        self.assertEqual(result["notes"][0]["content"], "real body")
        self.assertEqual(result["notes"][0]["tags"], ["topic"])
        self.assertEqual(result["notes"][1]["detail_status"], "failed")
        self.assertIn("detail unavailable", result["notes"][1]["detail_error"])
        self.assertNotIn("b" * 24, [note["note_id"] for note in result["notes"]])
        self.assertEqual(page.navigations[-1], creator.NOTE_MANAGER_URL)

    def test_invalid_note_id_never_navigates_to_update_page(self):
        page = FakePage([], {})
        with self.assertRaisesRegex(ValueError, "笔记 ID"):
            creator._read_note_detail(page, {"note_id": "../../bad"})
        self.assertEqual(page.navigations, [])


if __name__ == "__main__":
    unittest.main()
