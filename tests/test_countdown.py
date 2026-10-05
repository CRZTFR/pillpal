"""The countdown: two actions, and two sensors mirroring it from whichever controller."""
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send

from custom_components.pill_pal.api import PillPalCommandError
from custom_components.pill_pal.const import DOMAIN, SIGNAL_TOUCH

from .test_init import DEVICE, SNAPSHOT, entry, lamp  # noqa: F401  (fixtures)

# The contract's example (indicators.md, "Reported state"): started from the phone at grant
# 2, whose status gives it a name here too.
COUNTDOWN = {"id": 12, "owner": 2, "ownerLabel": "Kitchen phone", "durationSeconds": 720,
             "colour": "#ff8800", "end": {"style": "until_tapped", "pattern": "pulse"},
             "calm": False, "phase": "running", "remainingSeconds": 431,
             "endsAt": 1789636031000, "escalated": False, "shown": True}
ENDED = {"id": 11, "reason": "finished", "durationSeconds": 240, "at": 1789635400000}

CAPABLE = {**SNAPSHOT, "capabilities": {"overlapLimit": 2, "countdown": True}}

ENDS = "sensor.pill_pal_countdown_ends"
LAST = "sensor.pill_pal_last_countdown"


def _with(snapshot: dict = CAPABLE, **indicators) -> dict:
    return {**snapshot, "indicators": {**snapshot["indicators"], **indicators}}


async def _setup(hass: HomeAssistant, entry) -> None:  # noqa: F811
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _device(hass: HomeAssistant) -> str:
    return dr.async_get(hass).async_get_device(identifiers={(DOMAIN, DEVICE)}).id


async def test_no_sensors_on_a_lamp_without_the_countdown(hass: HomeAssistant, entry, lamp) -> None:  # noqa: F811
    await _setup(hass, entry)
    assert hass.states.get(ENDS) is None
    assert hass.states.get(LAST) is None


async def test_sensors_appear_when_the_lamp_states_it(hass: HomeAssistant, entry, lamp) -> None:  # noqa: F811
    # After a firmware update, without reloading the integration.
    await _setup(hass, entry)
    coordinator = entry.runtime_data.coordinator
    coordinator.async_set_updated_data(CAPABLE)
    await hass.async_block_till_done()
    assert hass.states.get(ENDS).state == "unknown"
    assert hass.states.get(ENDS).attributes.get("phase") is None
    assert hass.states.get(LAST).state == "unknown"

    # A second snapshot adds nothing twice.
    coordinator.async_set_updated_data(_with(countdown=COUNTDOWN))
    await hass.async_block_till_done()
    assert len([s for s in hass.states.async_entity_ids("sensor") if "countdown" in s]) == 2

    # Back to firmware without it.
    coordinator.async_set_updated_data(SNAPSHOT)
    await hass.async_block_till_done()
    assert hass.states.get(ENDS).state == "unavailable"
    assert hass.states.get(LAST).state == "unavailable"


@pytest.fixture
def capable_lamp(lamp):  # noqa: F811
    with patch("custom_components.pill_pal.api.PillPalClient.state", AsyncMock(return_value=CAPABLE)):
        yield lamp


async def test_a_countdown_started_on_the_phone(hass: HomeAssistant, entry, capable_lamp) -> None:  # noqa: F811
    await _setup(hass, entry)
    coordinator = entry.runtime_data.coordinator
    coordinator.async_set_updated_data(_with(countdown=COUNTDOWN, countdownEnded=ENDED))
    await hass.async_block_till_done()

    ends = hass.states.get(ENDS)
    assert ends.state == "2026-09-17T09:07:11+00:00"
    assert ends.attributes["device_class"] == "timestamp"
    assert {k: v for k, v in ends.attributes.items() if k not in ("device_class", "friendly_name")} == {
        "countdown_id": 12,
        "phase": "running",
        "remaining_seconds": 431,
        "duration_seconds": 720,
        "color": "#ff8800",
        "end_style": "until_tapped",
        "end_pattern": "pulse",
        "calm": False,
        "escalated": False,
        "shown": True,
        "set_by": "Kitchen phone",
        "set_by_home_assistant": False,
        "started_on_lamp": False,
    }

    # The one before it, which ended on its own.
    last = hass.states.get(LAST)
    assert last.state == "finished"
    assert last.attributes["countdown_id"] == 11
    assert last.attributes["at"] == "2026-09-17T08:56:40+00:00"
    assert last.attributes["duration_seconds"] == 240
    assert last.attributes["options"] == [
        "finished", "dismissed", "cancelled", "replaced", "ended_by_restart"
    ]


@pytest.mark.parametrize(
    ("change", "phase"),
    [
        ({"phase": "paused", "endsAt": None}, "paused"),
        ({"phase": "finished", "remainingSeconds": 0, "endsAt": None}, "finished"),
        # Running, but the lamp's clock is not valid, so it reports no end.
        ({"endsAt": None}, "running"),
    ],
)
async def test_no_end_time_unless_running_with_lamp_time(
    hass: HomeAssistant, entry, capable_lamp, change, phase  # noqa: F811
) -> None:
    await _setup(hass, entry)
    entry.runtime_data.coordinator.async_set_updated_data(_with(countdown={**COUNTDOWN, **change}))
    await hass.async_block_till_done()
    ends = hass.states.get(ENDS)
    assert ends.state == "unknown"
    assert ends.attributes["phase"] == phase


