"""The last reminder, which may have ended at the lamp, in the app or here."""
from datetime import UTC, datetime

import pytest

from custom_components.pill_pal.sensor import (
    _last_reminder_attributes,
    _last_reminder_state,
    who_acknowledged,
)

from .conftest import fixture

ACKNOWLEDGED_BY = fixture("acknowledged_by.json")["cases"]

# Rows in the shape the lamp writes them (wire.cpp, `recentOutcomes` and `occurrences`).
DATA = {
    "schedules": [
        {"id": 1, "name": "Morning tablets", "recurrence": "daily", "time": "08:00",
         "durationMinutes": 60},
        {"id": 2, "name": "Evening tablets", "recurrence": "daily", "time": "20:00",
         "durationMinutes": 60},
    ],
    "occurrences": [],
    "recentOutcomes": [
        {"id": 30, "source": "native", "ownerId": 1, "state": "acknowledged",
         "endedAt": 1790560000000},
        {"id": 31, "source": "native", "ownerId": 2, "state": "expired",
         "endedAt": 1790600000000},
        {"id": 29, "source": "native", "ownerId": 2, "state": "cancelled",
         "endedAt": 1790610000000},
    ],
}


def _at(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat()


def test_the_latest_outcome_wins() -> None:
    # Cancelled by a schedule edit is not something anyone did to a reminder.
    assert _last_reminder_state(DATA, 9) == "missed"
    assert _last_reminder_attributes(DATA, 9) == {
        "name": "Evening tablets",
        "at": _at(1790600000000),
        "occurrence_id": 31,
        "by": None,
        "by_label": None,
    }


def test_an_acknowledgement_reads_as_done() -> None:
    data = {**DATA, "recentOutcomes": DATA["recentOutcomes"][:1]}
    assert _last_reminder_state(data, 9) == "done"
    assert _last_reminder_attributes(data, 9)["name"] == "Morning tablets"


def test_a_snooze_after_the_last_outcome() -> None:
    deadline = 1790600000000 + 5 * 60_000 + 600_000
    data = {
        **DATA,
        "occurrences": [
            {"id": 32, "source": "native", "ownerId": 1, "state": "snoozed",
             "start": 1790599000000, "end": 1790602600000, "snoozeDeadline": deadline,
             "name": "Morning tablets"},
        ],
    }
    assert _last_reminder_state(data, 9) == "snoozed"
    # For a snooze, when it runs out.
    assert _last_reminder_attributes(data, 9) == {
        "name": "Morning tablets",
        "at": _at(deadline),
        "occurrence_id": 32,
        "by": None,
        "by_label": None,
    }


def test_a_snooze_before_the_last_outcome_loses() -> None:
    # Snoozed at 1790599000000, ten minutes before its deadline, and the expiry came later.
    data = {
        **DATA,
        "occurrences": [
            {"id": 32, "source": "native", "ownerId": 1, "state": "snoozed",
             "start": 1790598000000, "end": 1790601600000,
             "snoozeDeadline": 1790599000000 + 600_000},
        ],
    }
    assert _last_reminder_state(data, 9) == "missed"


def test_an_external_reminder_has_no_schedule_name() -> None:
    data = {
        "recentOutcomes": [
            {"id": 40, "source": "external", "ownerId": 1, "state": "acknowledged",
             "endedAt": 1790560000000},
        ],
        "schedules": DATA["schedules"],
    }
    assert _last_reminder_attributes(data, 9)["name"] is None


def test_nothing_has_ended_yet() -> None:
    assert _last_reminder_state({}, 9) is None
    assert _last_reminder_attributes({"recentOutcomes": [], "occurrences": []}, 9) == {}


@pytest.mark.parametrize("case", ACKNOWLEDGED_BY, ids=[c["id"] for c in ACKNOWLEDGED_BY])
def test_who_acknowledged_reads_every_fixture_case(case) -> None:
    assert who_acknowledged(case["row"]) == case["reads"]


# What the sensor says for each fixture row, as the latest outcome. A row's `by` is read
# only for an acknowledgement; a superseded one is not a last reminder at all.
SENSOR = {
    "touch": ("done", "touch", None),
    "phone": ("done", "phone", "Kitchen phone"),
    "integration": ("done", "integration", "Home Assistant"),
    "cloud-grant": ("done", "cloud", "Webhook"),
    "grant-gone": ("done", "phone", None),
    "expired": ("missed", None, None),
    "superseded": None,
    "older-lamp": ("done", "unknown", None),
    "unknown-kind": ("done", "unknown", "Kitchen speaker"),
    "by-not-an-object": ("done", "unknown", None),
    "grant-id-as-string": ("done", "phone", "Kitchen phone"),
    "touch-with-a-grant": ("done", "touch", None),
}


@pytest.mark.parametrize("case", ACKNOWLEDGED_BY, ids=[c["id"] for c in ACKNOWLEDGED_BY])
def test_the_sensor_says_who_acknowledged(case) -> None:
    assert set(SENSOR) == {c["id"] for c in ACKNOWLEDGED_BY}
    data = {"schedules": [{"id": 3, "name": "Morning"}], "recentOutcomes": [case["row"]]}
    expected = SENSOR[case["id"]]
    if expected is None:
        assert _last_reminder_state(data, 9) is None
        return
    state, by, label = expected
    attributes = _last_reminder_attributes(data, 9)
    assert _last_reminder_state(data, 9) == state
    assert (attributes["by"], attributes["by_label"]) == (by, label)
    assert attributes["name"] == "Morning"


def test_an_expiry_from_a_lamp_that_does_not_say_has_no_one() -> None:
    # No `by` on an expiry is not "unknown": nobody acknowledges a missed reminder.
    data = {"recentOutcomes": [{"id": 1, "source": "native", "ownerId": 3, "state": "expired",
                                "endedAt": 1790560000000}]}
    assert _last_reminder_attributes(data, 9)["by"] is None
