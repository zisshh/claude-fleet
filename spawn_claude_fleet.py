#!/usr/bin/env python3
"""Boot an agent fleet in cmux: Claude Code manager + 4 Opus workers.

Boots all five panes at once from claude-fleet.layout.json (manager on the left
half, worker-1..4 in a 2x2 on the right), colors and labels the workspace, and
writes <target>/.team/<fleet>.spawn.json so the fleet can be re-found later.

The manager is always Claude Code. The workers run Claude Code by default, or
Cursor Agent (pinned to Claude Opus) with --cwa. Everything worker-CLI-specific
lives in fleet_runtime.py; this script resolves the runtime once and injects it.

Unlike a 3-tier design, the manager IS a pane -- there is no outside orchestrator
to exec into. This script boots the fleet and exits; you then talk to the manager
pane directly.

Usage:
    ./spawn_claude_fleet.py <fleet-slug> [--cwd DIR] [--env-file PATH]
    ./spawn_claude_fleet.py <fleet-slug> --cwa [--cursor-model ID]   # Cursor workers
    ./spawn_claude_fleet.py <fleet-slug> --close

Requires python3 >= 3.11 and nothing else -- no third-party packages.

Ported from disler/learning-cmux-with-agents (scripts/spawn_fast.py, MIT).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import NoReturn

from fleet_runtime import (
    INSTRUCTIONS_FILE,
    WORKER_ROLES,
    FleetContext,
    RuntimeUnavailable,
    WorkerRuntime,
    make_runtime,
    record_boot,
    worker_config,
)

# Assets live beside this script, wherever it is installed. Deriving the path from
# __file__ (rather than assuming ~/.claude/cmux) keeps the whole thing relocatable.
ASSETS = Path(__file__).resolve().parent
LAYOUT_FILE = ASSETS / "claude-fleet.layout.json"

ROLES = ["manager", *WORKER_ROLES]
MANAGER_MODEL = "fable"  # the manager is ALWAYS Claude Code; --cwa never touches it
WORKER_COMMAND = "__WORKER_COMMAND__"  # layout placeholder, filled by the worker runtime
LAUNCH_DIR = "launch"  # under the bus: one script per pane, holding its full command
UUID_RE = re.compile(r"[0-9a-fA-F-]{36}")

# .team/ is runtime state; make it self-ignoring in whatever repo it lands in.
TEAM_GITIGNORE = "*\n!.gitignore\n"


def die(msg: str) -> NoReturn:
    print(msg, file=sys.stderr)
    sys.exit(1)


def sh(*args: str) -> subprocess.CompletedProcess:
    """Run a command, capturing stdout/stderr as text. Never raises."""
    return subprocess.run(list(args), capture_output=True, text=True)


def cmux(*args: str) -> subprocess.CompletedProcess:
    return sh("cmux", *args)


def cmux_json(*args: str):
    out = cmux(*args).stdout.strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def slugify(name: str) -> str:
    """lowercase, spaces -> dashes, keep only [a-z0-9-]."""
    name = name.lower().replace(" ", "-")
    return re.sub(r"[^a-z0-9-]", "", name)


def ensure_cmux_running() -> None:
    """Preflight: if the socket is down, launch cmux and wait. Don't stop."""
    probe = cmux("identify", "--json")
    if probe.returncode == 0:
        return

    stderr = (probe.stderr or "") + (probe.stdout or "")
    socket_missing = "Socket not found" in stderr or "Connection refused" in stderr

    if socket_missing:
        print("cmux is not running; launching ...", flush=True)
        sh("open", "-a", "cmux")
        for _ in range(30):  # poll ~15s
            time.sleep(0.5)
            if cmux("identify", "--json").returncode == 0:
                return
        probe = cmux("identify", "--json")
        if probe.returncode == 0:
            return
        stderr = (probe.stderr or "") + (probe.stdout or "")

    explain_socket_refusal(stderr)


def explain_socket_refusal(stderr: str) -> None:
    """cmux is up but refused us. Under cmuxOnly, only agents INSIDE cmux may drive it."""
    inside = bool(os.environ.get("CMUX_SURFACE_ID") or os.environ.get("CMUX_WORKSPACE_ID"))
    lines = [
        "cmux refused this connection.",
        f"  cmux says: {stderr.strip() or '(no message)'}",
        "",
    ]
    if not inside:
        lines += [
            "You are running this from a normal terminal, OUTSIDE cmux.",
            "cmux's automation.socketControlMode defaults to 'cmuxOnly', which only lets",
            "agents running INSIDE a cmux terminal drive the socket. Two ways forward:",
            "",
            "  1. Run this script from inside a cmux terminal (no config change), or",
            "  2. Allow outside control -- edit ~/.config/cmux/cmux.json:",
            '         "automation": { "socketControlMode": "allowAll" }',
            "     then: cmux reload-config",
        ]
    else:
        lines += [
            "You appear to be inside cmux, so socketControlMode should not be the issue.",
            "Check `cmux identify --json` by hand.",
        ]
    die("\n".join(lines))


