# DAIry Agent Instructions

This repository builds DAIry, a small local memory helper for Codex-style coding sessions.

When changing this project:

- Keep the CLI dependency-free unless a dependency removes substantial complexity.
- Keep `.dairy/` in `.gitignore`; DAIry memory directories are local working memory, not repo content.
- If this DAIry tool repo is copied or vendored into another project, ensure that local checkout directory is also added to the host project's `.gitignore`.
- Do not make commands that rewrite or prune `.dairy/history.jsonl`; it is append-only by design.
- Keep `dairy checkpoint` hands-off: it should write only when git reports meaningful project changes and should skip duplicate change signatures.
- Keep `dairy start` optimized for fresh context boundaries: repair missing current memory, mechanically checkpoint unrecorded dirty git state, then print the compact current context. It should not be required before every prompt.
- Keep the installed pre-commit hook non-blocking. DAIry must never prevent a user from committing.
- Prefer compact, predictable file formats over cleverness. New sessions should be able to understand the workflow quickly.
- Run the test suite before finishing meaningful changes when the environment allows it.

For host projects that use DAIry, `dairy init` installs a managed `AGENTS.md` block that tells Codex to run `dairy start` at new thread/account/context boundaries, avoid opening `.dairy/history.jsonl`, run `dairy checkpoint --stdin` before ending sessions that changed files, and ensure `.dairy/` plus any local DAIry tool checkout are ignored by git.
