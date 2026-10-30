"""Base entity and shared BLE connection helper for the Pergola integration."""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta

from pergola_controller import Pergola

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from .const import DOMAIN
from .sun_tracker import DEFAULT_FULL_TRAVEL_TIME, WinterSunController
from .weather_guard import WindProtectionController

_LOGGER = logging.getLogger(__name__)

DEFAULT_IDLE_TIMEOUT = 60.0
# Safety margin applied to DEFAULT_FULL_TRAVEL_TIME while the blades are
# reported "opening"/"closing" (see PergolaConnection._on_status) - not the
# live, user-tunable WinterSunController.full_travel_time estimate, since
# this is just a "give up and disconnect anyway" cap, not a timing decision:
# comfortably above any realistic real travel time is all that matters here,
# precision isn't (2026-10-17).
MOTION_SAFETY_MULTIPLIER = 1.5
# How often to connect briefly, grab a fresh status report, and disconnect
# again - when NOT in "Stay connected" mode. Deliberately not permanent: the
# device only accepts one BLE client, so holding the link open 24/7 would
# lock out the manufacturer's phone app/web app indefinitely (2026-10-13 -
# explicitly rejected in favour of this periodic approach instead).
DEFAULT_STATUS_POLL_INTERVAL = 300.0  # 5 minutes
STATUS_WAIT_TIMEOUT = 7.0  # pergola_controller docs: status arrives within 5s of connecting


