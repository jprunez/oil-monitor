"""Integration tests: learning from recorded long-term statistics."""

from datetime import timedelta

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from homeassistant.components.recorder import models as recorder_models
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.oil_predict.const import DOMAIN

DAYS = 120


def _metadata(statistic_id: str, unit: str, *, mean: bool) -> dict:
    meta = {
        "has_sum": not mean,
        "name": None,
        "source": "recorder",
        "statistic_id": statistic_id,
        "unit_of_measurement": unit,
    }
    if "unit_class" in getattr(recorder_models.StatisticMetaData, "__annotations__", {}):
        # Newer Home Assistant versions require the unit's class.
        meta["unit_class"] = {"cm": "distance", "°C": "temperature", "h": "duration"}[unit]
    mean_type = getattr(recorder_models, "StatisticMeanType", None)
    if mean_type is not None:  # Home Assistant 2025.4+
        meta["mean_type"] = mean_type.ARITHMETIC if mean else mean_type.NONE
    else:
        meta["has_mean"] = mean
    return meta


def _hours():
    start = (dt_util.utcnow() - timedelta(days=DAYS)).replace(minute=0, second=0, microsecond=0)
    return [start + timedelta(hours=hour) for hour in range(DAYS * 24)]


async def _record_history(hass: HomeAssistant, *, runtime_as_total: bool) -> None:
    """Record ~4 months of hourly statistics: steady use, cold weather."""
    hours = _hours()
    first = hours[0]

    def series(value_at):
        rows = []
        for hour in hours:
            value = value_at((hour - first).total_seconds() / 86400)
            rows.append({"start": hour, "mean": value, "min": value, "max": value})
        return rows

    # Distance to the oil grows 0.4 cm/day as oil is used.
    async_import_statistics(
        hass, _metadata("sensor.tank_depth", "cm", mean=True), series(lambda d: 20 + 0.4 * d)
    )
    async_import_statistics(
        hass, _metadata("sensor.outdoor", "°C", mean=True), series(lambda d: 4 + 4 * ((d // 7) % 3))
    )
    async_import_statistics(
        hass, _metadata("sensor.indoor", "°C", mean=True), series(lambda d: 19.5)
    )
    if runtime_as_total:
        # A total_increasing runtime counter: 4 hours per day, never resetting.
        rows = []
        total = 0.0
        for hour in hours:
            total += 4 / 24
            rows.append({"start": hour, "state": total, "sum": total})
        async_import_statistics(hass, _metadata("sensor.runtime", "h", mean=False), rows)
    else:
        # A daily runtime that climbs through the day and resets at midnight.
        async_import_statistics(
            hass,
            _metadata("sensor.runtime", "h", mean=True),
            series(lambda d: 4 * (d % 1)),
        )
    await async_wait_recording_done(hass)


@pytest.mark.parametrize("runtime_as_total", [False, True])
async def test_learns_from_recorded_history(hass: HomeAssistant, runtime_as_total: bool) -> None:
    """Months of recorded statistics produce clean training weeks."""
    await _record_history(hass, runtime_as_total=runtime_as_total)
    hass.states.async_set("sensor.tank_depth", "68", {"unit_of_measurement": "cm"})
    hass.states.async_set("sensor.outdoor", "6", {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.indoor", "19.5", {"unit_of_measurement": "°C"})
    hass.states.async_set("climate.home", "heat", {"temperature": 19})

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id=DOMAIN,
        data={
            "depth_entity": "sensor.tank_depth",
            "level_type": "distance",
            "tank_capacity": 1000,
            "tank_shape": "custom",
            "full_distance_cm": 9,
            "empty_distance_cm": 126,
            "calibration": "9:100, 17:90, 30:80, 41:70, 55:60, 66:50, 78:40, 90:30, 101:20, 113:10, 126:0",
            "outdoor_entity": "sensor.outdoor",
            "indoor_entity": "sensor.indoor",
            "indoor_target_entity": "climate.home",
            "weather_entity": "weather.home",
            "runtime_entity": "sensor.runtime",
            "stale_after_minutes": 90,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    stale_id = er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, f"{entry.entry_id}_tank_sensor_stale"
    )
    attributes = hass.states.get(stale_id).attributes
    assert attributes["training_weeks"] >= 12, attributes


async def test_names_the_sensor_without_history(hass: HomeAssistant) -> None:
    """When a sensor has no statistics, the warning says which one."""
    await _record_history(hass, runtime_as_total=False)
    hass.states.async_set("sensor.tank_depth", "68", {"unit_of_measurement": "cm"})
    hass.states.async_set("climate.home", "heat", {"temperature": 19})
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id=DOMAIN,
        data={
            "depth_entity": "sensor.tank_depth",
            "level_type": "distance",
            "tank_capacity": 1000,
            "tank_shape": "vertical",
            "full_distance_cm": 10,
            "empty_distance_cm": 130,
            "outdoor_entity": "sensor.outdoor",
            "indoor_entity": "sensor.indoor",
            "indoor_target_entity": "climate.home",
            "weather_entity": "weather.home",
            "runtime_entity": "sensor.runtime_without_statistics",
            "stale_after_minutes": 90,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    stale_id = er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, f"{entry.entry_id}_tank_sensor_stale"
    )
    attributes = hass.states.get(stale_id).attributes
    assert attributes["training_weeks"] == 0
    assert attributes["history_days"]["runtime"] == 0
    assert attributes["history_days"]["level"] >= DAYS - 1
    assert "heating runtime (sensor.runtime_without_statistics)" in attributes["forecast_warning"]