def dq_escape(s: str) -> str:
    """Escape a string for interpolation INSIDE a double-quoted shell string.

    Only backslash, double-quote, dollar and backtick are special there; spaces
    and single quotes are literal. NOT interchangeable with shlex.quote: that
    emits a standalone quoted word, and dropped inside an existing double-quoted
    string its quotes shatter it -- a path like "Div's Second Brain" broke every
    pane command exactly this way.
    """
    out = s.replace("\\", "\\\\")
    for ch in ('"', "$", "`"):
        out = out.replace(ch, "\\" + ch)
    return out


def build_layout(fleet: str, target: Path, worker_commands: dict[str, str]) -> str:
    """Interpolate the layout template and strip its _comment; return compact JSON.

    Substitution happens on the PARSED tree, not the raw text, and each
    placeholder is escaped for the shell context it lands in:

      __FEATURE__  slug [a-z0-9-], safe in any context
      __CWD__      lands INSIDE the double-quoted boot prompts -> dq_escape
      __ASSETS__   lands as a bare argument word -> shlex.quote

    Mixing these up is not cosmetic: the wrong escaping for the wrong context
    produces a pane command the shell cannot parse, and the agent never boots.

    Worker surfaces carry __WORKER_COMMAND__; each is replaced AFTER the text
    substitution with the complete, already-quoted command the worker runtime
    built for that role (matched by the surface `name`, the layout's join key).
    """
    obj = json.loads(LAYOUT_FILE.read_text())
    obj.pop("_comment", None)

    subs = {
        "__FEATURE__": fleet,
        "__CWD__": dq_escape(str(target)),
        "__ASSETS__": shlex.quote(str(ASSETS)),
    }

    def walk(node):
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        if isinstance(node, str):
            for placeholder, value in subs.items():
                node = node.replace(placeholder, value)
            return node
        return node

    tree = walk(obj)

    filled = set()

    def inject(node):
        if isinstance(node, dict):
            if node.get("command") == WORKER_COMMAND:
                name = node.get("name")
                if name not in worker_commands:
                    die(f"layout surface {name!r} wants a worker command, but no runtime command exists for it")
                node["command"] = worker_commands[name]
                filled.add(name)
            for v in node.values():
                inject(v)
        elif isinstance(node, list):
            for v in node:
                inject(v)

    inject(tree)
    missing = set(worker_commands) - filled
    if missing:
        die(f"layout has no {WORKER_COMMAND} surface for: {', '.join(sorted(missing))}")
    return json.dumps(tree, separators=(",", ":"))


def stage_launch_scripts(layout: str, bus: Path) -> str:
    """Move every pane command into <bus>/launch/<name>.sh; the pane runs `sh <script>`.

    cmux types each pane command into the new shell before its line editor is
    up. In that canonical tty mode macOS keeps at most MAX_CANON (1024) bytes
    of a line and silently drops the rest -- Enter included -- so a longer
    command sits truncated at the prompt and the agent never boots. The boot
    prompts alone approach that limit, so no pane command is typed inline.
    `exec` makes the agent replace the wrapper shell, so interrupts reach the
    agent directly and quitting it returns to the pane's own shell as before.
    """
    launch_dir = bus / LAUNCH_DIR
    launch_dir.mkdir(parents=True, exist_ok=True)

    def stage(node):
        if isinstance(node, dict):
            if "command" in node and "name" in node:
                script = launch_dir / f"{node['name']}.sh"
                script.write_text(f"#!/bin/sh\nexec {node['command']}\n")
                node = {**node, "command": f"sh {shlex.quote(str(script))}"}
            return {k: stage(v) for k, v in node.items()}
        if isinstance(node, list):
            return [stage(v) for v in node]
        return node

    return json.dumps(stage(json.loads(layout)), separators=(",", ":"))


def find_or_create_window() -> tuple[str, bool, str | None]:
    """Reuse the open window (UUID is the only stable handle); create one only if none.

    `--id-format uuids` is a GLOBAL option and must precede the subcommand. cmux
    output defaults to positional refs, and a ref like `window:1` renumbers -- it
    would be stale by the time --close reads the spawn file back.
    """
    windows = cmux_json("--id-format", "uuids", "list-windows", "--json") or []
    if not windows:  # older/newer CLI, or --id-format rejected: take what we can get
        windows = cmux_json("list-windows", "--json") or []
    win = next((w["id"] for w in windows if w.get("key")), None)
    if not win and windows:
        win = windows[0].get("id")
    if win:
        if not UUID_RE.fullmatch(str(win)):
            print(
                f"warning: window handle '{win}' is a positional ref, not a UUID -- "
                "it may renumber; --close could miss this fleet.",
                file=sys.stderr,
            )
        return win, False, None

    created = cmux("new-window").stdout  # prints "OK <uuid>", not JSON
    match = UUID_RE.search(created or "")
    if not match:
        die("failed to create a window")
    win = match.group(0)
    wslist = cmux_json("workspace", "list", "--window", win, "--json") or {}
    workspaces = wslist.get("workspaces") or []
    default_ws = workspaces[0].get("ref") if workspaces else None
    return win, True, default_ws


