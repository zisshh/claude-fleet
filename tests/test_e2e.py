"""Simulated end-to-end: run the real spawner script in both modes against fake
cmux / claude / cursor-agent, execute every pane command in a real shell, then
exercise the bus the way the manager and workers do -- including a restart onto
the opposite worker runtime.

What this does NOT prove: that a live cmux renders the layout, or that a
logged-in Cursor account really serves Claude Opus. See README "Verification
status" for what was checked live and what was not.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import unittest
from pathlib import Path

from helpers import REPO, TempDirs, fake_env

import fleet_runtime as fr

SPAWN = REPO / "spawn_claude_fleet.py"


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = TempDirs()
        self.addCleanup(self.tmp.cleanup)
        self.layout = self.tmp.root / "layout.json"
        self.state = self.tmp.root / "cmux-state"
        self.user_cfg = self.tmp.root / "user-cursor"
        self.user_cfg.mkdir()
        self.env = fake_env(
            FAKE_CMUX_LOG=str(self.tmp.root / "cmux.log"), FAKE_CMUX_LAYOUT=str(self.layout),
            FAKE_CMUX_STATE=str(self.state), FAKE_DUMP_DIR=str(self.tmp.dump),
            FAKE_CURSOR_MODE="ok", CURSOR_CONFIG_DIR=str(self.user_cfg), HOME=str(self.tmp.root),
        )
        self.bus = self.tmp.target / ".team" / "test-fleet"

    def spawn(self, *extra: str) -> subprocess.CompletedProcess:
        r = subprocess.run([sys.executable, str(SPAWN), "test-fleet", "--cwd", str(self.tmp.target), *extra],
                           capture_output=True, text=True, env=self.env)
        return r

    def surfaces(self) -> dict[str, str]:
        out = {}

        def walk(n):
            if isinstance(n, dict):
                if "name" in n and "command" in n:
                    out[n["name"]] = n["command"]
                for v in n.values():
                    walk(v)
            elif isinstance(n, list):
                for v in n:
                    walk(v)

        walk(json.loads(self.layout.read_text()))
        return out

    def run_panes(self) -> None:
        for cmd in self.surfaces().values():
            subprocess.run(["sh", "-c", cmd], check=True, env=self.env, cwd=self.tmp.target)

    def test_claude_then_cursor_then_claude(self):
        # ---- run 1: default Claude workers ------------------------------ #
        r = self.spawn()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Manager: Claude Code (model: fable)", r.stdout)
        self.assertIn("Workers: Claude Code × 4", r.stdout)
        self.assertIn("Thinking: max (--effort max)", r.stdout)
        claude_panes = self.surfaces()
        self.assertEqual(set(claude_panes), {"manager", *fr.WORKER_ROLES})
        self.run_panes()
        dumps = {p.name: json.loads(p.read_text()) for p in self.tmp.dump.glob("claude-*.json")}
        self.assertEqual(len(dumps), 5)  # manager + 4 workers booted, each argv parsed by a real shell
        workers = [d for k, d in dumps.items() if k != "claude-manager.json"]
        for d in workers:
            argv = d["argv"]
            self.assertEqual(argv[argv.index("--model") + 1], "opus")
            self.assertEqual(argv[argv.index("--effort") + 1], "max")
        self.assertIn("--model", dumps["claude-manager.json"]["argv"])
        self.assertEqual(dumps["claude-manager.json"]["argv"][1], "fable")

        spawn_meta = json.loads((self.tmp.target / ".team" / "test-fleet.spawn.json").read_text())
        self.assertEqual((spawn_meta["manager_runtime"], spawn_meta["worker_runtime"]), ("claude", "claude"))
        self.assertEqual(spawn_meta["models"]["worker-1"], "opus")

        # ---- a worker writes the bus, manager records the backlog ------- #
        (self.bus / "manager.md").write_text("goal: add --json export\n")
        (self.bus / "backlog.md").write_text("- [T1] worker-1 | in-progress | add --json to export\n")
        (self.bus / "worker-1.md").write_text(
            "# worker-1 — add --json to export\n\nStatus: in-progress\n\n## Task\nadd --json to export\n\n"
            "## Files changed\nsrc/cli.py\n\n## Handoff\nflag parsed; serializer still TODO\n")
        with (self.bus / "requests.md").open("a") as fh:
            fh.write("worker-2 -> worker-1: need the export schema\n")

        # ---- close, restart with Cursor workers ------------------------- #
        r = self.spawn("--close")
        self.assertEqual(r.returncode, 0, r.stderr)
        for p in self.tmp.dump.iterdir():
            p.unlink()
        r = self.spawn("--cwa")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Manager: Claude Code (model: fable)", r.stdout)
        self.assertIn("Workers: Cursor Agent × 4", r.stdout)
        self.assertIn("Model: Claude Opus 4.8 Thinking Max (claude-opus-4-8-thinking-max)", r.stdout)
        self.assertIn("Thinking: max", r.stdout)
        cursor_panes = self.surfaces()
        self.assertEqual(cursor_panes["manager"], claude_panes["manager"])  # manager untouched
        self.run_panes()
        self.assertTrue((self.tmp.dump / "claude-manager.json").exists())
        cursor_dumps = sorted(self.tmp.dump.glob("cursor-worker-*.json"))
        self.assertEqual(len(cursor_dumps), 4)
        for p in cursor_dumps:
            argv = json.loads(p.read_text())["argv"]
            self.assertEqual(argv[argv.index("--model") + 1], "claude-opus-4-8-thinking-max")
            prompt = argv[-1]
            self.assertIn(str(self.bus / fr.INSTRUCTIONS_FILE), prompt)
            self.assertIn("The bus is the source of truth", prompt)

        # shared context survived the runtime switch, byte for byte
        self.assertIn("serializer still TODO", (self.bus / "worker-1.md").read_text())
        self.assertIn("[T1] worker-1", (self.bus / "backlog.md").read_text())
        self.assertIn("need the export schema", (self.bus / "requests.md").read_text())
        self.assertIn("goal", (self.bus / "manager.md").read_text())
        profile = json.loads((self.bus / fr.RUNTIME_FILE).read_text())
        self.assertEqual((profile["runtime"], profile["cli_model"]), ("cursor", "claude-opus-4-8-thinking-max"))

        # ---- the task moves from worker-1 to worker-3 ------------------- #
        (self.bus / "backlog.md").write_text("- [T1] worker-3 | in-progress | add --json to export\n")
        screen = self.tmp.root / "screen.txt"
        screen.write_text("ctrl+c to stop\n")  # Cursor TUI mid-turn
        d = subprocess.run(
            [sys.executable, str(self.bus / "dispatch.py"), "--surface", "surface:4", "--settle", "0",
             "--confirm-wait", "0.05", "--clear", str(self.bus / "worker-3.done"),
             f"Take over T1 from worker-1: read {self.bus}/worker-1.md first and continue from its Handoff."],
            capture_output=True, text=True, env=dict(self.env, FAKE_SCREEN=str(screen)))
        self.assertEqual(d.returncode, 0, d.stderr)
        self.assertIn("worker is working", d.stdout)

        # ---- and back to Claude: old Claude sessions are still resumable -- #
        sessions = json.loads((self.bus / fr.SESSIONS_FILE).read_text())
        first_claude = {r: sessions[r]["claude"]["session_id"] for r in fr.WORKER_ROLES}
        self.assertEqual(self.spawn("--close").returncode, 0)
        r = self.spawn()
        self.assertEqual(r.returncode, 0, r.stderr)
        sessions = json.loads((self.bus / fr.SESSIONS_FILE).read_text())
        self.assertEqual(sessions["current_runtime"], "claude")
        for role in fr.WORKER_ROLES:
            self.assertIn("cursor", sessions[role])                       # Cursor bookkeeping kept
            self.assertNotEqual(sessions[role]["claude"]["session_id"], first_claude[role])  # fresh launch
        hist = [line.split()[1] for line in (self.bus / fr.HISTORY_FILE).read_text().splitlines()]
        self.assertEqual(hist, ["workers=claude", "workers=cursor", "workers=claude"])

    def test_cursor_refusal_leaves_no_state(self):
        self.env["FAKE_CURSOR_MODE"] = "no_opus"
        r = self.spawn("--cwa")
        self.assertEqual(r.returncode, 1)
        self.assertIn("never fall back", r.stderr)
        self.assertFalse((self.tmp.target / ".team").exists())
        self.assertFalse(self.layout.exists())  # cmux never asked to create anything

    def test_worker_commands_survive_hostile_paths(self):
        r = self.spawn("--cwa")
        self.assertEqual(r.returncode, 0, r.stderr)
        for name, cmd in self.surfaces().items():
            if name.startswith("worker-"):
                script = Path(shlex.split(cmd)[-1])
                argv = shlex.split(script.read_text().splitlines()[-1])
                self.assertIn(f"under \"{self.tmp.target}\"", argv[-1])

    def test_pane_commands_fit_the_tty_line_limit(self):
        # cmux types each pane command before the shell's line editor is up. In
        # that canonical tty mode macOS keeps at most MAX_CANON (1024) bytes of a
        # line and drops the rest, Enter included, so a longer command is left
        # truncated at the prompt and never runs.
        for extra in ((), ("--cwa",)):
            r = self.spawn(*extra)
            self.assertEqual(r.returncode, 0, r.stderr)
            for name, cmd in self.surfaces().items():
                self.assertLess(len(cmd.encode()), 1024, f"{name}: {cmd[:80]}...")
            self.assertEqual(self.spawn("--close").returncode, 0)


if __name__ == "__main__":
    unittest.main()
