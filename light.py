"""Light platform for Pergola."""

from typing import Any

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import PergolaEntity

_STATUS_MAX_AGE = 20.0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the pergola's integrated LED strip."""
    async_add_entities([PergolaLight(entry)])


class PergolaLight(PergolaEntity, LightEntity):
    """Representation of the pergola's built-in dimmable LED strip.

    Brightness control over Bluetooth does work - what looked broken during
    0.2.0 testing (2026-10-13) was actually a status *decoder* bug (the card
    drops the trailing registration letter from its status messages right
    after an intensity change, and 0.2.0's decoder rejected that shape,
    making the readback look stuck). Fixed in pergola_controller 0.2.1
    (2026-10-14) - see docs/pergola_controller_0.2.0_ha_migration.md's
    "Mise à jour du 14/10" section. The write itself was never the problem."""

    _attr_name = "LED strip"
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize the pergola light."""
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_light"
        # Best guess from the last command WE sent - fallback for whenever a
        # live status reading isn't available (not connected, or stale).
        self._attr_is_on = None
        self._attr_brightness = None

    @property
    def is_on(self) -> bool | None:
        status = self._connection.status
        if status is not None and self._connection.status_age <= _STATUS_MAX_AGE:
            return status.light_on
        return self._attr_is_on

    @property
    def brightness(self) -> int | None:
        status = self._connection.status
        if (
            status is not None
            and status.intensity is not None
            and self._connection.status_age <= _STATUS_MAX_AGE
        ):
            return round(status.intensity * 255 / 100)
        return self._attr_brightness

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the LED strip, optionally at a given brightness."""
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        percent = round(brightness * 100 / 255) if brightness is not None else None
        async with self._async_connect() as pergola:
            if percent is not None and self.is_on:
                # Already on - change the level of an already-lit strip.
                await pergola.set_intensity(percent)
            else:
                # Off, or no particular brightness requested - light_on()
                # takes the level directly (2-100%, None = keep whatever it
                # was last at).
                await pergola.light_on(percent)
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

    async def async_added_to_hass(self) -> None:
        self._connection.add_status_listener(self._handle_status_update)

    async def async_will_remove_from_hass(self) -> None:
        self._connection.remove_status_listener(self._handle_status_update)

    def _handle_status_update(self) -> None:
        self.async_write_ha_state()
