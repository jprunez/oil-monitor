"""Long-term daily temperatures for the home's area, from Open-Meteo.

Used to project mild, typical and cold winters. Only an approximate
location is ever sent: coordinates are rounded to one decimal place (about
10 km) whether they come from a town/postcode lookup or from Home
Assistant's home location. No account or API key is needed.
"""

from __future__ import annotations

from dataclasses import dataclass
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
UK_OUTCODE_URL = "https://api.postcodes.io/outcodes/{outcode}"
# A UK postcode, or just its first half ("YO10", "YO10 5DD", "sw1a 1aa").
UK_POSTCODE = re.compile(r"^([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})?$")
YEARS = 25
REFRESH_AFTER = timedelta(days=30)
RETRY_AFTER = timedelta(hours=6)
TIMEOUT_SECONDS = 30
STORAGE_VERSION = 1


class ClimateHistoryError(Exception):
    """Weather history could not be obtained."""


@dataclass
class ClimateHistory:
    """Daily mean temperatures (°C) for an approximate location."""

    latitude: float
    longitude: float
    place: str
    daily: dict[date, float]

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


async def async_fetch_daily_temperatures(
    hass: HomeAssistant, latitude: float, longitude: float
) -> dict[date, float]:
    """Download about 25 years of daily mean temperatures in °C."""
    today = dt_util.now().date()
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "start_date": date(today.year - YEARS, 1, 1).isoformat(),
        "end_date": (today - timedelta(days=7)).isoformat(),
        "daily": "temperature_2m_mean",
        "timezone": "auto",
    }
    session = async_get_clientsession(hass)
    try:
        async with session.get(ARCHIVE_URL, params=params, timeout=TIMEOUT_SECONDS) as response:
            response.raise_for_status()
            payload = await response.json()
    except Exception as err:  # noqa: BLE001 - any network failure
        raise ClimateHistoryError(f"Could not download weather history: {err}") from err
    daily = payload.get("daily") or {}
    temperatures: dict[date, float] = {}
    for day_text, value in zip(daily.get("time", []), daily.get("temperature_2m_mean", [])):
        if value is not None:
            temperatures[date.fromisoformat(day_text)] = float(value)
    if not temperatures:
        raise ClimateHistoryError("The weather history service returned no data")
    return temperatures


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
            self.history = ClimateHistory(
                latitude=stored["latitude"],
                longitude=stored["longitude"],
                place=stored["place"],
                daily={
                    start + timedelta(days=index): value
                    for index, value in enumerate(stored["values"])
                    if value is not None
                },
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
            daily = await async_fetch_daily_temperatures(self._hass, latitude, longitude)
        except ClimateHistoryError as err:
            self.error = str(err)
            _LOGGER.warning("Oil Monitor weather history: %s", err)
            return self.history
        self.error = None
        self.history = ClimateHistory(latitude, longitude, place, daily)
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
            }
        )
        return self.history
