"""Cover platform for Pergola."""

import logging
import time
from typing import Any

from homeassistant.components.cover import (
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import PergolaEntity

_LOGGER = logging.getLogger(__name__)

# Guards against a rapid repeat of the same Open/Close command (double-tap,
# a UI retry, testing multiple voice phrases back to back...) re-sending the
# command while the device is still acting on the first one. Open/Close are
# a TOGGLE on this hardware - sending the same direction twice in quick
# succession stops the motor instead of continuing it. Until 2026-10-13,
# pergola_controller's own `_motion` tracking already absorbed this; with
# 0.2.0, that guard only applies when a real status reading is fresh
# (<15s old), and falls back to a 90s-expiring estimate otherwise - which,
# combined with the status feed not actually arriving over HA's BLE
# connection (separate, unresolved issue - see the 0.2.0 migration notes),
# left rapid repeats getting sent for real and toggling the motor on/off/
# on/off. This is a blunt client-side safety net independent of all that:
# does not touch _motion, does not need any status feedback, just refuses a
# same-entity move command too soon after the last one.
_MOVE_COMMAND_COOLDOWN = 2.5  # seconds


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the pergola cover."""
    cover = PergolaCover(entry)
    async_add_entities([cover])
    entry.runtime_data.wind_protection.attach_cover(cover)


class PergolaCover(PergolaEntity, CoverEntity):
    """Representation of the pergola's retractable roof.

    pergola_controller 0.2.0+ reports the blades' real state over BLE
    (opening/closing/stopped/open/closed) while connected - see
    PergolaConnection.status. `is_opening`/`is_closing`/`is_closed` below
    read that live value when it's fresh enough to trust; `_attr_is_closed`
    is kept as a fallback for the brief window right after a command, before
    the device's own status confirms it, and for whenever we're not
    connected at all (periodic-poll mode spends most of its time
    disconnected by design - see entity.py)."""

    _attr_name = None
    _attr_device_class = CoverDeviceClass.AWNING
    _attr_supported_features = (
        CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP
    )
    # STATUS_MAX_AGE: how long a cached status is still trusted as "this is
    # really what the device is doing right now", vs. just the last thing we
    # happened to overhear. Comfortably above the ~5s the device re-sends its
    # status at even with nothing changing.
    _STATUS_MAX_AGE = 20.0

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize the pergola cover."""
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_cover"
        # Best guess from the last command WE sent, used whenever a live
        # status reading isn't available (not connected, or stale) - this is
        # the pre-0.2.0 behaviour kept as a fallback, never a fact on its
        # own.
        self._attr_is_closed = None
        self._last_move_command_at = 0.0

    @property
    def _fresh_blades_state(self) -> str | None:
        status = self._connection.status
        if status is None or self._connection.status_age > self._STATUS_MAX_AGE:
            return None
        return status.blades

    @property
    def is_opening(self) -> bool | None:
        blades = self._fresh_blades_state
        if blades is None:
            return None
        return blades == "opening"

    @property
    def is_closing(self) -> bool | None:
        blades = self._fresh_blades_state
        if blades is None:
            return None
        return blades == "closing"

    @property
    def is_closed(self) -> bool | None:
        blades = self._fresh_blades_state
        if blades == "closed":
            return True
        if blades in ("open", "opening", "closing"):
            return False
        if blades == "stopped":
            # Stopped mid-travel - genuinely neither open nor closed, and
            # unlike the no-status case below we KNOW that, rather than just
            # not knowing. Reporting False here (2026-10-14 bug) made HA
            # think it was already fully open and grey out the Open button -
            # None instead keeps both Open and Close available, which is
            # the correct thing to offer from an unknown partial position.
            return None
        # No fresh live reading at all - fall back to our own best guess
        # from the last command sent (may be None/unknown).
        return self._attr_is_closed

    @property
    def available(self) -> bool:
        # Unlike most entities, this one stays available even while
        # disconnected (periodic-poll mode is disconnected most of the
        # time by design) - commands still work, they just (re)connect
        # first. Only genuinely unreachable if BLE discovery has never
        # heard of it at all, which _async_connect()'s HomeAssistantError
        # already surfaces as a service-call failure when it happens.
        return True

    def _check_move_cooldown(self) -> bool:
        """Returns True (and logs) if a move command came in too soon after
        the last one and should be dropped - see _MOVE_COMMAND_COOLDOWN."""
        now = time.monotonic()
        if now - self._last_move_command_at < _MOVE_COMMAND_COOLDOWN:
            _LOGGER.warning(
                "Pergola: ignoring open/close within %.1fs of the previous "
                "one (debounce) - Open/Close toggle this hardware, so "
                "repeating it would stop the motor instead of continuing it",
                _MOVE_COMMAND_COOLDOWN,
            )
            return True
        self._last_move_command_at = now
        return False

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the pergola."""
        if self._check_move_cooldown():
            return
        async with self._async_connect() as pergola:
            await pergola.open()
        self._attr_is_closed = False
        self.async_write_ha_state()
        # Winter sun mode never opens the cover itself - this is the only
        # place its tracking baseline gets (re)established.
        self._winter_sun.on_manual_open()

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the pergola."""
        if self._check_move_cooldown():
            return
        async with self._async_connect() as pergola:
            await pergola.close()
        self._attr_is_closed = True
        self.async_write_ha_state()
        # Keeps winter sun mode's "estimated open percentage" diagnostic
        # honest for anyone testing/watching it - doesn't change what winter
        # sun mode does next, see on_manual_close()'s docstring.
        self._winter_sun.on_manual_close()

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the pergola.

        The device has no real standalone stop: the documented stop command
        (cmd_active/'B') does nothing on real hardware. pergola_controller
        works around this itself - it tracks whichever direction it last
        sent on this same connection and re-sends it to halt the motor (same
        as the vendor app's buttons: Open/Close are a toggle). Nothing to
        duplicate here."""
        async with self._async_connect() as pergola:
            await pergola.stop()
        # A deliberate Stop should never itself be blocked by the cooldown
        # above (not debounced, see async_open_cover/async_close_cover), and
        # clears it too - someone who just stopped the pergola on purpose
        # very likely wants to immediately try Open or Close next, not wait
        # out a cooldown meant to catch accidental repeats.
        self._last_move_command_at = 0.0
        # Freezes the estimate where it actually stopped (e.g. a hand-timed
        # manual pulse: Close, wait a few seconds, Stop) instead of letting
        # it keep drifting towards whatever the preceding Open/Close was
        # heading for.
        self._winter_sun.on_manual_stop()

    async def async_added_to_hass(self) -> None:
        self._connection.add_status_listener(self._handle_status_update)

    async def async_will_remove_from_hass(self) -> None:
        self._connection.remove_status_listener(self._handle_status_update)

    def _handle_status_update(self) -> None:
        self.async_write_ha_state()
