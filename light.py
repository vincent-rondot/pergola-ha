"""Light platform for Pergola."""

from typing import Any

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import PergolaEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the pergola's integrated LED strip."""
    async_add_entities([PergolaLight(entry)])


class PergolaLight(PergolaEntity, LightEntity):
    """Representation of the pergola's built-in dimmable LED strip."""

    _attr_name = "LED strip"
    _attr_assumed_state = True
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize the pergola light."""
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_light"
        # No status feedback exists on this device: this is only ever a best
        # guess based on the last command sent from HA, never a fact.
        self._attr_is_on = None
        self._attr_brightness = 255

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the LED strip, optionally at a given brightness."""
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        async with self._async_connect() as pergola:
            if brightness is not None:
                await pergola.send_command("intensity", str(brightness))
            await pergola.light_on()
        if brightness is not None:
            self._attr_brightness = brightness
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the LED strip."""
        async with self._async_connect() as pergola:
            await pergola.light_off()
        self._attr_is_on = False
        self.async_write_ha_state()
