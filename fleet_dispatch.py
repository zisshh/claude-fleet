#!/usr/bin/env python3
"""Deliver one task to one fleet worker, and VERIFY it actually submitted.

Why this exists: `cmux send` types the task as one rapid burst, which a
worker TUI (Claude Code or Cursor Agent) treats like a paste. If the follow-up `send-key enter` lands
inside that paste-coalescing window, the Enter is absorbed as a newline and
the whole task sits parked in the input bar while the manager waits on a
worker that was never asked. This script owns the full ritual:

    [clear stale done-flag] -> send text -> settle -> enter -> verify
      -> if parked: enter again (harmless if actually submitted)
      -> if still parked: ctrl+c (clears the bar), retype once, enter, verify
      -> if STILL parked: exit 1 so the manager goes and looks

Recovery-ladder safety, in order of what each rung risks:
  * extra Enter on an empty input bar: no-op. Always safe.
  * single ctrl+c: clears typed text; never exits the app on one press.
  * retype: the ONLY dangerous rung (a double-send if we misread the screen),
    so it is gated behind two failed Enter rungs plus an explicit bar-clear,
    and it happens at most once.

Verification signals, checked in this order each round:
  1. done-file exists (worker already finished a very fast task) -> delivered
  2. the worker TUI's "turn in progress" text on screen           -> delivered
     (Claude Code: "esc to interrupt"; Cursor Agent: "ctrl+c to stop",
     "Thinking…", "Responding…" -- read from <bus>/runtime.json)
  3. task fragment visible in the input-bar region (bottom lines) -> parked
  4. none of the above -> assume delivered (one harmless Enter fired first)

Which TUI the workers run is recorded by the spawner in runtime.json beside
this script on the bus. The manager never needs to know; this script adapts.
A bus without runtime.json (older fleets) is treated as Claude Code.

Usage:
    fleet_dispatch.py --surface surface:5 --clear /path/bus/worker-1.done \
        "one single-line task ..."
    fleet_dispatch.py --surface surface:5 --interrupt   # stop the current turn (one keypress)

Exit codes: 0 delivered (or confidently assumed), 1 could not deliver,
2 bad invocation.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

BOX_CHARS = "│╭╮╰╯─┌┐└┘║╔╗╚╝>"

# Claude Code defaults: what a bus without runtime.json (pre --cwa fleets) means.
DEFAULT_PROFILE = {
    "runtime": "claude",
    "working_signals": ["esc to interrupt"],
    "interrupt_key": "escape",
    "clear_key": "ctrl+c",
}


def load_profile(bus: Path) -> dict:
    """The worker runtime's TUI dialect, as recorded by the spawner."""
    profile = dict(DEFAULT_PROFILE)
    try:
        data = json.loads((bus / "runtime.json").read_text())
    except (OSError, json.JSONDecodeError):
        return profile
    for key in DEFAULT_PROFILE:
        if data.get(key):
            profile[key] = data[key]
    profile["working_signals"] = [s.lower() for s in profile["working_signals"]]
    return profile


PROFILE = load_profile(Path(__file__).resolve().parent)


def sh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True)


def normalize(text: str) -> str:
    """Strip box-drawing noise and collapse whitespace so wrapped TUI text
    can be substring-matched against the original task."""
    for ch in BOX_CHARS:
        text = text.replace(ch, " ")
    return re.sub(r"\s+", " ", text).strip().lower()


def screen_tail(surface: str, lines: int) -> str:
    r = sh("cmux", "read-screen", "--surface", surface)
    if r.returncode != 0:
        return ""
    return "\n".join(r.stdout.splitlines()[-lines:])


def looks_parked(surface: str, task: str) -> bool:
    """Is the task text sitting in the input-bar region (bottom of viewport)?"""
    tail = normalize(screen_tail(surface, 6))
    if not tail:
        return False
    norm = normalize(task)
    head, tail_frag = norm[:40], norm[-40:]
    return (head and head in tail) or (tail_frag and tail_frag in tail)


def looks_working(surface: str) -> bool:
    screen = normalize(screen_tail(surface, 14))
    return any(signal in screen for signal in PROFILE["working_signals"])


