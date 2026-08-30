"""Shared weather helpers and the always-on wind protection controller."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from homeassistant.components.weather import (
    ATTR_CONDITION_HAIL,
    ATTR_CONDITION_LIGHTNING_RAINY,
    ATTR_CONDITION_POURING,
    ATTR_CONDITION_RAINY,
    ATTR_CONDITION_SNOWY_RAINY,
    ATTR_WEATHER_WIND_SPEED,
    ATTR_WEATHER_WIND_SPEED_UNIT,
)
from homeassistant.const import UnitOfSpeed
from homeassistant.core import Event, EventStateChangedData, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import SpeedConverter

_LOGGER = logging.getLogger(__name__)

CHECK_INTERVAL = timedelta(minutes=15)

RAIN_STATES = {
    ATTR_CONDITION_RAINY,
    ATTR_CONDITION_POURING,
    ATTR_CONDITION_LIGHTNING_RAINY,
    ATTR_CONDITION_SNOWY_RAINY,
    ATTR_CONDITION_HAIL,
}

# Unit the wind speed threshold, the "Wind speed threshold" Number entity,
# AND the "Wind speed" sensor are all expressed in - deliberately the same
# unit everywhere so the two numbers you'd actually compare side by side in
# the UI are never silently in different units. km/h rather than m/s: it's
# the everyday unit for wind speed, and it's what HA's own automatic sensor
# unit conversion already displays for a wind_speed device_class sensor by
# default on this system, so declaring it natively avoids fighting that
# conversion. The weather entity can report in any unit (Met.no reports
# km/h too, but that's a coincidence, not an assumption) - is_windy() always
# converts to this before comparing, regardless.
THRESHOLD_UNIT = UnitOfSpeed.KILOMETERS_PER_HOUR
DEFAULT_WIND_SPEED_THRESHOLD = 72.0  # km/h - equivalent to the original 20 m/s default

NO_WEATHER_ENTITY_ERROR = (
    "Configure a weather entity in the Pergola integration's options before "
    "enabling this."
)


def is_raining(hass: HomeAssistant, weather_entity_id: str | None) -> bool:
    """Whether the configured weather entity currently reports a rain condition."""
    if weather_entity_id is None:
        return False
    state = hass.states.get(weather_entity_id)
    return state is not None and state.state in RAIN_STATES


def get_wind_speed(hass: HomeAssistant, weather_entity_id: str | None) -> float | None:
    """Current wind speed reported by the configured weather entity, converted
    to THRESHOLD_UNIT (the entity's own reported unit can be anything - e.g.
    Met.no reports km/h). None if unavailable."""
    if weather_entity_id is None:
        return None
    state = hass.states.get(weather_entity_id)
    if state is None:
        return None
    wind_speed = state.attributes.get(ATTR_WEATHER_WIND_SPEED)
    if wind_speed is None:
        return None
    wind_speed_unit = state.attributes.get(ATTR_WEATHER_WIND_SPEED_UNIT)
    if wind_speed_unit is not None and wind_speed_unit != THRESHOLD_UNIT:
        wind_speed = SpeedConverter.convert(wind_speed, wind_speed_unit, THRESHOLD_UNIT)
    return wind_speed


def is_windy(
    hass: HomeAssistant, weather_entity_id: str | None, threshold: float
) -> bool:
    """Whether the configured weather entity's reported wind speed is at or
    above `threshold` (in THRESHOLD_UNIT). A missing entity/attribute reads
    as "not windy" here - winter sun mode uses this only to decide whether to
    pause a tracking cycle, where failing open (not pausing) is the right
    default. Wind protection itself refuses to even enable without a
    configured entity, so it never relies on this fallback."""
    wind_speed = get_wind_speed(hass, weather_entity_id)
    return wind_speed is not None and wind_speed >= threshold


class WindProtectionController:
    """Closes the pergola whenever the configured weather entity reports wind
    at or above the configured threshold.

    The device's own wind sensor was disconnected: its stock firmware
    behavior is to *open* on wind, which is wrong for this slat geometry
    (causes vibration). This replaces it with the correct behavior. Always
    available, independent of winter sun mode. Only ever closes, never
    opens, so it can run continuously with no self-disable/rearm logic - if
    wind is still high after a manual reopen, it simply closes again next
    check.
    """

    def __init__(
        self, hass: HomeAssistant, get_weather_entity_id: Callable[[], str | None]
    ) -> None:
        self._hass = hass
        self._get_weather_entity_id = get_weather_entity_id
        self._cover = None
        self.enabled = False
        self.wind_speed_threshold: float = DEFAULT_WIND_SPEED_THRESHOLD
        # Diagnostics, read by sensor.py/binary_sensor.py - updated on every
        # check regardless of whether it actually closed anything. `active`
        # only flips its *state* (as seen by HA - same value written twice
        # doesn't re-fire a state_changed event) on the rising/falling edge
        # of a windy episode, unlike `last_triggered` which bumps on every
        # single check while still windy - `active` is the one to automate
        # on if you want one notification per episode, not one per check.
        self.last_wind_speed: float | None = None
        self.last_triggered: datetime | None = None
        self.active: bool = False
        self._listeners: list[Callable[[], None]] = []
        self._unsub_interval: Callable[[], None] | None = None
        self._unsub_state: Callable[[], None] | None = None

    def attach_cover(self, cover) -> None:
        """Give the controller a way to actually close the cover, reusing the
        entity's own state-writing/error-handling instead of duplicating it."""
        self._cover = cover

    def add_listener(self, listener: Callable[[], None]) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[], None]) -> None:
        self._listeners.remove(listener)

    def _notify_listeners(self) -> None:
        for listener in self._listeners:
            listener()

    async def async_enable(self) -> None:
        if self.enabled:
            return
        if self._get_weather_entity_id() is None:
            raise HomeAssistantError(NO_WEATHER_ENTITY_ERROR)
        self.enabled = True
        self._unsub_interval = async_track_time_interval(
            self._hass, self._async_scheduled_check, CHECK_INTERVAL
        )
        weather_entity_id = self._get_weather_entity_id()
        self._unsub_state = async_track_state_change_event(
            self._hass, [weather_entity_id], self._async_state_changed
        )
        self._notify_listeners()
        await self._async_check()

    async def async_disable(self) -> None:
        if not self.enabled:
            return
        self.enabled = False
        if self._unsub_interval is not None:
            self._unsub_interval()
            self._unsub_interval = None
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None
        self.active = False
        self._notify_listeners()

    async def _async_state_changed(self, event: Event[EventStateChangedData]) -> None:
        await self._async_check()

    async def _async_scheduled_check(self, now: datetime) -> None:
        await self._async_check()

    async def _async_check(self) -> None:
        weather_entity_id = self._get_weather_entity_id()
        self.last_wind_speed = get_wind_speed(self._hass, weather_entity_id)
        self.active = (
            self.last_wind_speed is not None
            and self.last_wind_speed >= self.wind_speed_threshold
        )
        if self.active:
            self.last_triggered = dt_util.utcnow()
            _LOGGER.info(
                "Wind protection: closing (%.1f km/h at or above %.1f km/h threshold)",
                self.last_wind_speed,
                self.wind_speed_threshold,
            )
            if self._cover is not None:
                await self._cover.async_close_cover()
        self._notify_listeners()
