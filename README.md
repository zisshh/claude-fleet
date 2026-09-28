# Claude Fleet — Claude Code manager + 4 Opus workers in cmux

An agent fleet running as real terminal panes inside cmux. The manager is always
Claude Code. The four workers are Claude Opus agents running in **Claude Code**
(default) or in **Cursor Agent** (`--cwa`).

```
┌─────────────────────┬──────────────┬──────────────┐
│                     │  worker-1    │  worker-2    │
│   manager (Fable)   ├──────────────┼──────────────┤
│    Claude Code      │  worker-3    │  worker-4    │
└─────────────────────┴──────────────┴──────────────┘
        left half              2x2 right half
                     (Opus x4 · Claude Code or Cursor Agent)
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
- `claude` on PATH, logged in (the manager always needs it)
- `python3` >= 3.11 — no third-party packages, no venv, nothing to install
- For `--cwa` only: the Cursor CLI (`cursor-agent`) on PATH and logged in
  (`curl https://cursor.com/install -fsS | bash`, then `cursor-agent login`),
  on an account that lists a Claude Opus model

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

# Claude workers (default -- unchanged behavior)
./spawn_claude_fleet.py my-fleet --cwd ~/code/some-project

# Cursor workers: Claude Code manager + 4 Cursor Agent workers on Claude Opus
./spawn_claude_fleet.py my-fleet --cwd ~/code/some-project --cwa
```

The spawner says which it booted:

```
Manager: Claude Code (model: fable)
Workers: Claude Code × 4
Model: Claude Opus (alias 'opus')
Thinking: max (--effort max)
```

```
Manager: Claude Code (model: fable)
Workers: Cursor Agent × 4
Model: Claude Opus 5 1M Max Thinking (claude-opus-5-thinking-max)
Thinking: max -- named by the Cursor variant id claude-opus-5-thinking-max (extended thinking)
```

(The Cursor lines are whatever your account's Cursor CLI actually reports — see
[Cursor workers](#cursor-workers---cwa).) To switch, close the fleet and boot it
again with or without `--cwa`; see [Switching runtimes](#switching-runtimes).

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

## Cursor workers (`--cwa`)

`--cwa` changes only the four worker panes. The manager pane, the layout, the
bus, the dispatch helper, the task protocol and the completion protocol are the
same objects in both modes.

Everything below was verified by reading and exercising the installed CLI
(`cursor-agent 2026.09.26-dd393fe`), not guessed. If your CLI is newer and
behaves differently, the preflight fails loudly rather than approximating.

### Model: Claude Opus, never a silent fallback

Before anything is created, the spawner runs a **preflight**:

1. It runs `cursor-agent --list-models`. Those flat variant ids (e.g.
   `claude-opus-5-thinking-max`, `claude-opus-5-high-fast`) are **exactly** the
   values `--model` accepts.
2. It picks, among ids that say *Opus* and are Claude models: the newest
   version, then non-`fast`, then the highest reasoning level in the id, then
   a `thinking` variant over a plain one. `--cursor-model <id>` pins an exact
   id instead; it must be a Claude model in that list.
3. **No Opus → the boot refuses**, lists the Claude models it did see, and
   nothing is written. It never substitutes Sonnet, GPT, Gemini, Composer or
   `auto`. Not logged in → it tells you to run `cursor-agent login`.

The chosen id is passed verbatim on every worker's command line:
`cursor-agent --model claude-opus-5-thinking-max …`.

### Thinking: what "max" means here, exactly

Cursor encodes reasoning in the variant id itself: `-thinking-` turns on
extended thinking and the level suffix (`low` … `xhigh`, `max`) sets effort.
There is no separate thinking flag.

`cursor-agent acp` also describes models as a base id plus parameters
(`claude-opus-5` with `thinking` and `effort` options), which suggests a
bracket form like `claude-opus-5[effort=max]`. **cursor-agent 2026.09.26
rejects every bracketed id at launch** ("Cannot use this model: …"), so the
fleet never builds one. It only passes an id the CLI itself listed, and
reports the level that id names. An id with no level in its name is reported
as "model default", never as max.

### Subagents

| Subagent kind | Model | How it is enforced |
|---|---|---|
| Custom subagents (`.cursor/agents/*.md`, `.claude/agents/*.md`, `~/.cursor/agents`, plugins) | `inherit` → the worker's Claude Opus | Cursor's default when a file has no `model:`; plugin subagents are always forced to `inherit`. The preflight **warns** about any file that pins a non-inherit model or sets `force-default-model: true` — it does not edit your files. |
| Built-in **Explore** subagent | `inherit` → Claude Opus | Cursor's own default is "Cursor's default Explore model" (not guaranteed Claude). Each worker gets a private `CURSOR_CONFIG_DIR` whose `cli-config.json` is a copy of yours with `subagentModels.explore: "inherit"`. **Your global Cursor config is never modified.** |
| A subagent launched with an explicit per-call model | whatever the worker asks for | **Cannot be enforced by any CLI flag.** Cursor's Task tool accepts an optional `model` argument chosen by the worker model itself. The worker instructions forbid passing one or picking any non-Claude model; that is a prompt-level rule, not a guarantee. |

Cursor team/admin policy is server-side and can override any of this.

### Other Cursor specifics

- **Worker instructions.** Cursor has no public system-prompt flag (its
  `--system-prompt` is hidden and restricted to Cursor's own teams, and
  `--plugin-dir` rules are behind a server-side feature gate). So the spawner
  copies `fleet-worker.md` onto the bus as `worker-instructions.md`, and the
  boot prompt tells the worker to read it and treat it as its system prompt.
  Same text as the Claude workers get, but delivered as a user turn, so after
  heavy compaction a Cursor worker is told to re-read it.
- **Permissions.** Workers run with `--force` (run tools without prompting —
  the analogue of `--dangerously-skip-permissions`), `--trust` and
  `--approve-mcps`, so no dialog blocks the boot. Same risk as the Claude
  workers: not sandboxed.
- **Interrupting.** Cursor stops a turn on one `ctrl+c`; a second one soon
  after exits. `dispatch.py --interrupt` presses the right key exactly once
  (`escape` for Claude Code, `ctrl+c` for Cursor).
- **Dispatch verification** recognizes Cursor's in-progress screen
  (`ctrl+c to stop`, `Thinking…`, `Responding…`) from `<bus>/runtime.json`.

## Switching runtimes

`.team/<fleet>/` is the source of truth; no agent's conversation history is.
Switching is just a restart:

```bash
./spawn_claude_fleet.py my-fleet --close --cwd ~/code/proj
./spawn_claude_fleet.py my-fleet --cwd ~/code/proj --cwa     # Cursor workers now
```

What carries over (the bus survives `--close`):

- `manager.md` and `backlog.md` — the manager's standing instructions and task list,
- `worker-N.md` — each worker's task, progress, files changed, decisions, contracts
  and handoff, kept current *while* working, not just at the end,
- `requests.md` — any cross-worker request nobody routed yet.

What does **not** carry over: a Claude conversation is never converted into a
Cursor one (or back). A worker booting under the other CLI starts a fresh
conversation, reads its own `worker-N.md`, `manager.md` and `backlog.md`, and
reports `ready: worker-N (unfinished: …)` if its notes show work in progress. It
resumes only when the manager re-dispatches it (`Resume T3: read $BUS/worker-2.md
first.`). The manager moves a task between workers the same way.

Runtime-specific bookkeeping lives beside the shared state, never inside it:

| File | Holds |
|---|---|
| `runtime.json` | the current worker runtime, model, thinking setting, TUI signals |
| `sessions.json` | per worker, per runtime: Claude session id / Cursor config dir, and the exact resume command. Kept across switches. |
| `runtime-history.log` | one line per boot: when each runtime ran |
| `cursor/worker-N/` | each Cursor worker's private `CURSOR_CONFIG_DIR` (its `cli-config.json` and chat store) |

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

Workers are launched with a pinned session (`claude --session-id <uuid>`), or,
for Cursor, their own `CURSOR_CONFIG_DIR`, because all five panes share one cwd
and a bare `--continue` could pick up a sibling's session. The exact resume
command for every worker, for every runtime it has run under, is in
`<bus>/sessions.json`:

```bash
jq -r '."worker-2".claude.resume' .team/my-fleet/sessions.json
# cd '/path/proj' && claude --resume <uuid> --model opus --effort max --dangerously-skip-permissions --append-system-prompt-file /path/to/fleet-worker.md

jq -r '."worker-2".cursor.resume' .team/my-fleet/sessions.json
# cd '/path/proj' && env CURSOR_CONFIG_DIR=/path/proj/.team/my-fleet/cursor/worker-2 cursor-agent --model claude-opus-5-thinking-max --force --trust --approve-mcps --workspace /path/proj --continue
```

Resuming private history is a convenience. A fresh worker recovers everything
that matters from the bus.

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

## Files

| File | Role |
|---|---|
| `claude-fleet.layout.json` | Declarative 5-pane layout. `name` fields are the join key. Template: `__FEATURE__`, `__CWD__`, `__ASSETS__`; worker panes carry `__WORKER_COMMAND__`. |
| `spawn_claude_fleet.py` | Boot + teardown. Parses `--cwa`, resolves the worker runtime once, injects it. Zero dependencies. |
| `fleet_runtime.py` | `WorkerRuntime` and its two implementations, `ClaudeWorkerRuntime` and `CursorWorkerRuntime`: launch / resume / send / stop, model + thinking selection, session bookkeeping. The only file that knows which worker CLI is in use. |
| `fleet-manager.md` | Manager system prompt — all cmux mechanics live here. Runtime-agnostic. |
| `fleet-worker.md` | Shared worker protocol — role- and runtime-agnostic; role comes from the boot prompt. System prompt for Claude workers, read from the bus by Cursor workers. |
| `fleet_dispatch.py` | Verified dispatch: send → settle → Enter → confirm on the worker's screen → recover; `--interrupt` for one runtime-correct interrupt. Copied onto each fleet's bus as `dispatch.py` at boot. |
| `tests/` | `python3 -m unittest discover -s tests -t tests` — stdlib only; fake `cmux`, `claude` and `cursor-agent` in `tests/fakes/`. |

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
| `<fleet>/manager.md` | the manager | Standing instructions for all workers: goal, conventions, decisions. |
| `<fleet>/backlog.md` | the manager | Living task list: task, assignee, status, full task text. |
| `<fleet>/<role>.md` | each worker | Task, progress, files changed, decisions, contracts, handoff. Kept current while working. Workers read each other's freely. |
| `<fleet>/<role>.done` | each worker | Completion flag: `<role> \| <summary>`. The manager polls these, with a deadline. |
| `<fleet>/requests.md` | any worker | Cross-worker requests, append-only. The manager drains by **rotating** the file — truncating it would destroy a concurrent append. Survives restarts. |
| `<fleet>/dispatch.py` | the spawner | Verified-dispatch helper; the manager's only sanctioned way to send a task or interrupt a worker. |
| `<fleet>/worker-instructions.md` | the spawner | Copy of `fleet-worker.md` inside the workspace (Cursor workers read it). |
| `<fleet>/launch/<pane>.sh` | the spawner | Each pane's full launch command. The pane itself only runs `sh <script>`: cmux types pane commands before the shell's line editor is up, and macOS drops anything past 1024 bytes of such a line (Enter included), which left long boot commands stuck unrun at the prompt. Rewritten on every boot. |
| `<fleet>/runtime.json`, `sessions.json`, `runtime-history.log`, `cursor/` | the spawner | Worker-runtime bookkeeping — see [Switching runtimes](#switching-runtimes). |
| `<fleet>.spawn.json` | the spawner | Window handle + workspace name + manager/worker runtime. **No surface refs** — they renumber. |

A restart clears only the `.done` flags (a stale flag would read as an instant
completion). Everything else on the bus is fleet context and is kept.

## Design decisions worth knowing

**Boot is one call, not five.** `cmux workspace create --layout` carries each
pane's `command`, so cmux launches all five agents itself. No send/send-key boot
loop, no `sleep` gate. Workers boot at their runtime's deepest reasoning
(`--effort max` for Claude Code; the highest-level listed Opus variant for
Cursor); the manager stays on Fable at default effort — its job is routing and
synthesis, and its context budget is the fleet's scarcest resource. `--cwa`
never changes that.

**The worker CLI is a pluggable runtime.** `fleet_runtime.py` defines
`WorkerRuntime` (`launch`, `resume`, `send`, `stop`, plus a preflight that
resolves the model). `--cwa` is interpreted in exactly one place
(`worker_config()`); `make_runtime()` returns `ClaudeWorkerRuntime` or
`CursorWorkerRuntime`, and the spawner uses whichever it gets. Adding another
worker CLI is one new subclass.

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

**`--cwa` / runtime abstraction (this change).** What was checked, and how:

- *Automated* (`tests/`, 55 tests, stdlib only): CLI parsing, runtime
  selection, both runtimes' launch/resume/send/stop, the Cursor model probe
  against a fake `cursor-agent` that speaks ACP (newest Opus picked, max
  effort, explicit/non-Claude/unknown models, no-Opus refusal, not-logged-in,
  old-CLI fallback, boolean thinking), subagent-file warnings, per-worker
  Cursor config, prompt generation, bus paths, completion signaling and
  runtime-aware dispatch against a fake `cmux`. An end-to-end test runs the
  real spawner in both modes against fake `cmux`/`claude`/`cursor-agent`,
  executes every generated pane command in a real `sh` (target path with a
  space and an apostrophe), writes the bus as manager and worker would,
  switches Claude → Cursor → Claude, moves a task between workers, and checks
  that the manager command is byte-identical in both modes.
- *Live, real Cursor CLI* (`2026.09.26-dd393fe`, not logged in): the flags,
  the bracket-parameter syntax, the silent "healing" of invalid parameters,
  `CURSOR_CONFIG_DIR`, the Explore-model setting, subagent `model: inherit`
  and the TUI's working/interrupt strings were read from the CLI itself; the
  ACP probe was run against it and correctly refused with "not logged in".
- *Live, real Claude Code* (`2.1.283`, print mode, since there is no cmux
  here): a worker launched with the generated flags booted, read the bus and
  answered `ready: worker-1`; resumed through the `sessions.json` command it
  did a real task and wrote notes → `.done` → sentinel in the new schema; a
  brand-new worker-3 session with no history then took the task over purely
  from worker-1's notes and finished it.

**Not yet proven live:** the full fleet in cmux (needs macOS), and a
**logged-in** Cursor account actually serving Claude Opus at the selected
effort — the preflight will show you exactly what it selected the first time
you run `--cwa`. Cursor's TUI working strings come from its source, not from a
live screen read, so the first live `--cwa` dispatch is the first real test of
`dispatch.py`'s Cursor signals (it falls back safely to the Enter-ladder if
they are wrong).

**Watch the manager's context.** It carries the cmux mechanics, the roster, the bus
paths, and every worker's returned state. The manager compacting mid-run — and
losing its roster or its in-flight task table — is the most likely first failure.
