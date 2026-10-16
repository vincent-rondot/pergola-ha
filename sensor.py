"""Sensor platform for Pergola - wind protection diagnostics."""

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import PergolaEntity
from .weather_guard import THRESHOLD_UNIT


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the pergola diagnostic sensors."""
    async_add_entities(
        [
            PergolaWindSpeedSensor(entry),
            PergolaWindProtectionLastTriggeredSensor(entry),
            PergolaBladesStateSensor(entry),
        ]
    )


class PergolaBladesStateSensor(PergolaEntity, SensorEntity):
    """Raw blades state as last reported by the card itself
    (pergola_controller 0.2.1+): opening/closing/stopped/open/closed.
    Diagnostic/debug only - the cover entity's own is_opening/is_closing/
    is_closed already expose this properly for automations and the normal
    UI; this is for seeing the raw value directly when troubleshooting."""

    _attr_name = "Blades state"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["opening", "closing", "stopped", "open", "closed"]

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_blades_state"

    @property
    def native_value(self) -> str | None:
        status = self._connection.status
        return status.blades if status is not None else None

    async def async_added_to_hass(self) -> None:
        self._connection.add_status_listener(self._handle_update)

    async def async_will_remove_from_hass(self) -> None:
        self._connection.remove_status_listener(self._handle_update)

    def _handle_update(self) -> None:
        self.async_write_ha_state()


class PergolaWindSpeedSensor(PergolaEntity, SensorEntity):
    """Last wind speed reading wind protection acted on, already converted
    to the same unit as the Wind speed threshold number entity - so this is
    what to compare against the threshold when something looks off."""

    _attr_name = "Wind speed"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.WIND_SPEED
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = THRESHOLD_UNIT

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_wind_speed"

    @property
    def native_value(self) -> float | None:
        return self._wind_protection.last_wind_speed

    async def async_added_to_hass(self) -> None:
        self._wind_protection.add_listener(self._handle_update)

    async def async_will_remove_from_hass(self) -> None:
        self._wind_protection.remove_listener(self._handle_update)

    def _handle_update(self) -> None:
        self.async_write_ha_state()


class PergolaWindProtectionLastTriggeredSensor(PergolaEntity, SensorEntity):
    """When wind protection last closed the cover for wind."""

    _attr_name = "Wind protection last triggered"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_wind_protection_last_triggered"

    @property
    def native_value(self):
        return self._wind_protection.last_triggered

    async def async_added_to_hass(self) -> None:
        self._wind_protection.add_listener(self._handle_update)

    async def async_will_remove_from_hass(self) -> None:
        self._wind_protection.remove_listener(self._handle_update)

    def _handle_update(self) -> None:
        self.async_write_ha_state()
