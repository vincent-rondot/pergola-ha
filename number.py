"""Number platform for Pergola - adjusts the idle-disconnect timeout."""

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, PERCENTAGE, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import PergolaEntity
from .weather_guard import THRESHOLD_UNIT


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the pergola number entities."""
    async_add_entities(
        [
            PergolaIdleTimeoutNumber(entry),
            PergolaFullTravelTimeNumber(entry),
            PergolaPulseStartDelayNumber(entry),
            PergolaWindThresholdNumber(entry),
            PergolaPositionDeadbandNumber(entry),
            PergolaEstimatedOpenPercentageNumber(entry),
        ]
    )


class PergolaIdleTimeoutNumber(PergolaEntity, NumberEntity):
    """How long to keep the BLE connection open after the last command,
    when not staying connected."""

    _attr_name = "Idle timeout"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 5
    _attr_native_max_value = 600
    _attr_native_step = 5
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_mode = NumberMode.BOX

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


class PergolaFullTravelTimeNumber(PergolaEntity, NumberEntity):
    """How long a full Open-to-Close (or Close-to-Open) run takes on the real
    hardware. Time this once with a stopwatch and set it here - winter sun
    mode uses it to size its Close pulses, since the device has no real
    position feedback to calibrate against."""

    _attr_name = "Full travel time"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 5
    _attr_native_max_value = 120
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_mode = NumberMode.BOX

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize the number entity."""
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_full_travel_time"

    @property
    def native_value(self) -> float:
        return self._winter_sun.full_travel_time

    async def async_set_native_value(self, value: float) -> None:
        self._winter_sun.full_travel_time = value
        self.async_write_ha_state()


class PergolaPulseStartDelayNumber(PergolaEntity, NumberEntity):
    """Fixed overhead added to every winter sun Close pulse, on top of the
    proportional delta*full_travel_time duration, to account for however
    long the motor/BLE link takes to actually start moving after the
    command is sent. Every pulse pays this cost regardless of its size, so
    small pulses are disproportionately shortchanged if it's left at 0 -
    this is believed to be why winter sun mode's real slats drifted more
    open than the modelled position through the afternoon of 2026-10-10.

    Default (0.4s, doubled from 0.2s on 2026-10-12 - still not enough) is an
    arbitrary placeholder, not yet measured. To
    calibrate it properly: from fully open, trigger a continuous Close (not
    a short pulse) while filming or timing it, and note separately (a) the
    time from sending the command to the first visible slat movement - that
    is this value - and (b) the time from that first movement to the slats
    stopping - set that as "Full travel time" instead."""

    _attr_name = "Pulse start delay"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 0
    _attr_native_max_value = 10
    _attr_native_step = 0.1
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_mode = NumberMode.BOX

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize the number entity."""
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_pulse_start_delay"

    @property
    def native_value(self) -> float:
        return self._winter_sun.pulse_start_delay

    async def async_set_native_value(self, value: float) -> None:
        self._winter_sun.pulse_start_delay = value
        self.async_write_ha_state()


class PergolaWindThresholdNumber(PergolaEntity, NumberEntity):
    """Wind speed at or above which wind protection (and winter sun mode's
    pause) kicks in, in THRESHOLD_UNIT (km/h) - the same unit `sensor.
    pergola_wind_speed` shows, deliberately, so the two numbers you'd
    actually want to compare side by side are never in different units.
    Whatever unit the configured weather entity itself reports in is
    converted before comparing - see weather_guard.is_windy()."""

    _attr_name = "Wind speed threshold"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 0
    _attr_native_max_value = 150
    _attr_native_step = 1
    _attr_native_unit_of_measurement = THRESHOLD_UNIT
    _attr_mode = NumberMode.BOX

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize the number entity."""
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_wind_speed_threshold"

    @property
    def native_value(self) -> float:
        return self._wind_protection.wind_speed_threshold

    async def async_set_native_value(self, value: float) -> None:
        self._wind_protection.wind_speed_threshold = value
        self.async_write_ha_state()


class PergolaPositionDeadbandNumber(PergolaEntity, NumberEntity):
    """Minimum cumulative sun-position movement, in %, before winter sun
    mode bothers with a Close pulse. Lower this for testing (e.g. against
    the simulated device) so pulses trigger without waiting for a large
    azimuth change."""

    _attr_name = "Winter sun deadband"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 0
    _attr_native_max_value = 50
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.BOX

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize the number entity."""
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_winter_sun_deadband"

    @property
    def native_value(self) -> float:
        return self._winter_sun.position_deadband * 100

    async def async_set_native_value(self, value: float) -> None:
        self._winter_sun.position_deadband = value / 100
        self.async_write_ha_state()


class PergolaEstimatedOpenPercentageNumber(PergolaEntity, NumberEntity):
    """Winter sun mode's internal guess at how open the pergola is (0-100%) -
    editable so you can recalibrate it by hand.

    This is NOT a real measurement - the device has no position feedback at
    all. It's an open-loop estimate: it resets to 100% whenever the cover is
    opened through HA, and only ever decreases from there as winter sun mode
    sends timed Close pulses, calculated from the "Full travel time" number
    entity. It drifts silently out of sync with reality whenever the cover is
    moved by anything this integration doesn't know about (the physical
    remote, the vendor app). Set this value directly - e.g. after actually
    looking at the real pergola - to correct what's *displayed*.

    Setting it does NOT change what winter sun mode does next: a hand
    estimate is not the hard fact that "the motor just ran to its physical
    Open/Close end-stop" is, so - deliberately - it can never feed the
    pulse-sizing math, only correct the display. Don't build automations
    against it as a source of truth; it exists for visibility and manual
    correction, not as a sensor to react to.
    """

    _attr_name = "Estimated open percentage"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE
    # A slider (the default) doesn't show the actual figure - this is read
    # far more often than it's set, so a numeric box showing the value
    # plainly matters more here than slider convenience.
    _attr_mode = NumberMode.BOX

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize the number entity."""
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_estimated_open_percentage"

    @property
    def native_value(self) -> int:
        return self._winter_sun.estimated_open_percentage

    async def async_set_native_value(self, value: float) -> None:
        self._winter_sun.recalibrate(value)
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        self._winter_sun.add_listener(self._handle_update)

    async def async_will_remove_from_hass(self) -> None:
        self._winter_sun.remove_listener(self._handle_update)

    def _handle_update(self) -> None:
        self.async_write_ha_state()