class PergolaConnection:
    """Holds a single BLE connection to the pergola, shared by every entity
    of a config entry.

    The device only accepts a single BLE connection at a time, so staying
    connected locks out the manufacturer's phone app. Two modes, controlled
    by the "Stay connected" switch entity:

    - Off (default): connect on demand, but keep the link open for
      `idle_timeout` seconds after the last command instead of disconnecting
      immediately - repeated commands in a short session stay fast, and the
      phone app is only blocked briefly after actual use. In this mode, a
      periodic background poll (see `_async_periodic_poll`) connects briefly
      every `status_poll_interval` seconds just to pick up a fresh
      `PergolaStatus`, then disconnects right away - so entities still see a
      reasonably live state without the connection ever lingering long.
    - On: stay connected indefinitely (with automatic reconnect if the link
      drops), until turned off, entry unload, or HA stop. Status/events
      stream continuously in this mode, no polling needed.
    """

    def __init__(self, hass: HomeAssistant, address: str, client_id: str, pin: str) -> None:
        self._hass = hass
        self._address = address
        self._client_id = client_id
        self._pin = pin
        self._pergola: Pergola | None = None
        # Serializes async_get()'s check-and-connect against itself - see
        # there for the race it closes (2026-10-17, diagnosed with the
        # pergola_controller maintainer after intermittent "Not connected to
        # any device" command failures).
        self._connect_lock = asyncio.Lock()
        self.stay_connected: bool = False
        self.idle_timeout: float = DEFAULT_IDLE_TIMEOUT
        self.status_poll_interval: float = DEFAULT_STATUS_POLL_INTERVAL
        self._cancel_idle_timer: Callable[[], None] | None = None
        self._unsub_poll: Callable[[], None] | None = None
        self._reconnect_task = None
        self._stopping = False
        self._listeners: list[Callable[[], None]] = []
        # Latest PergolaStatus (0.2.0+), updated from the device's own
        # notifications while connected - None until the first one arrives.
        # Purely a cache of what the device last reported; still subject to
        # the same staleness concerns as everything else here once
        # disconnected (see `status_age` below).
        self.status = None
        self._status_listeners: list[Callable[[], None]] = []
        # Counts motor/light commands currently in flight via
        # async_command() below - checked by _async_periodic_poll() so it
        # never disconnects out from under one (2026-10-22, see
        # async_command()'s docstring).
        self._commands_in_flight = 0

    def add_listener(self, callback: Callable[[], None]) -> None:
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[], None]) -> None:
        self._listeners.remove(callback)

    def _notify_listeners(self) -> None:
        for callback in self._listeners:
            callback()

    def add_status_listener(self, callback: Callable[[], None]) -> None:
        """Subscribe to PergolaStatus updates (fired on every status message
        AND on disconnect, so an `available` property depending on this can
        react to the link dropping too)."""
        self._status_listeners.append(callback)

    def remove_status_listener(self, callback: Callable[[], None]) -> None:
        self._status_listeners.remove(callback)

    def _notify_status_listeners(self) -> None:
        for callback in self._status_listeners:
            callback()

    @property
    def is_connected(self) -> bool:
        return self._pergola is not None and self._pergola.is_connected

    @property
    def status_age(self) -> float:
        """Seconds since the last status message, or infinity if none yet."""
        if self.status is None:
            return float("inf")
        return self.status.age()

    async def async_get(self) -> Pergola:
        """Return a connected Pergola, reconnecting only if needed.

        Guarded by `_connect_lock` (2026-10-17 fix): without it, two callers
        racing in while disconnected (e.g. the periodic status poll and a
        user command landing close together) could each decide "not
        connected, let's connect", and the OLD code published its new
        instance to `self._pergola` *before* awaiting `connect()` - so the
        loser's own `await pergola.connect()` would still succeed, but by
        the time it returned, `self._pergola` had already been overwritten
        by the winner's (possibly still-connecting) instance. The loser's
        very next command then read back that wrong instance and failed
        with "Not connected to any device", while its own, actually-
        connected instance was silently orphaned - never disconnected, its
        SOA loop and status listener running forever, potentially locking
        the device's single BLE client slot away from the manufacturer's
        app indefinitely. Diagnosed together with the pergola_controller
        maintainer - not fixable from the library side, since the problem
        is two instances existing at all, not a race *within* one; single
        ownership has to live here. Only publishes `self._pergola` once
        `connect()` has actually succeeded, and defensively disconnects
        whatever stale instance preceded it first."""
        self._cancel_pending_timer()
        async with self._connect_lock:
            if self._pergola is not None and self._pergola.is_connected:
                return self._pergola
            old, self._pergola = self._pergola, None
            if old is not None:
                try:
                    await old.disconnect()
                except Exception:  # noqa: BLE001 - best-effort cleanup of a half-dead instance
                    pass
            ble_device = bluetooth.async_ble_device_from_address(
                self._hass, self._address, connectable=True
            )
            pergola = Pergola(
                self._address, self._client_id, self._pin, ble_device=ble_device
            )
            pergola.add_listener(self._make_status_handler(pergola))
            try:
                await pergola.connect()
            except Exception:
                try:
                    await pergola.disconnect()
                except Exception:  # noqa: BLE001 - best-effort cleanup after a failed connect
                    pass
                raise
            self._pergola = pergola
            self._notify_listeners()
            return pergola

    def _make_status_handler(self, pergola: Pergola) -> Callable[[object], None]:
        """Binds a status callback to one specific Pergola instance, so a
        stale instance - e.g. one mid-teardown right after being superseded
        in async_get() - can't still mutate `self.status` after it's no
        longer the active connection."""

        def handler(status: object) -> None:
            if pergola is self._pergola:
                self._on_status(status)

        return handler

    def _on_status(self, status) -> None:
        """Called by the lib on every status update, AND when the link
        drops (see pergola_controller 0.2.0 docs) - in that second case
        `self.is_connected` reads False even though `status` itself may
        just be the last known value repeated."""
        previous_blades = self.status.blades if self.status is not None else None
        self.status = status
        self._notify_status_listeners()
        if self.stay_connected and not self.is_connected and not self._stopping:
            self._async_ensure_reconnecting()
            return
        if self.stay_connected or not self.is_connected or self._stopping:
            return
        # Motion-aware idle disconnect (2026-10-17): only react to an actual
        # opening/closing <-> settled transition, never to a repeated
        # heartbeat status (the device re-sends its status every ~5s even
        # at rest while connected - reacting to every one of those would
        # keep re-arming a timer forever and the periodic-disconnect design
        # this is built around would never fire). On the transition INTO
        # motion, swap whatever idle timer is running for a single, fixed
        # safety-cap timer (not renewed by further "still moving" statuses,
        # so a genuinely stuck motor that keeps reporting "opening" forever
        # still eventually gets disconnected rather than holding the
        # connection - and locking out the manufacturer's app - forever).
        # On the transition OUT of motion (confirmed open/closed/stopped),
        # immediately fall back to the normal short idle countdown instead
        # of waiting out whatever's left of the safety cap.
        moving = status.blades in ("opening", "closing")
        was_moving = previous_blades in ("opening", "closing")
        if moving and not was_moving:
            self.async_schedule_idle_disconnect(expect_motion=True)
        elif was_moving and not moving:
            self.async_schedule_idle_disconnect()

    def _async_ensure_reconnecting(self) -> None:
        """Fire-and-forget: (re)connect because we're supposed to stay
        connected and currently aren't. No-op if a reconnect attempt is
        already in flight."""
        if self._reconnect_task is not None:
            return
        self._reconnect_task = self._hass.async_create_task(
            self._async_reconnect_loop(), "pergola stay-connected reconnect"
        )

    async def _async_reconnect_loop(self) -> None:
        delay = 2.0
        try:
            while self.stay_connected and not self.is_connected and not self._stopping:
                try:
                    await self.async_get()
                    return
                except Exception as err:  # noqa: BLE001 - best-effort retry loop
                    _LOGGER.warning(
                        "Pergola: reconnect attempt failed (%s), retrying in %.0fs", err, delay
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 60.0)
        finally:
            self._reconnect_task = None

    def async_schedule_idle_disconnect(self, *, expect_motion: bool = False) -> None:
        """Call after a command completes. (Re)starts the idle-disconnect
        countdown unless staying connected.

        `expect_motion=True` - pass this when the command just sent (Open/
        Close) is expected to keep the motor running well past the normal
        idle_timeout - arms the long motion-safety timer directly, without
        waiting for a confirmed "opening"/"closing" status first. Status
        decoding can silently fail for an entire BLE session in some
        conditions (observed 2026-10-17, right after a light/intensity
        command - matches the known decoder quirk in CLAUDE.md) - waiting
        for that confirmation before protecting against an early disconnect
        isn't reliable enough on its own, so callers that know they just
        triggered a long move say so directly instead. `_on_status()`
        still independently arms this same long timer when it DOES see a
        confirmed "opening"/"closing" transition (covers motion this
        controller didn't itself command, e.g. the physical remote) and
        still shortens it back to normal the moment motion is confirmed
        done."""
        if self.stay_connected:
            return
        self._cancel_pending_timer()
        if expect_motion or (self.status is not None and self.status.blades in ("opening", "closing")):
            self._cancel_idle_timer = async_call_later(
                self._hass,
                DEFAULT_FULL_TRAVEL_TIME * MOTION_SAFETY_MULTIPLIER,
                self._async_idle_disconnect,
            )
            return
        self._cancel_idle_timer = async_call_later(
            self._hass, self.idle_timeout, self._async_idle_disconnect
        )

    @asynccontextmanager
    async def async_command(self, *, expect_motion: bool = False) -> AsyncIterator[Pergola]:
        """Get the shared connection for a motor/light command. Every
        command (entities via PergolaEntity._async_connect(), and the
        winter-sun controller's own open/close/stop calls) must go through
        this - not a bare async_get() - so _async_periodic_poll() below
        knows one is in flight and skips its own disconnect instead of
        racing it.

        Bug found 2026-10-22 (Vincent: pergola fully closed by a pulse that
        was only supposed to nudge it 8%): the periodic poll disconnects
        unconditionally after its own wait_for_status() - it doesn't know
        or care whether a command is mid-flight on the same shared
        connection. That day, this pulse's BLE session never got a single
        status notification (the decoder quirk referenced in
        async_schedule_idle_disconnect()'s docstring), so _on_status()
        never armed the motion-safety timer either - nothing was protecting
        the connection at all, and the poll's disconnect landed right after
        the pulse's own stop() was sent, before it could reliably reach the
        device. The motor then ran unchecked to its real physical end-of-
        travel instead of stopping after the intended short pulse. See
        _async_periodic_poll()."""
        self._commands_in_flight += 1
        try:
            pergola = await self.async_get()
            yield pergola
        finally:
            self._commands_in_flight -= 1
            self.async_schedule_idle_disconnect(expect_motion=expect_motion)

    def async_set_stay_connected(self, value: bool) -> None:
        self.stay_connected = value
        if value:
            self._cancel_pending_timer()
            self._async_ensure_reconnecting()
        elif self.is_connected:
            self.async_schedule_idle_disconnect()

    def async_set_status_poll_interval(self, seconds: float) -> None:
        self.status_poll_interval = seconds
        self._reschedule_poll()

    def _cancel_pending_timer(self) -> None:
        if self._cancel_idle_timer is not None:
            self._cancel_idle_timer()
            self._cancel_idle_timer = None

    async def _async_idle_disconnect(self, _now=None) -> None:
        self._cancel_idle_timer = None
        await self.async_disconnect()

    def async_start(self) -> None:
        """Call once, after setup. Starts the periodic status poll (unless
        staying connected, in which case an immediate connect is more useful
        - see async_set_stay_connected) and does one poll right away so
        entities don't wait a full interval for their first real status."""
        self._reschedule_poll()
        if self.stay_connected:
            self._async_ensure_reconnecting()
        else:
            self._hass.async_create_task(
                self._async_periodic_poll(), "pergola initial status poll"
            )

    def _reschedule_poll(self) -> None:
        if self._unsub_poll is not None:
            self._unsub_poll()
        self._unsub_poll = async_track_time_interval(
            self._hass,
            self._async_periodic_poll,
            timedelta(seconds=self.status_poll_interval),
        )

    async def _async_periodic_poll(self, _now=None) -> None:
        """Briefly connect just to pick up a fresh PergolaStatus, then
        disconnect - the non-permanent way of staying reasonably up to date
        without locking out the manufacturer's app. No-op if we're already
        connected for some other reason (stay_connected, or a command still
        within its idle-timeout window) - no need to pay the connect/
        disconnect cost twice.

        Skips the disconnect (2026-10-22 fix) if a command - via
        async_command() - is now in flight on this same shared connection:
        unconditionally disconnecting here used to race an open/close/stop
        command landing at the same scheduled tick, cutting it off before
        it reliably reached the device. See async_command()'s docstring."""
        if self.stay_connected or self.is_connected or self._stopping:
            return
        try:
            pergola = await self.async_get()
            await pergola.wait_for_status(timeout=STATUS_WAIT_TIMEOUT)
        except Exception as err:  # noqa: BLE001 - this is a best-effort background poll
            _LOGGER.debug("Pergola: periodic status poll failed: %s", err)
        finally:
            if self._commands_in_flight == 0:
                await self.async_disconnect()

    def async_stop(self) -> None:
        """Call on entry unload / HA stop, before async_disconnect()."""
        self._stopping = True
        if self._unsub_poll is not None:
            self._unsub_poll()
            self._unsub_poll = None
        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
            self._reconnect_task = None

    async def async_disconnect(self) -> None:
        """Disconnect if connected. Call on entry unload / HA stop / idle
        timeout / after a periodic poll."""
        self._cancel_pending_timer()
        pergola = self._pergola
        self._pergola = None
        if pergola is not None:
            try:
                await pergola.disconnect()
            except Exception:  # pragma: no cover - best-effort cleanup
                pass
            self._notify_listeners()
            self._notify_status_listeners()


