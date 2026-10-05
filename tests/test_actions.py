"""Actions become the payloads the lamp's contract defines (indicator_payloads.json)."""
from datetime import UTC, datetime

import pytest

from custom_components.pill_pal import SCHEMAS, _payload

from .conftest import fixture

CASES = {case["id"]: case for case in fixture("indicator_payloads.json")["cases"]}


def build(service: str, data: dict) -> tuple[str, dict | None]:
    return _payload(service, SCHEMAS[service]({"device_id": "d", **data}))


def test_notify_matches_the_contract() -> None:
    operation, payload = build("notify", {"pattern": "pulse", "color": [0, 255, 0], "duration": 5})
    assert operation == "notify"
    assert payload == CASES["notify-pulse"]["canonical"]


def test_rainbow_sends_no_colour() -> None:
    _, payload = build("notify", {"pattern": "rainbow", "duration": 60})
    assert "colour" not in payload
    assert payload["durationSeconds"] == 60


def test_status_with_everything() -> None:
    when = datetime.fromtimestamp(CASES["status-everything"]["canonical"]["clearsAt"] / 1000, tz=UTC)
    _, payload = build(
        "set_status",
        {
            "status_id": "door.front-1_a",
            "color": [0, 0, 255],
            "pattern": "pulse",
            "brightness": 40,
            "label": "Front door",
            "priority": "high",
            "clears_at": when,
        },
    )
    canonical = CASES["status-everything"]["canonical"]
    assert payload == canonical


def test_status_ids_the_lamp_refuses_are_refused_here() -> None:
    import pytest
    import voluptuous as vol

    for bad in ("garage door", "a" * 24, "", "garagé"):
        with pytest.raises(vol.Invalid):
            SCHEMAS["set_status"]({"device_id": "d", "status_id": bad, "color": [1, 2, 3]})


def test_gauge_matches_the_contract() -> None:
    _, payload = build(
        "set_gauge",
        {"value": 1, "color": [255, 0, 0], "color_end": [0, 255, 0], "stale_after": 60},
    )
    assert payload == CASES["gauge-full-gradient"]["canonical"]


def test_occurrence_actions() -> None:
    assert build("acknowledge", {"occurrence_id": 7}) == ("acknowledge", {"occurrenceId": 7})
    assert build("snooze", {"occurrence_id": 7}) == ("snooze", {"occurrenceId": 7})
    assert build("clear_gauge", {}) == ("clear_gauge", None)


# ---- Countdown: every start_countdown and cancel_countdown case in the contract ----------

COUNTDOWN_CASES = [
    case for case in CASES.values()
    if case["operation"] in ("start_countdown", "cancel_countdown")
]


def _countdown_action(payload: dict) -> dict:
    """The action fields a person would fill in to send this contract payload."""
    data: dict = {}
    if "durationSeconds" in payload:
        data["duration"] = payload["durationSeconds"]
    colour = payload.get("colour")
    if isinstance(colour, str) and len(colour) == 7 and colour.startswith("#"):
        data["color"] = [int(colour[i:i + 2], 16) for i in (1, 3, 5)]
    elif colour is not None:
        data["color"] = colour
    end = payload.get("end")
    if isinstance(end, dict):
        if end.get("style") is not None:
            data["end_style"] = end["style"]
        if end.get("pattern") is not None:
            data["end_pattern"] = end["pattern"]
    elif end is not None:
        data["end_style"] = end
    if payload.get("calm") is not None:
        data["calm"] = payload["calm"]
    return data


def _well_typed(payload: dict) -> bool:
    duration, end, calm = payload["durationSeconds"], payload["end"], payload["calm"]
    return (
        type(duration) is int
        and isinstance(payload["colour"], str)
        and isinstance(end, dict)
        and all(isinstance(end[k], str) for k in ("style", "pattern"))
        and type(calm) is bool
    )


def test_every_countdown_case_is_covered() -> None:
    assert len(COUNTDOWN_CASES) == 26


@pytest.mark.parametrize("case", COUNTDOWN_CASES, ids=[c["id"] for c in COUNTDOWN_CASES])
def test_countdown_matches_the_contract(case: dict) -> None:
    import voluptuous as vol

    if case["operation"] == "cancel_countdown":
        operation, payload = build("cancel_countdown", {})
        assert operation == "cancel_countdown"
        # No payload; the lamp reads an absent one as {}.
        assert (payload or {}) == case["canonical"]
        return

    data = _countdown_action(case["payload"] or {})
    if case["expect"] == "valid":
        assert build("start_countdown", data) == ("start_countdown", case["canonical"])
    elif case["expect"] == "invalid_payload":
        # Refused here with a clear message, before the lamp sees it.
        with pytest.raises(vol.Invalid):
            build("start_countdown", data)
    else:
        # A wrong type on the wire. Home Assistant either refuses the value or converts it
        # the way it converts any field ("60" seconds, calm 1), so what is sent is never
        # malformed.
        assert case["expect"] == "malformed"
        try:
            _, payload = build("start_countdown", data)
        except vol.Invalid:
            return
        assert _well_typed(payload)


def test_countdown_duration_from_the_duration_selector() -> None:
    _, payload = build(
        "start_countdown",
        {"duration": {"hours": 0, "minutes": 25, "seconds": 0}, "color": [51, 102, 255]},
    )
    assert payload == CASES["countdown-from-an-integration"]["canonical"]
    _, payload = build("start_countdown", {"duration": "12:00:00", "color": [1, 2, 3]})
    assert payload["durationSeconds"] == 43_200


def test_countdown_limits_say_what_is_wrong() -> None:
    import voluptuous as vol

    with pytest.raises(vol.Invalid, match="10 seconds to 12 hours"):
        build("start_countdown", {"duration": {"hours": 13}, "color": [1, 2, 3]})
    with pytest.raises(vol.Invalid, match="whole number of seconds"):
        build("start_countdown", {"duration": 60.5, "color": [1, 2, 3]})
