"""Winter sun-tracking mode.

Nudges the pergola's slats closed through the day to follow the sun, using
only timed Close pulses - the device only exposes Open/Close/Stop, no real
position feedback. This controller never opens the cover itself: the user
opens it by hand whenever they want the day's tracking to start from "fully
open", and everything from there is just Close pulses tracking the sun's
westward sweep. See weather_guard.py for the wind/rain awareness this pauses
on.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import date, datetime
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util

from .weather_guard import NO_WEATHER_ENTITY_ERROR, is_raining, is_windy

if TYPE_CHECKING:
    from .entity import PergolaConnection

_LOGGER = logging.getLogger(__name__)

# Choices for the "Check interval" select entity - all divide evenly into an
# hour, so the schedule always lands on the same clock minutes (e.g. :00,
# :15, :30, :45 for 15) rather than drifting relative to whenever the switch
# was turned on.
CHECK_INTERVAL_CHOICES = [5, 10, 15, 30, 60]
DEFAULT_CHECK_INTERVAL_MINUTES = 15

# Tuned for a terrace facing close to due south (S-SSE, ~165 deg), slats
# perpendicular to the wall, closing towards the west. Hand-adjust these if
# tracking starts/stops too early or late relative to when the sun actually
# clears/leaves the terrace.
TRACK_START_AZIMUTH = 100.0
TRACK_END_AZIMUTH = 260.0
MIN_POSITION = 0.4  # never auto-close past 60% closed - keep maximizing sun

# Default for the "Winter sun deadband" number entity - ignore corrections
# smaller than this (as a 0-1 fraction). Lower it (e.g. via that entity) to
# get pulses to trigger without waiting for a large azimuth change, e.g.
# when testing against the simulated device.
DEFAULT_POSITION_DEADBAND = 0.05

DEFAULT_FULL_TRAVEL_TIME = 67.0  # seconds - measured with a stopwatch on the real hardware (1 min 7 s)
ANIMATION_TICK = 1.0  # seconds - purely cosmetic granularity for the diagnostic sensor


def _target_position(azimuth: float) -> float:
    """Desired virtual position (1.0 = fully open) for a given solar azimuth."""
    if azimuth <= TRACK_START_AZIMUTH:
        return 1.0
    if azimuth >= TRACK_END_AZIMUTH:
        return MIN_POSITION
    fraction = (azimuth - TRACK_START_AZIMUTH) / (TRACK_END_AZIMUTH - TRACK_START_AZIMUTH)
    return 1.0 - fraction * (1.0 - MIN_POSITION)


class WinterSunController:
    """Tracks the sun's azimuth through the day and nudges the slats closed
    to follow it, starting from whenever the user last opened the cover by
    hand. Never opens the cover itself."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        connection: "PergolaConnection",
        get_weather_entity_id: Callable[[], str | None],
        get_wind_threshold: Callable[[], float],
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._connection = connection
        self._get_weather_entity_id = get_weather_entity_id
        # Reads WindProtectionController.wind_speed_threshold - the two
        # features share one threshold so they never disagree on "windy".
        self._get_wind_threshold = get_wind_threshold
        self.enabled = False
        self.full_travel_time: float = DEFAULT_FULL_TRAVEL_TIME
        self.position_deadband: float = DEFAULT_POSITION_DEADBAND
        self.check_interval_minutes: int = DEFAULT_CHECK_INTERVAL_MINUTES
        # The sun-tracking baseline: the target position (see
        # _target_position) as of the last check that actually pulsed, and
        # the local calendar date it happened on. Pulse size is computed
        # from how much *this* has moved since last time - never from
        # _virtual_position - so a wildly-wrong absolute position estimate
        # (e.g. winter sun mode was off for days, or the physical remote was
        # used) can never produce a wrong-sized pulse: the worst case is
        # bounded by how far the sun itself has actually moved. None (or a
        # date other than today) means no baseline exists yet for today -
        # _async_check() then assumes fully-open and starts fresh from
        # there, which is safe even when wrong (see _async_check). Set
        # precisely by on_manual_open() whenever HA does see a real open.
        self._last_check_target: float | None = None
        self._last_check_date: date | None = None
        # Purely cosmetic best-guess for the diagnostic sensor - never read
        # for any decision-making. Updated instantly (unlike the animated
        # _display_position below).
        self._virtual_position = 1.0
        # What sensor.pergola_estimated_open_percentage shows - walks toward
        # _virtual_position over roughly the real travel time instead of
        # snapping instantly, so it looks like what the motor is actually
        # doing. Purely cosmetic: nothing reads this for decision-making.
        self._display_position = 1.0
        self._animation_task: asyncio.Task | None = None
        self._listeners: list[Callable[[], None]] = []
        self._unsub_interval: Callable[[], None] | None = None

    def add_listener(self, listener: Callable[[], None]) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[], None]) -> None:
        self._listeners.remove(listener)

    def _notify_listeners(self) -> None:
        for listener in self._listeners:
            listener()

    @property
    def estimated_open_percentage(self) -> int:
        """Rough, open-loop guess at how open the pergola is (0-100), for
        debugging/testing only - see sensor.py's docstring for why this can
        never be trusted as a fact."""
        return round(self._display_position * 100)

    def _start_animation_to(self, target: float) -> None:
        """(Re)start the background task walking `_display_position` towards
        `target`, cancelling any animation already in progress - e.g. a
        close clicked right after an open that hadn't finished animating
        yet just continues smoothly from wherever the display was, rather
        than jumping."""
        if self._animation_task is not None and not self._animation_task.done():
            self._animation_task.cancel()
        duration = abs(target - self._display_position) * self.full_travel_time
        self._animation_task = self._entry.async_create_background_task(
            self._hass,
            self._async_animate_display(target, duration),
            "pergola winter sun display animation",
        )

    async def _async_animate_display(self, target: float, duration: float) -> None:
        start = self._display_position
        if duration <= 0 or start == target:
            self._display_position = target
            self._notify_listeners()
            return
        elapsed = 0.0
        while elapsed < duration:
            tick = min(ANIMATION_TICK, duration - elapsed)
            await asyncio.sleep(tick)  # if cancelled here, _display_position
            # simply stays at its last interpolated value - the next
            # animation picks up smoothly from there.
            elapsed += tick
            self._display_position = start + (target - start) * (elapsed / duration)
            self._notify_listeners()
        self._display_position = target
        self._notify_listeners()

    def on_manual_open(self) -> None:
        """Call whenever the cover is opened (by the user, via HA). This is
        the only trustworthy "we know the real position" moment there is -
        establishes today's tracking baseline (see _last_check_target) so
        the very next check can catch up to wherever the sun already is,
        and every check after that only ever moves by small, bounded steps
        from here."""
        self._last_check_target = 1.0
        self._last_check_date = dt_util.now().date()
        self._virtual_position = 1.0
        self._notify_listeners()
        self._start_animation_to(1.0)

    def on_manual_close(self) -> None:
        """Call whenever the cover is closed (by the user, or wind
        protection, via HA). Doesn't change what winter sun mode itself will
        do next - it only ever issues further Close pulses regardless (see
        module docstring on why that's always safe) - this exists purely so
        `estimated_open_percentage` reflects reality for anyone watching it,
        rather than staying stuck at whatever it last computed."""
        self._virtual_position = 0.0
        self._notify_listeners()
        self._start_animation_to(0.0)

    def on_manual_stop(self) -> None:
        """Call whenever the cover is stopped mid-travel (by the user, via
        HA) - e.g. a manual Close followed by Stop a few seconds later to do
        a hand-timed partial pulse. Freezes both the authoritative position
        and the display at wherever the animation had gotten to, instead of
        letting the display keep drifting towards whatever the preceding
        Open/Close was heading for. This is actually the most accurate this
        estimate can ever be, since "stopped here, right now" is exactly
        what happened - unlike Open/Close, which only know the direction
        they're heading, not when they'll be told to stop."""
        if self._animation_task is not None and not self._animation_task.done():
            self._animation_task.cancel()
        self._virtual_position = self._display_position
        self._notify_listeners()

    def recalibrate(self, percentage: float) -> None:
        """Manually correct the *displayed* estimate, e.g. after eyeballing
        the real pergola. Deliberately cosmetic only - does NOT touch
        _last_check_target/_last_check_date, unlike on_manual_open(). A hand
        estimate is not a hard fact the way "motor ran to its physical
        Open/Close end-stop" is, so it must never be allowed to feed the
        pulse-sizing math - that's the exact failure mode the delta model
        was built to rule out in the first place. This only fixes what's
        shown, never what's decided. No animation - correcting a belief
        isn't a physical movement, so the display jumps straight to the new
        value."""
        position = max(0.0, min(1.0, percentage / 100))
        if self._animation_task is not None and not self._animation_task.done():
            self._animation_task.cancel()
        self._virtual_position = position
        self._display_position = position
        self._notify_listeners()

    def _register_schedule(self) -> None:
        """(Re)subscribe the periodic check at the clock minutes implied by
        `check_interval_minutes` (e.g. 15 -> :00/:15/:30/:45). Safe to call
        while already enabled - drops the old subscription first, so a
        changed interval takes effect immediately instead of after the next
        firing of the old one."""
        if self._unsub_interval is not None:
            self._unsub_interval()
        check_minutes = list(range(0, 60, self.check_interval_minutes))
        self._unsub_interval = async_track_time_change(
            self._hass, self._async_scheduled_check, minute=check_minutes, second=0
        )

    async def async_set_check_interval(self, minutes: int) -> None:
        """Change how often winter sun mode re-checks the sun's position.
        Takes effect immediately if currently enabled."""
        self.check_interval_minutes = minutes
        if self.enabled:
            self._register_schedule()
        self._notify_listeners()

    async def async_enable(self) -> None:
        if self.enabled:
            return
        if self._get_weather_entity_id() is None:
            raise HomeAssistantError(NO_WEATHER_ENTITY_ERROR)
        self.enabled = True
        self._register_schedule()
        self._notify_listeners()
        await self._async_check()

    async def async_disable(self) -> None:
        if not self.enabled:
            return
        self.enabled = False
        if self._unsub_interval is not None:
            self._unsub_interval()
            self._unsub_interval = None
        self._notify_listeners()

    async def _async_scheduled_check(self, now: datetime) -> None:
        await self._async_check()

    async def _async_check(self) -> None:
        sun_state = self._hass.states.get("sun.sun")
        if sun_state is None or sun_state.state != "above_horizon":
            return

        weather_entity_id = self._get_weather_entity_id()
        if is_raining(self._hass, weather_entity_id) or is_windy(
            self._hass, weather_entity_id, self._get_wind_threshold()
        ):
            # The device's own rain sensor, and the separate wind protection
            # switch, are already the ones responsible for closing in these
            # conditions - just stay quiet rather than add redundant traffic.
            return

        azimuth = sun_state.attributes.get("azimuth")
        if azimuth is None:
            return

        target = _target_position(azimuth)

        today = dt_util.now().date()
        if self._last_check_target is None or self._last_check_date != today:
            # No same-day baseline - either winter sun mode (or HA itself)
            # was off since before today, or it was never told about an
            # open today at all (e.g. opened with the physical remote, not
            # through HA - this integration has no way to tell). We
            # genuinely don't know the real position, so we do NOT assume
            # one and do NOT pulse based on a guess - silently seed the
            # baseline at today's *current* target instead and wait for the
            # next check. From here on, pulses only ever reflect how much
            # the sun moves relative to this starting point - always small
            # and sensible regardless of what the real starting position
            # actually was. on_manual_open() is the one place that DOES
            # know the real position (1.0) and can safely catch up
            # immediately instead of waiting one extra check.
            self._last_check_target = target
            self._last_check_date = today
            return

        delta = self._last_check_target - target
        if delta <= self.position_deadband:
            # Not worth a pulse yet - but deliberately NOT updating
            # _last_check_target here: several sub-threshold checks in a
            # row still need to add up against the *original* baseline, so
            # the eventual catch-up pulse reflects their sum, not just the
            # most recent (possibly tiny) increment.
            return

        pulse_seconds = delta * self.full_travel_time
        _LOGGER.info(
            "Winter sun mode: pulsing closed for %.1fs (azimuth %.0f°, "
            "target moved %.2f -> %.2f since last check)",
            pulse_seconds,
            azimuth,
            self._last_check_target,
            target,
        )
        self._last_check_target = target
        self._last_check_date = dt_util.now().date()
        self._virtual_position = max(0.0, self._virtual_position - delta)
        self._start_animation_to(self._virtual_position)  # runs concurrently with the real pulse below
        await self._async_pulse_close(pulse_seconds)

    async def _async_pulse_close(self, duration: float) -> None:
        pergola = await self._connection.async_get()
        try:
            await pergola.close()
            await asyncio.sleep(duration)
            # NOT pergola.stop() - confirmed on the real hardware that the
            # dedicated stop command (cmd_active/'B') does not interrupt
            # motion at all. The device has no real standalone stop:
            # re-sending the direction that's already moving is what
            # actually halts it (same as the vendor app's buttons - holding
            # Open or Close is a toggle, not press-to-move/press-to-stop).
            await pergola.close()
        finally:
            self._connection.async_schedule_idle_disconnect()
