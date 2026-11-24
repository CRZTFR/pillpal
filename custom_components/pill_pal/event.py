"""Touches and knocks on the lamp, as they happen.

Every tap, double tap and long press outside a pairing window, whatever the lamp did
with it locally (indicators.md, "Touch events"). The lamp has two inputs, a touch pad
and a knock sensor, and each event names which one in `input`. Each input is its own
entity, so an automation can listen to one. The attributes say what the lamp was
showing and what it did, so an automation can act only on, say, a long press while a
particular status is showing.

A lamp from before `input` existed had touch pads only, so an event without it is a
touch.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PillPalConfigEntry
from .const import GESTURES, KNOCK_GESTURES, SIGNAL_TOUCH
from .entity import PillPalEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: PillPalConfigEntry, add: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.coordinator
    add([PillPalTouch(coordinator), PillPalKnock(coordinator)])


class _PillPalInput(PillPalEntity, EventEntity):
    """One of the lamp's inputs. Subclasses say which, and what its gestures are called."""

    _input: str
    # The wire's gesture name to this entity's event type.
    _event_types: dict[str, str]

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, self._input)
        self._attr_translation_key = self._input
        self._attr_event_types = list(self._event_types.values())

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_TOUCH.format(device_id=self.coordinator.client.device_id),
                self._touched,
            )
        )

    @callback
    def _touched(self, event: dict[str, Any]) -> None:
        if event.get("input", "touch") != self._input:
            return
        event_type = self._event_types.get(event.get("gesture"))
        if event_type is None:
            return
        target = event.get("target") or {}
        attributes = {
            "mode": event.get("mode"),
            "action": event.get("action"),
            "input": self._input,
            "pad": event.get("pad"),
            "target_kind": target.get("kind"),
            "occurrence_id": target.get("id") if target.get("kind") == "occurrence" else None,
            "status_id": target.get("id") if target.get("kind") == "status" else None,
            "status_owner": target.get("owner") if target.get("kind") == "status" else None,
        }
        self._trigger_event(event_type, attributes)
        self.async_write_ha_state()


class PillPalTouch(_PillPalInput):
    _input = "touch"
    _event_types = {gesture: gesture for gesture in GESTURES}


class PillPalKnock(_PillPalInput):
    _input = "knock"
    _event_types = KNOCK_GESTURES

    @property
    def available(self) -> bool:
        # Only while the lamp says its knock sensor is working: a lamp without one, or
        # one whose sensor did not start, shows this entity as unavailable.
        capabilities = (self.coordinator.data or {}).get("capabilities") or {}
        return super().available and capabilities.get("knock") is True