def prepare_team_dir(fleet: str, target: Path) -> Path:
    """Create this fleet's private bus under .team/<fleet>/.

    Namespaced per fleet: two fleets pointed at one target would otherwise share
    <role>.done and requests.md, and this wipe would silently destroy a live
    fleet's in-flight state.
    """
    team = target / ".team"
    team.mkdir(parents=True, exist_ok=True)
    gitignore = team / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(TEAM_GITIGNORE)

    bus = team / fleet
    if bus.exists() and (team / f"{fleet}.spawn.json").exists():
        die(
            f"fleet '{fleet}' already has state at {bus}\n"
            f"  close it first: {Path(__file__).name} {fleet} --close --cwd {shlex.quote(str(target))}"
        )
    bus.mkdir(parents=True, exist_ok=True)

    # A stale flag from a previous run would read as an instant completion.
    for role in ROLES:
        (bus / f"{role}.done").unlink(missing_ok=True)
    # Everything else on the bus -- worker notes, backlog, manager notes and any
    # request nobody routed yet -- is fleet context and survives a restart,
    # including a restart onto the other worker runtime.
    (bus / "requests.md").touch()

    # The manager dispatches ONLY through this helper (send -> settle -> enter
    # -> verify -> recover). It lives on the bus because the bus path is the
    # one thing the manager can always re-derive, even after a resume.
    shutil.copy(ASSETS / "fleet_dispatch.py", bus / "dispatch.py")
    (bus / "dispatch.py").chmod(0o755)
    # The worker protocol, inside the workspace, for runtimes that read it from
    # disk instead of loading it as a system prompt. Same text for every runtime.
    shutil.copy(ASSETS / "fleet-worker.md", bus / INSTRUCTIONS_FILE)
    return bus


def write_spawn_file(fleet: str, win: str, target: Path, runtime: WorkerRuntime) -> Path:
    """Record the STABLE handle (window UUID) + workspace name. Never surface refs."""
    spawn = target / ".team" / f"{fleet}.spawn.json"
    worker_model = runtime.require_selection().cli_model
    spawn.write_text(
        json.dumps(
            {
                "fleet": fleet,
                "window": win,
                "workspace_name": fleet,
                "cwd": str(target),
                "bus": str(target / ".team" / fleet),
                "roles": ROLES,
                "models": {"manager": MANAGER_MODEL, **{r: worker_model for r in WORKER_ROLES}},
                "manager_runtime": "claude",
                "worker_runtime": runtime.name,
                "layout": str(LAYOUT_FILE),
            },
            indent=2,
        )
        + "\n"
    )
    return spawn


def find_workspace(win: str, name: str) -> str | None:
    """Locate a fleet's workspace by NAME within its window -- refs renumber, names don't."""
    wslist = cmux_json("workspace", "list", "--window", win, "--json") or {}
    for ws in wslist.get("workspaces") or []:
        if ws.get("custom_title") == name:
            return ws.get("ref")
    return None


def close_fleet(fleet: str, target: Path) -> None:
    """Close ONLY this fleet's workspace, located via its spawn file. Never close broadly."""
    spawn = target / ".team" / f"{fleet}.spawn.json"
    if not spawn.exists():
        die(f"no spawn file at {spawn} -- nothing to close")
    meta = json.loads(spawn.read_text())
    win, name = meta["window"], meta["workspace_name"]

    ws = find_workspace(win, name)
    if not ws:
        print(f"fleet '{fleet}' is not open (no workspace named '{name}' in window {win})")
        spawn.unlink()
        return

    cmux("close-workspace", "--workspace", ws, "--window", win)
    spawn.unlink()
    print(f"closed fleet '{fleet}' (workspace {ws})")


