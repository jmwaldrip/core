"""Data update coordinator for the Proximity integration."""

from dataclasses import dataclass
import logging

from homeassistant.const import (
    ATTR_LATITUDE,
    ATTR_LONGITUDE,
    ATTR_NAME,
    CONF_DEVICES,
    CONF_UNIT_OF_MEASUREMENT,
    CONF_ZONE,
    UnitOfLength,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util.location import distance
from homeassistant.util.unit_conversion import DistanceConverter

from .const import (
    ATTR_DIR_OF_TRAVEL,
    ATTR_DIST_TO,
    ATTR_IN_IGNORED_ZONE,
    ATTR_NEAREST,
    CONF_IGNORED_ZONES,
    CONF_TOLERANCE,
    DEFAULT_DIR_OF_TRAVEL,
    DEFAULT_DIST_TO_ZONE,
    DEFAULT_NEAREST,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class StateChangedData:
    """StateChangedData class."""

    entity_id: str
    old_state: State | None
    new_state: State | None


@dataclass
class ProximityData:
    """ProximityCoordinatorData class."""

    proximity: dict[str, str | float]
    entities: dict[str, dict[str, str | int | None]]


DEFAULT_DATA = ProximityData(
    {
        ATTR_DIST_TO: DEFAULT_DIST_TO_ZONE,
        ATTR_DIR_OF_TRAVEL: DEFAULT_DIR_OF_TRAVEL,
        ATTR_NEAREST: DEFAULT_NEAREST,
    },
    {},
)


class ProximityDataUpdateCoordinator(DataUpdateCoordinator[ProximityData]):
    """Proximity data update coordinator."""

    def __init__(
        self, hass: HomeAssistant, friendly_name: str, config: ConfigType
    ) -> None:
        """Initialize the Proximity coordinator."""
        self.ignored_zones: list[str] = config[CONF_IGNORED_ZONES]
        self.tracked_entities: list[str] = config[CONF_DEVICES]
        self.tolerance: int = config[CONF_TOLERANCE]
        self.proximity_zone: str = config[CONF_ZONE]
        self.unit_of_measurement: str = config.get(
            CONF_UNIT_OF_MEASUREMENT, hass.config.units.length_unit
        )
        self.friendly_name = friendly_name

        super().__init__(
            hass,
            _LOGGER,
            name=friendly_name,
            update_interval=None,
        )

        self.data = DEFAULT_DATA

        self.state_change_data: StateChangedData | None = None

    async def async_check_proximity_state_change(
        self, entity: str, old_state: State | None, new_state: State | None
    ) -> None:
        """Fetch and process state change event."""
        self.state_change_data = StateChangedData(entity, old_state, new_state)
        await self.async_refresh()

    def _convert(self, value: float | str) -> float | str:
        """Round and convert given distance value."""
        if isinstance(value, str):
            return value
        return round(
            DistanceConverter.convert(
                value,
                UnitOfLength.METERS,
                self.unit_of_measurement,
            )
        )

    def _calc_distance_to_zone(
        self,
        zone: State,
        device: State,
        latitude: float | None,
        longitude: float | None,
    ) -> int | None:
        if device.state.lower() == self.proximity_zone.lower():
            _LOGGER.debug(
                "%s: %s in zone -> distance=0",
                self.friendly_name,
                device.entity_id,
            )
            return 0

        if latitude is None or longitude is None:
            _LOGGER.debug(
                "%s: %s has no coordinates -> distance=None",
                self.friendly_name,
                device.entity_id,
            )
            return None

        distance_to_zone = distance(
            zone.attributes[ATTR_LATITUDE],
            zone.attributes[ATTR_LONGITUDE],
            latitude,
            longitude,
        )

        # it is ensured, that distance can't be None, since zones must have lat/lon coordinates
        assert distance_to_zone is not None
        return round(distance_to_zone)

    def _calc_direction_of_travel(
        self,
        zone: State,
        device: State,
        old_latitude: float | None,
        old_longitude: float | None,
        new_latitude: float | None,
        new_longitude: float | None,
    ) -> str | None:
        if device.state.lower() == self.proximity_zone.lower():
            _LOGGER.debug(
                "%s: %s in zone -> direction_of_travel=arrived",
                self.friendly_name,
                device.entity_id,
            )
            return "arrived"

        if (
            old_latitude is None
            or old_longitude is None
            or new_latitude is None
            or new_longitude is None
        ):
            return None

        old_distance = distance(
            zone.attributes[ATTR_LATITUDE],
            zone.attributes[ATTR_LONGITUDE],
            old_latitude,
            old_longitude,
        )
        new_distance = distance(
            zone.attributes[ATTR_LATITUDE],
            zone.attributes[ATTR_LONGITUDE],
            new_latitude,
            new_longitude,
        )

        # it is ensured, that distance can't be None, since zones must have lat/lon coordinates
        assert old_distance is not None
        assert new_distance is not None
        distance_travelled = round(new_distance - old_distance, 1)

        if distance_travelled < self.tolerance * -1:
            return "towards"

        if distance_travelled > self.tolerance:
            return "away_from"

        return "stationary"

    async def _async_update_data(self) -> ProximityData:
        """Calculate Proximity data."""
        if (zone_state := self.hass.states.get(f"zone.{self.proximity_zone}")) is None:
            _LOGGER.debug(
                "%s: zone %s does not exist -> reset",
                self.friendly_name,
                self.proximity_zone,
            )
            return DEFAULT_DATA

        entities_data = self.data.entities

        # calculate distance for all tracked entities
        for entity_id in self.tracked_entities:
            if (tracked_entity_state := self.hass.states.get(entity_id)) is None:
                if entities_data.pop(entity_id, None) is not None:
                    _LOGGER.debug(
                        "%s: %s does not exist -> remove", self.friendly_name, entity_id
                    )
                continue

            if entity_id not in entities_data:
                _LOGGER.debug("%s: %s is new -> add", self.friendly_name, entity_id)
                entities_data[entity_id] = {
                    ATTR_DIST_TO: None,
                    ATTR_DIR_OF_TRAVEL: None,
                    ATTR_NAME: tracked_entity_state.name,
                    ATTR_IN_IGNORED_ZONE: False,
                }
            entities_data[entity_id][ATTR_IN_IGNORED_ZONE] = (
                tracked_entity_state.state.lower() in self.ignored_zones
            )
            entities_data[entity_id][ATTR_DIST_TO] = self._calc_distance_to_zone(
                zone_state,
                tracked_entity_state,
                tracked_entity_state.attributes.get(ATTR_LATITUDE),
                tracked_entity_state.attributes.get(ATTR_LONGITUDE),
            )
            if entities_data[entity_id][ATTR_DIST_TO] is None:
                _LOGGER.debug(
                    "%s: %s has unknown distance got -> direction_of_travel=None",
                    self.friendly_name,
                    entity_id,
                )
                entities_data[entity_id][ATTR_DIR_OF_TRAVEL] = None

        # calculate direction of travel only for last updated tracked entity
        if (state_change_data := self.state_change_data) is not None and (
            new_state := state_change_data.new_state
        ) is not None:
            _LOGGER.debug(
                "%s: calculate direction of travel for %s",
                self.friendly_name,
                state_change_data.entity_id,
            )

            if (old_state := state_change_data.old_state) is not None:
                old_lat = old_state.attributes.get(ATTR_LATITUDE)
                old_lon = old_state.attributes.get(ATTR_LONGITUDE)
            else:
                old_lat = None
                old_lon = None

            entities_data[state_change_data.entity_id][
                ATTR_DIR_OF_TRAVEL
            ] = self._calc_direction_of_travel(
                zone_state,
                new_state,
                old_lat,
                old_lon,
                new_state.attributes.get(ATTR_LATITUDE),
                new_state.attributes.get(ATTR_LONGITUDE),
            )

        # takeover data for legacy proximity entity
        proximity_data: dict[str, str | float] = {
            ATTR_DIST_TO: DEFAULT_DIST_TO_ZONE,
            ATTR_DIR_OF_TRAVEL: DEFAULT_DIR_OF_TRAVEL,
            ATTR_NEAREST: DEFAULT_NEAREST,
        }
        for entity_data in entities_data.values():
            if (distance_to := entity_data[ATTR_DIST_TO]) is None or entity_data[
                ATTR_IN_IGNORED_ZONE
            ]:
                continue

            if isinstance((nearest_distance_to := proximity_data[ATTR_DIST_TO]), str):
                _LOGGER.debug("set first entity_data: %s", entity_data)
                proximity_data = {
                    ATTR_DIST_TO: distance_to,
                    ATTR_DIR_OF_TRAVEL: entity_data[ATTR_DIR_OF_TRAVEL] or "unknown",
                    ATTR_NEAREST: str(entity_data[ATTR_NAME]),
                }
                continue

            if float(nearest_distance_to) > float(distance_to):
                _LOGGER.debug("set closer entity_data: %s", entity_data)
                proximity_data = {
                    ATTR_DIST_TO: distance_to,
                    ATTR_DIR_OF_TRAVEL: entity_data[ATTR_DIR_OF_TRAVEL] or "unknown",
                    ATTR_NEAREST: str(entity_data[ATTR_NAME]),
                }
                continue

            if float(nearest_distance_to) == float(distance_to):
                _LOGGER.debug("set equally close entity_data: %s", entity_data)
                proximity_data[
                    ATTR_NEAREST
                ] = f"{proximity_data[ATTR_NEAREST]}, {str(entity_data[ATTR_NAME])}"

        proximity_data[ATTR_DIST_TO] = self._convert(proximity_data[ATTR_DIST_TO])

        return ProximityData(proximity_data, entities_data)
