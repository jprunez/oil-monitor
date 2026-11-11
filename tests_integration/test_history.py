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


async def _record_history(
    hass: HomeAssistant,
    *,
    runtime_as_total: bool,
    indoor: bool = True,
    weather_driven: bool = False,
) -> None:
    """Record ~4 months of hourly statistics: steady use, cold weather."""
    hours = _hours()
    first = hours[0]

    def series(value_at):
        rows = []
        for hour in hours:
            value = value_at((hour - first).total_seconds() / 86400)
            rows.append({"start": hour, "mean": value, "min": value, "max": value})
        return rows

    def outdoor_at(d: float) -> float:
        return 4 + 4 * ((d // 7) % 3)

    def hours_at(d: float) -> float:
        # Heating hours per day: fixed, or more in colder weeks.
        return 0.4 * (19.5 - outdoor_at(d)) if weather_driven else 4.0

    # Distance to the oil grows about 0.1 cm per heating hour as oil is used.
    depth_rows = []
    depth = 20.0
    for hour in hours:
        d = (hour - first).total_seconds() / 86400
        depth += 0.1 * hours_at(d) / 24
        depth_rows.append({"start": hour, "mean": depth, "min": depth, "max": depth})
    async_import_statistics(hass, _metadata("sensor.tank_depth", "cm", mean=True), depth_rows)
    async_import_statistics(
        hass, _metadata("sensor.outdoor", "°C", mean=True), series(outdoor_at)
    )
    if indoor:
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
            series(lambda d: hours_at(d) * (d % 1)),
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



async def test_thermostat_chosen_as_indoor_sensor(hass: HomeAssistant) -> None:
    """An indoor entity without history still gives estimates, and is flagged."""
    from custom_components.oil_predict.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    await _record_history(hass, runtime_as_total=False, indoor=False)
    hass.states.async_set("sensor.tank_depth", "68", {"unit_of_measurement": "cm", "state_class": "measurement"})
    hass.states.async_set("sensor.outdoor", "6", {"unit_of_measurement": "°C", "state_class": "measurement"})
    hass.states.async_set("sensor.runtime", "2", {"unit_of_measurement": "h", "state_class": "measurement"})
    # A thermostat-like sensor: a temperature, but no state class.
    hass.states.async_set("sensor.thermostat", "19.1", {"unit_of_measurement": "°C"})
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
            "indoor_entity": "sensor.thermostat",
            "indoor_target_entity": "climate.home",
            "weather_entity": "weather.missing",
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
    # History is usable thanks to the thermostat-setting fallback.
    assert attributes["training_weeks"] >= 12
    checks = {row["role"]: row for row in attributes["setup_check"]}
    assert checks["indoor"]["status"] == "warning"
    assert "no state class" in checks["indoor"]["message"]
    assert "19.0 °C indoors" in checks["indoor"]["message"]
    assert checks["level"]["status"] == "ok"
    assert checks["level"]["history_days"] >= DAYS - 1
    # The weather entity doesn't exist: flagged as a problem.
    assert checks["weather"]["status"] == "problem"

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["settings"]["indoor_entity"] == "sensor.thermostat"
    assert diagnostics["history"]["indoor_history_fallback_c"] == 19
    assert diagnostics["model"]["sample_count"] >= 12
    assert diagnostics["sources"]["weather_entity"] == {"entity_id": "weather.missing", "found": False}
    assert diagnostics["setup_check"] == attributes["setup_check"]


@pytest.mark.parametrize(("drift_per_c", "expect_active"), [(0.0017, True), (0.0, False)])
async def test_ultrasonic_temperature_correction(
    hass: HomeAssistant, drift_per_c: float, expect_active: bool
) -> None:
    """Drift in the recorded readings switches the correction on; none leaves it off."""
    import math

    from custom_components.oil_predict.model import (
        TankConfig,
        reading_to_litres,
        temperature_corrected_distance,
    )

    hours = _hours()
    first = hours[0]

    def temp_on(day: int) -> float:
        return 8 + 6 * math.sin(day / 5.0) + 3 * math.sin(day * 1.7)

    depth_rows, outdoor_rows, indoor_rows = [], [], []
    true_distance = 30.0
    for hour in hours:
        day = int((hour - first).total_seconds() // 86400)
        temp = temp_on(day)
        true_distance += max(0.0, 0.05 + 0.02 * (18 - temp)) / 3 / 24
        measured = true_distance * (1 - drift_per_c * (temp - 10))
        depth_rows.append({"start": hour, "mean": measured, "min": measured, "max": measured})
        outdoor_rows.append({"start": hour, "mean": temp, "min": temp, "max": temp})
        indoor_rows.append({"start": hour, "mean": 19.5, "min": 19.5, "max": 19.5})
    async_import_statistics(hass, _metadata("sensor.tank_depth", "cm", mean=True), depth_rows)
    async_import_statistics(hass, _metadata("sensor.outdoor", "°C", mean=True), outdoor_rows)
    async_import_statistics(hass, _metadata("sensor.indoor", "°C", mean=True), indoor_rows)
    await async_wait_recording_done(hass)

    hass.states.async_set("sensor.tank_depth", "60", {"unit_of_measurement": "cm", "state_class": "measurement"})
    hass.states.async_set("sensor.outdoor", "-2", {"unit_of_measurement": "°C", "state_class": "measurement"})
    hass.states.async_set("sensor.indoor", "19.5", {"unit_of_measurement": "°C", "state_class": "measurement"})
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
            "stale_after_minutes": 90,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    stale = hass.states.get(
        registry.async_get_entity_id("binary_sensor", DOMAIN, f"{entry.entry_id}_tank_sensor_stale")
    )
    correction = stale.attributes["temp_correction"]
    assert correction["checked"] is True
    assert correction["active"] is expect_active, correction
    rows = {row["role"]: row for row in stale.attributes["setup_check"]}
    assert rows["temp_correction"]["message"].startswith("On." if expect_active else "Not needed.")

    tank = TankConfig(
        capacity_litres=1000, shape="vertical", full_distance_cm=10, empty_distance_cm=130
    )
    reading = 60.0
    if expect_active:
        assert correction["coefficient_per_c"] == pytest.approx(0.0017, abs=0.0004)
        # It is -2 °C now: the raw 60 cm reads long, so the corrected tank is fuller.
        reading = temperature_corrected_distance(60.0, -2.0, correction["reference_c"])
        assert reading < 59.5
    litres = hass.states.get(
        registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_litres_remaining")
    )
    assert float(litres.state) == pytest.approx(reading_to_litres(reading, tank), abs=0.01)



def _register_weather(hass: HomeAssistant) -> None:
    """Provide a weather.get_forecasts service returning seven cold days."""
    from homeassistant.core import SupportsResponse

    async def get_forecasts(call):
        return {
            "weather.home": {
                "forecast": [{"temperature": 7.0, "templow": 1.0} for _ in range(8)]
            }
        }

    hass.services.async_register(
        "weather", "get_forecasts", get_forecasts, supports_response=SupportsResponse.ONLY
    )


@pytest.mark.parametrize("download_works", [True, False])
async def test_winter_scenarios(
    hass: HomeAssistant, hass_storage, no_weather_history_download, download_works: bool
) -> None:
    """Run-out dates are given for cold, typical and mild winters."""
    from datetime import date

    if download_works:
        # 20 years of archive: each year uniformly warmer or colder than 6 °C.
        today = dt_util.now().date()
        archive = {}
        day = date(today.year - 20, 1, 1)
        while day < today - timedelta(days=7):
            archive[day] = 6.0 + ((day.year * 7) % 9 - 4)
            day += timedelta(days=1)
        no_weather_history_download.side_effect = None
        no_weather_history_download.return_value = archive

    await _record_history(hass, runtime_as_total=False, weather_driven=True)
    _register_weather(hass)
    hass.states.async_set("sensor.tank_depth", "60", {"unit_of_measurement": "cm", "state_class": "measurement"})
    hass.states.async_set("sensor.outdoor", "6", {"unit_of_measurement": "°C", "state_class": "measurement"})
    hass.states.async_set("sensor.indoor", "19.5", {"unit_of_measurement": "°C", "state_class": "measurement"})
    hass.states.async_set("sensor.runtime", "2", {"unit_of_measurement": "h", "state_class": "measurement"})
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
            "runtime_entity": "sensor.runtime",
            "history_location": "YO10 5DD",
            "stale_after_minutes": 90,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)

    def state(platform: str, key: str):
        return hass.states.get(
            registry.async_get_entity_id(platform, DOMAIN, f"{entry.entry_id}_{key}")
        )

    stale = state("binary_sensor", "tank_sensor_stale")
    assert stale.attributes["forecast_warning"] is None, stale.attributes
    info = stale.attributes["scenario_info"]
    rows = {row["role"]: row for row in stale.attributes["setup_check"]}
    if download_works:
        assert info["source"] == "weather_history"
        assert info["years"] >= 15
        assert (info["latitude"], info["longitude"]) == (54.0, -1.1)
        assert rows["scenarios"]["status"] == "ok"
        assert "YO10 area" in rows["scenarios"]["message"]
    else:
        assert info["source"] == "last_year_shifted"
        assert rows["scenarios"]["status"] == "warning"
        assert "offline" in rows["scenarios"]["message"]

    empty = state("sensor", "days_until_empty")
    typical = float(empty.state)
    assert empty.attributes["earliest_days"] < typical < empty.attributes["latest_days"]
    assert empty.attributes["earliest_date"] <= empty.attributes["date"] <= empty.attributes["latest_date"]
    point = empty.attributes["projection"][-1]
    assert point["cold"] <= point["litres"] <= point["mild"]
    assert set(point) == {"date", "litres", "mild", "cold"}
