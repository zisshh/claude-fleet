"""Completion signaling + runtime-aware dispatch, against a fake cmux."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest

from helpers import REPO, TempDirs, fake_env

import fleet_runtime as fr


class DispatchCase(unittest.TestCase):
    runtime = "claude"

    def setUp(self):
        self.tmp = TempDirs()
        self.addCleanup(self.tmp.cleanup)
        self.bus = self.tmp.target / ".team" / "test-fleet"
        self.bus.mkdir(parents=True)
        shutil.copy(REPO / "fleet_dispatch.py", self.bus / "dispatch.py")
        if self.runtime:
            rt = {"claude": fr.ClaudeWorkerRuntime, "cursor": fr.CursorWorkerRuntime}[self.runtime]
            profile = {"runtime": self.runtime, "working_signals": list(rt.working_signals),
                       "interrupt_key": rt.interrupt_key, "clear_key": rt.clear_key}
            (self.bus / "runtime.json").write_text(json.dumps(profile))
        self.log = self.tmp.root / "cmux.log"
        self.screen = self.tmp.root / "screen.txt"

    def dispatch(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        e = fake_env(FAKE_CMUX_LOG=str(self.log), FAKE_SCREEN=str(self.screen), **env)
        return subprocess.run([sys.executable, str(self.bus / "dispatch.py"), "--surface", "surface:5",
                               "--settle", "0", "--confirm-wait", "0.05", *args],
                              capture_output=True, text=True, env=e)

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]


class TestCompletionSignaling(DispatchCase):
    def test_done_flag_cleared_then_detected(self):
        done = self.bus / "worker-1.done"
        done.write_text("worker-1 | previous task\n")  # stale flag must not count
        r = self.dispatch("--clear", str(done), "task one", FAKE_DONE_ON_ENTER=str(done))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("done-flag appeared", r.stdout)
        # notes exist and precede the flag; the flag has the protocol format
        self.assertTrue((self.bus / "worker-1.md").exists())
        self.assertEqual(done.read_text(), "worker-1 | fake task finished\n")
        self.assertEqual(self.calls()[0], ["send", "--surface", "surface:5", "task one"])

    def test_multiline_task_rejected(self):
        r = self.dispatch("--clear", str(self.bus / "worker-1.done"), "line one\nline two")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.calls(), [])

    def test_parked_task_exhausts_ladder_and_fails_loudly(self):
        task = "a task that will never submit because the bar is stuck"
        self.screen.write_text(f"> {task}\n")
        r = self.dispatch("--clear", str(self.bus / "worker-1.done"), task)
        self.assertEqual(r.returncode, 1)
        sends = [c for c in self.calls() if c[0] == "send"]
        self.assertEqual(len(sends), 2, "retype happens at most once")


class TestClaudeSignals(DispatchCase):
    runtime = "claude"

    def test_claude_working_signal(self):
        self.screen.write_text("✻ Thinking… (esc to interrupt)\n")
        r = self.dispatch("--clear", str(self.bus / "worker-1.done"), "task")
        self.assertIn("worker is working", r.stdout)

    def test_cursor_signal_means_nothing_to_claude_profile(self):
        self.screen.write_text("ctrl+c to stop\n")
        r = self.dispatch("--clear", str(self.bus / "worker-1.done"), "task")
        self.assertNotIn("worker is working", r.stdout)

    def test_interrupt_is_one_escape(self):
        r = self.dispatch("--interrupt")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.calls(), [["send-key", "--surface", "surface:5", "escape"]])


class TestCursorSignals(DispatchCase):
    runtime = "cursor"

    def test_cursor_working_signals(self):
        for screen in ("  Add a follow-up                         ctrl+c to stop\n", "⠋ Thinking…\n",
                       "Responding…\n"):
            self.screen.write_text(screen)
            r = self.dispatch("--clear", str(self.bus / "worker-2.done"), "task")
            self.assertIn("worker is working", r.stdout, screen)

    def test_interrupt_is_one_ctrl_c(self):
        r = self.dispatch("--interrupt")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.calls(), [["send-key", "--surface", "surface:5", "ctrl+c"]])


class TestLegacyBus(DispatchCase):
    runtime = None  # a bus from before --cwa existed: no runtime.json

    def test_defaults_to_claude(self):
        self.screen.write_text("esc to interrupt\n")
        r = self.dispatch("--clear", str(self.bus / "worker-1.done"), "task")
        self.assertIn("worker is working", r.stdout)
        self.dispatch("--interrupt")
        self.assertEqual(self.calls()[-1][-1], "escape")


if __name__ == "__main__":
    unittest.main()
