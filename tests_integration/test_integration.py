"""Integration tests: setup flow, migration, live values and settings."""

from datetime import timedelta

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.oil_predict.const import DOMAIN

SENSORS = {
    "outdoor_entity": "sensor.outdoor",
    "indoor_entity": "sensor.indoor",
    "indoor_target_entity": "climate.home",
    "weather_entity": "weather.home",
    "stale_after_minutes": 90,
}

V1_DATA = {
    "depth_entity": "sensor.tank_depth",
    "timestamp_entity": "sensor.tank_time",
    "outdoor_entity": "sensor.outdoor",
    "indoor_entity": "sensor.indoor",
    "runtime_entity": "sensor.runtime",
    "indoor_target_entity": "sensor.target",
    "weather_entity": "weather.home",
    "tank_capacity": 1000,
    "stale_after_minutes": 90,
}


def _set_house(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.outdoor", "7", {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.indoor", "19.5", {"unit_of_measurement": "°C"})
    hass.states.async_set("climate.home", "heat", {"temperature": 20})
    hass.states.async_set("sensor.target", "20", {"unit_of_measurement": "°C"})


def _entity_state(hass: HomeAssistant, entry: MockConfigEntry, platform: str, key: str):
    entity_id = er.async_get(hass).async_get_entity_id(
        platform, DOMAIN, f"{entry.entry_id}_{key}"
    )
    assert entity_id, key
    return hass.states.get(entity_id)


async def _start_flow(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def test_setup_flow_distance_sensor_upright_tank(hass: HomeAssistant, recorder_mock) -> None:
    """A distance sensor asks for the tank shape; optional sensors may be left out."""
    result = await _start_flow(hass)
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"depth_entity": "sensor.tank", "level_type": "distance", "tank_capacity": 1200},
    )
    assert result["step_id"] == "shape"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"tank_shape": "vertical", "full_distance_cm": 10, "empty_distance_cm": 110},
    )
    assert result["step_id"] == "sensors"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], SENSORS)
    # The sensor check is shown before saving; confirming it creates the entry.
    assert result["step_id"] == "check"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data["tank_shape"] == "vertical"
    assert data["tank_capacity"] == 1200
    assert "runtime_entity" not in data
    assert "timestamp_entity" not in data


async def test_setup_flow_percent_sensor_skips_shape(hass: HomeAssistant, recorder_mock) -> None:
    """Percent and litre sensors need no tank shape."""
    result = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"depth_entity": "sensor.tank_pct", "level_type": "percent", "tank_capacity": 1000},
    )
    assert result["step_id"] == "sensors"


@pytest.mark.parametrize(
    ("shape_input", "field", "error"),
    [
        (
            {"tank_shape": "vertical", "full_distance_cm": 100, "empty_distance_cm": 20},
            "empty_distance_cm",
            "empty_not_below_full",
        ),
        (
            {
                "tank_shape": "custom",
                "full_distance_cm": 10,
                "empty_distance_cm": 110,
                "calibration": "nonsense",
            },
            "calibration",
            "invalid_calibration",
        ),
    ],
)
async def test_shape_step_rejects_mistakes(
    hass: HomeAssistant, recorder_mock, shape_input, field, error
) -> None:
    """Impossible distances and unreadable tables are caught in the form."""
    result = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"depth_entity": "sensor.tank", "level_type": "distance", "tank_capacity": 1000},
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], shape_input)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {field: error}


async def test_version_1_entry_migrates_and_keeps_its_numbers(
    hass: HomeAssistant, recorder_mock
) -> None:
    """An existing install keeps the prototype table and the same litres."""
    _set_house(hass)
    hass.states.async_set("sensor.tank_depth", "54", {"unit_of_measurement": "cm"})
    hass.states.async_set("sensor.tank_time", dt_util.utcnow().isoformat())
    entry = MockConfigEntry(domain=DOMAIN, data=V1_DATA, version=1, unique_id=DOMAIN)
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.version == 2
    assert entry.data["level_type"] == "distance"
    assert entry.data["tank_shape"] == "custom"
    assert entry.data["calibration"].startswith("9:100, 17:90")
    litres = _entity_state(hass, entry, "sensor", "litres_remaining")
    assert float(litres.state) == pytest.approx(607.14, abs=0.01)
    assert _entity_state(hass, entry, "binary_sensor", "tank_sensor_stale").state == "off"


