"""Long-term daily temperatures for the home's area, from Open-Meteo.

Used to project mild, typical and cold winters. Only an approximate
location is ever sent: coordinates are rounded to one decimal place (about
10 km) whether they come from a town/postcode lookup or from Home
Assistant's home location. No account or API key is needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import logging
import re
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
# Daily series: our name -> Open-Meteo variable.
SERIES = {
    "temperature": "temperature_2m_mean",  # °C
    "wind": "wind_speed_10m_mean",  # km/h
    "sun": "shortwave_radiation_sum",  # MJ/m²
}
FORECAST_REFRESH_AFTER = timedelta(hours=6)
UK_OUTCODE_URL = "https://api.postcodes.io/outcodes/{outcode}"
# A UK postcode, or just its first half ("YO10", "YO10 5DD", "sw1a 1aa").
UK_POSTCODE = re.compile(r"^([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})?$")
YEARS = 25
REFRESH_AFTER = timedelta(days=30)
RETRY_AFTER = timedelta(hours=6)
TIMEOUT_SECONDS = 60
WIND_SUN_DAYS = 400
STORAGE_VERSION = 1


class ClimateHistoryError(Exception):
    """Weather history could not be obtained."""


@dataclass
class ClimateHistory:
    """Daily weather for an approximate location.

    ``daily`` is mean temperature (°C); ``wind`` is mean wind speed (km/h)
    and ``sun`` is solar radiation (MJ/m²).
    """

    latitude: float
    longitude: float
    place: str
    daily: dict[date, float]
    wind: dict[date, float] = field(default_factory=dict)
    sun: dict[date, float] = field(default_factory=dict)

    @property
    def years(self) -> int:
        """Whole years of data held."""
        if not self.daily:
            return 0
        return (max(self.daily) - min(self.daily)).days // 365


def approximate(value: float) -> float:
    """Round a coordinate to one decimal place (roughly 10 km)."""
    return round(float(value), 1)


async def async_geocode(
    hass: HomeAssistant, text: str, country: str | None
) -> tuple[float, float, str]:
    """Find approximate coordinates for a town name or postcode.

    For a UK postcode only the first half (the district, e.g. "YO10") is
    looked up, so the full postcode never leaves Home Assistant.
    """
    session = async_get_clientsession(hass)
    match = UK_POSTCODE.match(text.strip().upper())
    if match and (country or "GB") == "GB":
        outcode = match.group(1)
        try:
            async with session.get(
                UK_OUTCODE_URL.format(outcode=outcode), timeout=TIMEOUT_SECONDS
            ) as response:
                if response.status == 200:
                    result = (await response.json()).get("result") or {}
                    if result.get("latitude") is not None:
                        return (
                            approximate(result["latitude"]),
                            approximate(result["longitude"]),
                            f"{outcode} area",
                        )
        except Exception as err:  # noqa: BLE001 - fall through to the general lookup
            _LOGGER.debug("UK postcode lookup failed: %s", err)
        text = outcode
    params: dict[str, Any] = {"name": text, "count": 1, "format": "json"}
    if country:
        params["countryCode"] = country
    try:
        async with session.get(GEOCODE_URL, params=params, timeout=TIMEOUT_SECONDS) as response:
            response.raise_for_status()
            payload = await response.json()
    except Exception as err:  # noqa: BLE001 - any network failure
        raise ClimateHistoryError(f"Could not look up \"{text}\": {err}") from err
    results = payload.get("results") or []
    if not results:
        raise ClimateHistoryError(
            f"No place was found for \"{text}\". Try a town name or the first part of the postcode."
        )
    result = results[0]
    return approximate(result["latitude"]), approximate(result["longitude"]), str(
        result.get("name") or text
    )


def _parse_daily(payload: dict[str, Any]) -> dict[str, dict[date, float]]:
    daily = payload.get("daily") or {}
    days = [date.fromisoformat(text) for text in daily.get("time", [])]
    return {
        name: {
            day: float(value)
            for day, value in zip(days, daily.get(variable) or [])
            if value is not None
        }
        for name, variable in SERIES.items()
    }


async def _async_fetch_archive(
    hass: HomeAssistant,
    latitude: float,
    longitude: float,
    first_day: date,
    names: tuple[str, ...],
) -> dict[str, dict[date, float]]:
    """Download the named daily series from ``first_day`` to a week ago."""
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "start_date": first_day.isoformat(),
        "end_date": (dt_util.now().date() - timedelta(days=7)).isoformat(),
        "daily": ",".join(SERIES[name] for name in names),
        "timezone": "auto",
    }
    session = async_get_clientsession(hass)
    try:
        async with session.get(ARCHIVE_URL, params=params, timeout=TIMEOUT_SECONDS) as response:
            if response.status >= 400:
                reason = ""
                try:
                    reason = (await response.json()).get("reason", "")
                except Exception:  # noqa: BLE001 - not JSON
                    pass
                raise ClimateHistoryError(
                    f"Could not download weather history: {reason or f'HTTP {response.status}'}"
                )
            payload = await response.json()
    except ClimateHistoryError:
        raise
    except TimeoutError as err:
        raise ClimateHistoryError(
            "Could not download weather history: the service took too long to answer"
        ) from err
    except Exception as err:  # noqa: BLE001 - any network failure
        raise ClimateHistoryError(f"Could not download weather history: {err}") from err
    return _parse_daily(payload)


async def async_fetch_daily_weather(
    hass: HomeAssistant, latitude: float, longitude: float
) -> dict[str, dict[date, float]]:
    """Download daily weather history for an approximate location.

    Temperature covers about 25 years, for the winter scenarios. Wind and
    sun cover only the last 13 months, which is all the model learns from;
    long spans of those are slow for the service to produce. A failure of
    the wind and sun request leaves them empty rather than failing.
    """
    today = dt_util.now().date()
    series = await _async_fetch_archive(
        hass, latitude, longitude, date(today.year - YEARS, 1, 1), ("temperature",)
    )
    if not series["temperature"]:
        raise ClimateHistoryError("The weather history service returned no data")
    try:
        recent = await _async_fetch_archive(
            hass, latitude, longitude, today - timedelta(days=WIND_SUN_DAYS), ("wind", "sun")
        )
    except ClimateHistoryError as err:
        _LOGGER.debug("Oil Monitor wind and sun history: %s", err)
        recent = {}
    return {
        "temperature": series["temperature"],
        "wind": recent.get("wind", {}),
        "sun": recent.get("sun", {}),
    }


async def async_fetch_recent_and_forecast(
    hass: HomeAssistant, latitude: float, longitude: float
) -> dict[str, dict[date, float]]:
    """Download wind and sun for the last few weeks and the coming week.

    The archive runs about a week behind, so this also fills that gap.
    """
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "daily": f"{SERIES['wind']},{SERIES['sun']}",
        "past_days": 21,
        "forecast_days": 8,
        "timezone": "auto",
    }
    session = async_get_clientsession(hass)
    try:
        async with session.get(FORECAST_URL, params=params, timeout=TIMEOUT_SECONDS) as response:
            response.raise_for_status()
            payload = await response.json()
    except Exception as err:  # noqa: BLE001 - any network failure
        raise ClimateHistoryError(f"Could not download the wind and sun forecast: {err}") from err
    return _parse_daily(payload)


class ClimateHistoryManager:
    """Fetch weather history occasionally and keep it in Home Assistant's storage."""

    def __init__(self, hass: HomeAssistant, location_text: str | None) -> None:
        """Remember where to look; nothing is fetched until asked."""
        self._hass = hass
        self._location_text = (location_text or "").strip()
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.climate_history")
        self._loaded = False
        self._last_attempt: datetime | None = None
        self.history: ClimateHistory | None = None
        self.fetched_at: datetime | None = None
        self.error: str | None = None
        # Recent and forecast wind/sun, refreshed more often than the archive.
        self.recent: dict[str, dict[date, float]] = {"wind": {}, "sun": {}}
        self._recent_fetched_at: datetime | None = None

    async def _async_location(self) -> tuple[float, float, str]:
        if self._location_text:
            return await async_geocode(
                self._hass, self._location_text, self._hass.config.country
            )
        return (
            approximate(self._hass.config.latitude),
            approximate(self._hass.config.longitude),
            "Home Assistant home area",
        )

    async def _async_load_stored(self) -> None:
        self._loaded = True
        stored = await self._store.async_load()
        if not stored or stored.get("location_text") != self._location_text:
            return
        try:
            start = date.fromisoformat(stored["start"])

            def series(key: str) -> dict[date, float]:
                return {
                    start + timedelta(days=index): value
                    for index, value in enumerate(stored[key])
                    if value is not None
                }

            self.history = ClimateHistory(
                latitude=stored["latitude"],
                longitude=stored["longitude"],
                place=stored["place"],
                daily=series("values"),
                wind=series("wind"),
                sun=series("sun"),
            )
            self.fetched_at = dt_util.parse_datetime(stored["fetched_at"])
        except (KeyError, TypeError, ValueError):
            self.history = None
            self.fetched_at = None

    async def async_get(self) -> ClimateHistory | None:
        """Return weather history, downloading it when missing or a month old."""
        if not self._loaded:
            await self._async_load_stored()
        now = dt_util.utcnow()
        fresh = self.fetched_at is not None and now - self.fetched_at < REFRESH_AFTER
        if self.history is not None and fresh:
            return self.history
        if self._last_attempt is not None and now - self._last_attempt < RETRY_AFTER:
            return self.history
        self._last_attempt = now
        try:
            latitude, longitude, place = await self._async_location()
            weather = await async_fetch_daily_weather(self._hass, latitude, longitude)
        except ClimateHistoryError as err:
            self.error = str(err)
            _LOGGER.warning("Oil Monitor weather history: %s", err)
            return self.history
        self.error = None
        daily = weather["temperature"]
        wind = weather.get("wind") or {}
        sun = weather.get("sun") or {}
        self.history = ClimateHistory(latitude, longitude, place, daily, wind, sun)
        self.fetched_at = now
        start = min(daily)
        days = (max(daily) - start).days + 1
        await self._store.async_save(
            {
                "location_text": self._location_text,
                "latitude": latitude,
                "longitude": longitude,
                "place": place,
                "fetched_at": now.isoformat(),
                "start": start.isoformat(),
                "values": [daily.get(start + timedelta(days=i)) for i in range(days)],
                "wind": [wind.get(start + timedelta(days=i)) for i in range(days)],
                "sun": [sun.get(start + timedelta(days=i)) for i in range(days)],
            }
        )
        return self.history

    async def async_get_recent(self) -> dict[str, dict[date, float]]:
        """Return recent and forecast wind/sun, refreshing every few hours.

        Failures keep whatever was fetched before (possibly nothing).
        """
        now = dt_util.utcnow()
        if self.history is None:
            return self.recent
        if (
            self._recent_fetched_at is not None
            and now - self._recent_fetched_at < FORECAST_REFRESH_AFTER
        ):
            return self.recent
        self._recent_fetched_at = now
        try:
            self.recent = await async_fetch_recent_and_forecast(
                self._hass, self.history.latitude, self.history.longitude
            )
        except ClimateHistoryError as err:
            _LOGGER.debug("Oil Monitor wind and sun forecast: %s", err)
        return self.recent
