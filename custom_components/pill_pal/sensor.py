"""What the lamp is doing, for dashboards and conditions.

How many reminders are signalling comes from the lamp's occurrences. The next reminder
needs the schedules too, because the lamp only turns a schedule into an occurrence once
its window opens, so tomorrow's reminder is never in the list; it is resolved here with
the lamp's own time zone rule, the same way the lamp will resolve it. The statuses sensor lists what every controller has set, with the ones this integration
owns marked, so an automation reconciling after a restart can see what is still there.
The Wi-Fi signal is a diagnostic, off by default like every signal-strength sensor, for
the day someone asks why the lamp keeps dropping out; firmware from 2026-09-29 reports it.

The gauge and the last reminder mirror what any controller did, so a gauge set from the
app or a reminder acknowledged at the lamp shows here as well. An occurrence that ends
leaves the live list, so how it ended comes from the snapshot's recent outcomes; a snooze
is not terminal and is read from the live occurrence instead. A done reminder says who
acknowledged it from the outcome's `by`, or "unknown" from a lamp that does not say.

The countdown is mirrored the same way, from whichever controller started it, including
the lamp's own pads. Its two sensors exist only on a lamp that states the capability: they
are added when a snapshot first states it, so a firmware update needs no reload, and they
go unavailable if a later snapshot stops stating it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import logging
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, SIGNAL_STRENGTH_DECIBELS_MILLIWATT, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import PillPalConfigEntry
from .const import (
    CAPABILITY_COUNTDOWN,
    COUNTDOWN_END_REASONS,
    INTEGRATION_NAME,
    LAMP_OWNER_LABEL,
    SNOOZE_MS,
)
from .coordinator import PillPalCoordinator
from .entity import PillPalEntity, set_by, wire_time
from .instants import Instant
from .posix_tz import PosixTzError, parse_posix_tz
from .windows import next_window_from

_LOGGER = logging.getLogger(__name__)


def _next_reminder(data: dict[str, Any], _grant: int) -> datetime | None:
    starts = [
        datetime.fromtimestamp(o["start"] / 1000, tz=UTC)
        for o in data.get("occurrences") or []
        if o.get("state") in ("upcoming", "snoozed") and isinstance(o.get("start"), int)
    ]
    starts.extend(_next_scheduled(data))
    return min(starts) if starts else None


def _next_scheduled(data: dict[str, Any]) -> list[datetime]:
    schedules = data.get("schedules") or []
    if not schedules or not data.get("timeZone"):
        return []
    try:
        zone = parse_posix_tz(data["timeZone"])
    except PosixTzError:
        _LOGGER.debug("Pill Pal reported a time zone it cannot use: %s", data["timeZone"])
        return []
    now = Instant(dt_util.utcnow().replace(tzinfo=None))
    starts = []
    for schedule in schedules:
        try:
            window = next_window_from(schedule, zone, now)
        except (KeyError, TypeError, ValueError):
            # A recurrence newer than this copy of the resolver, or a malformed entry.
            continue
        if window is not None:
            starts.append(window.start_utc.replace(tzinfo=UTC))
    return starts


def _signalling(data: dict[str, Any], _grant: int) -> int:
    return sum(1 for o in data.get("occurrences") or [] if o.get("state") == "due")


def _signalling_attributes(data: dict[str, Any], _grant: int) -> dict[str, Any]:
    """The reminders showing, with the occurrence IDs acknowledge and snooze need.

    The touch target is the one a tap on the lamp would act on, so an automation can
    acknowledge or snooze the same reminder a person at the lamp would.
    """
    schedules = {
        s.get("id"): s.get("name") for s in data.get("schedules") or [] if isinstance(s, dict)
    }
    target = data.get("touchTarget")
    reminders = []
    for o in data.get("occurrences") or []:
        if not isinstance(o, dict) or o.get("state") != "due":
            continue
        start, end = wire_time(o.get("start")), wire_time(o.get("end"))
        name = o.get("name")
        if name is None and o.get("source") == "native":
            name = schedules.get(o.get("ownerId"))
        reminders.append(
            {
                "occurrence_id": o.get("id"),
                "name": name,
                "due_at": start.isoformat() if start else None,
                "ends_at": end.isoformat() if end else None,
            }
        )
    return {
        "touch_target": target if isinstance(target, int) and not isinstance(target, bool) else None,
        "reminders": reminders,
    }


def _statuses(data: dict[str, Any], _grant: int) -> int:
    return len(((data.get("indicators") or {}).get("statuses")) or [])


def _wifi_signal(data: dict[str, Any], _grant: int) -> int | None:
    rssi = (data.get("network") or {}).get("rssi")
    return rssi if isinstance(rssi, int) and rssi != 0 else None


def _wifi_attributes(data: dict[str, Any], _grant: int) -> dict[str, Any]:
    network = data.get("network") or {}
    return {"network": network.get("ssid")}


def _status_attributes(data: dict[str, Any], grant: int) -> dict[str, Any]:
    statuses = ((data.get("indicators") or {}).get("statuses")) or []
    return {
        "statuses": [
            {
                "id": s.get("id"),
                "label": s.get("label"),
                "shown": s.get("shown"),
                "dimmed": s.get("dimmed"),
                "priority": s.get("priority"),
                "set_by": s.get("ownerLabel"),
                "set_by_home_assistant": s.get("owner") == grant,
            }
            for s in statuses
        ]
    }


def _gauge(data: dict[str, Any]) -> dict[str, Any] | None:
    gauge = (data.get("indicators") or {}).get("gauge")
    return gauge if isinstance(gauge, dict) else None


def _gauge_value(data: dict[str, Any], _grant: int) -> float | None:
    gauge = _gauge(data)
    value = gauge.get("value") if gauge else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    # The lamp keeps ten-thousandths, which is two decimal places of a percentage.
    return round(value * 100.0, 2)


def _gauge_attributes(data: dict[str, Any], grant: int) -> dict[str, Any]:
    gauge = _gauge(data)
    if gauge is None:
        return {}
    updated = wire_time(gauge.get("updatedAt"))
    return {
        "color": gauge.get("colour"),
        "color_end": gauge.get("colourEnd"),
        "stale": gauge.get("stale"),
        "shown": gauge.get("shown"),
        "updated_at": updated.isoformat() if updated else None,
        **set_by(data, gauge.get("owner"), grant),
    }


OUTCOMES = {"acknowledged": "done", "expired": "missed"}
LAST_REMINDER_STATES = ["done", "missed", "snoozed"]
ACKNOWLEDGER_KINDS = ("touch", "phone", "integration", "cloud")
NOT_SAID = "unknown"


def who_acknowledged(row: dict[str, Any]) -> dict[str, Any] | str | None:
    """`by` on an outcome or occurrence, read as acknowledged_by.json says: an object
    {kind, grantId, label}; None for no one (`by` is null); "unknown" when the lamp did
    not say (`by` absent, as from a lamp without acknowledgedBy, or unusable). A kind this
    integration does not know reads as "unknown" with its label kept."""
    if "by" not in row:
        return NOT_SAID
    by = row["by"]
    if by is None:
        return None
    if not isinstance(by, dict) or not isinstance(by.get("kind"), str):
        return NOT_SAID
    kind = by["kind"] if by["kind"] in ACKNOWLEDGER_KINDS else "unknown"
    if kind == "touch":  # a touch names no grant, whatever else it holds
        return {"kind": "touch", "grantId": None, "label": None}
    grant_id, label = by.get("grantId"), by.get("label")
    return {
        "kind": kind,
        "grantId": grant_id if isinstance(grant_id, int) and not isinstance(grant_id, bool) else None,
        "label": label if isinstance(label, str) else None,
    }


def _last_reminder(data: dict[str, Any]) -> dict[str, Any] | None:
    """The latest acknowledgement, expiry or snooze, from whichever controller."""
    occurrences = [o for o in data.get("occurrences") or [] if isinstance(o, dict)]
    names = {o.get("id"): o.get("name") for o in occurrences}
    schedules = {
        s.get("id"): s.get("name") for s in data.get("schedules") or [] if isinstance(s, dict)
    }
    ended = set()
    candidates = []
    for row in data.get("recentOutcomes") or []:
        if not isinstance(row, dict):
            continue
        ended.add(row.get("id"))
        at = wire_time(row.get("endedAt"))
        if row.get("state") not in OUTCOMES or at is None:
            continue
        name = names.get(row.get("id"))
        if name is None and row.get("source") == "native":
            name = schedules.get(row.get("ownerId"))
        by = who_acknowledged(row) if row["state"] == "acknowledged" else None
        candidates.append((at, row.get("id"), OUTCOMES[row["state"]], at, name, by))
    for o in occurrences:
        until = wire_time(o.get("snoozeDeadline"))
        if o.get("state") == "snoozed" and o.get("id") not in ended and until is not None:
            snoozed_at = until - timedelta(milliseconds=SNOOZE_MS)
            candidates.append((snoozed_at, o.get("id"), "snoozed", until, o.get("name"), None))
    if not candidates:
        return None
    _, occurrence, state, at, name, by = max(candidates, key=lambda c: (c[0], c[1] or 0))
    return {"state": state, "at": at, "occurrence_id": occurrence, "name": name, "by": by}


def _last_reminder_state(data: dict[str, Any], _grant: int) -> str | None:
    last = _last_reminder(data)
    return last["state"] if last else None


def _last_reminder_attributes(data: dict[str, Any], _grant: int) -> dict[str, Any]:
    last = _last_reminder(data)
    if last is None:
        return {}
    by = last["by"]
    # Who acknowledged it: only a done reminder has someone. A miss or a snooze has
    # neither attribute set.
    return {
        "name": last["name"],
        "at": last["at"].isoformat(),
        "occurrence_id": last["occurrence_id"],
        "by": by["kind"] if isinstance(by, dict) else by,
        "by_label": by["label"] if isinstance(by, dict) else None,
    }


def _countdown(data: dict[str, Any]) -> dict[str, Any] | None:
    countdown = (data.get("indicators") or {}).get("countdown")
    return countdown if isinstance(countdown, dict) else None


def _countdown_ends(data: dict[str, Any], _grant: int) -> datetime | None:
    """When a running countdown reaches zero. None while paused or finished, and without
    lamp time, because the lamp then reports no end."""
    countdown = _countdown(data)
    if countdown is None or countdown.get("phase") != "running":
        return None
    return wire_time(countdown.get("endsAt"))


def _countdown_attributes(data: dict[str, Any], grant: int) -> dict[str, Any]:
    countdown = _countdown(data)
    if countdown is None:
        return {}
    end = countdown.get("end") if isinstance(countdown.get("end"), dict) else {}
    owner = countdown.get("owner")
    label = countdown.get("ownerLabel")
    if owner == 0:
        label = LAMP_OWNER_LABEL
    elif label is None and owner == grant:
        label = INTEGRATION_NAME
    return {
        "countdown_id": countdown.get("id"),
        "phase": countdown.get("phase"),
        "remaining_seconds": countdown.get("remainingSeconds"),
        "duration_seconds": countdown.get("durationSeconds"),
        "color": countdown.get("colour"),
        "end_style": end.get("style"),
        "end_pattern": end.get("pattern"),
        "calm": countdown.get("calm"),
        "escalated": countdown.get("escalated"),
        "shown": countdown.get("shown"),
        "set_by": label,
        "set_by_home_assistant": owner == grant,
        "started_on_lamp": owner == 0,
    }


def _countdown_ended(data: dict[str, Any]) -> dict[str, Any] | None:
    ended = (data.get("indicators") or {}).get("countdownEnded")
    return ended if isinstance(ended, dict) else None


def _last_countdown_state(data: dict[str, Any], _grant: int) -> str | None:
    ended = _countdown_ended(data)
    reason = ended.get("reason") if ended else None
    return reason if reason in COUNTDOWN_END_REASONS else None


def _last_countdown_attributes(data: dict[str, Any], _grant: int) -> dict[str, Any]:
    ended = _countdown_ended(data)
    if ended is None:
        return {}
    at = wire_time(ended.get("at"))
    return {
        "countdown_id": ended.get("id"),
        "at": at.isoformat() if at else None,
        "duration_seconds": ended.get("durationSeconds"),
    }


@dataclass(frozen=True, kw_only=True)
class PillPalSensorDescription(SensorEntityDescription):
    value: Callable[[dict[str, Any], int], Any]
    attributes: Callable[[dict[str, Any], int], dict[str, Any]] | None = None
    # A capability the lamp must state in its snapshot for this sensor to exist.
    capability: str | None = None


SENSORS = (
    PillPalSensorDescription(
        key="next_reminder",
        translation_key="next_reminder",
        device_class=SensorDeviceClass.TIMESTAMP,
        value=_next_reminder,
    ),
    PillPalSensorDescription(
        key="signalling",
        translation_key="signalling",
        value=_signalling,
        attributes=_signalling_attributes,
    ),
    PillPalSensorDescription(
        key="statuses",
        translation_key="statuses",
        value=_statuses,
        attributes=_status_attributes,
    ),
    PillPalSensorDescription(
        key="gauge",
        translation_key="gauge",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value=_gauge_value,
        attributes=_gauge_attributes,
    ),
    PillPalSensorDescription(
        key="last_reminder",
        translation_key="last_reminder",
        device_class=SensorDeviceClass.ENUM,
        options=LAST_REMINDER_STATES,
        value=_last_reminder_state,
        attributes=_last_reminder_attributes,
    ),
    PillPalSensorDescription(
        key="wifi_signal",
        translation_key="wifi_signal",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value=_wifi_signal,
        attributes=_wifi_attributes,
    ),
    PillPalSensorDescription(
        key="countdown_ends",
        translation_key="countdown_ends",
        device_class=SensorDeviceClass.TIMESTAMP,
        value=_countdown_ends,
        attributes=_countdown_attributes,
        capability=CAPABILITY_COUNTDOWN,
    ),
    PillPalSensorDescription(
        key="last_countdown",
        translation_key="last_countdown",
        device_class=SensorDeviceClass.ENUM,
        options=COUNTDOWN_END_REASONS,
        value=_last_countdown_state,
        attributes=_last_countdown_attributes,
        capability=CAPABILITY_COUNTDOWN,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: PillPalConfigEntry, add: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.coordinator
    pending = {d.capability for d in SENSORS if d.capability is not None}

    @callback
    def add_stated() -> None:
        stated = {c for c in pending if coordinator.supports(c)}
        if stated:
            pending.difference_update(stated)
            add(PillPalSensor(coordinator, d) for d in SENSORS if d.capability in stated)

    add(PillPalSensor(coordinator, d) for d in SENSORS if d.capability is None)
    add_stated()
    if pending:
        # A firmware update that adds a capability shows up in a later snapshot.
        entry.async_on_unload(coordinator.async_add_listener(add_stated))


class PillPalSensor(PillPalEntity, SensorEntity):
    entity_description: PillPalSensorDescription

    def __init__(self, coordinator: PillPalCoordinator, description: PillPalSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        capability = self.entity_description.capability
        return super().available and (
            capability is None or self.coordinator.supports(capability)
        )

    @property
    def native_value(self) -> Any:
        return self.entity_description.value(self.coordinator.data or {}, self.coordinator.client.grant_id)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attributes is None:
            return None
        return self.entity_description.attributes(
            self.coordinator.data or {}, self.coordinator.client.grant_id
        )
