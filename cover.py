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
    async_add_entities([PergolaCover(entry)])


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

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the pergola."""
        async with self._async_connect() as pergola:
            await pergola.close()
        self._attr_is_closed = True
        self.async_write_ha_state()

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the pergola."""
        async with self._async_connect() as pergola:
            await pergola.stop()
