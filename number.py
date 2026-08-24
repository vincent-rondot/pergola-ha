"""Number platform for Pergola - adjusts the idle-disconnect timeout."""

from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import PergolaEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the pergola idle timeout number."""
    async_add_entities([PergolaIdleTimeoutNumber(entry)])


class PergolaIdleTimeoutNumber(PergolaEntity, NumberEntity):
    """How long to keep the BLE connection open after the last command,
    when not staying connected."""

    _attr_name = "Idle timeout"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 5
    _attr_native_max_value = 600
    _attr_native_step = 5
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize the number entity."""
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_idle_timeout"

    @property
    def native_value(self) -> float:
        return self._connection.idle_timeout

    async def async_set_native_value(self, value: float) -> None:
        self._connection.idle_timeout = value
        self.async_write_ha_state()
