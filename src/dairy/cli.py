from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
from dataclasses import dataclass
import fnmatch
import getpass
import hashlib
import importlib.resources as resources
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import textwrap
from collections import deque
from typing import Iterable


DAIRY_DIR = ".dairy"
CURRENT_FILE = "current.md"
HISTORY_FILE = "history.jsonl"
CONFIG_FILE = "config.json"
STATE_FILE = "state.json"
LOCK_FILE = ".lock"
AGENTS_FILE = "AGENTS.md"
GITIGNORE_FILE = ".gitignore"
AGENTS_START = "<!-- DAIry:start -->"
AGENTS_END = "<!-- DAIry:end -->"
HOOK_START = "# DAIry:start"
HOOK_END = "# DAIry:end"
DEFAULT_CURRENT_MAX_BYTES = 8192
DEFAULT_NOTE_MAX_BYTES = 12000
DEFAULT_CHECKPOINT_MAX_FILES = 80
DEFAULT_CHECKPOINT_SUMMARY_CHARS = 3500
DEFAULT_PRIOR_CURRENT_CHARS = 2200
DEFAULT_CHECKPOINT_IGNORE_PATTERNS = [
    ".dairy",
    ".dairy/*",
    ".git",
    ".git/*",
    ".vscode",
    ".vscode/*",
    ".idea",
    ".idea/*",
    "__pycache__",
    "__pycache__/*",
    "*/__pycache__/*",
    ".pytest_cache",
    ".pytest_cache/*",
    ".ruff_cache",
    ".ruff_cache/*",
    ".mypy_cache",
    ".mypy_cache/*",
    "node_modules",
    "node_modules/*",
    "*/node_modules/*",
    ".DS_Store",
    "*.pyc",
    "*.pyo",
    "*.swp",
    "*.tmp",
]


class DairyError(RuntimeError):
    """User-facing CLI error."""


@dataclass(frozen=True)
class GitChange:
    status: str
    path: str
    raw: str
    paths: tuple[str, ...]


SENSITIVE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("OpenAI-style API key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")),
    ("GitHub token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,}\b")),
    ("GitHub fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "secret assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret|token|password|passwd)\b\s*[:=]\s*['\"]?[A-Za-z0-9_./+=\-]{12,}"
        ),
    ),
]


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def template_text(name: str) -> str:
    return resources.files("dairy.templates").joinpath(name).read_text(encoding="utf-8")


def default_actor() -> str:
    return f"{getpass.getuser()}@{socket.gethostname()}"


def project_name(root: Path) -> str:
    return root.resolve().name


def render_default_current(root: Path) -> str:
    return (
        template_text("current.md")
        .replace("{{PROJECT_NAME}}", project_name(root))
        .replace("{{UPDATED_UTC}}", utc_now())
    )


def default_config(root: Path) -> dict[str, object]:
    return {
        "schema": "dairy.config.v1",
        "project": project_name(root),
        "created_utc": utc_now(),
        "current_file": f"{DAIRY_DIR}/{CURRENT_FILE}",
        "history_file": f"{DAIRY_DIR}/{HISTORY_FILE}",
        "state_file": f"{DAIRY_DIR}/{STATE_FILE}",
        "current_max_bytes": DEFAULT_CURRENT_MAX_BYTES,
        "note_max_bytes": DEFAULT_NOTE_MAX_BYTES,
        "checkpoint_max_files": DEFAULT_CHECKPOINT_MAX_FILES,
        "history_format": "jsonl",
        "history_policy": "append-only; do not read during normal Codex sessions",
    }


def load_config(root: Path) -> dict[str, object]:
    config_path = root / DAIRY_DIR / CONFIG_FILE
    if not config_path.exists():
        return default_config(root)
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DairyError(f"invalid config JSON at {config_path}: {exc}") from exc


def load_state(root: Path) -> dict[str, object]:
    state_path = root / DAIRY_DIR / STATE_FILE
    if not state_path.exists():
        return {"schema": "dairy.state.v1"}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DairyError(f"invalid state JSON at {state_path}: {exc}") from exc


def save_state(root: Path, state: dict[str, object]) -> None:
    state["schema"] = "dairy.state.v1"
    atomic_write(root / DAIRY_DIR / STATE_FILE, json.dumps(state, indent=2, sort_keys=True) + "\n")


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            if not content.endswith("\n"):
                handle.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise


@contextlib.contextmanager
def file_lock(dairy_dir: Path):
    dairy_dir.mkdir(parents=True, exist_ok=True)
    lock_path = dairy_dir / LOCK_FILE
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def require_initialized(root: Path) -> Path:
    dairy_dir = root / DAIRY_DIR
    if not dairy_dir.exists():
        raise DairyError(f"{root} is not initialized; run `dairy init {root}` first")
    return dairy_dir