async def test_who_started_it(hass: HomeAssistant, entry, capable_lamp) -> None:  # noqa: F811
    await _setup(hass, entry)
    coordinator = entry.runtime_data.coordinator

    coordinator.async_set_updated_data(_with(countdown={**COUNTDOWN, "owner": 0, "ownerLabel": None}))
    await hass.async_block_till_done()
    attributes = hass.states.get(ENDS).attributes
    assert (attributes["set_by"], attributes["started_on_lamp"]) == ("Lamp", True)

    coordinator.async_set_updated_data(
        _with(countdown={**COUNTDOWN, "owner": 9, "ownerLabel": "Home Assistant"})
    )
    await hass.async_block_till_done()
    attributes = hass.states.get(ENDS).attributes
    assert attributes["set_by"] == "Home Assistant"
    assert attributes["set_by_home_assistant"] is True
    assert attributes["started_on_lamp"] is False

    # The phone that started it was removed; the countdown keeps running.
    coordinator.async_set_updated_data(_with(countdown={**COUNTDOWN, "ownerLabel": None}))
    await hass.async_block_till_done()
    assert hass.states.get(ENDS).attributes["set_by"] is None


async def test_last_countdown_reasons(hass: HomeAssistant, entry, capable_lamp) -> None:  # noqa: F811
    await _setup(hass, entry)
    coordinator = entry.runtime_data.coordinator
    for reason in ("dismissed", "cancelled", "replaced", "ended_by_restart"):
        coordinator.async_set_updated_data(_with(countdownEnded={**ENDED, "reason": reason}))
        await hass.async_block_till_done()
        assert hass.states.get(LAST).state == reason

    # Ended before the lamp had valid time.
    coordinator.async_set_updated_data(_with(countdownEnded={**ENDED, "at": None}))
    await hass.async_block_till_done()
    assert hass.states.get(LAST).attributes["at"] is None

    # A reason from newer firmware is not guessed at.
    coordinator.async_set_updated_data(_with(countdownEnded={**ENDED, "reason": "melted"}))
    await hass.async_block_till_done()
    assert hass.states.get(LAST).state == "unknown"


async def test_start_and_cancel_reach_the_lamp(hass: HomeAssistant, entry, capable_lamp) -> None:  # noqa: F811
    await _setup(hass, entry)
    await hass.services.async_call(
        DOMAIN, "start_countdown",
        {"device_id": _device(hass), "duration": {"minutes": 25}, "color": [255, 136, 0],
         "end_style": "until_tapped", "end_pattern": "sweep", "calm": True},
        blocking=True,
    )
    assert capable_lamp.call_args.args == (
        "start_countdown",
        {"durationSeconds": 1500, "colour": "#ff8800",
         "end": {"style": "until_tapped", "pattern": "sweep"}, "calm": True},
    )

    await hass.services.async_call(
        DOMAIN, "cancel_countdown", {"device_id": _device(hass)}, blocking=True
    )
    assert capable_lamp.call_args.args == ("cancel_countdown", None)


@pytest.mark.parametrize("service", ["start_countdown", "cancel_countdown"])
async def test_a_lamp_without_the_countdown_is_not_asked(
    hass: HomeAssistant, entry, lamp, service  # noqa: F811
) -> None:
    await _setup(hass, entry)
    lamp.reset_mock()
    data = {"device_id": _device(hass)}
    if service == "start_countdown":
        data |= {"duration": 300, "color": [255, 179, 71]}
    with pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call(DOMAIN, service, data, blocking=True)
    assert raised.value.translation_key == "no_countdown"
    assert "needs firmware that includes the countdown" in str(raised.value)
    lamp.assert_not_called()


async def test_not_supported_from_the_lamp_reads_the_same(
    hass: HomeAssistant, entry, capable_lamp  # noqa: F811
) -> None:
    await _setup(hass, entry)
    capable_lamp.side_effect = PillPalCommandError("not_supported", {"outcome": "not_supported"})
    with pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call(
            DOMAIN, "start_countdown",
            {"device_id": _device(hass), "duration": 300, "color": [255, 179, 71]},
            blocking=True,
        )
    assert raised.value.translation_key == "no_countdown"


async def test_a_lamp_refusal_is_still_reported(hass: HomeAssistant, entry, capable_lamp) -> None:  # noqa: F811
    await _setup(hass, entry)
    capable_lamp.side_effect = PillPalCommandError("clock_invalid", {"outcome": "clock_invalid"})
    with pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call(
            DOMAIN, "start_countdown",
            {"device_id": _device(hass), "duration": 300, "color": [255, 179, 71]},
            blocking=True,
        )
    assert raised.value.translation_key == "refused"


@pytest.mark.parametrize(
    ("gesture", "mode", "action", "target"),
    [
        ("long_press", "idle", "start_countdown", {"kind": "countdown", "id": 13}),
        ("tap", "countdown", "pause_countdown", {"kind": "countdown", "id": 13}),
        ("tap", "countdown", "resume_countdown", {"kind": "countdown", "id": 13}),
        ("double_tap", "countdown", "cancel_countdown", {"kind": "countdown", "id": 13}),
        ("tap", "notify", "dismiss_countdown", {"kind": "countdown", "id": 13}),
    ],
)
async def test_countdown_touches(
    hass: HomeAssistant, entry, lamp, gesture, mode, action, target  # noqa: F811
) -> None:
    await _setup(hass, entry)
    async_dispatcher_send(
        hass, SIGNAL_TOUCH.format(device_id=DEVICE),
        {"gesture": gesture, "pad": 0, "mode": mode, "target": target, "action": action},
    )
    await hass.async_block_till_done()
    event = hass.states.get("event.pill_pal_touch")
    assert event.attributes["event_type"] == gesture
    assert event.attributes["mode"] == mode
    assert event.attributes["action"] == action
    assert event.attributes["target_kind"] == "countdown"
    assert event.attributes["countdown_id"] == 13
    assert event.attributes["occurrence_id"] is None
    assert event.attributes["status_id"] is None
