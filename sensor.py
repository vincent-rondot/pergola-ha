"""Sensor platform for Pergola - wind protection diagnostics."""

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from .entity import PergolaEntity
from .sun_tracker import MAX_SLAT_ANGLE, target_position
from .weather_guard import THRESHOLD_UNIT

ANGLE_UNIT = "°"


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
            PergolaSunAzimuthSensor(entry),
            PergolaSunElevationSensor(entry),
            PergolaTargetSlatAngleSensor(entry),
            PergolaTargetSlatInclinationSensor(entry),
            PergolaEstimatedSlatAngleSensor(entry),
            PergolaEstimatedSlatInclinationSensor(entry),
        ]
    )


class _SunTrackingDiagnosticSensor(PergolaEntity, SensorEntity):
    """Shared plumbing for the three sun-tracking diagnostic sensors below:
    all three are pure functions of sun.sun's current azimuth/elevation
    attributes, so they all just need to re-render on every sun.sun state
    change rather than poll (2026-10-17 - added so Vincent can compare the
    winter sun model's live target angle against the real slats by eye,
    after noticing them looking off)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = ANGLE_UNIT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_track_state_change_event(self.hass, "sun.sun", self._handle_update)
        )

    @callback
    def _handle_update(self, event: Event[EventStateChangedData]) -> None:
        self.async_write_ha_state()


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


class PergolaSunAzimuthSensor(_SunTrackingDiagnosticSensor):
    """Sun's current compass azimuth, mirrored from sun.sun for convenience
    so it sits on the same device/history graph as Target slat angle,
    making it easy to read "what the model saw" at any point in time
    without cross-referencing sun.sun separately."""

    _attr_name = "Sun azimuth"

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_sun_azimuth"

    @property
    def native_value(self) -> float | None:
        sun_state = self.hass.states.get("sun.sun")
        if sun_state is None:
            return None
        return sun_state.attributes.get("azimuth")


class PergolaSunElevationSensor(_SunTrackingDiagnosticSensor):
    """Sun's current elevation above the horizon, mirrored from sun.sun -
    see PergolaSunAzimuthSensor."""

    _attr_name = "Sun elevation"

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_sun_elevation"

    @property
    def native_value(self) -> float | None:
        sun_state = self.hass.states.get("sun.sun")
        if sun_state is None:
            return None
        return sun_state.attributes.get("elevation")


class PergolaTargetSlatAngleSensor(_SunTrackingDiagnosticSensor):
    """What the winter sun model currently says the slats *should* be
    angled at (0° = closed/flat, 140° = fully open - see
    sun_tracker.MAX_SLAT_ANGLE), recomputed live from sun.sun's current
    azimuth/elevation.

    NOT the same thing as what was actually last commanded to the real
    device: WinterSunController only acts (and updates its own
    `_last_check_target` baseline) on its periodic check, rate-limited by
    the deadband - this sensor ignores all of that and always shows the
    live, instantaneous target, precisely so it's comparable against a
    protractor held up to the real slats at any moment, independent of
    when the last pulse happened. Floored at the same
    sun_tracker.MIN_POSITION the real pulses are (56° = 0.4 * 140°) - see
    MIN_POSITION's docstring for why the model doesn't chase shallower
    angles than that. Reads 140° (fully open) through the whole morning
    side the one-directional mechanism can't usefully track at all - see
    sun_tracker.profile_angle()'s docstring."""

    _attr_name = "Target slat angle"

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_target_slat_angle"

    @property
    def native_value(self) -> float | None:
        sun_state = self.hass.states.get("sun.sun")
        if sun_state is None:
            return None
        azimuth = sun_state.attributes.get("azimuth")
        elevation = sun_state.attributes.get("elevation")
        if azimuth is None or elevation is None:
            return None
        return round(target_position(azimuth, elevation) * MAX_SLAT_ANGLE, 1)


def _slat_inclination(angle: float) -> float:
    """Re-expresses a 0°-MAX_SLAT_ANGLE model angle (0° = closed-flat,
    measured one-directional) as the real physical inclination of the
    slat from horizontal (always 0-90°, direction-agnostic) - what a
    level/protractor held up to the actual slat reads, directly
    comparable without any mental math. A flat slat has no front/back, so
    past 90°/vertical the model angle keeps climbing while the physical
    tilt folds back down: 90 - |angle - 90| (e.g. 100° and 80° model
    angles both read as an 80° physical tilt, just leaning the other way
    past vertical)."""
    return round(90 - abs(angle - 90), 1)


class PergolaTargetSlatInclinationSensor(_SunTrackingDiagnosticSensor):
    """`Target slat angle` re-expressed as real physical tilt from
    horizontal (see `_slat_inclination`) - directly comparable to a level/
    protractor reading taken on the actual slats, without doing the fold-
    back math by hand. Added 2026-10-18 for Vincent's slat-angle
    calibration checks."""

    _attr_name = "Target slat inclination"

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_target_slat_inclination"

    @property
    def native_value(self) -> float | None:
        sun_state = self.hass.states.get("sun.sun")
        if sun_state is None:
            return None
        azimuth = sun_state.attributes.get("azimuth")
        elevation = sun_state.attributes.get("elevation")
        if azimuth is None or elevation is None:
            return None
        angle = target_position(azimuth, elevation) * MAX_SLAT_ANGLE
        return _slat_inclination(angle)


class PergolaEstimatedSlatAngleSensor(PergolaEntity, SensorEntity):
    """`number.pergola_estimated_open_percentage` (winter sun mode's
    open-loop guess, NOT a real measurement - see that entity's own
    docstring) re-expressed on the same 0°-MAX_SLAT_ANGLE scale as
    `Target slat angle`, so the two sit side by side without doing the
    percent->angle conversion by hand. Added 2026-10-18."""

    _attr_name = "Estimated slat angle"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = ANGLE_UNIT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_estimated_slat_angle"

    @property
    def native_value(self) -> float:
        return round(
            self._winter_sun.estimated_open_percentage / 100 * MAX_SLAT_ANGLE, 1
        )

    async def async_added_to_hass(self) -> None:
        self._winter_sun.add_listener(self._handle_update)

    async def async_will_remove_from_hass(self) -> None:
        self._winter_sun.remove_listener(self._handle_update)

    def _handle_update(self) -> None:
        self.async_write_ha_state()


class PergolaEstimatedSlatInclinationSensor(PergolaEntity, SensorEntity):
    """`Estimated slat angle` re-expressed as real physical tilt from
    horizontal (see `_slat_inclination`) - directly comparable to a level/
    protractor reading. Added 2026-10-18."""

    _attr_name = "Estimated slat inclination"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = ANGLE_UNIT
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_estimated_slat_inclination"

    @property
    def native_value(self) -> float:
        angle = self._winter_sun.estimated_open_percentage / 100 * MAX_SLAT_ANGLE
        return _slat_inclination(angle)

    async def async_added_to_hass(self) -> None:
        self._winter_sun.add_listener(self._handle_update)

    async def async_will_remove_from_hass(self) -> None:
        self._winter_sun.remove_listener(self._handle_update)

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
