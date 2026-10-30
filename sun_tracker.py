"""Winter sun-tracking mode.

Nudges the pergola's slats closed through the day to follow the sun, aiming
the slats' angle at the sun's real profile angle (see profile_angle()) so
they pass maximum light rather than blocking it - using only timed Close
pulses, the device's only fine-grained control. Each day is bounded by two
deliberate full moves, both one-shot: an auto-open at the first check above
the horizon if the weather looks clear (_async_auto_open()), and an
auto-close at the first check below the horizon, unconditionally - closing
is always the safe state (_async_auto_close(), 2026-10-17). Those two moves
are the ONLY times the tracking baseline (_last_check_target/
_last_check_date) ever gets (re)established. Every check in between is
still only ever a Close pulse sized from how far the sun has moved since the
last one - deliberately blind to manual interventions (HA, the vendor app,
or the remote, partial or complete) happening in between: those are never
seen as a baseline reset any more, only as a (welcome) temporary offset the
tracking keeps pulsing on top of (2026-10-17 redesign, after a manual
midday light reduction got silently overwritten 15 minutes later by a
pulse that had no idea it happened). See weather_guard.py for the wind
awareness the regular pulses (not the auto-open/auto-close moves) pause on.

The "estimated open percentage" diagnostic is entirely separate and purely
cosmetic (see sensor.py and CLAUDE.md) - it mirrors the device's own live
`blades` status (open/closed/opening/closing/stopped) via
_on_connection_status(), regardless of who or what caused the movement, and
never feeds into any of the above.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from astral import Observer
from astral.sun import azimuth as astral_azimuth, elevation as astral_elevation
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .weather_guard import (
    NO_WEATHER_ENTITY_ERROR,
    is_raining,
    is_rain_forecast,
    is_windy,
)

if TYPE_CHECKING:
    from .entity import PergolaConnection

_LOGGER = logging.getLogger(__name__)

# Choices for the "Check interval" select entity - all divide evenly into an
# hour, so the schedule always lands on the same clock minutes (e.g. :00,
# :15, :30, :45 for 15) rather than drifting relative to whenever the switch
# was turned on.
CHECK_INTERVAL_CHOICES = [5, 10, 15, 30, 60]
DEFAULT_CHECK_INTERVAL_MINUTES = 15

# Confirmed 2026-10-16 (Vincent - order e-mail to the installer, and the
# vendor's spec sheet): the terrace faces 165 deg exactly, slats run
# perpendicular to the wall (each slat's long axis points away from the
# house) and pivot together from 0 deg (closed, flat, overlapping -
# watertight) up to 140 deg (fully open). Deliberately ordered to open
# towards the west ("pour maximiser le soleil du soir" - Vincent's own
# words to the installer) - see profile_angle()'s docstring for how that
# shapes the model below.
WALL_ORIENTATION = 165.0  # degrees, compass bearing the terrace faces
MAX_SLAT_ANGLE = 140.0  # degrees, 0 = closed/flat

TRACK_END_AZIMUTH = 260.0  # safety cap - treat anything past this as MIN_POSITION
MIN_POSITION = 0.25  # never auto-close past 75% closed (angle 35 deg) - real
# slats have real thickness, so the idealized "angle = exact profile angle"
# geometry below stops meaning anything useful at shallow angles anyway (a
# near-flat slat doesn't pass meaningfully more light than a fully closed
# one, whatever the math says). Lowered from 0.4 (56 deg/60% closed) on
# 2026-10-18 - Vincent wanted it to keep closing further than that floor
# allowed (briefly tried 0.2/28 deg first).

# Default for the "Winter sun deadband" number entity - ignore corrections
# smaller than this (as a 0-1 fraction). Lower it (e.g. via that entity) to
# get pulses to trigger without waiting for a large azimuth change, e.g.
# when testing against the simulated device.
DEFAULT_POSITION_DEADBAND = 0.025  # lowered from 0.05 on 2026-10-30 (Vincent),
# now that pulses target the mid-interval sun position instead of lagging a
# full check_interval behind - a tighter deadband tracks that more closely
# without pulsing on noise. Sub-threshold checks still accumulate against
# the original baseline rather than being lost (see _async_check()).

# seconds. Was 67.0 (stopwatch guess). Remeasured 2026-10-14 from real board
# events (pergola_controller 0.2.1+ event feed, not available when the
# stopwatch figure was taken): a full, uninterrupted open from `A`
# (opening started, 07:29:10.059) to `K` (end_of_travel, 07:30:24.699) took
# 74.6s - rounded up here. Confirmed `blades: 'open'` on the next poll.
DEFAULT_FULL_TRAVEL_TIME = 75.0

# Fixed per-pulse overhead added on top of the proportional delta*full_travel_time
# duration, to account for however long the motor/BLE link takes to actually start
# moving after a Close command is sent - a cost every pulse pays regardless of its
# size, so it disproportionately shortchanges small pulses if left uncompensated
# (observed 2026-10-10: winter sun mode's small nudges left the real slats visibly
# more open than the modelled position by late afternoon). 2026-10-11 placeholder:
# arbitrary (5% of that day's ~3.9s average pulse), not yet measured - replace with
# a real value once timed against the hardware (see "Pulse start delay" number
# entity docstring for the measurement procedure). 2026-10-12: 0.2s still wasn't
# enough (pergola still under-closing by end of day) - doubled to 0.4s, still an
# arbitrary placeholder pending real measurement.
DEFAULT_PULSE_START_DELAY = 0.4  # seconds

# Persists _last_check_target/_last_check_date/_virtual_position across
# HA restarts (2026-10-09 fix). Before this, every restart silently reset
# the sun-tracking baseline to "100% open, no reference for today" even
# though the real pergola hadn't moved - the next check would then pulse
# against that wrong baseline, overshooting. One file per config entry.
STORAGE_VERSION = 1
STORAGE_KEY_PREFIX = "pergola_winter_sun"
ANIMATION_TICK = 1.0  # seconds - purely cosmetic granularity for the diagnostic sensor


def profile_angle(azimuth: float, elevation: float) -> float:
    """The sun's "profile angle": how high it appears, in degrees, within
    the vertical plane perpendicular to the slats' rotation axis - this is
    the angle the slats need to match to lie edge-on to the sun and pass
    the most light through the gaps, instead of blocking it with their own
    width. Standard architectural shading formula
    (atan(tan(elevation)/sin(azimuth - WALL_ORIENTATION))), the axis-
    perpendicular-to-wall form (equivalent to the usual
    atan(tan(elevation)/cos(azimuth - reference)) used for a standard
    window overhang once `reference` is shifted 90 deg, since cos(x-90) ==
    sin(x) - these slats' axis runs away from the house, not along the
    wall).

    The raw formula only ever returns a value in (-90, 90) - but a flat
    slat has no front/back, so rotating it a further 180 deg lands on
    *exactly the same physical plane* (same shading effect), not a
    different one. Whenever the raw angle is negative (the direct result
    the one-directional motor can't reach, since it only ever rotates
    0 -> MAX_SLAT_ANGLE forward from closed), this returns its +180
    equivalent instead of giving up - which is very often still within
    [0, MAX_SLAT_ANGLE] and is the actual correct target, not an
    approximation of one.

    2026-10-17 bug fix (Vincent, live observation at 12:44 with the sun
    almost exactly on the slats' axis - azimuth 165.8 deg vs
    WALL_ORIENTATION's 165 - correctly expecting ~90 deg/vertical and
    instead seeing the pergola still pinned at 140/fully open since the
    morning): the previous version of this function returned None
    whenever the *raw* angle was negative, which is true for roughly the
    whole 1.5-2h stretch right before the sun crosses the slats' axis -
    even though the +180 equivalent was often already well within
    [0, MAX_SLAT_ANGLE] and should have been tracked smoothly through
    that whole window instead of snapping from 140 to ~90 only at the
    exact crossing. target_position() is what actually decides whether
    the (now always non-None) result is reachable, by comparing it to
    MAX_SLAT_ANGLE."""
    delta = math.radians(azimuth - WALL_ORIENTATION)
    sin_term = math.sin(delta)
    if sin_term == 0:
        return 90.0  # exactly on the axis - the undefined 0/0 limit
    raw = math.degrees(math.atan(math.tan(math.radians(elevation)) / sin_term))
    if raw < 0:
        raw += 180.0  # same physical plane, the direction the motor can reach
    return raw


def target_position(azimuth: float, elevation: float) -> float:
    """Desired virtual position (1.0 = fully open) for the sun's current
    position - follows the real slat geometry (profile_angle) rather than
    a simple azimuth ramp, floored at MIN_POSITION throughout."""
    if azimuth >= TRACK_END_AZIMUTH:
        return MIN_POSITION
    if elevation <= 0:
        # Sun at/below the horizon - HA's sun.sun state can still read
        # "above_horizon" for a narrow band around here (atmospheric
        # refraction margin), but there's no real light yet/any more to
        # shade against. profile_angle()'s atan()-based plane correction
        # (raw<0 -> raw+180) is only meaningful once elevation is clearly
        # positive: right at this boundary its sign flips on an
        # infinitesimal change in elevation, which snaps the result from
        # the correct "can't reach anything useful, stay open" (1.0) down
        # to the opposite extreme (MIN_POSITION) for a hair's difference
        # around zero (bug found 2026-10-26: auto-opened then immediately
        # pulsed closed for 56s at 07:15, right at sunrise). Treat it the
        # same as deep morning instead.
        return 1.0
    profile = profile_angle(azimuth, elevation)
    if profile > MAX_SLAT_ANGLE:
        # Deep morning (or any other position) this one-directional
        # mechanism genuinely can't reach any useful angle for - nothing
        # to gain by closing at all.
        return 1.0
    return max(MIN_POSITION, profile / MAX_SLAT_ANGLE)


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
        get_rain_forecast_entity_id: Callable[[], str | None],
        get_wind_threshold: Callable[[], float],
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._connection = connection
        self._get_weather_entity_id = get_weather_entity_id
        # meteofrance "next rain" nowcast sensor - a second, more immediate
        # rain source checked only for the dawn auto-open gate (2026-10-21,
        # see is_rain_forecast()'s docstring). Optional: None if not
        # configured, in which case that extra check simply never fires.
        self._get_rain_forecast_entity_id = get_rain_forecast_entity_id
        # Reads WindProtectionController.wind_speed_threshold - the two
        # features share one threshold so they never disagree on "windy".
        self._get_wind_threshold = get_wind_threshold
        self.enabled = False
        self.full_travel_time: float = DEFAULT_FULL_TRAVEL_TIME
        self.pulse_start_delay: float = DEFAULT_PULSE_START_DELAY
        self.position_deadband: float = DEFAULT_POSITION_DEADBAND
        self.check_interval_minutes: int = DEFAULT_CHECK_INTERVAL_MINUTES
        # The sun-tracking baseline: the target position (see
        # target_position) as of the last check that actually pulsed, and
        # the local calendar date it happened on. Pulse size is computed
        # from how much *this* has moved since last time - never from
        # _virtual_position - so a wildly-wrong absolute position estimate
        # (e.g. winter sun mode was off for days, or the physical remote was
        # used) can never produce a wrong-sized pulse: the worst case is
        # bounded by how far the sun itself has actually moved. None (or a
        # date other than today) means no baseline exists yet for today -
        # _async_check() then assumes fully-open and starts fresh from
        # there, which is safe even when wrong (see _async_check). Set
        # exclusively by _async_auto_open() (to 1.0) and _async_auto_close()
        # (to 0.0) - the only two places that ever (re)establish this
        # baseline (2026-10-17 - see module docstring).
        self._last_check_target: float | None = None
        self._last_check_date: date | None = None
        # One-shot daily auto-open attempt (2026-10-16): set the moment the
        # first check of a new day runs, regardless of whether the attempt
        # that follows actually succeeds - deliberately NOT retried later
        # that day if blocked by weather (Vincent's choice: one try at
        # dawn, give up and open by hand otherwise, rather than keep
        # retrying and opening unexpectedly later in the day).
        self._auto_open_attempted_date: date | None = None
        # One-shot daily auto-close attempt (2026-10-17), symmetric to the
        # above but never gated on weather - closing for the night is
        # always the safe move, see _async_auto_close().
        self._auto_close_attempted_date: date | None = None
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
        self._store: Store[dict] = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY_PREFIX}_{entry.entry_id}")
        # pergola_controller 0.2.0+ reports the blades' real state over BLE,
        # live, regardless of who/what caused it (HA, the physical remote,
        # the vendor app, or this controller's own auto-open/auto-close) -
        # _on_connection_status() mirrors it straight into the cosmetic
        # display estimate (open/closed snap instantly, opening/closing
        # animate, stopped freezes) and never touches the tracking baseline
        # above (2026-10-17 redesign - see module docstring).
        # _last_known_blades guards
        # against re-firing on every repeated status message while it just
        # sits open/closed (only react on an actual transition).
        self._last_known_blades: str | None = None
        connection.add_status_listener(self._on_connection_status)

    async def async_load(self) -> None:
        """Restore the sun-tracking baseline saved by _schedule_save(), if
        any. Must run before anything can call async_enable()/_async_check()
        - see __init__.py, which awaits this right after construction."""
        data = await self._store.async_load()
        if not data:
            return
        target = data.get("last_check_target")
        date_str = data.get("last_check_date")
        position = data.get("virtual_position")
        auto_open_date_str = data.get("auto_open_attempted_date")
        auto_close_date_str = data.get("auto_close_attempted_date")
        if target is not None:
            self._last_check_target = target
        if date_str is not None:
            self._last_check_date = date.fromisoformat(date_str)
        if position is not None:
            self._virtual_position = position
            self._display_position = position
        if auto_open_date_str is not None:
            self._auto_open_attempted_date = date.fromisoformat(auto_open_date_str)
        if auto_close_date_str is not None:
            self._auto_close_attempted_date = date.fromisoformat(auto_close_date_str)

    def _schedule_save(self) -> None:
        """Fire-and-forget persist of the current baseline - called from
        sync methods, so this can't just be awaited inline."""
        data = {
            "last_check_target": self._last_check_target,
            "last_check_date": (
                self._last_check_date.isoformat() if self._last_check_date else None
            ),
            "virtual_position": self._virtual_position,
            "auto_open_attempted_date": (
                self._auto_open_attempted_date.isoformat()
                if self._auto_open_attempted_date
                else None
            ),
            "auto_close_attempted_date": (
                self._auto_close_attempted_date.isoformat()
                if self._auto_close_attempted_date
                else None
            ),
        }
        self._hass.async_create_task(self._store.async_save(data))

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

    def _on_connection_status(self) -> None:
        """Fired on every PergolaStatus update (see PergolaConnection) -
        mirrors the device's own live `blades` state straight into the
        cosmetic display estimate, regardless of who or what caused the
        movement (HA, the vendor app, the remote, or this controller's own
        auto-open/auto-close) and entirely independently of the tracking
        baseline (_last_check_target/_last_check_date), which this never
        touches (2026-10-17 redesign - see module docstring). Only acts on
        an actual transition - repeated status messages while it just sits
        in one state must not keep re-triggering this (the device re-sends
        its status every ~5s even at rest)."""
        status = self._connection.status
        if status is None:
            return
        blades = status.blades
        if blades == self._last_known_blades:
            return
        self._last_known_blades = blades
        if blades == "open":
            self._snap_display(1.0)
        elif blades == "closed":
            self._snap_display(0.0)
        elif blades == "opening":
            self._virtual_position = 1.0
            self._start_animation_to(1.0)
            self._schedule_save()
        elif blades == "closing":
            self._virtual_position = 0.0
            self._start_animation_to(0.0)
            self._schedule_save()
        elif blades == "stopped":
            self._freeze_display()

    def _snap_display(self, value: float) -> None:
        """Instantly set the cosmetic display estimate - used for the
        device's own confirmed "open"/"closed" states, which are facts, not
        projections, so there's nothing to animate towards."""
        if self._animation_task is not None and not self._animation_task.done():
            self._animation_task.cancel()
        self._virtual_position = value
        self._display_position = value
        self._notify_listeners()
        self._schedule_save()

    def _freeze_display(self) -> None:
        """Freeze the cosmetic display estimate at wherever the animation
        had gotten to - used for the device's confirmed "stopped" state
        (halted mid-travel, by hand or by remote). This is actually the
        most accurate this estimate can ever be, since "stopped here, right
        now" is exactly what happened - unlike opening/closing, which only
        know the direction, not when it'll end."""
        if self._animation_task is not None and not self._animation_task.done():
            self._animation_task.cancel()
        self._virtual_position = self._display_position
        self._notify_listeners()
        self._schedule_save()

    def recalibrate(self, percentage: float) -> None:
        """Manually correct the *displayed* estimate, e.g. after eyeballing
        the real pergola. Deliberately cosmetic only - does NOT touch
        _last_check_target/_last_check_date, unlike _async_auto_open()/
        _async_auto_close(). A hand estimate is not a hard fact the way
        "motor ran to its physical Open/Close end-stop" is, so it must never
        be allowed to feed the pulse-sizing math - that's the exact failure
        mode the delta model
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
        self._schedule_save()

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

    def unsubscribe_schedule(self) -> None:
        """Cancel the periodic check subscription without touching
        `enabled` or notifying listeners - used on HA shutdown/entry
        unload instead of async_disable() so the switch's last *real*
        on/off value is what gets restored on the next restart, not a
        shutdown-time forced "off" (2026-10-09 fix, see __init__.py)."""
        if self._unsub_interval is not None:
            self._unsub_interval()
            self._unsub_interval = None

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

    async def _async_auto_open(self) -> None:
        """One-shot daily auto-open, called only from _async_check() on the
        first above-horizon check of the day once weather looks clear (see
        there). Just issues a plain Open, exactly as if the user had
        pressed the cover's own Open button - no need to check the real
        status first, since Open is always safe to (re)send regardless of
        current position. This is one of only two places
        (_async_auto_close() is the other) that ever (re)establish the
        tracking baseline - see module docstring. Best-effort: a BLE
        failure here just means today's baseline doesn't get this
        immediate 1.0 catch-up - the existing silent-seed fallback in
        _async_check() still applies, same as if this feature didn't exist
        at all. The cosmetic display estimate is NOT touched here - it
        follows the device's own live status independently, see
        _on_connection_status()."""
        try:
            async with self._connection.async_command(expect_motion=True) as pergola:
                await pergola.open()
        except Exception as err:  # noqa: BLE001 - best-effort, never block the rest of the check
            _LOGGER.warning("Winter sun mode: daily auto-open failed (%s)", err)
            return
        _LOGGER.info("Winter sun mode: auto-opened for the day (clear weather at dawn)")
        self._last_check_target = 1.0
        self._last_check_date = dt_util.now().date()
        self._schedule_save()

    async def _async_auto_close(self) -> None:
        """One-shot daily auto-close, called only from _async_check() on the
        first below-horizon check of the day (i.e. nightfall) - symmetric
        to _async_auto_open(), but never gated on weather: closing for the
        night is always the safe move, unlike opening onto active rain.
        Just issues a plain Close and lets it run to its own physical
        end-of-travel, same as _async_auto_open() does for Open - no
        _async_pulse_close()-style timed stop needed. The other of only two
        places that ever (re)establish the tracking baseline - see module
        docstring."""
        try:
            async with self._connection.async_command(expect_motion=True) as pergola:
                await pergola.close()
        except Exception as err:  # noqa: BLE001 - best-effort, never block the rest of the check
            _LOGGER.warning("Winter sun mode: daily auto-close failed (%s)", err)
            return
        _LOGGER.info("Winter sun mode: auto-closed for the night")
        self._last_check_target = 0.0
        self._last_check_date = dt_util.now().date()
        self._schedule_save()

    async def _async_check(self) -> None:
        sun_state = self._hass.states.get("sun.sun")
        if sun_state is None:
            return

        today = dt_util.now().date()

        if sun_state.state == "below_horizon":
            # Night - the one and only opportunity to auto-close for
            # tonight, symmetric to the auto-open below (same "mark
            # attempted before acting" reasoning: a single miss means
            # giving up until tomorrow, not retrying every check -
            # harmless here since closing is never time-sensitive the way
            # opening onto changing weather is, but keeps the two
            # symmetric and simple). Never gated on weather - see
            # _async_auto_close().
            # Close for the night whose daylight we actually just saw -
            # keyed off the date of that day's auto-open, not "today" at
            # the moment of this check. Night straddles midnight: the
            # first below_horizon check after midnight is still closing
            # for *yesterday's* dusk, not today's. Comparing against
            # "today" directly made that post-midnight check consume the
            # whole day's one-shot guard immediately, so the real dusk
            # later that same calendar day got silently skipped until the
            # next midnight rollover - the pergola then sat open ~6h past
            # every real sunset (bug found 2026-10-26, Vincent: pergola
            # still open at 19:52, well after dusk at 17:44).
            closing_for = self._auto_open_attempted_date or today
            if self._auto_close_attempted_date != closing_for:
                self._auto_close_attempted_date = closing_for
                self._schedule_save()
                await self._async_auto_close()
            return

        if sun_state.state != "above_horizon":
            # Neither "above_horizon" nor "below_horizon" - a transient
            # state (e.g. "unknown") seen right after a HA Core restart,
            # before the sun integration has finished its first
            # calculation. Bug found 2026-10-18 (Vincent: sunset came and
            # went with no auto-close): this used to be treated as "night"
            # by the `!=  "above_horizon"` check above, which could burn
            # the day's one-shot auto-close attempt hours early during a
            # daytime restart, leaving nothing to fire at the real sunset.
            # Just wait for a real reading instead of guessing.
            return

        weather_entity_id = self._get_weather_entity_id()

        if self._auto_open_attempted_date != today:
            # First check above the horizon today - the one and only
            # opportunity to auto-open for the day. Checks rain AND wind
            # (unlike the regular pulse-pause below, which only checks wind
            # - see 2026-10-16 discussion: pausing *pulses* for rain only
            # ever mattered in one narrow case, a deliberate manual reopen
            # mid-rain, and even then the consequence was a small, harmless
            # catch-up pulse - not worth the complexity. Opening onto active
            # rain is a completely different, much more real problem, so
            # rain still gates this). Marked attempted *before* the checks,
            # on purpose: a single miss means giving up for today, not
            # retrying every 15 minutes and potentially opening unexpectedly
            # later - open by hand instead if that happens.
            self._auto_open_attempted_date = today
            self._schedule_save()
            # Two independent rain sources (2026-10-21): Met.no's current
            # condition (is_raining) catches "raining right now", and the
            # meteofrance nowcast (is_rain_forecast) catches "about to start
            # within the next 30 min" - either one says yes and the open is
            # skipped, since opening right before rain starts is exactly the
            # scenario the onboard rain sensor can't protect against (it
            # only reacts once rain has actually begun). See
            # weather_guard.is_rain_forecast()'s docstring.
            if (
                not is_raining(self._hass, weather_entity_id)
                and not is_rain_forecast(
                    self._hass, self._get_rain_forecast_entity_id()
                )
                and not is_windy(
                    self._hass, weather_entity_id, self._get_wind_threshold()
                )
            ):
                await self._async_auto_open()

        if is_windy(self._hass, weather_entity_id, self._get_wind_threshold()):
            # Wind protection (separate switch) already closes on high wind
            # independently of winter sun mode - just stay quiet rather than
            # add redundant traffic. Rain is deliberately NOT checked here
            # any more (2026-10-16) - see the auto-open comment above.
            return

        # Aim for the sun's position at the *middle* of this check
        # interval, not "now" - the slats will sit at whatever we compute
        # here until the next check (check_interval_minutes away), so
        # targeting "now" always lags for the whole interval: correct the
        # instant we compute it, increasingly stale until the next check
        # catches up. Targeting the midpoint balances that instead -
        # slightly ahead of the sun in the first half of the interval,
        # slightly behind in the second half (proposed 2026-10-30,
        # Vincent). Uses astral directly (already a dependency of HA's
        # own `sun` integration, nothing new to install) rather than
        # extrapolating from the last sample, since that would have
        # nothing to extrapolate from on the day's first regular check
        # and would be inaccurate right after sunrise, where the rate of
        # change itself is changing fastest.
        projected_at = dt_util.now() + timedelta(
            minutes=self.check_interval_minutes / 2
        )
        try:
            observer = Observer(
                latitude=self._hass.config.latitude,
                longitude=self._hass.config.longitude,
                elevation=self._hass.config.elevation,
            )
            azimuth = astral_azimuth(observer, projected_at)
            elevation = astral_elevation(observer, projected_at)
        except Exception:
            _LOGGER.debug(
                "Winter sun mode: sun position projection failed, "
                "falling back to the current reading",
                exc_info=True,
            )
            azimuth = sun_state.attributes.get("azimuth")
            elevation = sun_state.attributes.get("elevation")
            if azimuth is None or elevation is None:
                return

        target = target_position(azimuth, elevation)

        if self._last_check_target is None or self._last_check_date != today:
            # No same-day baseline - either winter sun mode (or HA itself)
            # was off since before today's auto-open ran, or that auto-open
            # itself failed (see _async_auto_open()). We genuinely don't
            # know the real position, so we do NOT assume one and do NOT
            # pulse based on a guess - silently seed the baseline at
            # today's *current* target instead and wait for the next
            # check. From here on, pulses only ever reflect how much the
            # sun moves relative to this starting point - always small and
            # sensible regardless of what the real starting position
            # actually was.
            self._last_check_target = target
            self._last_check_date = today
            self._schedule_save()
            return

        delta = self._last_check_target - target
        if delta <= self.position_deadband:
            # Not worth a pulse yet - but deliberately NOT updating
            # _last_check_target here: several sub-threshold checks in a
            # row still need to add up against the *original* baseline, so
            # the eventual catch-up pulse reflects their sum, not just the
            # most recent (possibly tiny) increment.
            return

        pulse_seconds = delta * self.full_travel_time + self.pulse_start_delay
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
        self._schedule_save()
        # The cosmetic display estimate is NOT updated here - the Close
        # pulse below will itself make the device report "closing" then
        # "stopped" over BLE, which _on_connection_status() picks up and
        # animates on its own (2026-10-17 redesign - single source of truth
        # for the display, see module docstring).
        await self._async_pulse_close(pulse_seconds)

    async def _async_pulse_close(self, duration: float) -> None:
        # async_command(expect_motion=True) (2026-10-22 fix, replacing a
        # bare async_get()): without it, the periodic status poll landing
        # on the same shared connection could disconnect right after
        # stop() was sent below, before it reliably reached the device - on
        # a BLE session where no status notification arrived at all (a
        # known decoder quirk, see async_schedule_idle_disconnect()'s
        # docstring), nothing else was protecting the connection either.
        # The motor then kept running, unstopped, to its own physical
        # end-of-travel instead of halting after this short pulse. See
        # PergolaConnection.async_command()'s docstring.
        async with self._connection.async_command(expect_motion=True) as pergola:
            await pergola.close()
            await asyncio.sleep(duration)
            # 2026-10-08 fix: this used to call pergola.close() again here,
            # relying on the open/close toggle to halt the motor. But
            # Pergola.close() no-ops when self._motion is already "close"
            # (see pergola_controller/__init__.py) - so that second close()
            # never actually sent anything over BLE, and the motor just
            # kept running on its own past this pulse's intended duration
            # (observed: small nudge pulses ended up fully closing the
            # pergola). pergola.stop() has no such early-return guard - it
            # unconditionally resends the current direction (close, here)
            # to really toggle the motor off.
            await pergola.stop()