def detect_sensitive(text: str) -> list[str]:
    matches: list[str] = []
    for name, pattern in SENSITIVE_PATTERNS:
        if pattern.search(text):
            matches.append(name)
    return matches


def ensure_not_sensitive(text: str, allow_sensitive: bool) -> None:
    matches = detect_sensitive(text)
    if matches and not allow_sensitive:
        found = ", ".join(matches)
        raise DairyError(
            f"refusing to store possible sensitive content ({found}); "
            "remove it or rerun with --allow-sensitive if this is intentional"
        )


def ensure_size(label: str, text: str, max_bytes: int | None, force: bool = False) -> None:
    if max_bytes is None:
        return
    size = len(text.encode("utf-8"))
    if size > max_bytes and not force:
        raise DairyError(f"{label} is {size} bytes, above the {max_bytes} byte budget; shorten it or use --force")


def collect_text(args: argparse.Namespace) -> str:
    text = collect_optional_text(args)
    if not text:
        raise DairyError("no text supplied; pass a message, --from-file, or --stdin")
    return text


def collect_optional_text(args: argparse.Namespace) -> str:
    parts: list[str] = []
    if getattr(args, "message", None):
        parts.append(" ".join(args.message))
    from_file = getattr(args, "from_file", None)
    if from_file:
        parts.append(Path(from_file).read_text(encoding="utf-8"))
    if getattr(args, "stdin", False):
        parts.append(sys.stdin.read())
    text = "\n".join(part.strip() for part in parts if part and part.strip()).strip()
    return text


def normalize_gitignore_entry(entry: str) -> str:
    cleaned = entry.strip().replace("\\", "/")
    if cleaned and not cleaned.endswith("/"):
        cleaned += "/"
    return cleaned


def has_gitignore_entry(content: str, entry: str = f"{DAIRY_DIR}/") -> bool:
    normalized = normalize_gitignore_entry(entry)
    bare = normalized.rstrip("/")
    entries = {line.strip() for line in content.splitlines() if line.strip() and not line.lstrip().startswith("#")}
    return normalized in entries or bare in entries


def package_checkout_inside(root: Path) -> str | None:
    source = Path(__file__).resolve()
    package_root: Path | None = None
    for candidate in source.parents:
        if (candidate / "pyproject.toml").exists() and (candidate / "src" / "dairy").exists():
            package_root = candidate
            break
    if package_root is None:
        return None
    try:
        rel = package_root.relative_to(root.resolve())
    except ValueError:
        return None
    if not rel.parts:
        return None
    return normalize_gitignore_entry(rel.as_posix())


def ensure_gitignore(root: Path, extra_entries: Iterable[str] = ()) -> bool:
    path = root / GITIGNORE_FILE
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    wanted = [f"{DAIRY_DIR}/"]
    wanted.extend(normalize_gitignore_entry(entry) for entry in extra_entries if entry.strip())
    missing = []
    for entry in dict.fromkeys(wanted):
        if not has_gitignore_entry(content, entry):
            missing.append(entry)
    if not missing:
        return False
    separator = "" if not content else ("\n" if content.endswith("\n") else "\n\n")
    addition = f"{separator}# DAIry local AI memory and tool checkouts\n" + "\n".join(missing) + "\n"
    atomic_write(path, content + addition)
    return True


def gitignore_extra_entries(args: argparse.Namespace, root: Path) -> list[str]:
    entries = list(getattr(args, "ignore_tool_dir", None) or [])
    detected = package_checkout_inside(root)
    if detected:
        entries.append(detected)
    return entries


def upsert_agents_block(root: Path) -> bool:
    path = root / AGENTS_FILE
    block = template_text("agents_block.md").strip() + "\n"
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    if AGENTS_START in content and AGENTS_END in content:
        start = content.index(AGENTS_START)
        end = content.index(AGENTS_END, start) + len(AGENTS_END)
        updated = content[:start].rstrip() + "\n\n" + block + "\n" + content[end:].lstrip()
    elif content.strip():
        updated = content.rstrip() + "\n\n" + block
    else:
        updated = "# Agent Instructions\n\n" + block
    if updated == content:
        return False
    atomic_write(path, updated)
    return True


def write_if_missing(path: Path, content: str, force: bool = False) -> bool:
    if path.exists() and not force:
        return False
    atomic_write(path, content)
    return True


def run_git(root: Path, args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(root), "-c", "core.quotePath=false", *args],
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "git command failed"
        raise DairyError(detail)
    return result


def git_dir(root: Path) -> Path | None:
    result = run_git(root, ["rev-parse", "--git-dir"], check=False)
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def hook_pythonpath_source() -> str | None:
    source = Path(__file__).resolve()
    for candidate in source.parents:
        if (candidate / "pyproject.toml").exists() and (candidate / "src" / "dairy").exists():
            return str((candidate / "src").resolve())
    return None


