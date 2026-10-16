"""Winter sun-tracking mode.

Nudges the pergola's slats closed through the day to follow the sun, aiming
the slats' angle at the sun's real profile angle (see _profile_angle()) so
they pass maximum light rather than blocking it - using only timed Close
pulses, the device's only fine-grained control. Tracking for a given day
starts from "fully open" (1.0): either the user opens it by hand (HA, the
vendor app, or the remote - see on_manual_open()), or - since
pergola_controller 0.2.1 gave this integration real status feedback - this
controller does it itself, once, at the first check of each day, if the
weather looks clear (see _async_auto_open(), 2026-10-16). Every check after
that is still only ever a Close pulse, same as before. See weather_guard.py
for the wind/rain awareness this pauses on - rain only gates the daily
auto-open, not the regular pulses (2026-10-16 - see _async_check()'s
comments for why).
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable
from datetime import date, datetime
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
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

# Confirmed 2026-10-16 (Vincent - order e-mail to the installer, and the
# vendor's spec sheet): the terrace faces 165 deg exactly, slats run
# perpendicular to the wall (each slat's long axis points away from the
# house) and pivot together from 0 deg (closed, flat, overlapping -
# watertight) up to 140 deg (fully open). Deliberately ordered to open
# towards the west ("pour maximiser le soleil du soir" - Vincent's own
# words to the installer) - see _profile_angle()'s docstring for how that
# shapes the model below.
WALL_ORIENTATION = 165.0  # degrees, compass bearing the terrace faces
MAX_SLAT_ANGLE = 140.0  # degrees, 0 = closed/flat

TRACK_END_AZIMUTH = 260.0  # safety cap - treat anything past this as MIN_POSITION
MIN_POSITION = 0.4  # never auto-close past 60% closed (angle 56 deg) - real
# slats have real thickness, so the idealized "angle = exact profile angle"
# geometry below stops meaning anything useful at shallow angles anyway (a
# near-flat slat doesn't pass meaningfully more light than a fully closed
# one, whatever the math says) - confirmed by Vincent 2026-10-16, keeping
# this floor rather than chasing the model all the way to ~5% near sunset.

# Default for the "Winter sun deadband" number entity - ignore corrections
# smaller than this (as a 0-1 fraction). Lower it (e.g. via that entity) to
# get pulses to trigger without waiting for a large azimuth change, e.g.
# when testing against the simulated device.
DEFAULT_POSITION_DEADBAND = 0.05

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


def _profile_angle(azimuth: float, elevation: float) -> float | None:
    """The sun's "profile angle": how high it appears, in degrees, within
    the vertical plane perpendicular to the slats' rotation axis - this is
    the angle the slats need to match to lie edge-on to the sun and pass
    the most light through the gaps, instead of blocking it with their own
    width. Standard architectural shading formula
    (atan(tan(elevation)/cos(azimuth - reference))), adapted by 90 deg from
    its usual form because these slats are perpendicular to the wall, not
    parallel to it like a standard window overhang.

    Returns None when the sun is on the wrong side of the slats' axis for
    this one-directional mechanism (the "opens towards the west" choice
    made at order time only ever gives a sensible, usable angle once the
    sun has crossed to the western half of the sky - mornings have no
    meaningful profile angle for it at all). Confirmed against real azimuth/
    elevation data from 2026-10-16: the crossing happens right around solar
    noon, exactly where "opens towards the west" would predict it should."""
    reference_bearing = WALL_ORIENTATION + 90.0
    cos_term = math.cos(math.radians(azimuth - reference_bearing))
    if cos_term <= 0:
        return None
    return math.degrees(math.atan(math.tan(math.radians(elevation)) / cos_term))


def _target_position(azimuth: float, elevation: float) -> float:
    """Desired virtual position (1.0 = fully open) for the sun's current
    position - follows the real slat geometry (_profile_angle) rather than
    a simple azimuth ramp, floored at MIN_POSITION throughout."""
    if azimuth >= TRACK_END_AZIMUTH:
        return MIN_POSITION
    profile = _profile_angle(azimuth, elevation)
    if profile is None:
        # Morning, or any other position this one-directional mechanism
        # can't usefully align to - nothing to gain by closing at all.
        return 1.0
    angle = max(0.0, min(MAX_SLAT_ANGLE, profile))
    return max(MIN_POSITION, angle / MAX_SLAT_ANGLE)


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
        self.pulse_start_delay: float = DEFAULT_PULSE_START_DELAY
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
        # One-shot daily auto-open attempt (2026-10-16): set the moment the
        # first check of a new day runs, regardless of whether the attempt
        # that follows actually succeeds - deliberately NOT retried later
        # that day if blocked by weather (Vincent's choice: one try at
        # dawn, give up and open by hand otherwise, rather than keep
        # retrying and opening unexpectedly later in the day).
        self._auto_open_attempted_date: date | None = None
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
        # pergola_controller 0.2.0+ reports the blades' real state over BLE.
        # Whenever it confirms "open" or "closed" - regardless of who/what
        # caused it (HA, the physical remote, the vendor app) - that's a
        # fact we didn't have before, so sync the cosmetic display estimate
        # to match (100%/0%) exactly like on_manual_open()/on_manual_close()
        # already do for HA-driven commands. _last_known_blades guards
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
        if target is not None:
            self._last_check_target = target
        if date_str is not None:
            self._last_check_date = date.fromisoformat(date_str)
        if position is not None:
            self._virtual_position = position
            self._display_position = position
        if auto_open_date_str is not None:
            self._auto_open_attempted_date = date.fromisoformat(auto_open_date_str)

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
        """Fired on every PergolaStatus update (see PergolaConnection). Only
        acts on an actual open<->closed transition - repeated status
        messages while it just sits in one of those states must NOT keep
        re-triggering on_manual_open()/on_manual_close(), or the tracking
        baseline would get force-reset every ~5s all day and winter sun mode
        could never accumulate a meaningful delta."""
        status = self._connection.status
        if status is None:
            return
        blades = status.blades
        if blades == self._last_known_blades:
            return
        self._last_known_blades = blades
        if blades == "open":
            self.on_manual_open()
        elif blades == "closed":
            self.on_manual_close()

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
        self._schedule_save()

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
        self._schedule_save()

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
        self._schedule_save()

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
        current position. Best-effort: a BLE failure here just means
        today's baseline doesn't get the immediate 1.0 catch-up that
        on_manual_open() gives - the existing silent-seed fallback in
        _async_check() still applies, same as if this feature didn't exist
        at all."""
        try:
            pergola = await self._connection.async_get()
            await pergola.open()
        except Exception as err:  # noqa: BLE001 - best-effort, never block the rest of the check
            _LOGGER.warning("Winter sun mode: daily auto-open failed (%s)", err)
            return
        finally:
            self._connection.async_schedule_idle_disconnect()
        _LOGGER.info("Winter sun mode: auto-opened for the day (clear weather at dawn)")
        self.on_manual_open()

    async def _async_check(self) -> None:
        sun_state = self._hass.states.get("sun.sun")
        if sun_state is None or sun_state.state != "above_horizon":
            return

        weather_entity_id = self._get_weather_entity_id()

        today = dt_util.now().date()
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
            if not is_raining(self._hass, weather_entity_id) and not is_windy(
                self._hass, weather_entity_id, self._get_wind_threshold()
            ):
                await self._async_auto_open()

        if is_windy(self._hass, weather_entity_id, self._get_wind_threshold()):
            # Wind protection (separate switch) already closes on high wind
            # independently of winter sun mode - just stay quiet rather than
            # add redundant traffic. Rain is deliberately NOT checked here
            # any more (2026-10-16) - see the auto-open comment above.
            return

        azimuth = sun_state.attributes.get("azimuth")
        elevation = sun_state.attributes.get("elevation")
        if azimuth is None or elevation is None:
            return

        target = _target_position(azimuth, elevation)

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
        self._virtual_position = max(0.0, self._virtual_position - delta)
        self._start_animation_to(self._virtual_position)  # runs concurrently with the real pulse below
        self._schedule_save()
        await self._async_pulse_close(pulse_seconds)

    async def _async_pulse_close(self, duration: float) -> None:
        pergola = await self._connection.async_get()
        try:
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
        finally:
            self._connection.async_schedule_idle_disconnect()