async def test_percent_sensor_without_timestamp_sensor(hass: HomeAssistant, recorder_mock) -> None:
    """Freshness falls back to when the tank sensor last reported."""
    _set_house(hass)
    hass.states.async_set("sensor.tank_pct", "40", {"unit_of_measurement": "%"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id=DOMAIN,
        data={
            **SENSORS,
            "depth_entity": "sensor.tank_pct",
            "level_type": "percent",
            "tank_capacity": 1500,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert float(_entity_state(hass, entry, "sensor", "litres_remaining").state) == 600
    assert _entity_state(hass, entry, "binary_sensor", "tank_sensor_stale").state == "off"
    # Defaults scale with the tank: reorder at 25 %, empty at 5 %.
    assert float(_entity_state(hass, entry, "number", "reorder_level").state) == 375
    assert float(_entity_state(hass, entry, "number", "empty_level").state) == 75
    # No recorded history at all: the estimate says which sensors lack it.
    stale_attrs = _entity_state(hass, entry, "binary_sensor", "tank_sensor_stale").attributes
    assert stale_attrs["training_weeks"] == 0
    assert "No daily history was found for tank (sensor.tank_pct)" in stale_attrs["forecast_warning"]


async def test_live_update_and_stale_reading(hass: HomeAssistant, recorder_mock) -> None:
    """Litres follow the sensor immediately; an old measurement is flagged stale."""
    _set_house(hass)
    hass.states.async_set("sensor.tank_depth", "54")
    hass.states.async_set("sensor.tank_time", dt_util.utcnow().isoformat())
    entry = MockConfigEntry(domain=DOMAIN, data=V1_DATA, version=1, unique_id=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    hass.states.async_set("sensor.tank_depth", "66")
    hass.states.async_set(
        "sensor.tank_time", (dt_util.utcnow() - timedelta(hours=3)).isoformat()
    )
    await hass.async_block_till_done()
    assert float(_entity_state(hass, entry, "sensor", "litres_remaining").state) == 500
    assert _entity_state(hass, entry, "binary_sensor", "tank_sensor_stale").state == "on"


async def test_settings_screen_changes_tank_and_clears_optional_sensor(
    hass: HomeAssistant, recorder_mock
) -> None:
    """The settings flow can switch sensor type and remove an optional sensor."""
    _set_house(hass)
    hass.states.async_set("sensor.tank_depth", "54")
    hass.states.async_set("sensor.tank_time", dt_util.utcnow().isoformat())
    hass.states.async_set("sensor.tank_pct", "50")
    entry = MockConfigEntry(domain=DOMAIN, data=V1_DATA, version=1, unique_id=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"depth_entity": "sensor.tank_pct", "level_type": "percent", "tank_capacity": 1000},
    )
    assert result["step_id"] == "sensors"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "outdoor_entity": "sensor.outdoor",
            "indoor_entity": "sensor.indoor",
            "indoor_target_entity": "sensor.target",
            "weather_entity": "weather.home",
            "stale_after_minutes": 120,
        },
    )
    assert result["step_id"] == "check"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert entry.data["level_type"] == "percent"
    assert entry.data["depth_entity"] == "sensor.tank_pct"
    assert "runtime_entity" not in entry.data
    assert "timestamp_entity" not in entry.data
    assert entry.data["stale_after_minutes"] == 120
    # The integration reloaded with the new sensor: 50 % of 1000 L.
    assert float(_entity_state(hass, entry, "sensor", "litres_remaining").state) == 500
