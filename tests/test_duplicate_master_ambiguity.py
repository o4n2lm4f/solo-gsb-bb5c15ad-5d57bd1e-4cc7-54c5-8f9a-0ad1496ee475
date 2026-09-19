"""Regression tests for ambiguous UID groups (more than one master VEVENT).

A UID group may contain exactly one master VEVENT (a component without
``RECURRENCE-ID``) plus any number of ``RECURRENCE-ID`` overrides. The
recurrence kernel used to accept groups with several masters and silently
expand whichever master source order placed first. The grouping stage now
recognizes the ambiguity before any expansion/provenance work and raises
``ExpansionError``; every interface built on the shared kernel must surface
it as HTTP 422 with the same message naming the UID and the master count.

These tests lock that behavior across /expand, /provenance and every
interface reusing the expander (/agenda, /alarms, /attendance,
/conflicts, /availability, /freebusy-export, /calendar-diff and
/fair-rotation), while guarding the two legal shapes that must not be
flagged: one master plus overrides, and several overrides sharing one
RECURRENCE-ID (the last one wins).

Run from the repository root:  python -m pytest tests/test_duplicate_master_ambiguity.py -v
"""

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app.expander import expand_calendar
from app.main import app
from app.recurrence import (
    ExpansionError,
    _parse_calendar,
    compute_candidates,
    group_events_by_uid,
)

client = TestClient(app)

CAL_HEAD = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//ambiguous//EN\r\n"
CAL_TAIL = "END:VCALENDAR\r\n"

JAN_START = "2026-01-01T00:00:00Z"
JAN_END = "2026-02-01T00:00:00Z"


def cal(*events: str) -> str:
    return CAL_HEAD + "".join(events) + CAL_TAIL


def event(*lines: str) -> str:
    return "BEGIN:VEVENT\r\n" + "".join(line + "\r\n" for line in lines) + "END:VEVENT\r\n"


def two_masters(uid="dup") -> str:
    """Two plain (no RECURRENCE-ID) VEVENTs sharing one UID."""
    return cal(
        event(
            f"UID:{uid}",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
            "SUMMARY:first master",
        ),
        event(
            f"UID:{uid}",
            "DTSTART:20260106T140000Z",
            "DTEND:20260106T150000Z",
            "SUMMARY:second master",
        ),
    )


def one_master_with_overrides() -> str:
    """Legal shape: one master, several overrides (two share one RID)."""
    return cal(
        event(
            "UID:series",
            "DTSTART:20260105T090000Z",
            "DTEND:20260105T100000Z",
            "RRULE:FREQ=DAILY;COUNT=4",
            "SUMMARY:daily",
        ),
        event(  # first override for Jan 6...
            "UID:series",
            "RECURRENCE-ID:20260106T090000Z",
            "DTSTART:20260106T100000Z",
            "DTEND:20260106T110000Z",
            "SUMMARY:older override",
        ),
        event(  # ...and a later one for the same RID: last one must win
            "UID:series",
            "RECURRENCE-ID:20260106T090000Z",
            "DTSTART:20260106T120000Z",
            "DTEND:20260106T130000Z",
            "SUMMARY:winning override",
        ),
        event(  # plus an ordinary override on another slot
            "UID:series",
            "RECURRENCE-ID:20260107T090000Z",
            "DTSTART:20260107T150000Z",
            "DTEND:20260107T160000Z",
        ),
    )


# ---------------------------------------------------------------------------
# Kernel level: ambiguity is recognized at grouping time
# ---------------------------------------------------------------------------


def test_grouping_rejects_multiple_masters_naming_uid_and_count():
    with pytest.raises(ExpansionError) as excinfo:
        group_events_by_uid(_parse_calendar(two_masters("evt-7")))
    message = str(excinfo.value)
    assert "evt-7" in message
    assert "2" in message
    assert "master" in message


def test_grouping_rejects_three_masters_with_exact_count():
    ics = cal(
        event("UID:tri", "DTSTART:20260105T090000Z"),
        event("UID:tri", "DTSTART:20260106T090000Z"),
        event("UID:tri", "DTSTART:20260107T090000Z"),
    )
    with pytest.raises(ExpansionError) as excinfo:
        group_events_by_uid(_parse_calendar(ics))
    assert "3 master VEVENTs" in str(excinfo.value)


def test_ambiguity_is_reported_even_when_other_uids_are_clean():
    ics = (
        CAL_HEAD
        + event("UID:fine", "DTSTART:20260105T090000Z")
        + event("UID:dup", "DTSTART:20260105T090000Z")
        + event("UID:dup", "DTSTART:20260106T090000Z")
        + CAL_TAIL
    )
    with pytest.raises(ExpansionError) as excinfo:
        group_events_by_uid(_parse_calendar(ics))
    assert "'dup'" in str(excinfo.value)


