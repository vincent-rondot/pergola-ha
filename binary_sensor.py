"""Binary sensor platform for Pergola - shows live BLE connection state."""

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
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
    """Set up the pergola connection status sensor."""
    async_add_entities([PergolaConnectedSensor(entry)])


class PergolaConnectedSensor(PergolaEntity, BinarySensorEntity):
    """Shows whether HA currently holds a BLE connection to the pergola."""

    _attr_name = "Connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_connected"

    @property
    def is_on(self) -> bool:
        return self._connection.is_connected

    async def async_added_to_hass(self) -> None:
        self._connection.add_listener(self._handle_connection_update)

    async def async_will_remove_from_hass(self) -> None:
        self._connection.remove_listener(self._handle_connection_update)

    def _handle_connection_update(self) -> None:
        self.async_write_ha_state()
