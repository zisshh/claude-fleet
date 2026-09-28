"""Unit tests: CLI parsing, runtime selection, both runtimes, prompts, bus paths."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import unittest
import uuid
from pathlib import Path
from unittest import mock

from helpers import FAKES, REPO, TempDirs, fake_env

import fleet_runtime as fr
import spawn_claude_fleet as spawn


def ctx_for(tmp: TempDirs, fleet: str = "test-fleet") -> fr.FleetContext:
    return fr.FleetContext(fleet=fleet, target=tmp.target, bus=tmp.target / ".team" / fleet, assets=REPO)


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = TempDirs()
        self.addCleanup(self.tmp.cleanup)
        self.ctx = ctx_for(self.tmp)


# --------------------------------------------------------------------------- #
class TestCliParsing(unittest.TestCase):
    def test_default_is_claude(self):
        args = spawn.parse_args(["fleet"])
        self.assertEqual(args.runtime, "claude")
        self.assertFalse(args.cwa)
        self.assertEqual(args.worker_config, fr.WorkerConfig(runtime="claude"))

    def test_cwa_is_cursor(self):
        args = spawn.parse_args(["fleet", "--cwa"])
        self.assertEqual(args.runtime, "cursor")
        self.assertEqual(args.worker_config.model, "claude-opus")
        self.assertEqual(args.worker_config.thinking, "max")

    def test_flag_position_does_not_matter(self):
        self.assertEqual(spawn.parse_args(["--cwa", "fleet", "--cwd", "/tmp"]).runtime, "cursor")

    def test_cursor_model_requires_cwa(self):
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            spawn.parse_args(["fleet", "--cursor-model", "claude-opus-4-8"])

    def test_cursor_model_with_cwa(self):
        args = spawn.parse_args(["fleet", "--cwa", "--cursor-model", "claude-opus-4-5"])
        self.assertEqual(args.worker_config.cursor_model, "claude-opus-4-5")

    def test_existing_flags_unchanged(self):
        args = spawn.parse_args(["my-fleet", "--cwd", "/x", "--env-file", "/e", "--close"])
        self.assertEqual((args.fleet, args.cwd, args.env_file, args.close), ("my-fleet", "/x", "/e", True))
        self.assertEqual(args.runtime, "claude")


class TestRuntimeSelection(unittest.TestCase):
    def test_make_runtime(self):
        self.assertIsInstance(fr.make_runtime(fr.WorkerConfig("claude")), fr.ClaudeWorkerRuntime)
        self.assertIsInstance(fr.make_runtime(fr.WorkerConfig("cursor")), fr.CursorWorkerRuntime)

    def test_unknown_runtime_rejected(self):
        with self.assertRaises(ValueError):
            fr.make_runtime(fr.WorkerConfig("gemini"))

    def test_worker_config_is_the_only_flag_interpretation(self):
        self.assertEqual(fr.worker_config(False).runtime, "claude")
        self.assertEqual(fr.worker_config(True).runtime, "cursor")

    def test_both_are_worker_runtimes(self):
        for rt in (fr.ClaudeWorkerRuntime(fr.WorkerConfig()), fr.CursorWorkerRuntime(fr.WorkerConfig("cursor"))):
            self.assertIsInstance(rt, fr.WorkerRuntime)
            for method in ("preflight", "new_session", "launch", "resume", "send", "stop"):
                self.assertTrue(callable(getattr(rt, method)))


# --------------------------------------------------------------------------- #
class TestClaudeRuntime(TempCase):
    def setUp(self):
        super().setUp()
        self.rt = fr.ClaudeWorkerRuntime(fr.WorkerConfig())
        self.sel = self.rt.preflight(self.ctx)

    def test_model_configuration(self):
        self.assertEqual(self.sel.cli_model, "opus")
        self.assertTrue(self.sel.thinking_verified)
        self.assertIn("--effort max", self.sel.thinking)

    def test_launch_command(self):
        session = self.rt.new_session("worker-2", self.ctx)
        uuid.UUID(session["session_id"])  # valid uuid
        argv = shlex.split(self.rt.launch("worker-2", self.ctx, session))
        self.assertEqual(argv[0], "claude")
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        self.assertEqual(argv[argv.index("--effort") + 1], "max")
        self.assertIn("--dangerously-skip-permissions", argv)
        self.assertEqual(argv[argv.index("--append-system-prompt-file") + 1], str(REPO / "fleet-worker.md"))
        self.assertEqual(argv[argv.index("--session-id") + 1], session["session_id"])
        prompt = argv[-1]
        self.assertIn("You are worker-2 on fleet test-fleet.", prompt)
        self.assertIn(str(self.ctx.bus), prompt)  # path with spaces + apostrophe survives
        self.assertIn("ready: worker-2", prompt)

    def test_resume_uses_same_session_and_flags(self):
        session = self.rt.new_session("worker-1", self.ctx)
        cmd = self.rt.resume("worker-1", self.ctx, session)
        self.assertTrue(cmd.startswith(f"cd {shlex.quote(str(self.tmp.target))} && "))
        argv = shlex.split(cmd.split(" && ", 1)[1])
        self.assertEqual(argv[argv.index("--resume") + 1], session["session_id"])
        self.assertIn("--append-system-prompt-file", argv)
        self.assertEqual(argv[argv.index("--effort") + 1], "max")

    def test_sessions_are_unique(self):
        ids = {self.rt.new_session(r, self.ctx)["session_id"] for r in fr.WORKER_ROLES}
        self.assertEqual(len(ids), 4)

    def test_stop_is_one_escape(self):
        calls = []
        self.rt.stop("surface:5", runner=lambda argv: calls.append(argv) or subprocess.CompletedProcess(argv, 0))
        self.assertEqual(calls, [["cmux", "send-key", "--surface", "surface:5", "escape"]])

    def test_send_goes_through_bus_dispatcher(self):
        calls = []
        rc = self.rt.send("worker-3", "surface:4", "do the thing", self.ctx,
                          runner=lambda argv: calls.append(argv) or subprocess.CompletedProcess(argv, 0))
        self.assertEqual(rc, 0)
        argv = calls[0]
        self.assertEqual(argv[1], str(self.ctx.bus / "dispatch.py"))
        self.assertEqual(argv[argv.index("--clear") + 1], str(self.ctx.bus / "worker-3.done"))
        self.assertEqual(argv[-1], "do the thing")


# --------------------------------------------------------------------------- #
class CursorCase(TempCase):
    mode = "ok"

    def setUp(self):
        super().setUp()
        self.user_cfg = self.tmp.root / "user-cursor-config"
        self.user_cfg.mkdir()
        (self.user_cfg / "cli-config.json").write_text(json.dumps({
            "version": 1, "approvalMode": "allowlist", "editor": {"vimMode": True},
            "exploreSubagentModel": "default"}))
        patcher = mock.patch.dict(os.environ, {"FAKE_CURSOR_MODE": self.mode})
        patcher.start()
        self.addCleanup(patcher.stop)

    def runtime(self, **cfg) -> fr.CursorWorkerRuntime:
        return fr.CursorWorkerRuntime(fr.WorkerConfig("cursor", **cfg), binary=str(FAKES / "cursor-agent"),
                                      user_config_dir=self.user_cfg)


class TestCursorModelSelection(CursorCase):
    def test_picks_newest_opus_thinking_max(self):
        sel = self.runtime().preflight(self.ctx)
        # newest version, not -fast, highest level, thinking over non-thinking
        self.assertEqual(sel.cli_model, "claude-opus-4-8-thinking-max")
        self.assertTrue(sel.thinking_verified)
        self.assertTrue(sel.thinking.startswith("max"))
        self.assertIn("Claude Opus 4.8 Thinking Max", sel.display)
        self.assertIn("--list-models", sel.source)

    def test_never_emits_bracketed_ids(self):
        # the CLI rejects 'id[param=value]' outright: "Cannot use this model"
        self.assertNotIn("[", self.runtime().preflight(self.ctx).cli_model)

    def test_never_selects_non_claude(self):
        sel = self.runtime().preflight(self.ctx)
        for bad in ("gpt", "gemini", "composer", "auto", "sonnet"):
            self.assertNotIn(bad, sel.cli_model.lower())

    def test_explicit_model(self):
        sel = self.runtime(cursor_model="claude-opus-4-5-thinking-max").preflight(self.ctx)
        self.assertEqual(sel.cli_model, "claude-opus-4-5-thinking-max")

    def test_explicit_lower_level_is_reported_as_such(self):
        sel = self.runtime(cursor_model="claude-opus-4-8-high").preflight(self.ctx)
        self.assertEqual(sel.cli_model, "claude-opus-4-8-high")
        self.assertTrue(sel.thinking.startswith("high"))

    def test_explicit_non_claude_model_refused(self):
        with self.assertRaisesRegex(fr.RuntimeUnavailable, "not a Claude model"):
            self.runtime(cursor_model="gpt-5.5-high").preflight(self.ctx)

    def test_explicit_unknown_model_refused(self):
        with self.assertRaisesRegex(fr.RuntimeUnavailable, "not in this account"):
            self.runtime(cursor_model="claude-opus-4-8[effort=max]").preflight(self.ctx)

    def test_explicit_claude_non_opus_warns(self):
        rt = self.runtime(cursor_model="claude-sonnet-4-6-thinking-max")
        rt.preflight(self.ctx)
        self.assertTrue(any("not Opus" in w for w in rt.warnings))


class TestCursorNoOpus(CursorCase):
    mode = "no_opus"

    def test_no_silent_fallback(self):
        with self.assertRaisesRegex(fr.RuntimeUnavailable, "never fall back"):
            self.runtime().preflight(self.ctx)


class TestCursorNotLoggedIn(CursorCase):
    mode = "noauth"

    def test_login_hint(self):
        with self.assertRaisesRegex(fr.RuntimeUnavailable, "login"):
            self.runtime().preflight(self.ctx)


class TestCursorLaunch(CursorCase):
    def setUp(self):
        super().setUp()
        self.rt = self.runtime()
        self.rt.preflight(self.ctx)

    def test_launch_command(self):
        session = self.rt.new_session("worker-1", self.ctx)
        argv = shlex.split(self.rt.launch("worker-1", self.ctx, session))
        self.assertEqual(argv[0], "env")
        self.assertEqual(argv[1], f"CURSOR_CONFIG_DIR={self.ctx.bus / 'cursor' / 'worker-1'}")
        self.assertEqual(argv[2], str(FAKES / "cursor-agent"))
        self.assertEqual(argv[argv.index("--model") + 1], "claude-opus-4-8-thinking-max")
        for flag in ("--force", "--trust", "--approve-mcps"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--workspace") + 1], str(self.tmp.target))
        prompt = argv[-1]
        self.assertIn("You are worker-1 on fleet test-fleet.", prompt)
        self.assertIn(str(self.ctx.bus / fr.INSTRUCTIONS_FILE), prompt)

    def test_per_worker_config_pins_explore_and_keeps_user_settings(self):
        session = self.rt.new_session("worker-3", self.ctx)
        cfg = json.loads((Path(session["config_dir"]) / "cli-config.json").read_text())
        self.assertEqual(cfg["subagentModels"]["explore"], "inherit")
        self.assertEqual(cfg["exploreSubagentModel"], "inherit")
        self.assertEqual(cfg["editor"], {"vimMode": True})  # user's own settings carried over
        # the user's global config is untouched
        user = json.loads((self.user_cfg / "cli-config.json").read_text())
        self.assertEqual(user["exploreSubagentModel"], "default")

    def test_resume_continues_that_workers_chat(self):
        session = self.rt.new_session("worker-4", self.ctx)
        argv = shlex.split(self.rt.resume("worker-4", self.ctx, session).split(" && ", 1)[1])
        self.assertIn("--continue", argv)
        self.assertEqual(argv[1], f"CURSOR_CONFIG_DIR={self.ctx.bus / 'cursor' / 'worker-4'}")
        self.assertEqual(argv[argv.index("--model") + 1], "claude-opus-4-8-thinking-max")

    def test_stop_is_one_ctrl_c(self):
        calls = []
        self.rt.stop("surface:9", runner=lambda argv: calls.append(argv) or subprocess.CompletedProcess(argv, 0))
        self.assertEqual(calls, [["cmux", "send-key", "--surface", "surface:9", "ctrl+c"]])

    def test_launch_command_parses_in_a_real_shell(self):
        session = self.rt.new_session("worker-2", self.ctx)
        cmd = self.rt.launch("worker-2", self.ctx, session)
        env = fake_env(FAKE_DUMP_DIR=str(self.tmp.dump))
        subprocess.run(["sh", "-c", cmd], check=True, env=env, cwd=self.tmp.target)
        dumped = json.loads((self.tmp.dump / "cursor-worker-2.json").read_text())
        self.assertEqual(dumped["argv"], shlex.split(cmd)[3:])
        self.assertEqual(dumped["CURSOR_CONFIG_DIR"], str(self.ctx.bus / "cursor" / "worker-2"))


class TestSubagentScan(TempCase):
    def test_flags_non_inheriting_subagents(self):
        agents = self.tmp.target / ".cursor" / "agents"
        agents.mkdir(parents=True)
        (agents / "ok.md").write_text("---\nname: ok\nmodel: inherit\n---\nbody\n")
        (agents / "gpt.md").write_text("---\nname: g\nmodel: gpt-5\n---\nbody\n")
        (agents / "forced.md").write_text("---\nname: f\nforce-default-model: true\n---\nbody\n")
        (agents / "sonnet.md").write_text("---\nname: s\nmodel: sonnet\n---\nbody\n")
        (agents / "nomodel.md").write_text("---\nname: n\n---\nbody\n")
        warnings = fr.scan_subagent_definitions(self.tmp.target, home=self.tmp.root / "nohome")
        joined = "\n".join(warnings)
        self.assertEqual(len(warnings), 3)
        self.assertIn("gpt-5 (not a Claude model)", joined)
        self.assertIn("force-default-model", joined)
        self.assertIn("sonnet (Claude, but not the worker's Opus)", joined)


# --------------------------------------------------------------------------- #
class TestWorkerPrompt(TempCase):
    def test_boot_prompts_share_the_protocol(self):
        claude = fr.worker_boot_prompt("worker-1", self.ctx)
        cursor = fr.worker_boot_prompt("worker-1", self.ctx, instructions=self.ctx.bus / fr.INSTRUCTIONS_FILE)
        for prompt in (claude, cursor):
            self.assertIn("The bus is the source of truth", prompt)
            self.assertIn("worker-1.md", prompt)
            self.assertIn("manager.md and backlog.md", prompt)
            self.assertIn("ready: worker-1", prompt)
        # Cursor additionally reads the SAME protocol text from disk (no system-prompt flag).
        self.assertTrue(cursor.startswith(claude.split(" The bus is")[0]))
        self.assertIn("worker-instructions.md", cursor)
        self.assertNotIn("worker-instructions.md", claude)

    def test_prompt_is_single_line(self):
        self.assertNotIn("\n", fr.worker_boot_prompt("worker-4", self.ctx, instructions=Path("/x")))

    def test_worker_instructions_cover_the_contract(self):
        text = (REPO / "fleet-worker.md").read_text()
        for needle in (
            "authoritative shared state", "source of truth", "Before beginning work",
            "Read the manager's instructions", "Check for work already completed by other workers",
            "Do not overwrite another worker's work blindly", "Record important decisions",
            "Record dependencies and blockers", "## Files changed", "## Handoff",
            "WORKER_DONE: <role> | <one-line summary>", "BLOCKED:", "<bus>/<role>.done", "Subagents",
            "Never pass a model override", "Taking over a task",
        ):
            self.assertIn(needle, text, needle)
        self.assertNotIn("Claude-only", text)


# --------------------------------------------------------------------------- #
class TestSharedContext(TempCase):
    def boot_state(self, runtime: fr.WorkerRuntime) -> dict:
        runtime.preflight(self.ctx)
        spawn.prepare_team_dir(self.ctx.fleet, self.ctx.target)
        sessions = {r: runtime.new_session(r, self.ctx) for r in fr.WORKER_ROLES}
        fr.record_boot(runtime, self.ctx, sessions)
        return sessions

    def test_bus_paths(self):
        bus = spawn.prepare_team_dir("test-fleet", self.tmp.target)
        self.assertEqual(bus, self.tmp.target / ".team" / "test-fleet")
        self.assertEqual((self.tmp.target / ".team" / ".gitignore").read_text(), "*\n!.gitignore\n")
        self.assertTrue((bus / "dispatch.py").is_file())
        self.assertTrue((bus / "requests.md").is_file())
        self.assertEqual((bus / fr.INSTRUCTIONS_FILE).read_text(), (REPO / "fleet-worker.md").read_text())

    def test_restart_preserves_context_and_clears_flags(self):
        bus = spawn.prepare_team_dir("test-fleet", self.tmp.target)
        (bus / "worker-1.md").write_text("# worker-1 — half done\n\nStatus: in-progress\n")
        (bus / "backlog.md").write_text("- [T1] worker-1 | in-progress | build it\n")
        (bus / "manager.md").write_text("goal: build it\n")
        (bus / "requests.md").write_text("worker-2 -> worker-1: need the schema\n")
        (bus / "worker-1.done").write_text("worker-1 | stale\n")
        spawn.prepare_team_dir("test-fleet", self.tmp.target)  # restart (spawn file absent = closed)
        self.assertIn("half done", (bus / "worker-1.md").read_text())
        self.assertIn("[T1]", (bus / "backlog.md").read_text())
        self.assertIn("goal", (bus / "manager.md").read_text())
        self.assertIn("need the schema", (bus / "requests.md").read_text())
        self.assertFalse((bus / "worker-1.done").exists())

    def test_live_fleet_is_not_clobbered(self):
        bus = spawn.prepare_team_dir("test-fleet", self.tmp.target)
        (self.tmp.target / ".team" / "test-fleet.spawn.json").write_text("{}")
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            spawn.prepare_team_dir("test-fleet", self.tmp.target)
        self.assertTrue(bus.exists())

    def test_runtime_switch_keeps_both_sessions(self):
        claude_sessions = self.boot_state(fr.ClaudeWorkerRuntime(fr.WorkerConfig()))
        with mock.patch.dict(os.environ, {"FAKE_CURSOR_MODE": "ok"}):
            cursor = fr.CursorWorkerRuntime(fr.WorkerConfig("cursor"), binary=str(FAKES / "cursor-agent"),
                                            user_config_dir=self.tmp.root)
            self.boot_state(cursor)
        data = json.loads((self.ctx.bus / fr.SESSIONS_FILE).read_text())
        self.assertEqual(data["current_runtime"], "cursor")
        for role in fr.WORKER_ROLES:
            self.assertEqual(data[role]["claude"]["session_id"], claude_sessions[role]["session_id"])
            self.assertIn("--resume", data[role]["claude"]["resume"])
            self.assertIn("--continue", data[role]["cursor"]["resume"])
        profile = json.loads((self.ctx.bus / fr.RUNTIME_FILE).read_text())
        self.assertEqual(profile["runtime"], "cursor")
        self.assertEqual(profile["interrupt_key"], "ctrl+c")
        history = (self.ctx.bus / fr.HISTORY_FILE).read_text().splitlines()
        self.assertEqual([h.split()[1] for h in history], ["workers=claude", "workers=cursor"])


# --------------------------------------------------------------------------- #
class TestLayout(TempCase):
    def commands(self, runtime):
        runtime.preflight(self.ctx)
        return {r: runtime.launch(r, self.ctx, runtime.new_session(r, self.ctx)) for r in fr.WORKER_ROLES}

    def surfaces(self, layout_json: str) -> dict:
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

        walk(json.loads(layout_json))
        return out

    def test_workers_injected_manager_unchanged(self):
        claude = self.surfaces(spawn.build_layout("test-fleet", self.tmp.target,
                                                  self.commands(fr.ClaudeWorkerRuntime(fr.WorkerConfig()))))
        with mock.patch.dict(os.environ, {"FAKE_CURSOR_MODE": "ok"}):
            cursor_rt = fr.CursorWorkerRuntime(fr.WorkerConfig("cursor"), binary=str(FAKES / "cursor-agent"),
                                               user_config_dir=self.tmp.root)
            cursor = self.surfaces(spawn.build_layout("test-fleet", self.tmp.target, self.commands(cursor_rt)))
        self.assertEqual(set(claude), {"manager", *fr.WORKER_ROLES})
        self.assertEqual(claude["manager"], cursor["manager"])  # --cwa never touches the manager
        self.assertTrue(claude["manager"].startswith("claude --model fable "))
        for role in fr.WORKER_ROLES:
            self.assertTrue(shlex.split(claude[role])[0] == "claude")
            self.assertIn("cursor-agent", shlex.split(cursor[role])[2])
            self.assertNotIn(spawn.WORKER_COMMAND, claude[role] + cursor[role])

    def test_missing_worker_command_dies(self):
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            spawn.build_layout("test-fleet", self.tmp.target, {"worker-1": "true"})


if __name__ == "__main__":
    unittest.main()
