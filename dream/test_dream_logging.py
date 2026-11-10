"""Offline tests for log placement, rotation, and retention."""

from datetime import datetime, timedelta
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import dream_logging


class DreamLoggingTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.temp)
        self.source = self.root / "dream"
        self.source.mkdir()
        self.enterContext(patch.object(
            dream_logging, "__file__", str(self.source / "dream_logging.py")
        ))

    def handler(self, path, **kwargs):
        handler = dream_logging.DreamFileHandler(str(path), **kwargs)
        self.addCleanup(handler.close)
        return handler

    def test_legacy_absolute_path_redirects_outside_source(self):
        self.assertEqual(
            dream_logging._resolve_log_path(str(self.source / "dream.log")),
            str(self.root / "logs" / "dream" / "dream.log"),
        )

    def test_legacy_relative_path_redirects_outside_source(self):
        relative = os.path.relpath(self.source / "dream.log")
        self.assertEqual(
            dream_logging._resolve_log_path(relative),
            str(self.root / "logs" / "dream" / "dream.log"),
        )

    def test_custom_path_is_respected(self):
        path = self.root / "custom" / "dream.log"
        self.assertEqual(dream_logging._resolve_log_path(str(path)), str(path))

    def test_output_directory_is_created_and_size_rolls_over(self):
        handler = self.handler(self.root / "logs" / "dream" / "dream.log",
                               maxBytes=1)
        record = logging.makeLogRecord({"msg": "message"})
        handler.emit(record)
        initial = handler.baseFilename
        handler.emit(record)
        self.assertNotEqual(handler.baseFilename, initial)
        self.assertEqual(Path(initial).read_text(encoding="utf-8"), "message\n")
        self.assertEqual(Path(handler.baseFilename).read_text(encoding="utf-8"),
                         "message\n")

    def test_date_rollover(self):
        handler = self.handler(self.root / "dream.log")
        handler._today = "2000-01-01"
        self.assertTrue(handler.shouldRollover(logging.makeLogRecord({})))
        handler.doRollover()
        self.assertIn(datetime.now().strftime("%Y-%m-%d"), handler.baseFilename)
        self.assertTrue(handler.baseFilename.endswith(".0.log"))

    def test_startup_purges_only_expired_matching_logs(self):
        old_date = (datetime.now() - timedelta(days=31)).strftime("%Y-%m-%d")
        recent_date = datetime.now().strftime("%Y-%m-%d")
        old = self.root / f"dream.{old_date}.0.log"
        recent = self.root / f"dream.{recent_date}.0.log"
        unrelated = self.root / f"other.{old_date}.0.log"
        for path in (old, recent, unrelated):
            path.touch()
        handler = self.handler(self.root / "dream.log", keepDays=30)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(unrelated.exists())
        handler.emit(logging.makeLogRecord({"msg": "appended"}))
        self.assertEqual(recent.read_text(encoding="utf-8"), "appended\n")

    def test_retention_can_be_disabled(self):
        old = self.root / "dream.2000-01-01.0.log"
        old.touch()
        self.handler(self.root / "dream.log", keepDays=0)
        self.assertTrue(old.exists())

    def test_cleanup_failure_is_reported(self):
        old = self.root / "dream.2000-01-01.0.log"
        old.touch()
        with (
            patch.object(dream_logging.os, "remove",
                         side_effect=PermissionError("locked")),
            self.assertLogs("dream", level="WARNING") as captured,
        ):
            self.handler(self.root / "dream.log")
        self.assertIn("Could not remove expired dream log", captured.output[0])


if __name__ == "__main__":
    unittest.main()
