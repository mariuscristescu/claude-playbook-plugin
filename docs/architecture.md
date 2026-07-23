# Architecture

How the plugin is put together, and how the enforcement actually works.

## Layout

The plugin (`plugins/playbook/` in this repo) has four user-visible parts plus two engine directories:

- `commands/` — seven `/playbook:*` slash commands (markdown the agent executes as instructions).
- `skills/` — six skill bundles loaded at `tasks bootstrap` (playbook patterns, judge, monitor, merge, stack, task template).
- `hooks/hooks.json` — six lifecycle hook registrations (below).
- `scripts/` — executable entry points: the `tasks` dispatcher, hook scripts, `sandbox`, `monitor`, `init`, the `playbook-*` provider launchers.
- `tasks/` — the Python package behind the `tasks` CLI (dispatcher sets `PYTHONPATH` here).
- `provider/` — provider adapters and judge-dispatch machinery ([providers](providers.md)).

Everything is plain files — bash entry points, Python 3 stdlib, markdown as the runtime language. No build step, no dependencies.

## Hooks & enforcement

Six hooks enforce the structure at the OS level, because warnings don't stick — blocking does:

| Hook | What it does |
|---|---|
| `SessionStart` | Runs bootstrap orientation (mind map + pending tasks + CLI reference). |
| `PreToolUse` (matcher `Edit\|Write\|search_replace\|write\|Bash\|Shell\|StrReplace\|run_terminal_command`) | The **task gate**: BLOCKS code edits when no task is active. Grok names (`write`, `search_replace`, `run_terminal_command`, `Shell`, `StrReplace`) map to Claude Edit/Write/Bash via the normalizer — same gate, every provider. |
| `UserPromptSubmit` | Appends every user message to `.agent/chat_log.md` (timestamped, agent-tagged — feeds task attribution and `tasks log`). |
| `PostToolUse` | Echoes gate state after every tool call, keeping the current gate in the agent's face. |
| `Stop` / `SessionEnd` | Finalize session state. |

`/playbook:init` additionally writes a **deny-list** into the project's `.claude/settings.json` blocking `TodoWrite`, `Task`, and `EnterPlanMode` — those would compete with task.md as the source of truth. If those tools suddenly error in a playbook project, that's why.

A `bash-log` shell integration records commands into `.agent/bash_history`, so terminal work is auditable alongside the chat log.

## Task system

A task is a directory under `.agent/tasks/<N>-<type>-<name>/` whose `task.md` is both the plan and the execution trace: Design Phase gates (understand → structure → reflect → verify) → judge review → Work Plan gates → implementation review → pre-review. State lives on disk, keyed by a PID-based session ID that works across providers — which is why tasks survive context compaction and session restarts, and why two agents can hand a task off through the file alone.

## The monitor

A second Claude process that watches the front agent's session transcript incrementally and posts nudges through a hook when the trajectory goes wrong. Separate context window — it judges from outside, without the front agent's anchoring. Components: `.claude/bin/monitor` (wrapper), the plugin's `scripts/monitor` + `monitor-lib/`, and per-project rules under `.agent/monitor/` (scaffolded by init).

## The sandbox

`.claude/bin/sandbox` runs the agent with `--dangerously-skip-permissions` inside OS-level containment: **macOS seatbelt** or **Linux bubblewrap** with deny-write-by-default. The project directory is writable, `.git` is read-only (history can't be mangled), everything outside is blocked at the kernel level. One honest caveat: when no containment primitive is available (neither `sandbox-exec` nor `bwrap`, or nesting inside a foreign sandbox forbids it), the agent currently runs with bypass flags and **no** kernel containment — check your platform has one of the two before relying on the blast-radius guarantee. Pairs with the task system for the "two agents, one task" pattern: orchestrator outside, worker inside, task.md as the handoff.

## Judges

Blind by construction: a judge gets the repo but not your conversation, so it can't anchor to whatever was already agreed in chat. Single judge (`plan-review` / `impl-review`) writes findings into the task.md; the panel (`panel-review`) fans out to every seat in `models.json` in parallel and writes `judge.md`. Judge output is triaged, not obeyed — the task template's review gates require an accept/park/reject decision per finding.

Since v1.4.3 the judge process is also read-only: sandboxed with the project mounted no-write, so a judge physically cannot edit the repo or task.md. Because that OS containment is unavailable on some platforms (Windows, nested sandboxes), a working-tree tamper guard backs it up — the review paths snapshot git status + the task.md hash before and after, and if a judge changed anything the review is saved with a loud TAMPER banner, ingestion is refused, and the run exits non-zero.

## Tests

`tests/` — stdlib-unittest suites (no external deps), one file per subsystem: invocation contracts for agy/grok, model-availability machinery, config resolution, mind-map sorting, merge ref-integrity, README-drift detection. Run any file directly: `python3 tests/test_<name>.py`.
