"""Fixtures for Home Assistant integration tests.

Run from the repository root with pytest-homeassistant-custom-component
installed:  python -m pytest tests_integration
"""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_socket

pytest_plugins = "pytest_homeassistant_custom_component"


if sys.platform == "win32":
    # The Windows event loop needs a local socket pair, which the harness's
    # socket blocker forbids. Turn the blocker off for local Windows runs only;
    # Linux CI keeps it.
    pytest_socket.disable_socket = lambda *args, **kwargs: None


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """Allow Home Assistant to load the integration from custom_components."""
    yield


@pytest.fixture(autouse=True)
def mock_panel(recorder_mock, hass):
    """Skip the sidebar panel: the frontend is not available in tests."""
    hass.config.components.add("panel_custom")
    with (
        patch("custom_components.oil_predict.async_setup", AsyncMock(return_value=True)),
        patch("custom_components.oil_predict.async_register_panel", AsyncMock()) as register,
        patch("custom_components.oil_predict.frontend", MagicMock()),
    ):
        yield register


@pytest.fixture(autouse=True)
def no_weather_history_download():
    """Never contact the weather-history service from tests."""
    from custom_components.oil_predict.climate_history import ClimateHistoryError

    with (
        patch(
            "custom_components.oil_predict.climate_history.async_fetch_daily_weather",
            AsyncMock(side_effect=ClimateHistoryError("Could not download weather history: offline")),
        ) as fetch,
        patch(
            "custom_components.oil_predict.climate_history.async_geocode",
            AsyncMock(return_value=(54.0, -1.1, "YO10 area")),
        ),
        patch(
            "custom_components.oil_predict.climate_history.async_fetch_recent_and_forecast",
            AsyncMock(return_value={"temperature": {}, "wind": {}, "sun": {}}),
        ),
    ):
        yield fetch
