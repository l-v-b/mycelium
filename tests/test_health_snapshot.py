"""Tests for the health-snapshot metrics added 2026-07-22.

Covers the two signals that exist to catch silent failures: unpushed vault
commits (auto-push has broken three times) and sub-task adoption (the
checkbox half of the todo convention was almost unused).
"""
from __future__ import annotations

import pytest

from mycelium import health_snapshot as hs
from mycelium import vault


def _write(dirpath, slug, *, status=None, body="Body."):
    fm = [f"title: {slug}", "author: liam", "updated: '2026-07-20T00:00:00+00:00'"]
    if status is not None:
        fm.append(f"status: {status}")
    (dirpath / f"{slug}.md").write_text(
        "---\n" + "\n".join(fm) + "\n---\n\n" + body, encoding="utf-8"
    )


@pytest.fixture
def notes_dir(tmp_path, monkeypatch):
    d = tmp_path / "notes"
    d.mkdir()
    monkeypatch.setattr(hs, "NOTES_DIR", d)
    return d


# --- sub-task adoption -----------------------------------------------------

def test_adoption_counts_only_tracked_notes(notes_dir):
    # untracked note with checkboxes must not inflate adoption
    _write(notes_dir, "untracked", status=None, body="- [ ] a\n- [x] b\n")
    _write(notes_dir, "tracked_plain", status="open", body="no checkboxes")
    _, _, subs = hs._note_metrics()
    assert subs["tracked_notes"] == 1
    assert subs["tracked_with_subtasks"] == 0
    assert subs["adoption_pct"] == 0.0


def test_adoption_and_subtask_tallies(notes_dir):
    _write(notes_dir, "a", status="in-progress", body="- [x] done\n- [ ] todo\n")
    _write(notes_dir, "b", status="open", body="- [ ] one\n")
    _write(notes_dir, "c", status="done", body="no checkboxes")
    _, _, subs = hs._note_metrics()
    assert subs["tracked_notes"] == 3
    assert subs["tracked_with_subtasks"] == 2
    assert subs["adoption_pct"] == round(100 * 2 / 3, 1)
    assert (subs["subtasks_total"], subs["subtasks_done"], subs["subtasks_open"]) == (3, 1, 2)


def test_unfinished_adoption_excludes_closed_work(notes_dir):
    _write(notes_dir, "a", status="open", body="- [ ] one\n")
    _write(notes_dir, "b", status="done", body="- [x] shipped\n")
    _write(notes_dir, "c", status="blocked", body="no checkboxes")
    _, _, subs = hs._note_metrics()
    assert subs["unfinished_notes"] == 2          # open + blocked, not done
    assert subs["unfinished_with_subtasks"] == 1
    assert subs["unfinished_adoption_pct"] == 50.0


def test_fenced_examples_do_not_count_as_adoption(notes_dir):
    """Shares parse_subtasks with list_todos, so the convention note's own
    worked example must not register as real sub-tasks."""
    _write(notes_dir, "a", status="open",
           body="```markdown\n- [ ] example\n```\nprose only")
    _, _, subs = hs._note_metrics()
    assert subs["tracked_with_subtasks"] == 0
    assert subs["subtasks_total"] == 0


def test_status_distribution_still_reported(notes_dir):
    _write(notes_dir, "a", status="open")
    _write(notes_dir, "b", status=None)
    _, status_dist, _ = hs._note_metrics()
    assert status_dist == {"open": 1, "_unset": 1}


def test_empty_vault_does_not_divide_by_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(hs, "NOTES_DIR", tmp_path / "nope")
    _, _, subs = hs._note_metrics()
    assert subs["adoption_pct"] == 0.0
    assert subs["unfinished_adoption_pct"] == 0.0


# --- vault git sync --------------------------------------------------------

def _fake_run(mapping):
    """Return a subprocess.run stub keyed on a distinctive arg."""
    class R:
        def __init__(self, rc, out=b""):
            self.returncode, self.stdout = rc, out

    def run(args, **kw):
        for key, result in mapping.items():
            if key in args:
                return result
        return R(1)
    return run


def test_unpushed_commits_parsed(monkeypatch, tmp_path):
    class R:
        def __init__(self, rc, out=b""):
            self.returncode, self.stdout = rc, out
    monkeypatch.setattr(hs.subprocess, "run", _fake_run({
        "rev-parse": R(0, b"origin/main\n"),
        "rev-list": R(0, b"15\n"),
    }))
    monkeypatch.setattr(hs, "_GIT_LOG", tmp_path / "absent.log")
    out = hs._vault_git_sync()
    assert out["unpushed_commits"] == 15
    assert out["upstream"] == "origin/main"


def test_missing_push_log_is_unknown_not_success(monkeypatch, tmp_path):
    """A container rebuild wipes git.log — absence must never read as healthy."""
    class R:
        def __init__(self, rc, out=b""):
            self.returncode, self.stdout = rc, out
    monkeypatch.setattr(hs.subprocess, "run", _fake_run({
        "rev-parse": R(0, b"origin/main\n"),
        "rev-list": R(0, b"0\n"),
    }))
    monkeypatch.setattr(hs, "_GIT_LOG", tmp_path / "absent.log")
    assert hs._vault_git_sync()["last_push_failed"] is None


def test_last_push_failure_detected(monkeypatch, tmp_path):
    class R:
        def __init__(self, rc, out=b""):
            self.returncode, self.stdout = rc, out
    log = tmp_path / "git.log"
    log.write_text(
        "[2026-07-22T07:00:00+00:00] push initiated\n"
        "To github.com:l-v-b/mycelium-vault.git\n   aaa..bbb  HEAD -> main\n"
        "[2026-07-22T07:31:05+00:00] push initiated\n"
        "Bad owner or permissions on /root/.ssh/config\n"
        "fatal: Could not read from remote repository.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(hs.subprocess, "run", _fake_run({
        "rev-parse": R(0, b"origin/main\n"),
        "rev-list": R(0, b"15\n"),
    }))
    monkeypatch.setattr(hs, "_GIT_LOG", log)
    out = hs._vault_git_sync()
    assert out["last_push_failed"] is True
    assert "Bad owner" in out["last_push_error"]


def test_successful_last_push_not_flagged(monkeypatch, tmp_path):
    class R:
        def __init__(self, rc, out=b""):
            self.returncode, self.stdout = rc, out
    log = tmp_path / "git.log"
    log.write_text(
        "[2026-07-22T07:31:05+00:00] push initiated\n"
        "Bad owner or permissions on /root/.ssh/config\nfatal: nope\n"
        "[2026-07-22T08:27:13+00:00] push initiated\n"
        "To github.com:l-v-b/mycelium-vault.git\n   953e879..a20bf24  HEAD -> main\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(hs.subprocess, "run", _fake_run({
        "rev-parse": R(0, b"origin/main\n"),
        "rev-list": R(0, b"0\n"),
    }))
    monkeypatch.setattr(hs, "_GIT_LOG", log)
    out = hs._vault_git_sync()
    assert out["last_push_failed"] is False   # only the LATEST attempt counts
    assert out["unpushed_commits"] == 0


def test_no_upstream_is_reported_not_crashed(monkeypatch, tmp_path):
    class R:
        def __init__(self, rc, out=b""):
            self.returncode, self.stdout = rc, out
    monkeypatch.setattr(hs.subprocess, "run", _fake_run({"rev-parse": R(128)}))
    monkeypatch.setattr(hs, "_GIT_LOG", tmp_path / "absent.log")
    out = hs._vault_git_sync()
    assert out["unpushed_commits"] == -1
    assert out["last_push_error"] == "no upstream configured"
