from __future__ import annotations

import json
from pathlib import Path
import subprocess

from dairy.cli import main


def git(project: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(project), *args], check=True, capture_output=True, text=True)


def init_git_project(project: Path) -> None:
    git(project, "init")
    git(project, "config", "user.email", "dairy@example.invalid")
    git(project, "config", "user.name", "DAIry Test")


def test_init_creates_memory_files_gitignore_and_agents(tmp_path: Path) -> None:
    assert main(["init", str(tmp_path)]) == 0

    assert (tmp_path / ".dairy" / "current.md").exists()
    assert (tmp_path / ".dairy" / "history.jsonl").exists()
    assert (tmp_path / ".dairy" / "config.json").exists()
    assert ".dairy/" in (tmp_path / ".gitignore").read_text(encoding="utf-8")

    agents = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "DAIry Project Memory" in agents
    assert "Ensure `.dairy/` is present" in agents
    assert "Do not open, read, grep, cat, or summarize `.dairy/history.jsonl`" in agents
    assert "dairy checkpoint --stdin" in agents


def test_init_can_ignore_local_tool_checkout(tmp_path: Path) -> None:
    assert main(["init", str(tmp_path), "--ignore-tool-dir", "DAIry"]) == 0

    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert ".dairy/" in gitignore
    assert "DAIry/" in gitignore


def test_init_installs_nonblocking_git_hook(tmp_path: Path) -> None:
    init_git_project(tmp_path)

    assert main(["init", str(tmp_path)]) == 0

    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    assert hook.exists()
    assert "DAIry:start" in hook.read_text(encoding="utf-8")