def render_pre_commit_hook_block() -> str:
    pythonpath = hook_pythonpath_source()
    pythonpath_prefix = ""
    if pythonpath:
        escaped = pythonpath.replace('"', '\\"')
        pythonpath_prefix = f'PYTHONPATH="{escaped}${{PYTHONPATH:+:$PYTHONPATH}}" '
    return (
        f"{HOOK_START}\n"
        "# Keep local DAIry memory current when commits happen. This hook never blocks commits.\n"
        "if command -v dairy >/dev/null 2>&1; then\n"
        '  dairy checkpoint --path "$PWD" --hook >/dev/null 2>&1 || true\n'
        "elif command -v python3 >/dev/null 2>&1; then\n"
        f'  {pythonpath_prefix}python3 -m dairy checkpoint --path "$PWD" --hook >/dev/null 2>&1 || true\n'
        "elif command -v python >/dev/null 2>&1; then\n"
        '  python -m dairy checkpoint --path "$PWD" --hook >/dev/null 2>&1 || true\n'
        "fi\n"
        f"{HOOK_END}\n"
    )


def is_shell_hook(content: str) -> bool:
    first_line = content.splitlines()[0] if content.splitlines() else ""
    if not first_line.startswith("#!"):
        return True
    return any(shell in first_line for shell in ("sh", "bash", "zsh", "dash"))


def install_pre_commit_hook(root: Path, force: bool = False) -> tuple[bool, str]:
    directory = git_dir(root)
    if directory is None:
        return False, "skipped hook install; target is not a git worktree"
    hooks_dir = directory / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hooks_dir / "pre-commit"
    block = render_pre_commit_hook_block()
    if hook_path.exists():
        content = hook_path.read_text(encoding="utf-8")
        if HOOK_START in content and HOOK_END in content:
            start = content.index(HOOK_START)
            end = content.index(HOOK_END, start) + len(HOOK_END)
            updated = content[:start].rstrip() + "\n\n" + block + "\n" + content[end:].lstrip()
        elif is_shell_hook(content) or force:
            separator = "" if content.endswith("\n") else "\n"
            updated = content + separator + "\n" + block
        else:
            return False, f"skipped hook install; {hook_path} is not a shell hook"
    else:
        updated = "#!/bin/sh\n\n" + block
    if hook_path.exists() and hook_path.read_text(encoding="utf-8") == updated:
        return False, f"{hook_path} already has DAIry hook"
    atomic_write(hook_path, updated)
    current_mode = hook_path.stat().st_mode
    hook_path.chmod(current_mode | 0o111)
    return True, str(hook_path)


def parse_status_line(line: str) -> GitChange | None:
    if len(line) < 4:
        return None
    status = line[:2].strip() or line[:2]
    rest = line[3:].strip()
    if not rest:
        return None
    if " -> " in rest:
        paths = tuple(part.strip() for part in rest.split(" -> ", 1))
        path = paths[-1]
    else:
        paths = (rest,)
        path = rest
    return GitChange(status=status, path=path, raw=line, paths=paths)


def normalize_repo_path(path: str) -> str:
    return path.strip().replace("\\", "/").strip("/")


def is_checkpoint_ignored(path: str, patterns: Iterable[str]) -> bool:
    normalized = normalize_repo_path(path)
    for pattern in patterns:
        clean = normalize_repo_path(pattern)
        if not clean:
            continue
        if fnmatch.fnmatch(normalized, clean) or fnmatch.fnmatch(normalized, clean.rstrip("/") + "/*"):
            return True
    return False


def changed_files(root: Path, extra_ignore_patterns: Iterable[str] = ()) -> list[GitChange]:
    if git_dir(root) is None:
        raise DairyError("checkpoint requires a git worktree so it can tell whether project files changed")
    result = run_git(root, ["status", "--porcelain=v1", "--untracked-files=all", "--", "."])
    patterns = [*DEFAULT_CHECKPOINT_IGNORE_PATTERNS, *extra_ignore_patterns]
    changes: list[GitChange] = []
    for line in result.stdout.splitlines():
        change = parse_status_line(line)
        if change is None:
            continue
        if all(is_checkpoint_ignored(path, patterns) for path in change.paths):
            continue
        changes.append(change)
    return sorted(changes, key=lambda item: item.raw)