def test_compute_candidates_also_rejects_directly_built_multi_master_group():
    # Even bypassing group_events_by_uid, the kernel must not silently keep
    # the first master.
    events = list(_parse_calendar(two_masters()).walk("VEVENT"))
    window = (
        dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        dt.datetime(2026, 2, 1, tzinfo=dt.timezone.utc),
    )
    with pytest.raises(ExpansionError):
        compute_candidates("dup", events, *window)


def test_one_master_with_several_overrides_stays_valid():
    # Same-RID duplicates are overrides, never duplicate masters.
    groups = group_events_by_uid(_parse_calendar(one_master_with_overrides()))
    assert set(groups) == {"series"}
    ws = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    we = dt.datetime(2026, 2, 1, tzinfo=dt.timezone.utc)
    instances = expand_calendar(one_master_with_overrides(), ws, we)
    by_start = {i.start: i for i in instances}
    # Last override for Jan 6 wins (12:00, "winning override"), the older
    # one must not leak through as a second instance.
    winning = by_start[dt.datetime(2026, 1, 6, 12, tzinfo=dt.timezone.utc)]
    assert winning.summary == "winning override"
    assert dt.datetime(2026, 1, 6, 10, tzinfo=dt.timezone.utc) not in by_start
    # Untouched and separately overridden slots still expand.
    assert dt.datetime(2026, 1, 5, 9, tzinfo=dt.timezone.utc) in by_start
    assert dt.datetime(2026, 1, 7, 15, tzinfo=dt.timezone.utc) in by_start
    assert dt.datetime(2026, 1, 8, 9, tzinfo=dt.timezone.utc) in by_start


def test_overrides_without_any_master_are_still_accepted():
    # A master-less group (orphan overrides only) is not ambiguous.
    ics = cal(
        event(
            "UID:orphan",
            "RECURRENCE-ID:20260106T090000Z",
            "DTSTART:20260106T150000Z",
            "DTEND:20260106T160000Z",
        ),
        event(
            "UID:orphan",
            "RECURRENCE-ID:20260107T090000Z",
            "DTSTART:20260107T150000Z",
            "DTEND:20260107T160000Z",
        ),
    )
    ws = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    we = dt.datetime(2026, 2, 1, tzinfo=dt.timezone.utc)
    instances = expand_calendar(ics, ws, we)
    assert {i.start for i in instances} == {
        dt.datetime(2026, 1, 6, 15, tzinfo=dt.timezone.utc),
        dt.datetime(2026, 1, 7, 15, tzinfo=dt.timezone.utc),
    }


# ---------------------------------------------------------------------------
# HTTP level: every interface on the shared kernel answers 422 identically
# ---------------------------------------------------------------------------


def assert_ambiguity_422(response):
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "'dup'" in detail
    assert "2 master VEVENTs" in detail
    return detail


def test_expand_rejects_duplicate_masters_with_422():
    resp = client.post(
        "/expand",
        json={"ics": two_masters(), "window_start": JAN_START, "window_end": JAN_END},
    )
    assert_ambiguity_422(resp)


def test_provenance_rejects_duplicate_masters_with_422():
    resp = client.post(
        "/provenance",
        json={
            "ics": two_masters(),
            "uid": "dup",
            "window_start": JAN_START,
            "window_end": JAN_END,
        },
    )
    assert_ambiguity_422(resp)


def test_provenance_rejects_even_for_a_different_uid_in_same_calendar():
    # Grouping validates the whole calendar before the requested UID is
    # projected, exactly like the other eager calendar checks.
    ics = (
        CAL_HEAD
        + event("UID:dup", "DTSTART:20260105T090000Z")
        + event("UID:dup", "DTSTART:20260106T090000Z")
        + event("UID:other", "DTSTART:20260108T090000Z")
        + CAL_TAIL
    )
    resp = client.post(
        "/provenance",
        json={
            "ics": ics,
            "uid": "other",
            "window_start": JAN_START,
            "window_end": JAN_END,
        },
    )
    assert resp.status_code == 422
    assert "'dup'" in resp.json()["detail"]


def test_expand_and_provenance_share_the_identical_422_detail():
    exp = client.post(
        "/expand",
        json={"ics": two_masters(), "window_start": JAN_START, "window_end": JAN_END},
    )
    prov = client.post(
        "/provenance",
        json={
            "ics": two_masters(),
            "uid": "dup",
            "window_start": JAN_START,
            "window_end": JAN_END,
        },
    )
    assert exp.status_code == prov.status_code == 422
    assert exp.json()["detail"] == prov.json()["detail"]


def test_agenda_rejects_duplicate_masters_with_422():
    resp = client.post(
        "/agenda",
        json={
            "ics": two_masters(),
            "window_start": JAN_START,
            "window_end": JAN_END,
            "timezone": "Asia/Tokyo",
        },
    )
    assert_ambiguity_422(resp)


def test_alarms_rejects_duplicate_masters_with_422():
    resp = client.post(
        "/alarms",
        json={"ics": two_masters(), "window_start": JAN_START, "window_end": JAN_END},
    )
    assert_ambiguity_422(resp)


