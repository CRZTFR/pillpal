"""The example blueprints, run as automations against a stand-in lamp.

Each one is loaded the way Home Assistant loads an imported blueprint, so a broken input,
template or action name fails here rather than in someone's home.
"""
from datetime import datetime, timedelta
from pathlib import Path
import shutil

import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.pill_pal.const import DOMAIN, SIGNAL_TOUCH

from .test_countdown import capable_lamp  # noqa: F401  (fixture)
from .test_init import DEVICE, entry, lamp  # noqa: F401  (fixtures)

BLUEPRINTS = Path(__file__).parent.parent / "blueprints" / "automation" / "pill_pal"


async def _automate(hass: HomeAssistant, entry, lamp, path: str, inputs: dict) -> None:  # noqa: F811
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    shutil.copytree(BLUEPRINTS, hass.config.path("blueprints/automation/pill_pal"),
                    dirs_exist_ok=True)
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, DEVICE)})
    if "touch" not in inputs:
        inputs = {"lamp": device.id, **inputs}
    assert await async_setup_component(
        hass, "automation",
        {"automation": [{"id": "example", "use_blueprint": {"path": f"pill_pal/{path}", "input": inputs}}]},
    )
    await hass.async_block_till_done()
    assert hass.states.get("automation.automation_0").state == "on"
    lamp.reset_mock()


def _sent(lamp) -> list:  # noqa: F811
    return [call.args for call in lamp.call_args_list]


async def test_notify_when(hass: HomeAssistant, entry, lamp) -> None:  # noqa: F811
    hass.states.async_set("sensor.washer", "running")
    await _automate(hass, entry, lamp, "notify_when.yaml",
                    {"watched": "sensor.washer", "to_state": "finished"})

    hass.states.async_set("sensor.washer", "finished")
    await hass.async_block_till_done()
    assert _sent(lamp) == [("notify", {"pattern": "pulse", "durationSeconds": 10, "colour": "#0078ff"})]

    # Back from a dropout already finished: not a new finish.
    lamp.reset_mock()
    hass.states.async_set("sensor.washer", "unavailable")
    hass.states.async_set("sensor.washer", "finished")
    await hass.async_block_till_done()
    assert _sent(lamp) == []


async def test_status_while(hass: HomeAssistant, entry, lamp) -> None:  # noqa: F811
    hass.states.async_set("cover.garage", "closed")
    await _automate(hass, entry, lamp, "status_while.yaml",
                    {"watched": "cover.garage", "on_state": "open", "status_id": "garage",
                     "label": "Garage door open"})

    hass.states.async_set("cover.garage", "open")
    await hass.async_block_till_done()
    assert _sent(lamp) == [("set_status", {"id": "garage", "colour": "#ff8800", "pattern": "solid",
                                           "brightness": 255, "priority": "normal",
                                           "label": "Garage door open"})]

    lamp.reset_mock()
    hass.states.async_set("cover.garage", "closed")
    await hass.async_block_till_done()
    assert _sent(lamp) == [("clear_status", {"id": "garage"})]


async def test_status_while_without_a_label(hass: HomeAssistant, entry, lamp) -> None:  # noqa: F811
    hass.states.async_set("binary_sensor.door", "off")
    await _automate(hass, entry, lamp, "status_while.yaml",
                    {"watched": "binary_sensor.door", "on_state": "on", "status_id": "door"})
    hass.states.async_set("binary_sensor.door", "on")
    await hass.async_block_till_done()
    assert _sent(lamp) == [("set_status", {"id": "door", "colour": "#ff8800", "pattern": "solid",
                                           "brightness": 255, "priority": "normal"})]


