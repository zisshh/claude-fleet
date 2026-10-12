# Claude Fleet — Fable manager + 4 Opus workers in cmux

A Claude-only agent fleet running as real terminal panes inside cmux.

```
┌─────────────────────┬──────────────┬──────────────┐
│                     │  worker-1    │  worker-2    │
│   manager (Fable)   ├──────────────┼──────────────┤
│                     │  worker-3    │  worker-4    │
└─────────────────────┴──────────────┴──────────────┘
        left half              2x2 right half
                               (Opus x4)
```

The manager is the only agent that knows the fleet exists. It dispatches work to
the four workers over cmux, polls for their completion flags, and integrates the
results. Workers never touch cmux and never address each other directly — they
publish notes to `.team/` and file requests the manager routes.

Ported from [disler/learning-cmux-with-agents](https://github.com/disler/learning-cmux-with-agents) (MIT) — his
`fs-team` geometry and `spawn_fast.py` boot algorithm, retargeted from a
pi/GLM/Minimax team to a Claude-only one.

## ⚠️ Read this first

The five agents run with `--dangerously-skip-permissions`. **They are not
sandboxed.** Each one can read and write anything your user account can — not
just the project you point it at. That is a deliberate trade (see
[Design decisions](#design-decisions-worth-knowing)), but it is your risk to
accept, not a default to inherit unread.

Point this at a target you are willing to have four unsupervised Opus agents
touch. If you want a real boundary, run it in a VM, a container, or a throwaway
clone.

## Requirements

- macOS + [cmux](https://cmux.com) (developed against `0.64.17`)
- `claude` on PATH, logged in
- `python3` >= 3.11 — no third-party packages, no venv, nothing to install

## Install

Clone it anywhere. The spawner derives its own location, so the assets travel
with it:

```bash
git clone <this-repo> ~/.claude/cmux    # or wherever you like
chmod +x ~/.claude/cmux/spawn_claude_fleet.py
```

## Usage

**Run the spawner from inside a cmux terminal.** cmux's
`automation.socketControlMode` defaults to `cmuxOnly`, which only allows
processes running inside cmux to drive the socket. The manager pane is inside,
so it is fine; the spawner is what needs a cmux terminal.

```bash
# in a cmux terminal:
./spawn_claude_fleet.py my-fleet --cwd ~/code/some-project
```

Paths with spaces or quotes in them are fine — quote them at your shell like
any other argument:

```bash
./spawn_claude_fleet.py vault-fleet --cwd "$HOME/Notes/My Second Brain/plugin-project"
```

A leading `~` works even inside quotes — the spawner expands it itself. Inside
the panes, the target path reaches every agent wrapped in visible double
quotes, so spaces and apostrophes survive end to end.

Then talk to the manager pane. Give it a job in plain English; it decomposes,
dispatches to the four workers in parallel, and reports back.

Tear down:

```bash
./spawn_claude_fleet.py my-fleet --close --cwd ~/code/some-project
```

This closes only the fleet's own workspace, located by the stable window UUID
recorded in its spawn file. It never loops a close over the tree.

If you would rather spawn from a normal terminal, raise the socket mode in
`~/.config/cmux/cmux.json` (back it up first) and `cmux reload-config`:

```jsonc
{ "automation": { "socketControlMode": "allowAll" } }   // or "password" + socketPassword
```

Note that `allowAll` lets any local process drive your terminals and read screen
contents.

## Resuming

Agent identity lives in **launch flags, not in the session**.
`--append-system-prompt-file` and `--effort` are process-level configuration: a
session resumed without them keeps its conversation memory but loses the pinned
fleet mechanics, and after enough compaction a bare-resumed manager slowly stops
being a manager.

So resume with the same flags the layout used. In the manager's pane (its cwd is
the target project, and sessions are per-directory, so `--continue` finds the
right one):

```bash
claude --continue --append-system-prompt-file /path/to/fleet-manager.md
```

A worker, likewise:

```bash
claude --continue --effort max --append-system-prompt-file /path/to/fleet-worker.md
```

Three things make a resumed manager viable by design:

- The in-TUI `/resume` picker keeps the running process's launch flags — that
  path is always safe. Only a *relaunch* can drop flags.
- Worker refs are never cached — every dispatch re-resolves them via
  `cmux tree`, so refs that renumbered while the manager was down cost nothing.
- The bus path is re-derivable from disk: `.team/<fleet>.spawn.json` in the
  manager's working directory carries the `bus` field, and the manager prompt
  tells it so.

Workers catch up from their own `<bus>/<role>.md` notes after a restart — that
file surviving process death is why the protocol writes it.

## Context management: /clear, compaction, re-anchoring

Files are the memory; sessions are disposable.

- **Identity survives resets.** Each pane command starts with
  `FLEET_ROLE=<role>`. That is process environment, so `/clear` and compaction
  cannot erase it, unlike the boot prompt.
- **The re-anchor hook.** `fleet_context_hook.py` is a SessionStart hook with
  matcher `clear|compact`. When `FLEET_ROLE` is set, it finds the bus through
  `.team/*.spawn.json` and prints the agent's state into the fresh context:
  - workers get their role, bus, `<role>.progress.md` and `<role>.md`
  - the manager gets its role, bus and `backlog.md`

  Outside a fleet pane it prints nothing, so it is safe to register globally:

  ```json
  "SessionStart": [{ "matcher": "clear|compact", "hooks": [{ "type": "command",
    "command": "python3 ~/.claude/cmux/fleet_context_hook.py", "timeout": 5 }] }]
  ```
- **Mid-task compaction is survivable.** Workers rewrite
  `<role>.progress.md` (task, done so far, decisions, next step) after each
  meaningful step and delete it on completion. A compacted worker resumes from
  "Next step" instead of restarting the task.
- **The manager clears workers only at task boundaries.** It clears after
  `.done` lands and is verified, when the worker is near the threshold or the
  next task is unrelated. It never clears mid-task. The next dispatch names the
  files to read (`<role>.md` "State for next task", other workers' Contracts),
  so dependent work costs a few thousand tokens of reading, not hundreds of
  thousands of carried context.
- **The manager's memory is `backlog.md`.** Its `## In flight` table is
  rewritten on every dispatch and completion, and re-injected after the
  manager's own compaction.

## Files

| File | Role |
|---|---|
| `claude-fleet.layout.json` | Declarative 5-pane layout. `name` fields are the join key. Template: `__FEATURE__`, `__CWD__`, `__ASSETS__`. |
| `spawn_claude_fleet.py` | Boot + teardown. Zero dependencies. |
| `fleet-manager.md` | Manager system prompt — all cmux mechanics live here. |
| `fleet-worker.md` | Shared worker system prompt — role-agnostic; role comes from the boot prompt. |
| `fleet_dispatch.py` | Verified dispatch: send → settle → Enter → confirm on the worker's screen → recover. Copied onto each fleet's bus as `dispatch.py` at boot. |
| `fleet_context_hook.py` | SessionStart (`clear\|compact`) hook: re-injects role, bus and state files after a context reset. No-op outside fleet panes. |

These are system-prompt *files* passed via `--append-system-prompt-file`, not
Claude Code subagents. Keep them out of `~/.claude/agents/` — filing them there
would register two bogus subagent types for no benefit.

Note `--append-system-prompt-file` is undocumented (it is absent from
`claude --help`) but works. Verified on Claude Code as of July 2026.

## `.team/<fleet>/` — the shared bus

Created in the **target project**, not here. Namespaced per fleet, so two fleets
pointed at one target cannot clobber each other's flags. Self-ignoring via
`.team/.gitignore`, so it will not pollute a repo.

| File | Written by | Purpose |
|---|---|---|
| `<fleet>/<role>.md` | each worker | Notes, decisions, contracts. Workers read each other's freely. |
| `<fleet>/<role>.done` | each worker | Completion flag: `<role> \| <summary>`. The manager polls these, with a deadline. |
| `<fleet>/requests.md` | any worker | Cross-worker requests, append-only. The manager drains by **rotating** the file — truncating it would destroy a concurrent append. |
| `<fleet>/backlog.md` | the manager | Living task list. |
| `<fleet>/dispatch.py` | the spawner | Verified-dispatch helper; the manager's only sanctioned way to send a task. |
| `<fleet>.spawn.json` | the spawner | Window handle + workspace name. **No surface refs** — they renumber. |

## Design decisions worth knowing

**Boot is one call, not five.** `cmux workspace create --layout` carries each
pane's `command`, so cmux launches all five agents itself. No send/send-key boot
loop, no `sleep` gate. Workers boot at `--effort max` (deepest reasoning); the
manager stays on default effort — its job is routing and synthesis, and its
context budget is the fleet's scarcest resource.

**There is no roster file.** cmux short refs (`surface:3`) are positional and
renumber as surfaces open and close, so a cached ref goes stale silently. The
manager rediscovers refs at time of use by mapping the layout's `name` fields:

```bash
cmux identify --json                  # -> my workspace ref
cmux tree --workspace <ws> --json     # -> panes AND surfaces; map name -> live ref
```

Only the window UUID — the one stable handle — is persisted.

Note `cmux list-pane-surfaces` is **not** usable here despite its name: it is
scoped to a single pane and defaults to the focused one, so it cannot enumerate
sibling panes. `cmux tree` is the pane-spanning command.

**Completion is a file, not a screen.** Workers write `<bus>/<role>.done`; the
manager polls for it with a deadline. The upstream repo instead greps the worker's
scrollback for a printed sentinel — proven for `pi`, but its workers are not
Claude Code.

First boot showed `read-screen` against a Claude TUI is better than expected: the
manager read a worker's screen to confirm a dispatch and cleared a dialog on all
four panes. So scraping is viable for *looking*. It is still the wrong control
channel: the TUI redraws, wraps, and repaints, so a sentinel can smear across
lines or scroll out of the viewport, and a missed sentinel is indistinguishable
from a worker still thinking. A file either exists or does not. Detection stays on
files; `read-screen` stays the diagnostic for a silent worker — which is exactly
what the polling deadline hands it.

**Dispatch is verified, not fire-and-forget.** `cmux send` types a task as one
rapid burst, which the worker's Claude TUI treats like a paste — and an Enter
arriving inside that coalescing window is absorbed as a newline instead of a
submit. The task parks in the worker's input bar; the manager waits forever on a
worker that was never asked. This failed live, on most dispatches, once worker
sessions grew heavy. So the manager dispatches only through `dispatch.py`, which
sends, waits out the paste window, presses Enter, then reads the worker's screen
to confirm submission — pressing Enter again if the text is parked (harmless if
it wasn't), and clearing the bar with a single `ctrl+c` before retyping at most
once. The one thing it never does is blindly resend into a non-empty input bar,
which would double the task.

**Workers cooperate through files.** A worker that needs something from another
appends one line to `<bus>/requests.md` and keeps going, rather than blocking.
The manager drains that file each poll and dispatches accordingly. This gives
cross-worker data flow without four agents prompting each other with no
scheduler.

**Workers run with `--dangerously-skip-permissions`, and they are not sandboxed.**
Without the flag, Claude Code declines the work, prints instructions, and ends
its turn — while still looking, from the outside, like a completed turn. With it,
all permission checks are bypassed.

Be clear-eyed about what that means: **each of the five agents can read and write
anything your user account can** — not just the target project. `--add-dir` does
not bound this; it is an additive allow-list that this flag ignores outright, so
it was removed rather than left in as false reassurance. The `--cwd` target is a
convention that tells the agents where to work; it is not a boundary.

Point this fleet at a target you are willing to have four unsupervised Opus
agents touch. If you want a real boundary, run it in a VM, a container, or a
throwaway clone — not in your home directory.

## The one rule that breaks everything

`cmux send` submits a separate prompt on **every newline**. A multi-line task is
N half-finished turns, not one task. Every dispatch is one single-line `send`
followed by one `send-key enter`. If a task is too big for one line, it is too
big for one task — split it.

## Verification status

**Boot path: proven.** First end-to-end boot succeeded against a real project on
cmux 0.64.17. Confirmed working:

- Five Claude Code TUIs boot from a single `workspace create --layout` call —
  manager on Fable, four workers on Opus, no per-pane driving.
- The manager discovers its own workers: `cmux identify --json` → `cmux tree
  --workspace <ws> --json`, mapping each layout `name` to a live surface ref.
- Workers boot with the correct role and bus path, and answer `ready: worker-N`.
- The bus is created at `.team/<fleet>/` with `requests.md` present.
- Dispatch lands: `cmux send` + `send-key enter` reaches a worker and executes.
- `read-screen` is usable for verification — the manager read a worker's screen to
  confirm a dispatch arrived, and cleared a confirmation dialog on all four panes.

**The refs came back out of order** — `worker-1 → surface:5`, `worker-2 →
surface:4`, `worker-3 → surface:3`, `worker-4 → surface:6`. Non-contiguous and not
in role order, on the very first boot. Any cached or assumed roster would have
mapped workers to the wrong panes. Rediscover every dispatch; this is not
theoretical.

**Work loop: proven on a plumbing task.** All four workers were dispatched a
trivial task (`date`) in parallel and all four reported back. Confirmed:

- The completion protocol end-to-end: `<role>.md` → `<role>.done` → the manager
  polling and reading it. All four flags landed in the documented format
  (`worker-N | test ok`).
- **Write ordering held.** Every `<role>.md` was written before its `<role>.done`,
  so the flag never appeared before the notes it announces.
- **Dispatch really is parallel.** Worker timestamps landed 2s apart
  (`16:39:43` … `16:39:45`) — four Opus agents working concurrently, not serially.
- **The output schema is followed.** Workers produced the mandated sections and
  cited the real command and its real output under **Verification** (`date` →
  `Wed Jul 15 16:39:43 IST 2026`), not an assertion of success.
- **Scope discipline held unprompted** — workers noted "no other work, plumbing
  test only" rather than improvising extra tasks.
- **The window handle is a real UUID** (`5EF07914-…`), so `--id-format uuids`
  works and `--close` will still find the fleet after refs renumber.

**Still unproven.** The task above was `date` — it exercises the plumbing, not the
work. Open:

- A substantive task: real edits, real verification, a worker that has to think.
- `requests.md` rotation under concurrent appends. No cross-worker request has been
  filed yet; the file is still empty.
- A `BLOCKED:` path — no worker has failed yet.
- The polling deadline firing on a genuinely dead worker.
- `--close` teardown against a live fleet.
- A live boot on a target whose path contains spaces or apostrophes. The
  original pane-command quoting broke on exactly such a path; the fix is
  verified against a real shell parse (`sh -c` argv dump) across four path
  shapes, but the first live boot on one is pending.
- The dispatch helper against live TUIs. The parked-input failure it fixes was
  observed live (by the end, on most dispatches), and the recovery ladder is
  tested against a stubbed cmux simulating four worker states — instantly
  working, parked-then-recovers, parked-forever, fast-finish — but its first
  live run is pending.

**Watch the manager's context.** It carries the cmux mechanics, the roster, the bus
paths, and every worker's returned state. The manager compacting mid-run — and
losing its roster or its in-flight task table — is the most likely first failure.
Mitigated by the `## In flight` table in `backlog.md` plus the re-anchor hook.
The hook's output has been tested offline, but not yet on a live compaction.