def test_note_appends_jsonl_entry(tmp_path: Path) -> None:
    assert main(["init", str(tmp_path), "--no-hooks"]) == 0
    assert main(["note", "-p", str(tmp_path), "Added auth retry guard.", "--kind", "change", "--file", "src/auth.py"]) == 0

    lines = (tmp_path / ".dairy" / "history.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["kind"] == "change"
    assert entry["summary"] == "Added auth retry guard."
    assert entry["files"] == ["src/auth.py"]


def test_current_replaces_context(tmp_path: Path) -> None:
    assert main(["init", str(tmp_path), "--no-hooks"]) == 0
    assert main(["current", "-p", str(tmp_path), "intent:\n- Ship the thing.\nnext:\n- Run tests."]) == 0

    current = (tmp_path / ".dairy" / "current.md").read_text(encoding="utf-8")
    assert "Ship the thing" in current
    assert "updated_utc:" in current


def test_secret_detection_blocks_notes(tmp_path: Path) -> None:
    assert main(["init", str(tmp_path), "--no-hooks"]) == 0
    result = main(["note", "-p", str(tmp_path), "api_key=sk-123456789012345678901234"])

    assert result == 2
    assert (tmp_path / ".dairy" / "history.jsonl").read_text(encoding="utf-8") == ""


def test_recover_builds_current_from_recent_history(tmp_path: Path) -> None:
    assert main(["init", str(tmp_path), "--no-hooks"]) == 0
    assert main(["note", "-p", str(tmp_path), "First entry", "--kind", "note"]) == 0
    assert main(["note", "-p", str(tmp_path), "Second entry", "--kind", "decision"]) == 0
    (tmp_path / ".dairy" / "current.md").unlink()

    assert main(["recover", "-p", str(tmp_path), "--limit", "1"]) == 0
    current = (tmp_path / ".dairy" / "current.md").read_text(encoding="utf-8")
    assert "Second entry" in current
    assert "First entry" not in current


def test_checkpoint_skips_when_no_project_changes(tmp_path: Path) -> None:
    init_git_project(tmp_path)
    (tmp_path / "app.py").write_text("print('hello')\n", encoding="utf-8")
    assert main(["init", str(tmp_path), "--no-hooks"]) == 0
    git(tmp_path, "add", ".gitignore", "AGENTS.md", "app.py")
    git(tmp_path, "commit", "-m", "initial")

    assert main(["checkpoint", "-p", str(tmp_path)]) == 0

    assert (tmp_path / ".dairy" / "history.jsonl").read_text(encoding="utf-8") == ""
    assert not (tmp_path / ".dairy" / "state.json").exists()


def test_checkpoint_records_changed_files_and_skips_duplicates(tmp_path: Path) -> None:
    init_git_project(tmp_path)
    (tmp_path / "app.py").write_text("print('hello')\n", encoding="utf-8")
    assert main(["init", str(tmp_path), "--no-hooks"]) == 0
    git(tmp_path, "add", ".gitignore", "AGENTS.md", "app.py")
    git(tmp_path, "commit", "-m", "initial")

    (tmp_path / "app.py").write_text("print('goodbye')\n", encoding="utf-8")
    summary = "recent:\n- Changed app.py greeting.\nnext:\n- Run tests."
    assert main(["checkpoint", "-p", str(tmp_path), summary]) == 0
    assert main(["checkpoint", "-p", str(tmp_path), summary]) == 0

    lines = (tmp_path / ".dairy" / "history.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["kind"] == "checkpoint"
    assert entry["summary_source"] == "codex"
    assert entry["files"] == ["app.py"]

    current = (tmp_path / ".dairy" / "current.md").read_text(encoding="utf-8")
    assert "Changed app.py greeting" in current
    assert "M app.py" in current
    assert (tmp_path / ".dairy" / "state.json").exists()


def test_checkpoint_ignores_dairy_only_changes(tmp_path: Path) -> None:
    init_git_project(tmp_path)
    assert main(["init", str(tmp_path), "--no-hooks"]) == 0
    git(tmp_path, "add", ".gitignore", "AGENTS.md")
    git(tmp_path, "commit", "-m", "initial")

    (tmp_path / ".dairy" / "current.md").write_text("local memory only\n", encoding="utf-8")
    assert main(["checkpoint", "-p", str(tmp_path)]) == 0

    assert (tmp_path / ".dairy" / "history.jsonl").read_text(encoding="utf-8") == ""


def test_start_prints_current_and_skips_clean_git_state(tmp_path: Path, capsys) -> None:
    init_git_project(tmp_path)
    (tmp_path / "app.py").write_text("print('hello')\n", encoding="utf-8")
    assert main(["init", str(tmp_path), "--no-hooks"]) == 0
    git(tmp_path, "add", ".gitignore", "AGENTS.md", "app.py")
    git(tmp_path, "commit", "-m", "initial")

    capsys.readouterr()
    assert main(["start", "-p", str(tmp_path)]) == 0
    output = capsys.readouterr().out

    assert "DAIry start" in output
    assert "checkpoint skipped: no project changes detected" in output
    assert "--- .dairy/current.md ---" in output
    assert "DAIry Current Context" in output
    assert (tmp_path / ".dairy" / "history.jsonl").read_text(encoding="utf-8") == ""


def test_start_auto_checkpoints_unrecorded_dirty_state(tmp_path: Path, capsys) -> None:
    init_git_project(tmp_path)
    (tmp_path / "app.py").write_text("print('hello')\n", encoding="utf-8")
    assert main(["init", str(tmp_path), "--no-hooks"]) == 0
    git(tmp_path, "add", ".gitignore", "AGENTS.md", "app.py")
    git(tmp_path, "commit", "-m", "initial")

    (tmp_path / "app.py").write_text("print('handoff')\n", encoding="utf-8")
    capsys.readouterr()
    assert main(["start", "-p", str(tmp_path)]) == 0
    output = capsys.readouterr().out

    assert "recorded automatic checkpoint for 1 changed file(s)" in output
    assert "M app.py" in output
    lines = (tmp_path / ".dairy" / "history.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["summary_source"] == "automatic"
    assert entry["tags"] == ["checkpoint", "automatic", "start"]
    assert entry["files"] == ["app.py"]
    assert (tmp_path / ".dairy" / "state.json").exists()


def test_start_recovers_missing_current_from_recent_history(tmp_path: Path, capsys) -> None:
    assert main(["init", str(tmp_path), "--no-hooks"]) == 0
    assert main(["note", "-p", str(tmp_path), "Recovered project direction.", "--kind", "decision"]) == 0
    (tmp_path / ".dairy" / "current.md").unlink()

    capsys.readouterr()
    assert main(["start", "-p", str(tmp_path), "--no-checkpoint"]) == 0
    output = capsys.readouterr().out

    assert "current context recovered from 1 recent history entry" in output
    assert "Recovered project direction" in output
    assert "checkpoint skipped: disabled" in output
    current = (tmp_path / ".dairy" / "current.md").read_text(encoding="utf-8")
    assert "Recovered project direction" in current
