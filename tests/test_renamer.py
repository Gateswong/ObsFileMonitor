"""录制后重命名的单元测试：python -m unittest discover -s tests"""

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from obs_overlay import renamer
from obs_overlay.renamer import (
    RenameJob,
    RenameWorker,
    SceneTracker,
    build_new_stem,
    parse_window_title,
    pick_app_title,
    run_job,
    unique_path,
)

PROFILE = "录制 4K60 单声道"


def item(name, kind, index, enabled=True, group=False):
    return {"sourceName": name, "inputKind": kind, "sceneItemIndex": index,
            "sceneItemEnabled": enabled, "isGroup": group}


class ParseWindowTitleTest(unittest.TestCase):
    def test_real_obs_values(self):
        self.assertEqual(
            parse_window_title("Don't Starve Together:opengles2.0:dontstarve_steam_x64.exe"),
            "Don't Starve Together",
        )
        self.assertEqual(parse_window_title("World of Warcraft:waApplication Window:Wow.exe"), "World of Warcraft")
        self.assertEqual(parse_window_title("MortalShell2  :UnrealWindow:MortalShell2-Win64-Shipping.exe"), "MortalShell2")

    def test_escaped_characters(self):
        self.assertEqual(parse_window_title("Half-Life 2#3A Episode Two:Valve001:hl2.exe"), "Half-Life 2: Episode Two")
        self.assertEqual(parse_window_title("C#22 Editor:Cls:a.exe"), "C# Editor")

    def test_empty(self):
        for value in (None, "", "   :Cls:a.exe"):
            self.assertIsNone(parse_window_title(value))


class PickAppTitleTest(unittest.TestCase):
    def setUp(self):
        self.settings = {
            "游戏": {"capture_mode": "window", "window": "Don't Starve Together:opengles2.0:dontstarve_steam_x64.exe"},
            "游戏2": {"capture_mode": "window", "window": "Project Zomboid:GLFW30:ProjectZomboid64.exe"},
            "全屏游戏": {},
            "窗口": {"window": "Notepad:Notepad:notepad.exe"},
            "显示器": {"monitor_id": "x"},
        }

    def pick(self, items, groups=None):
        return pick_app_title(items, lambda n: self.settings.get(n, {}), (groups or {}).get)

    def test_game_capture_first(self):
        items = [item("显示器", "monitor_capture", 0), item("窗口", "window_capture", 1), item("游戏", "game_capture", 2)]
        self.assertEqual(self.pick(items), "Don't Starve Together")

    def test_topmost_game_capture_wins(self):
        items = [item("游戏", "game_capture", 0), item("游戏2", "game_capture", 1)]
        self.assertEqual(self.pick(items), "Project Zomboid")

    def test_falls_back_to_window_then_display(self):
        self.assertEqual(self.pick([item("窗口", "window_capture", 0), item("显示器", "monitor_capture", 1)]), "Notepad")
        self.assertEqual(self.pick([item("显示器", "monitor_capture", 0)]), "Display Capture")
        self.assertIsNone(self.pick([item("麦克风", "wasapi_input_capture", 0)]))

    def test_game_capture_without_window_is_skipped(self):
        items = [item("全屏游戏", "game_capture", 1), item("窗口", "window_capture", 0)]
        self.assertEqual(self.pick(items), "Notepad")

    def test_disabled_sources_are_ignored(self):
        items = [item("游戏", "game_capture", 1, enabled=False), item("显示器", "monitor_capture", 0)]
        self.assertEqual(self.pick(items), "Display Capture")

    def test_groups_are_expanded(self):
        items = [item("分组", None, 0, group=True)]
        groups = {"分组": [item("游戏2", "game_capture", 0)]}
        self.assertEqual(self.pick(items, groups), "Project Zomboid")


class BuildNewStemTest(unittest.TestCase):
    def test_full(self):
        self.assertEqual(
            build_new_stem("2026-10-21 20-00-00", "Don't Starve Together", PROFILE),
            "2026-10-21 20-00-00 Don't Starve Together [录制 4K60 单声道]",
        )

    def test_missing_parts_have_no_double_spaces(self):
        self.assertEqual(build_new_stem("2026-10-21 20-00-00", None, PROFILE), "2026-10-21 20-00-00 [录制 4K60 单声道]")
        self.assertEqual(build_new_stem("a", "  Title  with   spaces ", None), "a Title with spaces")
        self.assertEqual(build_new_stem("a", "", ""), "a")

    def test_forbidden_characters_replaced(self):
        self.assertEqual(build_new_stem("a", 'Half-Life 2: "Ep2"?', "x/y"), "a Half-Life 2： ＂Ep2＂？ [x／y]")