def hash_file(path: Path) -> str:
    if not path.exists():
        return "missing"
    if path.is_dir():
        return "dir"
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def checkpoint_signature(root: Path, changes: list[GitChange]) -> str:
    digest = hashlib.sha256()
    for change in changes:
        digest.update(change.raw.encode("utf-8", errors="replace"))
        digest.update(b"\0")
        digest.update(hash_file(root / change.path).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def format_change(change: GitChange) -> str:
    status = " ".join(change.status.split())
    return f"{status or '?'} {change.path}"


def truncate_text(text: str, max_chars: int) -> str:
    stripped = text.strip()
    if len(stripped) <= max_chars:
        return stripped
    return stripped[: max_chars - 3].rstrip() + "..."


def indent_block(text: str, prefix: str = "  ") -> list[str]:
    return [prefix + line if line else prefix.rstrip() for line in text.splitlines()]


def checkpoint_auto_summary(changes: list[GitChange], max_files: int) -> str:
    lines = ["Automatic checkpoint: project files changed."]
    lines.append("")
    lines.append("changed_files:")
    for change in changes[:max_files]:
        lines.append(f"- {format_change(change)}")
    if len(changes) > max_files:
        lines.append(f"- ... {len(changes) - max_files} more")
    return "\n".join(lines)


def prior_current_excerpt(current_path: Path, max_chars: int) -> str:
    if not current_path.exists():
        return ""
    return truncate_text(current_path.read_text(encoding="utf-8"), max_chars)


def checkpoint_current_content(
    root: Path,
    summary: str,
    changes: list[GitChange],
    signature: str,
    explicit_summary: bool,
    prior_current: str,
    max_files: int,
) -> str:
    lines = [
        "# DAIry Current Context",
        "",
        "schema: dairy.current.v1",
        f"project: {project_name(root)}",
        f"updated_utc: {utc_now()}",
        "source: dairy checkpoint",
        f"change_signature: {signature[:16]}",
        "policy: Read this first in new Codex sessions. Do not read history.jsonl unless this file is missing or unusable.",
        "",
        "latest:",
    ]
    lines.extend(indent_block(truncate_text(summary, DEFAULT_CHECKPOINT_SUMMARY_CHARS)))
    lines.extend(["", "changed_files:"])
    for change in changes[:max_files]:
        lines.append(f"- {format_change(change)}")
    if len(changes) > max_files:
        lines.append(f"- ... {len(changes) - max_files} more")
    if not explicit_summary and prior_current:
        lines.extend(["", "previous_current_excerpt:"])
        lines.extend(indent_block(prior_current))
    lines.extend(
        [
            "",
            "active:",
            "- Continue from the latest checkpoint and inspect git diff when intent is unclear.",
            "",
            "hazards:",
            "- Automatic checkpoints know which files changed, not full human intent.",
            "- Never store secrets, credentials, private keys, customer data, or raw .env values in DAIry.",
            "",
            "next:",
            "- Before ending after more code changes, run `dairy checkpoint --stdin` with a compact current-state summary.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def append_history_entry(dairy_dir: Path, entry: dict[str, object]) -> None:
    line = json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with (dairy_dir / HISTORY_FILE).open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")


def current_is_usable(path: Path) -> bool:
    return path.exists() and bool(path.read_text(encoding="utf-8").strip())


def recover_current_file(root: Path, dairy_dir: Path, limit: int) -> tuple[str, int]:
    history_path = dairy_dir / HISTORY_FILE
    entries = list(iter_recent_history(history_path, limit)) if history_path.exists() else []
    content = recover_content(root, entries)
    atomic_write(dairy_dir / CURRENT_FILE, content)
    return content, len(entries)


def record_automatic_checkpoint(
    root: Path,
    dairy_dir: Path,
    config: dict[str, object],
    changes: list[GitChange],
    max_files: int,
    force: bool = False,
) -> tuple[bool, str]:
    signature = checkpoint_signature(root, changes)
    state = load_state(root)
    last = state.get("last_checkpoint") if isinstance(state.get("last_checkpoint"), dict) else {}
    if not force and isinstance(last, dict) and last.get("signature") == signature:
        return False, "already checkpointed"

    current_path = dairy_dir / CURRENT_FILE
    summary = checkpoint_auto_summary(changes, max_files)
    current_content = checkpoint_current_content(
        root=root,
        summary=summary,
        changes=changes,
        signature=signature,
        explicit_summary=False,
        prior_current=prior_current_excerpt(current_path, DEFAULT_PRIOR_CURRENT_CHARS),
        max_files=max_files,
    )
    ensure_size(
        "checkpoint current context",
        current_content,
        int(config.get("current_max_bytes", DEFAULT_CURRENT_MAX_BYTES)),
        force=True,
    )
    files = [change.path for change in changes]
    entry = {
        "schema": "dairy.history.v1",
        "ts": utc_now(),
        "kind": "checkpoint",
        "actor": default_actor(),
        "summary": summary,
        "files": files,
        "tags": ["checkpoint", "automatic", "start"],
        "cwd": str(Path.cwd()),
        "change_signature": signature,
        "change_count": len(changes),
        "summary_source": "automatic",
    }
    append_history_entry(dairy_dir, entry)
    atomic_write(current_path, current_content)
    state["last_checkpoint"] = {
        "ts": utc_now(),
        "signature": signature,
        "files": files[:max_files],
        "change_count": len(changes),
        "summary_source": "automatic",
    }
    save_state(root, state)
    return True, f"recorded automatic checkpoint for {len(changes)} changed file(s)"


def print_start_output(status_lines: list[str], current_text: str) -> None:
    print("DAIry start")
    for line in status_lines:
        print(f"- {line}")
    print(f"--- {DAIRY_DIR}/{CURRENT_FILE} ---")
    print(current_text, end="" if current_text.endswith("\n") else "\n")


def init_command(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    root.mkdir(parents=True, exist_ok=True)
    dairy_dir = root / DAIRY_DIR
    dairy_dir.mkdir(parents=True, exist_ok=True)

    changed: list[str] = []
    with file_lock(dairy_dir):
        if write_if_missing(dairy_dir / CURRENT_FILE, render_default_current(root), force=args.force):
            changed.append(f"{DAIRY_DIR}/{CURRENT_FILE}")
        history_path = dairy_dir / HISTORY_FILE
        if not history_path.exists():
            history_path.touch()
            changed.append(f"{DAIRY_DIR}/{HISTORY_FILE}")
        config_content = json.dumps(default_config(root), indent=2, sort_keys=True) + "\n"
        if write_if_missing(dairy_dir / CONFIG_FILE, config_content, force=args.force):
            changed.append(f"{DAIRY_DIR}/{CONFIG_FILE}")

    if ensure_gitignore(root, gitignore_extra_entries(args, root)):
        changed.append(GITIGNORE_FILE)
    if not args.no_agents and upsert_agents_block(root):
        changed.append(AGENTS_FILE)
    if not args.no_hooks:
        hook_changed, hook_message = install_pre_commit_hook(root, force=args.force_hooks)
        if hook_changed:
            changed.append(hook_message)

    if changed:
        print("Initialized DAIry:")
        for item in changed:
            print(f"- {item}")
    else:
        print("DAIry already initialized; no files changed.")
    return 0


def install_agents_command(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    changed = upsert_agents_block(root)
    if ensure_gitignore(root, gitignore_extra_entries(args, root)):
        changed = True
    print("Installed DAIry AGENTS.md block." if changed else "DAIry AGENTS.md block already present.")
    return 0


def install_hooks_command(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    changed, message = install_pre_commit_hook(root, force=args.force)
    print(f"Installed DAIry pre-commit hook: {message}" if changed else message)
    return 0


def note_command(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    dairy_dir = require_initialized(root)
    config = load_config(root)
    text = collect_text(args)
    ensure_not_sensitive(text, args.allow_sensitive)
    ensure_size("note", text, int(config.get("note_max_bytes", DEFAULT_NOTE_MAX_BYTES)), force=args.force)

    entry = {
        "schema": "dairy.history.v1",
        "ts": utc_now(),
        "kind": args.kind,
        "actor": args.actor or default_actor(),
        "summary": text,
        "files": args.file or [],
        "tags": args.tag or [],
        "cwd": str(Path.cwd()),
    }
    with file_lock(dairy_dir):
        append_history_entry(dairy_dir, entry)
    print(f"Appended 1 {args.kind!r} entry to {DAIRY_DIR}/{HISTORY_FILE}.")
    return 0


def checkpoint_command(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    dairy_dir = require_initialized(root)
    config = load_config(root)
    max_files = int(config.get("checkpoint_max_files", DEFAULT_CHECKPOINT_MAX_FILES))
    if args.max_files is not None:
        max_files = args.max_files
    changes = changed_files(root, extra_ignore_patterns=args.ignore_pattern or [])
    if not changes:
        if not args.hook:
            print("No project changes detected; checkpoint skipped.")
        return 0

    signature = checkpoint_signature(root, changes)
    supplied_summary = collect_optional_text(args)
    explicit_summary = bool(supplied_summary)
    if explicit_summary:
        ensure_not_sensitive(supplied_summary, args.allow_sensitive)
        ensure_size("checkpoint summary", supplied_summary, int(config.get("note_max_bytes", DEFAULT_NOTE_MAX_BYTES)), force=args.force)
        summary = supplied_summary
    else:
        summary = checkpoint_auto_summary(changes, max_files)

    current_path = dairy_dir / CURRENT_FILE
    prior = "" if explicit_summary else prior_current_excerpt(current_path, DEFAULT_PRIOR_CURRENT_CHARS)
    current_content = checkpoint_current_content(
        root=root,
        summary=summary,
        changes=changes,
        signature=signature,
        explicit_summary=explicit_summary,
        prior_current=prior,
        max_files=max_files,
    )
    ensure_size(
        "checkpoint current context",
        current_content,
        int(config.get("current_max_bytes", DEFAULT_CURRENT_MAX_BYTES)),
        force=True,
    )

    with file_lock(dairy_dir):
        state = load_state(root)
        last = state.get("last_checkpoint") if isinstance(state.get("last_checkpoint"), dict) else {}
        if not args.force and isinstance(last, dict) and last.get("signature") == signature:
            if not args.hook:
                print("Project changes already checkpointed; no files changed since last checkpoint.")
            return 0
        files = [change.path for change in changes]
        entry = {
            "schema": "dairy.history.v1",
            "ts": utc_now(),
            "kind": args.kind,
            "actor": args.actor or default_actor(),
            "summary": summary,
            "files": files,
            "tags": ["checkpoint", *list(args.tag or [])],
            "cwd": str(Path.cwd()),
            "change_signature": signature,
            "change_count": len(changes),
            "summary_source": "codex" if explicit_summary else "automatic",
        }
        append_history_entry(dairy_dir, entry)
        atomic_write(current_path, current_content)
        state["last_checkpoint"] = {
            "ts": utc_now(),
            "signature": signature,
            "files": files[:max_files],
            "change_count": len(changes),
            "summary_source": entry["summary_source"],
        }
        save_state(root, state)

    if not args.hook:
        print(f"Checkpoint recorded {len(changes)} changed file(s).")
        print(f"Updated {DAIRY_DIR}/{CURRENT_FILE} and appended to {DAIRY_DIR}/{HISTORY_FILE}.")
    return 0


def start_command(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    dairy_dir = require_initialized(root)
    config = load_config(root)
    current_path = dairy_dir / CURRENT_FILE
    status_lines: list[str] = []

    with file_lock(dairy_dir):
        if current_is_usable(current_path):
            status_lines.append("current context found")
        else:
            recovered, count = recover_current_file(root, dairy_dir, args.recover_limit)
            status_lines.append(f"current context recovered from {count} recent history entr{'y' if count == 1 else 'ies'}")
            if not recovered.strip():
                atomic_write(current_path, render_default_current(root))

    if not args.no_checkpoint and git_dir(root) is not None:
        max_files = int(config.get("checkpoint_max_files", DEFAULT_CHECKPOINT_MAX_FILES))
        if args.max_files is not None:
            max_files = args.max_files
        changes = changed_files(root, extra_ignore_patterns=args.ignore_pattern or [])
        if changes:
            with file_lock(dairy_dir):
                recorded, message = record_automatic_checkpoint(
                    root=root,
                    dairy_dir=dairy_dir,
                    config=config,
                    changes=changes,
                    max_files=max_files,
                    force=args.force,
                )
            status_lines.append(message if recorded else f"checkpoint skipped: {message}")
        else:
            status_lines.append("checkpoint skipped: no project changes detected")
    elif args.no_checkpoint:
        status_lines.append("checkpoint skipped: disabled")
    else:
        status_lines.append("checkpoint skipped: not a git worktree")

    if not current_path.exists():
        raise DairyError(f"{DAIRY_DIR}/{CURRENT_FILE} is missing and could not be recovered")
    print_start_output(status_lines, current_path.read_text(encoding="utf-8"))
    return 0


def render_current(root: Path, text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("#"):
        body = stripped
    else:
        body = textwrap.dedent(
            f"""\
            # DAIry Current Context

            schema: dairy.current.v1
            project: {project_name(root)}
            updated_utc: {utc_now()}
            budget: Keep this file compact. Target <= 120 lines and <= 8 KB.
            policy: Read this first in new Codex sessions. Do not read history.jsonl unless this file is missing or unusable.

            {stripped}
            """
        ).strip()
    if "updated_utc:" not in body.splitlines()[:12]:
        body = body.rstrip() + f"\n\nupdated_utc: {utc_now()}"
    return body.rstrip() + "\n"


def current_command(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    dairy_dir = require_initialized(root)
    current_path = dairy_dir / CURRENT_FILE
    if args.show:
        if not current_path.exists():
            raise DairyError(f"{DAIRY_DIR}/{CURRENT_FILE} is missing; run `dairy recover`")
        print(current_path.read_text(encoding="utf-8"), end="")
        return 0

    config = load_config(root)
    text = collect_text(args)
    ensure_not_sensitive(text, args.allow_sensitive)
    content = render_current(root, text)
    ensure_size(
        "current context",
        content,
        int(config.get("current_max_bytes", DEFAULT_CURRENT_MAX_BYTES)),
        force=args.force,
    )
    with file_lock(dairy_dir):
        atomic_write(current_path, content)
    print(f"Updated {DAIRY_DIR}/{CURRENT_FILE} ({len(content.encode('utf-8'))} bytes).")
    return 0


def status_command(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    dairy_dir = root / DAIRY_DIR
    print(f"root: {root}")
    print(f"initialized: {'yes' if dairy_dir.exists() else 'no'}")
    if not dairy_dir.exists():
        return 0
    for name in (CURRENT_FILE, HISTORY_FILE, CONFIG_FILE, STATE_FILE):
        path = dairy_dir / name
        if path.exists():
            stat = path.stat()
            modified = _dt.datetime.fromtimestamp(stat.st_mtime, _dt.timezone.utc).replace(microsecond=0)
            print(f"{DAIRY_DIR}/{name}: {stat.st_size} bytes, modified {modified.isoformat().replace('+00:00', 'Z')}")
        else:
            print(f"{DAIRY_DIR}/{name}: missing")
    gitignore = root / GITIGNORE_FILE
    ignored = gitignore.exists() and has_gitignore_entry(gitignore.read_text(encoding="utf-8"))
    print(f"gitignore_has_dairy: {'yes' if ignored else 'no'}")
    agents = root / AGENTS_FILE
    has_block = agents.exists() and AGENTS_START in agents.read_text(encoding="utf-8")
    print(f"agents_block: {'yes' if has_block else 'no'}")
    hook_dir = git_dir(root)
    hook = hook_dir / "hooks" / "pre-commit" if hook_dir else None
    has_hook = hook is not None and hook.exists() and HOOK_START in hook.read_text(encoding="utf-8")
    print(f"pre_commit_hook: {'yes' if has_hook else 'no'}")
    return 0


def iter_recent_history(history_path: Path, limit: int) -> Iterable[dict[str, object]]:
    recent: deque[str] = deque(maxlen=limit)
    with history_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped:
                recent.append(stripped)
    for line in recent:
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            yield {"ts": "unknown", "kind": "invalid", "summary": "[invalid JSONL entry skipped]"}


def one_line(value: object, max_chars: int = 500) -> str:
    text = " ".join(str(value).split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def recover_content(root: Path, entries: list[dict[str, object]]) -> str:
    lines = [
        "# DAIry Current Context",
        "",
        "schema: dairy.current.v1",
        f"project: {project_name(root)}",
        f"updated_utc: {utc_now()}",
        "source: recovered from recent append-only history entries because current.md was missing or unusable",
        "policy: This is a fallback summary. Refresh it after reorientation. Do not read history.jsonl during normal work.",
        "",
        "recent:",
    ]
    if not entries:
        lines.append("- No long-term entries found.")
    for entry in entries:
        ts = one_line(entry.get("ts", "unknown"), 80)
        kind = one_line(entry.get("kind", "note"), 80)
        summary = one_line(entry.get("summary", ""), 500)
        files = entry.get("files") or []
        file_text = f" files={','.join(map(str, files[:5]))}" if isinstance(files, list) and files else ""
        lines.append(f"- [{ts}] {kind}: {summary}{file_text}")
    lines.extend(
        [
            "",
            "active:",
            "- Ask the user or inspect the repo to confirm the latest state, then replace this recovered context.",
            "",
            "next:",
            "- Run `python -m dairy current --stdin` with a fresh compact summary before ending the session.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def recover_command(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    dairy_dir = require_initialized(root)
    current_path = dairy_dir / CURRENT_FILE
    if current_path.exists() and not args.force:
        print(f"{DAIRY_DIR}/{CURRENT_FILE} already exists; use --force to replace it.")
        return 0
    history_path = dairy_dir / HISTORY_FILE
    if not history_path.exists():
        raise DairyError(f"{DAIRY_DIR}/{HISTORY_FILE} is missing")
    entries = list(iter_recent_history(history_path, args.limit))
    content = recover_content(root, entries)
    if args.stdout:
        print(content, end="")
    else:
        with file_lock(dairy_dir):
            atomic_write(current_path, content)
        print(f"Recovered {DAIRY_DIR}/{CURRENT_FILE} from {len(entries)} recent history entries.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dairy",
        description="Local append-only AI diary and compact Codex context helper.",
    )
    parser.add_argument("--version", action="version", version="dairy 0.1.0")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="initialize DAIry in a project")
    init_parser.add_argument("path", nargs="?", default=".", help="target project path")
    init_parser.add_argument("--force", action="store_true", help="overwrite current.md and config.json templates")
    init_parser.add_argument("--no-agents", action="store_true", help="do not create or update AGENTS.md")
    init_parser.add_argument("--no-hooks", action="store_true", help="do not install the local non-blocking pre-commit hook")
    init_parser.add_argument("--force-hooks", action="store_true", help="append DAIry hook block even when an existing hook is unusual")
    init_parser.add_argument(
        "--ignore-tool-dir",
        action="append",
        default=[],
        help="extra local DAIry tool checkout directory to add to .gitignore; repeatable",
    )
    init_parser.set_defaults(func=init_command)

    start_parser = subparsers.add_parser("start", help="wake a new Codex session with repaired current context")
    start_parser.add_argument("-p", "--path", default=".", help="project path")
    start_parser.add_argument("--recover-limit", type=int, default=20, help="recent history entries to use if current.md is missing")
    start_parser.add_argument("--no-checkpoint", action="store_true", help="do not create an automatic checkpoint for dirty git state")
    start_parser.add_argument("--ignore-pattern", action="append", default=[], help="extra git path pattern to ignore; repeatable")
    start_parser.add_argument("--max-files", type=int, help="maximum changed files to show in current.md")
    start_parser.add_argument("--force", action="store_true", help="record automatic checkpoint even if the same change signature exists")
    start_parser.set_defaults(func=start_command)

    note_parser = subparsers.add_parser("note", help="append an entry to long-term memory")
    note_parser.add_argument("message", nargs="*", help="entry text")
    note_parser.add_argument("-p", "--path", default=".", help="project path")
    note_parser.add_argument("--stdin", action="store_true", help="read entry text from stdin")
    note_parser.add_argument("--from-file", help="read entry text from a file")
    note_parser.add_argument("--kind", default="note", help="entry kind, such as note, change, decision, hazard")
    note_parser.add_argument("--actor", help="actor label; defaults to user@host")
    note_parser.add_argument("--tag", action="append", help="tag for the entry; repeatable")
    note_parser.add_argument("--file", action="append", help="related project file; repeatable")
    note_parser.add_argument("--allow-sensitive", action="store_true", help="allow content that matches secret patterns")
    note_parser.add_argument("--force", action="store_true", help="allow notes over the configured size budget")
    note_parser.set_defaults(func=note_command)

    checkpoint_parser = subparsers.add_parser("checkpoint", help="record memory only when git project files changed")
    checkpoint_parser.add_argument("message", nargs="*", help="compact current-state summary")
    checkpoint_parser.add_argument("-p", "--path", default=".", help="project path")
    checkpoint_parser.add_argument("--stdin", action="store_true", help="read compact current-state summary from stdin")
    checkpoint_parser.add_argument("--from-file", help="read compact current-state summary from a file")
    checkpoint_parser.add_argument("--kind", default="checkpoint", help="history entry kind")
    checkpoint_parser.add_argument("--actor", help="actor label; defaults to user@host")
    checkpoint_parser.add_argument("--tag", action="append", help="tag for the entry; repeatable")
    checkpoint_parser.add_argument("--ignore-pattern", action="append", default=[], help="extra git path pattern to ignore; repeatable")
    checkpoint_parser.add_argument("--max-files", type=int, help="maximum changed files to show in current.md")
    checkpoint_parser.add_argument("--allow-sensitive", action="store_true", help="allow summary content that matches secret patterns")
    checkpoint_parser.add_argument("--force", action="store_true", help="record even if this exact change signature was already checkpointed")
    checkpoint_parser.add_argument("--hook", action="store_true", help=argparse.SUPPRESS)
    checkpoint_parser.set_defaults(func=checkpoint_command)

    current_parser = subparsers.add_parser("current", help="show or replace compact current context")
    current_parser.add_argument("message", nargs="*", help="replacement current context text")
    current_parser.add_argument("-p", "--path", default=".", help="project path")
    current_parser.add_argument("--show", action="store_true", help="print current.md")
    current_parser.add_argument("--stdin", action="store_true", help="read replacement context from stdin")
    current_parser.add_argument("--from-file", help="read replacement context from a file")
    current_parser.add_argument("--allow-sensitive", action="store_true", help="allow content that matches secret patterns")
    current_parser.add_argument("--force", action="store_true", help="allow context over the configured size budget")
    current_parser.set_defaults(func=current_command)

    status_parser = subparsers.add_parser("status", help="show DAIry file health without dumping history")
    status_parser.add_argument("-p", "--path", default=".", help="project path")
    status_parser.set_defaults(func=status_command)

    recover_parser = subparsers.add_parser("recover", help="rebuild current.md from recent history entries")
    recover_parser.add_argument("-p", "--path", default=".", help="project path")
    recover_parser.add_argument("--limit", type=int, default=20, help="number of recent history entries to use")
    recover_parser.add_argument("--force", action="store_true", help="replace current.md if it already exists")
    recover_parser.add_argument("--stdout", action="store_true", help="print recovered context instead of writing it")
    recover_parser.set_defaults(func=recover_command)

    agents_parser = subparsers.add_parser("install-agents", help="install or refresh the AGENTS.md DAIry block")
    agents_parser.add_argument("path", nargs="?", default=".", help="target project path")
    agents_parser.add_argument(
        "--ignore-tool-dir",
        action="append",
        default=[],
        help="extra local DAIry tool checkout directory to add to .gitignore; repeatable",
    )
    agents_parser.set_defaults(func=install_agents_command)

    hooks_parser = subparsers.add_parser("install-hooks", help="install the local non-blocking git pre-commit hook")
    hooks_parser.add_argument("path", nargs="?", default=".", help="target project path")
    hooks_parser.add_argument("--force", action="store_true", help="append hook block even when an existing hook is unusual")
    hooks_parser.set_defaults(func=install_hooks_command)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except DairyError as exc:
        print(f"dairy: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
