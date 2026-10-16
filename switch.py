"""Switch platform for Pergola - toggles staying connected."""

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .entity import PergolaEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the pergola switches."""
    async_add_entities(
        [
            PergolaStayConnectedSwitch(entry),
            PergolaWindProtectionSwitch(entry),
            PergolaWinterSunSwitch(entry),
        ]
    )


class PergolaStayConnectedSwitch(PergolaEntity, SwitchEntity):
    """Toggles whether the shared BLE connection stays open indefinitely."""

    _attr_name = "Stay connected"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize the switch."""
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_stay_connected"
        self._attr_is_on = self._connection.stay_connected

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Keep the BLE connection open indefinitely."""
        self._connection.async_set_stay_connected(True)
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Go back to connect-on-demand with an idle timeout."""
        self._connection.async_set_stay_connected(False)
        self._attr_is_on = False
        self.async_write_ha_state()


class PergolaWindProtectionSwitch(PergolaEntity, SwitchEntity):
    """Closes the pergola on high wind. Always available, independent of
    winter sun mode - replaces the device's own wind sensor, which was
    disconnected because its stock behavior (open on wind) is wrong for this
    slat geometry."""

    _attr_name = "Wind protection"

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize the switch."""
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_wind_protection"

    @property
    def is_on(self) -> bool:
        return self._wind_protection.enabled

    async def async_added_to_hass(self) -> None:
        self._wind_protection.add_listener(self._handle_update)

    async def async_will_remove_from_hass(self) -> None:
        self._wind_protection.remove_listener(self._handle_update)

    def _handle_update(self) -> None:
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._wind_protection.async_enable()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._wind_protection.async_disable()


class PergolaWinterSunSwitch(PergolaEntity, SwitchEntity, RestoreEntity):
    """Winter sun mode: nudges the slats closed through the day to track the
    sun, starting from whenever the cover was last opened by hand. Opt-in,
    seasonal - but restores whatever it was last set to across a HA restart
    (2026-10-09 fix) rather than always coming back off. It never turns
    itself on by force; it only resumes being on if it already was."""

    _attr_name = "Winter sun mode"

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize the switch."""
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_winter_sun_mode"

    @property
    def is_on(self) -> bool:
        return self._winter_sun.enabled

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._winter_sun.add_listener(self._handle_update)
        if not self._winter_sun.enabled:
            last_state = await self.async_get_last_state()
            if last_state is not None and last_state.state == "on":
                await self._winter_sun.async_enable()

    async def async_will_remove_from_hass(self) -> None:
        self._winter_sun.remove_listener(self._handle_update)

    def _handle_update(self) -> None:
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._winter_sun.async_enable()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._winter_sun.async_disable()
