"""Select platform for Pergola - winter sun mode's check interval."""

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import PergolaEntity
from .sun_tracker import CHECK_INTERVAL_CHOICES

# 5 -> "5 min", 60 -> "1 h"
_LABEL_BY_MINUTES = {
    minutes: ("1 h" if minutes == 60 else f"{minutes} min")
    for minutes in CHECK_INTERVAL_CHOICES
}
_MINUTES_BY_LABEL = {label: minutes for minutes, label in _LABEL_BY_MINUTES.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the pergola select entities."""
    async_add_entities([PergolaCheckIntervalSelect(entry)])


class PergolaCheckIntervalSelect(PergolaEntity, SelectEntity):
    """How often winter sun mode re-checks the sun's position and, if
    needed, sends a Close pulse. Always aligned to the clock (e.g. every 15
    min lands on :00/:15/:30/:45), not relative to when the mode was
    enabled."""

    _attr_name = "Winter sun check interval"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(_LABEL_BY_MINUTES.values())

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize the select entity."""
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_winter_sun_check_interval"

    @property
    def current_option(self) -> str:
        return _LABEL_BY_MINUTES[self._winter_sun.check_interval_minutes]

    async def async_select_option(self, option: str) -> None:
        await self._winter_sun.async_set_check_interval(_MINUTES_BY_LABEL[option])
        self.async_write_ha_state()
