<!-- DAIry:start -->
## DAIry Project Memory

This project uses DAIry for local AI working memory.

- At the start of a new Codex thread, account switch, context reset, rate-limit handoff, crash recovery, or long gap, run `dairy start` before project work.
- Do not run `dairy start` before every prompt in the same active chat; use it for fresh context boundaries.
- Treat the `.dairy/current.md` content printed by `dairy start` as the compact source of recent project state, decisions, hazards, and next steps.
- Do not open, read, grep, cat, or summarize `.dairy/history.jsonl` during normal work.
- If `.dairy/current.md` is missing or unusable, `dairy start` may recover it from recent history. Only run `dairy recover --limit 20` manually if `dairy start` fails.
- If `dairy start` reports an automatic checkpoint from uncheckpointed files, inspect `git status` and relevant diffs before making new edits; the checkpoint records changed files, not full prior intent.
- Before ending any Codex session that changed project files, run `dairy checkpoint --stdin` with a compact full current-state summary. Include what changed, why, active tasks, decisions, hazards, and next steps.
- If no project files changed, do not update DAIry memory. `dairy checkpoint` also detects this and exits without writing.
- Append long-term memory only through the CLI, preferably `dairy checkpoint --stdin` after code changes or `dairy note "..." --kind decision` for important non-code decisions.
- `dairy init` installs a local non-blocking git pre-commit hook as a backup checkpoint path. Do not rely on it for semantic summaries; still run `dairy checkpoint --stdin` when you changed code in a Codex session.
- Ensure `.dairy/` is present in the host repository's `.gitignore`; DAIry memory is local private state and should not be committed.
- If the DAIry tool repo itself is copied or vendored into this project, ensure that local checkout directory is also listed in `.gitignore`.
- Never put secrets, credentials, customer data, private keys, or raw `.env` values into DAIry memory.

If the `dairy` console script is unavailable, try `python3 -m dairy ...` or `python -m dairy ...`. If the DAIry tool repo is vendored but not installed, run with `PYTHONPATH=<DAIry checkout>/src python3 -m dairy ...`.
<!-- DAIry:end -->