def test_attendance_rejects_duplicate_masters_with_422():
    resp = client.post(
        "/attendance",
        json={"ics": two_masters(), "window_start": JAN_START, "window_end": JAN_END},
    )
    assert_ambiguity_422(resp)


def test_conflicts_rejects_duplicate_masters_with_422():
    resp = client.post(
        "/conflicts",
        json={
            "calendars": {"work": two_masters()},
            "window_start": "2026-01-05T00:00:00Z",
            "window_end": "2026-01-07T00:00:00Z",
            "minimum_concurrency": 2,
        },
    )
    assert_ambiguity_422(resp)


def test_availability_rejects_duplicate_masters_with_422():
    resp = client.post(
        "/availability",
        json={
            "calendars": {"work": two_masters()},
            "window_start": "2026-01-05T00:00:00Z",
            "window_end": "2026-01-10T00:00:00Z",
            "timezone": "UTC",
            "working_hours": {
                "monday": [{"start": "00:00", "end": "23:59"}],
                "tuesday": [{"start": "00:00", "end": "23:59"}],
                "wednesday": [{"start": "00:00", "end": "23:59"}],
                "thursday": [{"start": "00:00", "end": "23:59"}],
                "friday": [{"start": "00:00", "end": "23:59"}],
            },
            "minimum_minutes": 15,
        },
    )
    assert_ambiguity_422(resp)


def test_freebusy_export_rejects_duplicate_masters_with_422():
    resp = client.post(
        "/freebusy-export",
        json={
            "calendars": [
                {"calendar_id": "cal", "ics": two_masters(), "timezone": "UTC"}
            ],
            "window_start": JAN_START,
            "window_end": JAN_END,
        },
    )
    assert_ambiguity_422(resp)


def test_calendar_diff_rejects_duplicate_masters_in_either_side():
    good = cal(event("UID:solo", "DTSTART:20260105T090000Z", "DTEND:20260105T100000Z"))
    bad_old = client.post(
        "/calendar-diff",
        json={
            "old_ics": two_masters(),
            "new_ics": good,
            "window_start": JAN_START,
            "window_end": JAN_END,
        },
    )
    assert_ambiguity_422(bad_old)
    bad_new = client.post(
        "/calendar-diff",
        json={
            "old_ics": good,
            "new_ics": two_masters(),
            "window_start": JAN_START,
            "window_end": JAN_END,
        },
    )
    assert_ambiguity_422(bad_new)


def test_fair_rotation_rejects_duplicate_masters_in_participant_calendar():
    resp = client.post(
        "/fair-rotation",
        json={
            "organizer_timezone": "UTC",
            "dates": ["2026-01-05"],
            "candidate_times": ["09:00"],
            "duration_minutes": 30,
            "participants": [
                {"participant_id": "alice", "timezone": "UTC", "ics": two_masters()}
            ],
        },
    )
    assert resp.status_code == 422
    assert "dup" in resp.json()["detail"]
    assert "2 master VEVENTs" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# The legal shapes still pass through every shared-kernel interface
# ---------------------------------------------------------------------------


def test_legal_one_master_plus_overrides_expands_through_every_interface():
    ics = one_master_with_overrides()
    expand_resp = client.post(
        "/expand",
        json={"ics": ics, "window_start": JAN_START, "window_end": JAN_END},
    )
    assert expand_resp.status_code == 200
    prov_resp = client.post(
        "/provenance",
        json={
            "ics": ics,
            "uid": "series",
            "window_start": JAN_START,
            "window_end": JAN_END,
        },
    )
    assert prov_resp.status_code == 200
    agenda_resp = client.post(
        "/agenda",
        json={
            "ics": ics,
            "window_start": JAN_START,
            "window_end": JAN_END,
            "timezone": "UTC",
        },
    )
    assert agenda_resp.status_code == 200
    alarms_resp = client.post(
        "/alarms",
        json={"ics": ics, "window_start": JAN_START, "window_end": JAN_END},
    )
    assert alarms_resp.status_code == 200
    attendance_resp = client.post(
        "/attendance",
        json={"ics": ics, "window_start": JAN_START, "window_end": JAN_END},
    )
    assert attendance_resp.status_code == 200
    fb_resp = client.post(
        "/freebusy-export",
        json={
            "calendars": [{"calendar_id": "cal", "ics": ics, "timezone": "UTC"}],
            "window_start": JAN_START,
            "window_end": JAN_END,
        },
    )
    assert fb_resp.status_code == 200
    diff_resp = client.post(
        "/calendar-diff",
        json={
            "old_ics": ics,
            "new_ics": ics,
            "window_start": JAN_START,
            "window_end": JAN_END,
        },
    )
    assert diff_resp.status_code == 200
    conflicts_resp = client.post(
        "/conflicts",
        json={
            "calendars": {"work": ics},
            "window_start": JAN_START,
            "window_end": JAN_END,
            "minimum_concurrency": 2,
        },
    )
    assert conflicts_resp.status_code == 200
