"""Switch platform for Pergola - toggles staying connected."""

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import PergolaEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the pergola stay-connected switch."""
    async_add_entities([PergolaStayConnectedSwitch(entry)])


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