class SceneTrackerTest(unittest.TestCase):
    def test_longest(self):
        t = SceneTracker()
        self.assertIsNone(t.longest())
        t.tick("A", 10)
        t.tick("B", 3)
        t.tick("A", 1)
        t.tick("B", 9)
        t.tick(None, 100)
        self.assertEqual(t.longest(), "B")
        t.start()
        self.assertIsNone(t.longest())


class RunJobTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def make(self, name: str) -> float:
        path = self.dir / name
        path.write_bytes(b"x")
        return path.stat().st_birthtime

    def job(self, started_at, stopped_at, title="Don't Starve Together", profile=PROFILE):
        return RenameJob(self.dir, [".mkv", ".mp4"], started_at, stopped_at, profile, title, due=0)

    def names(self):
        return sorted(p.name for p in self.dir.iterdir())

    def test_renames_recording(self):
        t = self.make("2026-10-21 20-00-00.mp4")
        result = run_job(self.job(t, t))
        self.assertFalse(result.retry)
        self.assertEqual(self.names(), ["2026-10-21 20-00-00 Don't Starve Together [录制 4K60 单声道].mp4"])

    def test_ignores_files_outside_recording_window(self):
        old = self.make("old.mp4")
        time.sleep(0.2)
        cur = self.make("cur.mp4")
        time.sleep(0.2)
        nxt = self.make("next.mp4")  # 下一次录制在延迟期间开始
        # 查找窗口是 [started_at - 10, stopped_at + 5]，构造成只覆盖 cur 前后 0.05 秒
        job = self.job(
            started_at=cur + renamer.CREATED_BEFORE_START_SLACK_SEC - 0.05,
            stopped_at=cur - renamer.CREATED_AFTER_STOP_SLACK_SEC + 0.05,
        )
        self.assertLess(old, cur - 0.05)
        self.assertGreater(nxt, cur + 0.05)
        run_job(job)
        self.assertEqual(self.names(), ["cur Don't Starve Together [录制 4K60 单声道].mp4", "next.mp4", "old.mp4"])

    def test_split_segments_and_remux_copy(self):
        a = self.make("seg1.mkv")
        self.make("seg2.mkv")
        time.sleep(0.2)
        job = self.job(a, a - renamer.CREATED_AFTER_STOP_SLACK_SEC + 0.1)
        self.make("seg2.mp4")  # 停止录制后才生成的 remux 副本
        run_job(job)
        self.assertEqual(self.names(), [
            "seg1 Don't Starve Together [录制 4K60 单声道].mkv",
            "seg2 Don't Starve Together [录制 4K60 单声道].mkv",
            "seg2 Don't Starve Together [录制 4K60 单声道].mp4",
        ])

    def test_never_overwrites(self):
        t = self.make("rec.mp4")
        (self.dir / "rec [P].mp4").write_bytes(b"existing")
        run_job(self.job(t, t, title=None, profile="P"))
        self.assertEqual(self.names(), ["rec [P] (2).mp4", "rec [P].mp4"])
        self.assertEqual((self.dir / "rec [P].mp4").read_bytes(), b"existing")

    def test_locked_file_retries_then_succeeds(self):
        t = self.make("rec.mp4")
        job = self.job(t, t)
        with open(self.dir / "rec.mp4", "rb"):
            result = run_job(job)
        self.assertTrue(result.retry)
        self.assertEqual(self.names(), ["rec.mp4"])
        result = run_job(job)
        self.assertFalse(result.retry)
        self.assertEqual(self.names(), ["rec Don't Starve Together [录制 4K60 单声道].mp4"])
        # 再执行一次也不会重复追加
        run_job(job)
        self.assertEqual(self.names(), ["rec Don't Starve Together [录制 4K60 单声道].mp4"])

    def test_no_file_found(self):
        result = run_job(self.job(time.time() + 3600, time.time() + 3600))
        self.assertFalse(result.retry)
        self.assertIn("未", result.message)

    def test_unique_path(self):
        p = self.dir / "a.mp4"
        self.assertEqual(unique_path(p), p)
        p.write_bytes(b"")
        (self.dir / "a (2).mp4").write_bytes(b"")
        self.assertEqual(unique_path(p).name, "a (3).mp4")


class RenameWorkerTest(unittest.TestCase):
    def test_runs_due_job(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "rec.mkv"
            path.write_bytes(b"x")
            t = path.stat().st_birthtime
            done = threading.Event()
            messages = []
            worker = RenameWorker(on_result=lambda m: (messages.append(m), done.set()))
            worker.start()
            worker.submit(RenameJob(Path(d), [".mkv"], t, t, "P", "Game", due=time.time() + 0.2))
            self.assertTrue(done.wait(5))
            worker.stop()
            worker.join(2)
            self.assertEqual(os.listdir(d), ["rec Game [P].mkv"])
            self.assertIn("rec Game [P].mkv", messages[0])


if __name__ == "__main__":
    unittest.main()
