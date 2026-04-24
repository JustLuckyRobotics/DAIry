# DAIry

DAIry is a tiny local project-memory kit for Codex-style AI coding sessions.

It gives each project two memory layers:

- `.dairy/current.md`: a compact current-state summary that a new session can read immediately.
- `.dairy/history.jsonl`: an append-only long-term diary that should not be opened unless the current summary is missing or unusable.

The goal is practical: move between OpenAI accounts, machines, or sessions without spending a pile of tokens re-explaining the project every time.

## Status

This is intentionally small and local-first. It does not upload anything, call an API, run a daemon, or use a database.

## Install

If your Python environment allows normal installs:

```bash
cd /home/jake/acuity_misc/DAIry
python3 -m pip install -e .
dairy --help
```

Some systems, including many ROS2 setups, use an externally managed Python environment. If `pip` refuses with an externally-managed-environment error, do not use `--break-system-packages`. Use one of the options below.

### Recommended For ROS2: Dedicated DAIry Venv

This keeps ROS2 and system Python untouched while still giving you a reusable `dairy` command.

```bash
cd /home/jake/acuity_misc/DAIry
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e .
```

Run it directly:

```bash
/home/jake/acuity_misc/DAIry/.venv/bin/dairy --help
```

Optional shell alias:

```bash
echo 'alias dairy="/home/jake/acuity_misc/DAIry/.venv/bin/dairy"' >> ~/.bashrc
source ~/.bashrc
dairy --help
```

### Alternative: pipx

`pipx` also avoids touching system Python:

```bash
sudo apt install pipx
pipx ensurepath
pipx install -e /home/jake/acuity_misc/DAIry
dairy --help
```

Restart the shell if `dairy` is not found immediately after `pipx ensurepath`.

### No Install: PYTHONPATH

This works without installing anything:

```bash
PYTHONPATH=/home/jake/acuity_misc/DAIry/src python3 -m dairy --help
```

Use the same prefix for commands:

```bash
PYTHONPATH=/home/jake/acuity_misc/DAIry/src python3 -m dairy init .
PYTHONPATH=/home/jake/acuity_misc/DAIry/src python3 -m dairy start
```

## Install For Development

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e ".[dev]"
```

You can also run it directly from this repo:

```bash
PYTHONPATH=src python3 -m dairy --help
```

## Use In Another Project

After installing with one of the methods above, go to the target project:

```bash
cd /path/to/your/project
dairy init .
```

If you prefer not to install it, run it with `PYTHONPATH`:

```bash
PYTHONPATH=/path/to/DAIry/src python3 -m dairy init .
```

That creates:

```text
.dairy/
  current.md
  history.jsonl
  config.json
```

It also:

- adds `.dairy/` to the target project's `.gitignore`
- adds a detected local DAIry tool checkout to `.gitignore` when the checkout lives inside the target project
- creates or updates `AGENTS.md` with DAIry instructions
- installs a local non-blocking git pre-commit hook when the target is a git worktree

If you manually copy the DAIry repo into a project, you can be explicit:

```bash
dairy init . --ignore-tool-dir DAIry
```

Quick smoke test in the target project:

```bash
dairy start
```

## Daily Workflow

At the start of a new Codex thread, account switch, context reset, rate-limit handoff, crash recovery, or long gap, Codex runs:

```bash
dairy start
```

That prints `.dairy/current.md` for recontextualization. It also repairs missing current memory from recent history and creates an automatic mechanical checkpoint if git shows uncheckpointed project file changes.

Do not run `dairy start` before every prompt in the same active chat. It is for fresh context boundaries.

After code/config/docs changes, Codex should checkpoint automatically before its final response:

```bash
dairy checkpoint --stdin <<'EOF'
intent:
- Keep billing retries idempotent and observable.

recent:
- Added retry state guard in src/billing/retry.py.
- Updated tests around duplicate webhook delivery.

active:
- Check production metric names before merging.

decisions:
- Keep retry state server-side for now.

hazards:
- Do not remove the idempotency key check; duplicate events are common.

next:
- Run integration tests once staging credentials are available.
EOF
```

`dairy checkpoint` inspects git state and writes only when meaningful project files changed. It also stores a change signature in `.dairy/state.json`, so rerunning it without further changes does not duplicate history entries.

For important non-code decisions, append a note:

```bash
dairy note "Decided to keep retries server-side until queue metrics are stable." --kind decision
```

If `.dairy/current.md` is missing, rebuild it from the most recent long-term entries:

```bash
dairy recover --limit 20
```

## Commands

```text
dairy init [path]
dairy start
dairy checkpoint [message]
dairy note [message]
dairy current --show
dairy current [message]
dairy status
dairy recover
dairy install-agents
dairy install-hooks
```

Run `dairy <command> --help` for command-specific options.

## Privacy Model

DAIry assumes private local use. The default `init` flow keeps `.dairy/` out of git because the memory can contain project intent, mistakes, half-finished plans, customer references, or account-specific context.

The CLI includes simple secret-pattern checks for obvious tokens and private keys. This is a guardrail, not a security boundary. Treat `.dairy/` as sensitive local state.

## Design Notes

- Standard-library Python only.
- Atomic writes for the short-term context.
- File locking around long-term appends.
- Long-term memory is append-only JSONL.
- `dairy start` is the new-session wake-up command: it prints current memory, repairs missing current memory, and mechanically checkpoints dirty git state.
- Checkpoint memory writes only when git reports meaningful project changes.
- A local pre-commit hook provides a backup automatic checkpoint, but Codex-authored semantic summaries are better.
- Recovery reads only recent entries into a compact replacement current context.