def boot_fleet(fleet: str, target: Path, env_file: Path | None,
               runtime: WorkerRuntime, ctx: FleetContext) -> None:
    sessions = {role: runtime.new_session(role, ctx) for role in WORKER_ROLES}
    commands = {role: runtime.launch(role, ctx, sessions[role]) for role in WORKER_ROLES}
    layout = stage_launch_scripts(build_layout(fleet, target, commands), ctx.bus)
    win, created_win, default_ws = find_or_create_window()

    create_args = [
        "workspace", "create", "--window", win, "--name", fleet,
        "--cwd", str(target),
    ]
    if env_file:
        create_args += ["--env-file", str(env_file)]
    create_args += ["--layout", layout, "--focus", "true", "--json"]

    # Run the create EXACTLY ONCE. `workspace create` boots five agents; re-running
    # it to harvest an error string would spawn a second fleet before dying.
    result = cmux(*create_args)
    try:
        created = json.loads(result.stdout.strip()) or {}
    except (json.JSONDecodeError, AttributeError):
        created = {}

    ws = created.get("workspace_ref")
    manager = created.get("surface_ref")
    if not ws or not manager:
        die(
            "failed to create the fleet workspace from the layout.\n"
            f"  cmux said: {(result.stderr or result.stdout or '(nothing)').strip()}"
        )

    cmux("focus-window", "--window", win)
    # Only drop the empty default workspace if WE created the window -- sibling
    # fleets share a window, and closing another one's workspace is unrecoverable.
    if created_win and default_ws and default_ws != ws:
        cmux("close-workspace", "--workspace", default_ws)

    cmux("workspace-action", "--action", "set-color", "--workspace", ws, "--color", "Blue")
    cmux("set-status", "fleet", fleet, "--workspace", ws,
         "--color", "#3B82F6", "--icon", "bolt.fill")

    spawn = write_spawn_file(fleet, win, target, runtime)
    record_boot(runtime, ctx, sessions)  # only once the panes actually exist

    print(f"fleet '{fleet}' up  window={win}  workspace={ws}  manager={manager}")
    print(f"  target : {target}")
    print(f"  bus    : {target / '.team' / fleet}")
    print(f"  spawn  : {spawn}")
    print()
    print(f"Manager: Claude Code (model: {MANAGER_MODEL})")
    for line in runtime.summary_lines():
        print(line)
    for warning in runtime.warnings:
        print(f"  warning: {warning}")
    print()
    print("Give the manager pane a job. Close with:")
    print(f"  {Path(__file__).name} {fleet} --close --cwd {shlex.quote(str(target))}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Boot an agent fleet in cmux: Claude Code manager + 4 Opus workers "
                    "(Claude Code workers by default, Cursor Agent workers with --cwa)."
    )
    parser.add_argument("fleet", help="fleet slug (dash-case); names the workspace")
    parser.add_argument("--cwd", default=os.getcwd(),
                        help="target project the fleet works on (default: cwd)")
    parser.add_argument("--env-file", default=None,
                        help="KEY=VALUE file injected into every pane")
    parser.add_argument("--close", action="store_true",
                        help="close this fleet's workspace instead of booting it")
    parser.add_argument("--cwa", action="store_true",
                        help="Cursor Worker Agents: run the 4 workers in Cursor Agent on Claude Opus "
                             "(the manager stays Claude Code)")
    parser.add_argument("--cursor-model", default=None, metavar="ID",
                        help="with --cwa: exact Cursor model id to use instead of the auto-selected "
                             "newest Claude Opus (must be a Claude model the account lists)")
    args = parser.parse_args(argv)
    if args.cursor_model and not args.cwa:
        parser.error("--cursor-model only applies to Cursor workers (--cwa)")
    args.worker_config = worker_config(args.cwa, args.cursor_model)
    args.runtime = args.worker_config.runtime
    return args


def main() -> None:
    args = parse_args()

    fleet = slugify(args.fleet)
    if not fleet:
        die("usage: spawn_claude_fleet.py <fleet-slug> [--cwd DIR]")

    target = Path(args.cwd).expanduser().resolve()
    if not target.is_dir():
        die(f"--cwd is not a directory: {target}")

    env_file = None
    if args.env_file:
        env_file = Path(args.env_file).expanduser().resolve()
        if not env_file.is_file():
            die(f"--env-file does not exist: {env_file}")

    if not LAYOUT_FILE.is_file():
        die(f"layout not found: {LAYOUT_FILE}")

    os.environ["CMUX_QUIET"] = "1"
    ensure_cmux_running()

    if args.close:
        close_fleet(fleet, target)
        return

    # Resolve the worker runtime ONCE; from here on nothing branches on --cwa.
    runtime = make_runtime(args.worker_config)
    if not shutil.which("claude"):
        die("`claude` is not on PATH -- the manager always runs in Claude Code.")
    ctx = FleetContext(fleet=fleet, target=target, bus=target / ".team" / fleet, assets=ASSETS)
    try:
        runtime.preflight(ctx)  # before touching the bus: a refused runtime leaves no state
    except RuntimeUnavailable as exc:
        die(f"cannot start {runtime.label} workers:\n  {exc}")

    prepare_team_dir(fleet, target)
    boot_fleet(fleet, target, env_file, runtime, ctx)


if __name__ == "__main__":
    main()
