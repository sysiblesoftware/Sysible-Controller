"""A schedule you cannot change is a schedule you have to rebuild from memory.

Reported from the Scheduled jobs screen: "Why am I unable to edit the schedules?"
The row offered Run now, Pause and Delete, and nothing else. Moving a nightly job
by half an hour, or adding one host to it, meant deleting it and recreating it —
re-ticking every target by hand, from memory, with the original already gone.

Nothing was missing on the server. PATCH /api/schedules/{id} has always taken a
full update: the Pause button is one. Only the way in was missing.

There is no JS test runner in this repo, so these hold the RULES the editor has to
keep. Two of them exist because they are silent when wrong, and both were driven in
a real browser against the shipping component before they were written down:

  * `enabled` — a paused job edited and saved came back with enabled:false, so
    editing it does not quietly resume it.
  * `tz` — the job was created in America/Chicago and edited from a UTC browser;
    the PATCH carried America/Chicago. Re-stamping it with the editor's own zone
    would move a 02:00 run by six hours without anyone touching the time field,
    which is the bug the timezone handling already carries a comment about.

Also driven: Edit pre-fills name, action, time and every target (3 targets, all
ticked), a changed time PATCHes job-1 with `at: "03:30"` and everything else
intact, and the editor closes on save.
"""
import re
from pathlib import Path

import pytest

VIEW = (Path(__file__).resolve().parents[1]
        / "webgui" / "frontend" / "src" / "views" / "Schedules.jsx")


@pytest.fixture(scope="module")
def src():
    return VIEW.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def save_body(src):
    """The object the form sends — create and edit share it, so it is the one
    place where any of these rules can be broken."""
    m = re.search(r"const body = \{(.+?)\n    \};", src, re.S)
    assert m, "the form no longer builds one body for both create and edit"
    return m.group(1)


# ---- the reported gap ------------------------------------------------------
def test_a_row_offers_a_way_in(src):
    assert '"Close" : "Edit"' in src or ">Edit<" in src or '"Edit"' in src, \
        "the schedule rows offer no Edit control again"


def test_the_editor_is_wired_to_the_row_it_was_opened_from(src):
    assert "const edit = editing === j.id" in src, \
        "nothing tracks WHICH schedule is being edited"
    assert re.search(r"\{edit && \(", src), "the editor is never rendered"
    assert re.search(r"<ScheduleForm key=\{j\.id\}[^>]*job=\{j\}", src, re.S), \
        "the editor is not handed the job it is editing"


def test_creating_and_editing_are_the_same_form(src):
    """Two forms drift: a field added to one is missing from the other, and the
    one that is missing it silently resets that field on every save."""
    assert src.count("function ScheduleForm(") == 1
    assert "function NewSchedule(" not in src, "there are two forms again"
    assert "<ScheduleForm" in src


def test_an_edit_updates_rather_than_creating_a_duplicate(src):
    assert re.search(r"if \(editing\) await api\.scheduleUpdate\(job\.id, body\);", src), \
        "saving an edit no longer PATCHes the job it was opened from"
    assert "await api.scheduleCreate(body)" in src


# ---- the two that are silent when wrong ------------------------------------
def test_editing_a_paused_schedule_does_not_resume_it(save_body):
    """Pause, then fix a typo in the name, and the job starts running again
    tonight — with nothing on screen having said so."""
    m = re.search(r"enabled: (.+?),", save_body)
    assert m, "the body no longer carries enabled"
    assert "editing" in m.group(1) and "job.enabled" in m.group(1), \
        f"an edit does not preserve the paused state: enabled: {m.group(1)}"


def test_an_edit_keeps_the_schedule_s_own_timezone(save_body):
    """The time was set in the zone it fires in. Stamping the editor's browser
    zone over it moves the run by the difference between them, without the time
    field being touched — the '02:00 shows as 22:00' bug, from the other end."""
    m = re.search(r"tz: (.+?),", save_body)
    assert m, "the body no longer carries tz"
    expr = m.group(1)
    assert "editing" in expr and "job.tz" in expr, \
        f"an edit re-stamps the schedule's timezone: tz: {expr}"
    assert "browserTz" in expr, "a job with no recorded zone gets none at all"


def test_a_new_schedule_still_uses_the_browser_s_zone(src, save_body):
    assert "const browserTz = Intl.DateTimeFormat().resolvedOptions().timeZone" in src
    assert re.search(r"tz: editing \? \(job\.tz \|\| browserTz\) : browserTz", save_body)


# ---- nothing may reset itself on the way through ---------------------------
@pytest.mark.parametrize("field", ["name", "action", "arg", "cadence", "at", "weekday"])
def test_every_field_is_seeded_from_the_job(src, field):
    """A field the form does not read back is a field that silently reverts to
    its default the first time anyone edits anything else."""
    init = re.search(r"const \[f, setF\] = useState\(\{(.+?)\n  \}\);", src, re.S)
    assert init, "the form's initial state is gone"
    assert re.search(rf"\b{field}: job\?\.{field}", init.group(1)), \
        f"{field} is not seeded from the schedule being edited"


def test_the_targets_come_back_ticked(src):
    """The whole cost of delete-and-recreate: re-selecting every host by hand."""
    assert "useState(job?.targets || [])" in src, \
        "the editor opens with no targets selected, so saving would clear them"


def test_an_action_this_build_does_not_advertise_is_still_offered(src):
    """The select's value has to exist as an option, or the browser shows the
    first one instead and saving repoints the job to it."""
    m = re.search(r"const actionKeys = useMemo\(\(\) => \{(.+?)\}, ", src, re.S)
    assert m, "the action list is no longer computed from the job as well as the meta"
    assert "keys.includes(f.action)" in m.group(1)
    assert "[f.action, ...keys]" in m.group(1)
    assert "{meta.actions[k] || k}" in src, "such an action renders as a blank option"


# ---- and the two panels must not fight over the row ------------------------
def test_opening_one_editor_closes_the_other(src):
    assert "setShowNew(false); setEditing(j.id)" in src or \
           "setShowNew(false); setEditing(edit ? null : j.id)" in src, \
        "Edit leaves the New schedule form open above it"
    assert "setEditing(null); setShowNew((v) => !v)" in src, \
        "New schedule leaves a row's editor open below it"


def test_the_editor_can_be_closed_without_saving(src):
    assert "onCancel" in src and re.search(r"onClick=\{onCancel\}", src), \
        "there is no way out of the form but saving"
