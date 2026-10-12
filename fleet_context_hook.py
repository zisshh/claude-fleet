#!/usr/bin/env python3
"""SessionStart hook (sources: clear, compact): re-anchor a fleet agent.

A pane's role and bus path arrive in its boot prompt, which is ordinary
conversation -- /clear wipes it and compaction may summarise it away. The
system-prompt file survives both, but it only says "your boot prompt told you
your role". This hook closes that gap: it prints the role, the bus, and the
agent's own state files, and Claude Code adds stdout to the fresh context.

Identity comes from FLEET_ROLE, set per pane by the layout. Outside a fleet
pane FLEET_ROLE is unset and the hook prints nothing, so it is safe to install
globally. The bus is re-derived from the spawn record in the cwd (the target
project), never cached.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

MAX_FILE_CHARS = 12_000  # keep re-injected state well under the hook output cap


def find_bus(cwd: Path) -> Path | None:
    for record in sorted((cwd / ".team").glob("*.spawn.json")):
        try:
            bus = json.loads(record.read_text()).get("bus")
        except (OSError, ValueError):
            continue
        if bus and Path(bus).is_dir():
            return Path(bus)
    return None


def section(title: str, path: Path) -> str:
    if not path.is_file() or path.stat().st_size == 0:
        return f"## {title}\n(no {path.name} yet)\n"
    text = path.read_text(errors="replace")
    if len(text) > MAX_FILE_CHARS:
        text = text[:MAX_FILE_CHARS] + "\n...[truncated -- read the file for the rest]"
    return f"## {title} ({path})\n{text}\n"


def worker_context(role: str, bus: Path) -> list[str]:
    return [
        f"You are {role}. Your bus is {bus}. Your files: {bus}/{role}.md, "
        f"{bus}/{role}.progress.md, {bus}/{role}.done. Follow your fleet-worker system prompt.",
        "If progress.md shows an unfinished task, resume it from its 'Next step' -- do not restart "
        "it and do not wait for a re-dispatch. If there is no unfinished task, wait for the manager.",
        section("Your in-flight task checkpoint", bus / f"{role}.progress.md"),
        section("Your last completed notes", bus / f"{role}.md"),
    ]


def manager_context(bus: Path) -> list[str]:
    return [
        f"You are the MANAGER. Your bus is {bus}. Re-resolve worker refs now with "
        "`cmux identify --json` then `cmux tree --workspace <ws> --json` -- never reuse old refs.",
        "Your in-flight table in backlog.md is the truth about who is doing what. Resume polling "
        "any worker listed as in-flight; do not re-dispatch work that is already running.",
        section("Backlog and in-flight table", bus / "backlog.md"),
    ]


def main() -> None:
    role = os.environ.get("FLEET_ROLE", "").strip()
    if not role:
        return
    try:
        source = json.load(sys.stdin).get("source", "")
    except ValueError:
        source = ""
    bus = find_bus(Path.cwd())
    if bus is None:
        print(f"[fleet] You are {role}, but no .team/*.spawn.json was found in {Path.cwd()}. "
              "Ask the human for your bus path before doing anything.")
        return
    body = manager_context(bus) if role == "manager" else worker_context(role, bus)
    print(f"[fleet] Context was reset ({source or 'clear/compact'}). Re-anchoring.\n")
    print("\n".join(body))


if __name__ == "__main__":
    main()
