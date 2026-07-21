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


# --- validate_status -------------------------------------------------------

@pytest.mark.parametrize("value", ["open", "in-progress", "blocked", "done", "wont-fix"])
def test_validate_status_accepts_canonical(value):
    assert vault.validate_status(value) == value


@pytest.mark.parametrize("value", [" Open ", "IN-PROGRESS", "Done"])
def test_validate_status_normalises_case_and_space(value):
    assert vault.validate_status(value) == value.strip().lower()


@pytest.mark.parametrize("value", ["active", "backlog", "closed", "shipped", "wip"])
def test_validate_status_rejects_drift(value):
    with pytest.raises(ValueError, match="Unknown status"):
        vault.validate_status(value)


def test_write_note_rejects_non_canonical_status(notes_dir):
    with pytest.raises(ValueError, match="Unknown status"):
        vault.write_note("A title", "body", status="active")
    assert list(notes_dir.glob("*.md")) == []


def test_write_note_accepts_canonical_status(notes_dir):
    vault.write_note("A title", "body", status="in-progress")
    (note,) = vault.list_tracked_notes()
    assert note["status"] == "in-progress"


def test_upsert_of_a_legacy_note_is_not_blocked(notes_dir):
    """A note already carrying a drifted status must stay editable — the guard
    checks the incoming value, not what's on disk."""
    _write(notes_dir, "a-title", title="A title", status="active")
    vault.write_note("A title", "new body", status=None)
    (note,) = vault.list_tracked_notes()
    assert note["status"] == "active"
    assert "new body" in note["content"]


def test_clearing_status_still_works(notes_dir):
    _write(notes_dir, "a-title", title="A title", status="open")
    vault.write_note("A title", "body", status="")
    assert vault.list_tracked_notes() == []


# --- list_tracked_notes ----------------------------------------------------

def test_notes_without_status_are_not_work_items(notes_dir):
    _write(notes_dir, "synthesis", title="Some synthesis", status=None)
    _write(notes_dir, "tracked", title="Tracked thing", status="open")
    got = vault.list_tracked_notes()
    assert [n["title"] for n in got] == ["Tracked thing"]


def test_default_returns_every_status(notes_dir):
    _write(notes_dir, "a", title="A", status="open")
    _write(notes_dir, "b", title="B", status="done")
    assert {n["title"] for n in vault.list_tracked_notes()} == {"A", "B"}


def test_status_is_normalised_to_lowercase(notes_dir):
    _write(notes_dir, "a", title="A", status="In-Progress")
    assert [n["status"] for n in vault.list_tracked_notes()] == ["in-progress"]


def test_non_canonical_statuses_are_kept_not_dropped(notes_dir):
    """The vault has real notes on `active`/`backlog`; enumeration must return
    them so the drift is visible to the caller."""
    _write(notes_dir, "a", title="A", status="open")
    _write(notes_dir, "b", title="B", status="active")
    got = vault.list_tracked_notes()
    assert {n["title"] for n in got} == {"A", "B"}
    # ...and sort after the canonical ones rather than crashing the ranker.
    assert [n["title"] for n in got] == ["A", "B"]


def test_tag_filter_matches_any_tag(notes_dir):
    _write(notes_dir, "a", title="A", status="open", tags=["mycelium", "todo"])
    _write(notes_dir, "b", title="B", status="open", tags=["whelmed"])
    got = vault.list_tracked_notes(tags=["whelmed"])
    assert [n["title"] for n in got] == ["B"]


def test_author_filter(notes_dir):
    _write(notes_dir, "a", title="A", status="open", author="liam")
    _write(notes_dir, "b", title="B", status="open", author="someone-else")
    got = vault.list_tracked_notes(author="liam")
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

    assert [n["title"] for n in vault.list_tracked_notes()] == [
        "wip one", "blocked one", "new open", "old open", "done one",
    ]


def test_subtasks_are_attached(notes_dir):
    _write(notes_dir, "a", title="A", status="in-progress",
           body="## TODO\n- [x] done bit\n- [ ] todo bit\n")
    (note,) = vault.list_tracked_notes()
    assert note["subtasks"] == [
        {"done": True, "text": "done bit"},
        {"done": False, "text": "todo bit"},
    ]


