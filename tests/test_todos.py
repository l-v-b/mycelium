"""Tests for work-item enumeration (frontmatter `status` + checkbox sub-tasks).

Monkeypatches NOTES_DIR onto a tmp vault so these run without a real vault or
a ChromaDB backend — list_notes_by_status is deliberately disk-only.
"""
from __future__ import annotations

import pytest

from mycelium import vault


def _write(dirpath, slug, *, title, status=None, tags=None, author="liam",
           updated="2026-07-01T00:00:00+00:00", body="Body text."):
    fm = [f"title: {title}", f"author: {author}", f"updated: {updated}"]
    if status is not None:
        fm.append(f"status: {status}")
    if tags:
        fm.append("tags:")
        fm.extend(f"  - {t}" for t in tags)
    (dirpath / f"{slug}.md").write_text(
        "---\n" + "\n".join(fm) + "\n---\n\n" + body, encoding="utf-8"
    )


@pytest.fixture
def notes_dir(tmp_path, monkeypatch):
    d = tmp_path / "notes"
    d.mkdir()
    monkeypatch.setattr(vault, "NOTES_DIR", d)
    return d


# --- parse_subtasks --------------------------------------------------------

def test_parse_subtasks_reads_checked_and_unchecked():
    body = "## TODO\n\n- [ ] first step\n- [x] second step\n* [X] third step\n"
    assert vault.parse_subtasks(body) == [
        {"done": False, "text": "first step"},
        {"done": True, "text": "second step"},
        {"done": True, "text": "third step"},
    ]


def test_parse_subtasks_skips_fenced_examples():
    """The convention note documents the format with a fenced example — those
    checkboxes are documentation, not work."""
    body = (
        "Use checkboxes like this:\n\n"
        "```markdown\n- [ ] Specific actionable step 1\n- [x] Done step\n```\n\n"
        "## TODO\n- [ ] the real one\n"
    )
    assert vault.parse_subtasks(body) == [{"done": False, "text": "the real one"}]


def test_parse_subtasks_ignores_plain_bullets():
    assert vault.parse_subtasks("- not a checkbox\n- [ ] yes\n") == [
        {"done": False, "text": "yes"}
    ]


# --- list_notes_by_status --------------------------------------------------

def test_notes_without_status_are_not_work_items(notes_dir):
    _write(notes_dir, "synthesis", title="Some synthesis", status=None)
    _write(notes_dir, "tracked", title="Tracked thing", status="open")
    got = vault.list_notes_by_status()
    assert [n["title"] for n in got] == ["Tracked thing"]


def test_default_returns_every_status(notes_dir):
    _write(notes_dir, "a", title="A", status="open")
    _write(notes_dir, "b", title="B", status="done")
    assert {n["title"] for n in vault.list_notes_by_status()} == {"A", "B"}


def test_status_filter(notes_dir):
    _write(notes_dir, "a", title="A", status="open")
    _write(notes_dir, "b", title="B", status="done")
    _write(notes_dir, "c", title="C", status="blocked")
    got = vault.list_notes_by_status(statuses=vault.OPEN_STATUSES)
    assert {n["title"] for n in got} == {"A", "C"}


def test_status_matching_is_case_insensitive(notes_dir):
    _write(notes_dir, "a", title="A", status="In-Progress")
    got = vault.list_notes_by_status(statuses=["in-progress"])
    assert [n["status"] for n in got] == ["in-progress"]


def test_tag_filter_matches_any_tag(notes_dir):
    _write(notes_dir, "a", title="A", status="open", tags=["mycelium", "todo"])
    _write(notes_dir, "b", title="B", status="open", tags=["whelmed"])
    got = vault.list_notes_by_status(tags=["whelmed"])
    assert [n["title"] for n in got] == ["B"]


def test_author_filter(notes_dir):
    _write(notes_dir, "a", title="A", status="open", author="liam")
    _write(notes_dir, "b", title="B", status="open", author="someone-else")
    got = vault.list_notes_by_status(author="liam")
    assert [n["title"] for n in got] == ["A"]


def test_ordering_is_status_band_then_newest_first(notes_dir):
    _write(notes_dir, "old_open", title="old open", status="open",
           updated="2026-01-01T00:00:00+00:00")
    _write(notes_dir, "new_open", title="new open", status="open",
           updated="2026-07-01T00:00:00+00:00")
    _write(notes_dir, "done", title="done one", status="done",
           updated="2026-07-20T00:00:00+00:00")
    _write(notes_dir, "wip", title="wip one", status="in-progress",
           updated="2026-01-01T00:00:00+00:00")
    _write(notes_dir, "blocked", title="blocked one", status="blocked",
           updated="2026-01-01T00:00:00+00:00")

    assert [n["title"] for n in vault.list_notes_by_status()] == [
        "wip one", "blocked one", "new open", "old open", "done one",
    ]


def test_subtasks_are_attached(notes_dir):
    _write(notes_dir, "a", title="A", status="in-progress",
           body="## TODO\n- [x] done bit\n- [ ] todo bit\n")
    (note,) = vault.list_notes_by_status()
    assert note["subtasks"] == [
        {"done": True, "text": "done bit"},
        {"done": False, "text": "todo bit"},
    ]


def test_missing_notes_dir_is_empty_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "NOTES_DIR", tmp_path / "nope")
    assert vault.list_notes_by_status() == []