@dataclass
class PergolaRuntimeData:
    """Everything shared across this config entry's entities."""

    connection: PergolaConnection
    wind_protection: WindProtectionController
    winter_sun: WinterSunController


class PergolaEntity(Entity):
    """Base entity sharing device info and the shared BLE connection."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize the entity."""
        runtime_data: PergolaRuntimeData = entry.runtime_data
        self._connection = runtime_data.connection
        self._wind_protection = runtime_data.wind_protection
        self._winter_sun = runtime_data.winter_sun
        self._address = entry.data[CONF_ADDRESS]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Pergola",
        )

    @asynccontextmanager
    async def _async_connect(self, *, expect_motion: bool = False) -> AsyncIterator[Pergola]:
        """Get the shared connection, connecting if needed. Pass
        expect_motion=True for a command (Open/Close) expected to keep the
        motor running well past the connection's normal idle-disconnect
        window - see PergolaConnection.async_schedule_idle_disconnect().
        Delegates to PergolaConnection.async_command() (2026-10-22) so the
        periodic poll knows this command is in flight - see its docstring."""
        try:
            async with self._connection.async_command(expect_motion=expect_motion) as pergola:
                yield pergola
        except Exception as err:
            raise HomeAssistantError(
                f"Could not reach the pergola at {self._address}: {err}"
            ) from err