def test_missing_notes_dir_is_empty_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "NOTES_DIR", tmp_path / "nope")
    assert vault.list_tracked_notes() == []


# --- the list_todos tool ---------------------------------------------------

@pytest.fixture
def todos(notes_dir):
    """The list_todos tool, bound to the tmp vault."""
    import json as _json
    from mycelium import server

    def call(**kw):
        return _json.loads(server.list_todos(**kw))

    return call


def test_default_status_is_the_unfinished_set(todos, notes_dir):
    _write(notes_dir, "a", title="A", status="open")
    _write(notes_dir, "b", title="B", status="in-progress")
    _write(notes_dir, "c", title="C", status="blocked")
    _write(notes_dir, "d", title="D", status="done")
    _write(notes_dir, "e", title="E", status="wont-fix")
    out = todos()
    assert [t["title"] for t in out["todos"]] == ["B", "C", "A"]


def test_open_means_literally_open_not_the_unfinished_set(todos, notes_dir):
    """`open` is one of the five canonical values, so it must never be treated
    as a synonym for "everything outstanding" — a caller asking for `open` gets
    exactly the notes whose frontmatter says `status: open`. This is the
    grep-equivalence the read-API TODO set as its acceptance criterion."""
    _write(notes_dir, "a", title="A", status="open")
    _write(notes_dir, "b", title="B", status="in-progress")
    _write(notes_dir, "c", title="C", status="blocked")
    assert [t["title"] for t in todos(status="open")["todos"]] == ["A"]
    assert todos(status="unfinished")["matched"] == 3


def test_status_any_includes_closed_work(todos, notes_dir):
    _write(notes_dir, "a", title="A", status="open")
    _write(notes_dir, "b", title="B", status="done")
    assert todos(status="any")["matched"] == 2


def test_comma_separated_status(todos, notes_dir):
    _write(notes_dir, "a", title="A", status="open")
    _write(notes_dir, "b", title="B", status="blocked")
    _write(notes_dir, "c", title="C", status="done")
    out = todos(status="blocked,done")
    assert {t["title"] for t in out["todos"]} == {"B", "C"}


def test_census_covers_all_tracked_notes_not_just_matched(todos, notes_dir):
    _write(notes_dir, "a", title="A", status="open")
    _write(notes_dir, "b", title="B", status="done")
    _write(notes_dir, "c", title="C", status=None)
    out = todos()
    assert out["matched"] == 1
    assert out["total_tracked"] == 2
    assert out["counts_by_status"] == {"open": 1, "done": 1}


def test_non_canonical_statuses_are_reported(todos, notes_dir):
    _write(notes_dir, "a", title="A", status="open")
    _write(notes_dir, "b", title="B", status="active")
    _write(notes_dir, "c", title="C", status="backlog")
    out = todos()
    assert out["non_canonical_statuses"] == {"active": 1, "backlog": 1}
    assert "note" in out


def test_no_drift_means_no_warning_fields(todos, notes_dir):
    _write(notes_dir, "a", title="A", status="open")
    out = todos()
    assert "non_canonical_statuses" not in out
    assert "note" not in out


def test_truncation_keeps_the_most_actionable(todos, notes_dir):
    _write(notes_dir, "a", title="A", status="open")
    _write(notes_dir, "b", title="B", status="in-progress")
    out = todos(n_results=1)
    assert [t["title"] for t in out["todos"]] == ["B"]
    assert out["truncated"] is True
    assert out["matched"] == 2


def test_subtask_counts_always_present_body_optional(todos, notes_dir):
    _write(notes_dir, "a", title="A", status="open",
           body="- [x] one\n- [ ] two\n")
    compact = todos(include_subtasks=False)["todos"][0]
    assert (compact["subtasks_done"], compact["subtasks_total"]) == (1, 2)
    assert "subtasks" not in compact
    full = todos(include_subtasks=True)["todos"][0]
    assert len(full["subtasks"]) == 2