async def test_gauge_from_sensor(hass: HomeAssistant, entry, lamp) -> None:  # noqa: F811
    hass.states.async_set("sensor.solar_export", "0")
    await _automate(hass, entry, lamp, "gauge_from_sensor.yaml",
                    {"sensor": "sensor.solar_export", "minimum": 0, "maximum": 5000,
                     "color_end": [0, 200, 80]})

    hass.states.async_set("sensor.solar_export", "2500")
    await hass.async_block_till_done()
    assert _sent(lamp) == [("set_gauge", {"value": 0.5, "colour": "#ffc800", "staleSeconds": 900,
                                          "colourEnd": "#00c850"})]

    # Past the high mark fills the lamp; it is not refused.
    lamp.reset_mock()
    hass.states.async_set("sensor.solar_export", "7200")
    await hass.async_block_till_done()
    assert _sent(lamp)[0][1]["value"] == 1.0

    # Nothing sent, so the lamp's stale cue can do its job.
    lamp.reset_mock()
    hass.states.async_set("sensor.solar_export", "unavailable")
    await hass.async_block_till_done()
    assert _sent(lamp) == []


@pytest.mark.parametrize(
    ("gesture", "mode", "when", "runs"),
    [
        ("long_press", "idle", "any", True),
        ("tap", "idle", "any", False),
        ("long_press", "status", "status", True),
        ("long_press", "idle", "status", False),
        ("long_press", "countdown", "countdown", True),
    ],
)
async def test_touch_action(hass: HomeAssistant, entry, lamp, gesture, mode, when, runs) -> None:  # noqa: F811
    await _automate(hass, entry, lamp, "touch_action.yaml",
                    {"touch": "event.pill_pal_touch", "when": when,
                     "actions": [{"action": "input_boolean.toggle",
                                  "target": {"entity_id": "input_boolean.hallway"}}]})
    assert await async_setup_component(hass, "input_boolean", {"input_boolean": {"hallway": {}}})
    await hass.async_block_till_done()

    async_dispatcher_send(
        hass, SIGNAL_TOUCH.format(device_id=DEVICE),
        {"gesture": gesture, "pad": 0, "mode": mode, "target": None, "action": "event_only"},
    )
    await hass.async_block_till_done()
    assert hass.states.get("input_boolean.hallway").state == ("on" if runs else "off")


async def test_countdown_when_on_school_days(hass: HomeAssistant, entry, capable_lamp, freezer) -> None:  # noqa: F811
    lamp = capable_lamp
    # Monday 5 October 2026, a minute before the default 08:20, in Home Assistant's zone.
    monday = datetime(2026, 10, 5, 8, 19, tzinfo=dt_util.get_default_time_zone())
    freezer.move_to(monday)
    await _automate(hass, entry, lamp, "countdown_when.yaml", {})

    freezer.move_to(monday + timedelta(minutes=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert _sent(lamp) == [("start_countdown", {
        "durationSeconds": 1500, "colour": "#ffb347",
        "end": {"style": "brief", "pattern": "pulse"}, "calm": False,
    })]

    # Every weekday at 08:20, and not at the weekend.
    for day, school in [(1, True), (4, True), (5, False), (6, False), (7, True)]:
        lamp.reset_mock()
        freezer.move_to(monday + timedelta(days=day, minutes=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        assert len(_sent(lamp)) == (1 if school else 0), day


async def test_countdown_when_with_its_own_trigger(hass: HomeAssistant, entry, capable_lamp) -> None:  # noqa: F811
    lamp = capable_lamp
    hass.states.async_set("input_boolean.bath", "off")
    await _automate(hass, entry, lamp, "countdown_when.yaml", {
        "start": [{"trigger": "state", "entity_id": "input_boolean.bath", "to": "on"}],
        "only_if": [],
        "duration": {"hours": 0, "minutes": 12, "seconds": 0},
        "color": [255, 136, 0],
        "end_style": "until_tapped",
        "end_pattern": "sweep",
        "calm": True,
    })

    hass.states.async_set("input_boolean.bath", "on")
    await hass.async_block_till_done()
    assert _sent(lamp) == [("start_countdown", {
        "durationSeconds": 720, "colour": "#ff8800",
        "end": {"style": "until_tapped", "pattern": "sweep"}, "calm": True,
    })]
