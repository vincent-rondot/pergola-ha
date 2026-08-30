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
    """Set up the pergola binary sensors."""
    async_add_entities(
        [PergolaConnectedSensor(entry), PergolaWindProtectionActiveSensor(entry)]
    )


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


class PergolaWindProtectionActiveSensor(PergolaEntity, BinarySensorEntity):
    """On while wind protection currently considers it too windy (and is
    therefore keeping the cover closed). Unlike the "last triggered"
    timestamp sensor, this only flips once per windy episode - not once per
    15-minute check - so it's the one to trigger a single notification on,
    not a repeated one for as long as it stays windy."""

    _attr_name = "Wind protection active"
    _attr_device_class = BinarySensorDeviceClass.SAFETY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_wind_protection_active"

    @property
    def is_on(self) -> bool:
        return self._wind_protection.active

    async def async_added_to_hass(self) -> None:
        self._wind_protection.add_listener(self._handle_update)

    async def async_will_remove_from_hass(self) -> None:
        self._wind_protection.remove_listener(self._handle_update)

    def _handle_update(self) -> None:
        self.async_write_ha_state()
