"""不依赖 OBS 和图形界面的单元测试：python -m unittest discover -s tests"""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from obs_overlay.config import DEFAULTS, ConfigError, ConfigStore, validate
from obs_overlay.file_monitor import find_latest_recording, read_size
from obs_overlay.formatting import format_bytes, format_duration, format_skipped
from obs_overlay.obs_client import SkipCounters, SkipTracker
from obs_overlay.overlay import color_key_for
from obs_overlay.web_ui import _merge_password, _public_config


class FormattingTest(unittest.TestCase):
    def test_bytes(self):
        self.assertEqual(format_bytes(0), "0 B")
        self.assertEqual(format_bytes(1023), "1023 B")
        self.assertEqual(format_bytes(1536), "1.50 KB")
        self.assertEqual(format_bytes(5 * 1024**3), "5.00 GB")

    def test_duration(self):
        self.assertEqual(format_duration(0), "00:00:00")
        self.assertEqual(format_duration(3_723_999), "01:02:03")
        self.assertEqual(format_duration(100 * 3600 * 1000), "100:00:00")

    def test_skipped(self):
        self.assertEqual(format_skipped(0, 0), "0")
        self.assertEqual(format_skipped(12, 6000), "12 (0.20%)")


class ConfigTest(unittest.TestCase):
    def test_defaults_are_valid(self):
        self.assertEqual(validate({}), DEFAULTS)

    def test_rejects_bad_values(self):
        for bad in (
            {"recording": {"scan_interval_sec": 5}},
            {"overlay": {"text_opacity": 2}},
            {"overlay": {"text_opacity": 0}},
            {"overlay": {"bg_opacity": -0.1}},
            {"overlay": {"shadow_color": "#12345"}},
            {"overlay": {"position": "middle"}},
            {"overlay": {"bg_color": "red"}},
            {"obs": {"port": "abc"}},
        ):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                validate(bad)

    def test_background_off_by_default(self):
        cfg = validate({})
        self.assertEqual(cfg["overlay"]["bg_opacity"], 0.0)
        self.assertEqual(cfg["recording"]["size_source"], "directory")

    def test_normalizes_extensions(self):
        cfg = validate({"recording": {"extensions": "mkv, .MP4"}})
        self.assertEqual(cfg["recording"]["extensions"], [".mkv", ".mp4"])

    def test_store_roundtrip_and_listener(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            store = ConfigStore(path)
            self.assertTrue(path.exists())
            seen = []
            store.subscribe(lambda old, new: seen.append((old["obs"]["port"], new["obs"]["port"])))
            cfg = store.get()
            cfg["obs"]["port"] = 4460
            store.update(cfg)
            self.assertEqual(seen, [(4455, 4460)])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["obs"]["port"], 4460)
            self.assertEqual(ConfigStore(path).get()["obs"]["port"], 4460)


class ColorKeyTest(unittest.TestCase):
    def test_key_differs_from_shadow_by_one(self):
        self.assertEqual(color_key_for("#202020"), "#202021")
        self.assertEqual(color_key_for("#0000FF"), "#0000fe")


class PasswordHandlingTest(unittest.TestCase):
    def test_public_config_hides_password(self):
        cfg = validate({"obs": {"password": "secret"}})
        public = _public_config(cfg)
        self.assertIsNone(public["obs"]["password"])
        self.assertTrue(public["obs"]["password_set"])

    def test_merge_keeps_or_replaces(self):
        stored = {"password": "old"}
        self.assertEqual(_merge_password({"password": None, "password_set": True}, stored)["password"], "old")
        self.assertEqual(_merge_password({"password": ""}, stored)["password"], "")
        self.assertEqual(_merge_password({"password": "new"}, stored)["password"], "new")


class SkipTrackerTest(unittest.TestCase):
    def c(self, enc, enc_total, ren=0, ren_total=0):
        return SkipCounters(enc, enc_total, ren, ren_total)

    def test_delta_since_recording_start(self):
        t = SkipTracker()
        t.update(False, self.c(100, 10_000), 0)
        self.assertEqual(t.update(True, self.c(100, 10_000), 0), self.c(0, 0))
        self.assertFalse(t.partial)
        self.assertEqual(t.update(True, self.c(105, 10_600), 10_000), self.c(5, 600))

    def test_keeps_last_value_after_stop_and_resets_on_next_start(self):
        t = SkipTracker()
        t.update(True, self.c(0, 0), 0)
        t.update(True, self.c(3, 300), 5000)
        self.assertEqual(t.update(False, self.c(3, 300), 0), self.c(3, 300))
        self.assertEqual(t.update(True, self.c(3, 300), 0), self.c(0, 0))

    def test_partial_when_attached_mid_recording(self):
        t = SkipTracker()
        t.update(True, self.c(50, 5000), 60_000)
        self.assertTrue(t.partial)

    def test_counter_reset_rebases(self):
        t = SkipTracker()
        t.update(True, self.c(50, 5000), 0)
        self.assertEqual(t.update(True, self.c(1, 10), 1000), self.c(0, 0))
        self.assertEqual(t.update(True, self.c(2, 20), 2000), self.c(1, 10))


class FileMonitorTest(unittest.TestCase):
    def test_finds_newest_matching_file(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "old.mkv").write_bytes(b"x" * 10)
            time.sleep(0.05)
            (d / "notes.txt").write_bytes(b"ignored")
            (d / "new.MKV").write_bytes(b"x" * 20)
            found = find_latest_recording(d, [".mkv"], since=None)
            self.assertEqual(found.name, "new.MKV")
            self.assertEqual(read_size(found), 20)

    def test_since_excludes_old_recordings(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "old.mkv").write_bytes(b"x")
            self.assertIsNone(find_latest_recording(d, [".mkv"], since=time.time() + 3600))

    def test_size_of_file_open_for_writing(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "rec.mkv"
            with open(path, "wb") as f:
                f.write(b"x" * 4096)
                f.flush()
                os.fsync(f.fileno())
                self.assertEqual(read_size(path), 4096)

    def test_missing_directory_raises(self):
        with self.assertRaises(FileNotFoundError):
            find_latest_recording(Path("Z:/definitely/missing"), [".mkv"], None)


if __name__ == "__main__":
    unittest.main()
