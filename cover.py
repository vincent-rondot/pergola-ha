"""Cover platform for Pergola."""

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
    """Representation of the pergola's retractable roof."""

    _attr_name = None
    _attr_device_class = CoverDeviceClass.AWNING
    _attr_assumed_state = True
    _attr_supported_features = (
        CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP
    )

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize the pergola cover."""
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_cover"
        # No position feedback exists on this device: this is only ever a
        # best guess based on the last command sent from HA, never a fact.
        self._attr_is_closed = None

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the pergola."""
        async with self._async_connect() as pergola:
            await pergola.open()
        self._attr_is_closed = False
        self.async_write_ha_state()
        # Winter sun mode never opens the cover itself - this is the only
        # place its tracking baseline gets (re)established.
        self._winter_sun.on_manual_open()

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the pergola."""
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
        (>=0.1.7) already works around this itself - it tracks whichever
        direction it last sent on this same connection and re-sends it to
        halt the motor (same as the vendor app's buttons: Open/Close are a
        toggle). Nothing to duplicate here."""
        async with self._async_connect() as pergola:
            await pergola.stop()
        # Freezes the estimate where it actually stopped (e.g. a hand-timed
        # manual pulse: Close, wait a few seconds, Stop) instead of letting
        # it keep drifting towards whatever the preceding Open/Close was
        # heading for.
        self._winter_sun.on_manual_stop()