def delivered(args, why: str) -> None:
    print(f"delivered ({why})")
    sys.exit(0)


def main() -> None:
    ap = argparse.ArgumentParser(description="Send one task to one worker, verified.")
    ap.add_argument("--surface", required=True, help="worker surface ref (resolve via cmux tree first)")
    ap.add_argument("--clear", default=None, help="done-flag path to clear before dispatch (strongly recommended)")
    ap.add_argument("--settle", type=float, default=0.8, help="seconds between text and Enter")
    ap.add_argument("--confirm-wait", type=float, default=1.5, help="seconds before each verification read")
    ap.add_argument("--interrupt", action="store_true",
                    help="stop the worker's current turn with ONE press of its runtime's interrupt key")
    ap.add_argument("task", nargs="?", default="", help="the task, ONE line")
    args = ap.parse_args()

    if args.interrupt:
        # Exactly one press. Both TUIs exit on a quick second ctrl+c, and a
        # closed pane cannot be respawned.
        r = sh("cmux", "send-key", "--surface", args.surface, PROFILE["interrupt_key"])
        if r.returncode != 0:
            print(f"cmux send-key failed: {(r.stderr or r.stdout).strip()}", file=sys.stderr)
            sys.exit(1)
        print(f"interrupted ({PROFILE['interrupt_key']})")
        sys.exit(0)

    task = args.task.strip()
    if not task:
        print("empty task", file=sys.stderr)
        sys.exit(2)
    if "\n" in task or "\r" in task:
        print("task contains a newline; a multi-line send fires broken partial turns. "
              "One line per task -- if it does not fit, it is two tasks.", file=sys.stderr)
        sys.exit(2)
    if not args.clear:
        print("warning: no --clear given; fast-finish detection is off", file=sys.stderr)

    done = Path(args.clear) if args.clear else None
    if done:
        done.unlink(missing_ok=True)

    r = sh("cmux", "send", "--surface", args.surface, task)
    if r.returncode != 0:
        print(f"cmux send failed: {(r.stderr or r.stdout).strip()}", file=sys.stderr)
        sys.exit(1)
    time.sleep(args.settle)
    sh("cmux", "send-key", "--surface", args.surface, "enter")

    # Verification rounds: first Enter, then up to two recovery Enters.
    for attempt in range(3):
        time.sleep(args.confirm_wait)
        if done and done.exists():
            delivered(args, "done-flag appeared")
        if looks_working(args.surface):
            delivered(args, "worker is working")
        if not looks_parked(args.surface, task):
            if attempt == 0:
                # No parked text and no working signal yet -- one harmless
                # Enter in case the first was swallowed, then re-verify.
                sh("cmux", "send-key", "--surface", args.surface, "enter")
                continue
            delivered(args, "no parked text after re-check")
        # Task text is visibly parked in the bar: press Enter again.
        print(f"attempt {attempt + 1}: task parked in input bar, pressing Enter again", file=sys.stderr)
        sh("cmux", "send-key", "--surface", args.surface, "enter")

    # Enters alone did not do it. Clear the bar and retype ONCE.
    time.sleep(args.confirm_wait)
    if looks_working(args.surface) or (done and done.exists()):
        delivered(args, "late confirmation")
    if looks_parked(args.surface, task):
        print(f"retype: clearing input bar (single {PROFILE['clear_key']}) and resending once", file=sys.stderr)
        sh("cmux", "send-key", "--surface", args.surface, PROFILE["clear_key"])
        time.sleep(0.5)
        sh("cmux", "send", "--surface", args.surface, task)
        time.sleep(max(args.settle, 1.5))  # wider settle: the fast path already failed once
        sh("cmux", "send-key", "--surface", args.surface, "enter")
        time.sleep(args.confirm_wait)
        if looks_working(args.surface) or not looks_parked(args.surface, task):
            delivered(args, "after retype")
        print("could not deliver: task still parked after retype. "
              "read-screen the worker and intervene by hand.", file=sys.stderr)
        sys.exit(1)
    delivered(args, "no parked text after recovery rounds")


if __name__ == "__main__":
    main()
