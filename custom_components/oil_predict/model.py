"""Pure calculations used by the Oil Monitor integration."""

from dataclasses import dataclass
import math
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone

# Depth in centimetres mapped to percentage full, based on the tank calibration.
DEPTH_TO_PERCENT: tuple[tuple[float, float], ...] = (
    (9.0, 100.0),
    (17.0, 90.0),
    (30.0, 80.0),
    (41.0, 70.0),
    (55.0, 60.0),
    (66.0, 50.0),
    (78.0, 40.0),
    (90.0, 30.0),
    (101.0, 20.0),
    (113.0, 10.0),
    (126.0, 0.0),
)


def parse_sample_timestamp(value: str) -> datetime | None:
    """Parse an ISO timestamp or Unix timestamp from the radio sensor."""
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        numeric_value = None

    if numeric_value is not None and math.isfinite(numeric_value) and numeric_value > 0:
        # Millisecond Unix timestamps are common in MQTT payloads.
        if numeric_value > 100_000_000_000:
            numeric_value /= 1000
        try:
            return datetime.fromtimestamp(numeric_value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def depth_to_litres(depth_cm: float, tank_capacity_litres: float) -> float:
    """Convert a valid tank depth to litres using linear interpolation."""
    if not math.isfinite(depth_cm) or depth_cm <= 0:
        raise ValueError("Tank depth must be a finite positive number")
    if not math.isfinite(tank_capacity_litres) or tank_capacity_litres <= 0:
        raise ValueError("Tank capacity must be a finite positive number")

    if depth_cm <= DEPTH_TO_PERCENT[0][0]:
        percent_full = DEPTH_TO_PERCENT[0][1]
    elif depth_cm >= DEPTH_TO_PERCENT[-1][0]:
        percent_full = DEPTH_TO_PERCENT[-1][1]
    else:
        percent_full = 0.0
        for (low_depth, low_percent), (high_depth, high_percent) in zip(
            DEPTH_TO_PERCENT, DEPTH_TO_PERCENT[1:]
        ):
            if low_depth <= depth_cm <= high_depth:
                fraction = (depth_cm - low_depth) / (high_depth - low_depth)
                percent_full = low_percent + fraction * (high_percent - low_percent)
                break

    return tank_capacity_litres * percent_full / 100.0


@dataclass(frozen=True)
class DailyTrainingSample:
    """One clean day of measured oil use and heating conditions."""

    outdoor_mean_c: float
    indoor_mean_c: float
    heating_hours: float
    litres_used: float


@dataclass(frozen=True)
class DailyHistoryPoint:
    """One day's recorder statistics used to build weekly training samples."""

    day: date
    depth_mean_cm: float
    outdoor_mean_c: float
    indoor_mean_c: float
    runtime_max_hours: float


def build_training_samples(
    points: Sequence[DailyHistoryPoint], tank_capacity_litres: float
) -> list[DailyTrainingSample]:
    """Build low-noise daily-average training samples from complete weeks.

    Each sample represents the average day in one complete Monday-to-Sunday
    week. Tank-volume differences are calculated across the full week, reducing
    the impact of the ultrasonic sensor's one-centimetre quantisation.
    """
    by_day = {point.day: point for point in points}
    if len(by_day) != len(points):
        raise ValueError("History must contain at most one point per day")
    if not math.isfinite(tank_capacity_litres) or tank_capacity_litres <= 0:
        raise ValueError("Tank capacity must be a finite positive number")

    if not by_day:
        return []

    first_day = min(by_day)
    last_day = max(by_day)
    monday = first_day - timedelta(days=first_day.weekday())
    samples: list[DailyTrainingSample] = []

    while monday + timedelta(days=6) <= last_day:
        days = [monday + timedelta(days=offset) for offset in range(7)]
        week = [by_day.get(day) for day in days]
        previous_sunday = by_day.get(monday - timedelta(days=1))
        monday += timedelta(days=7)
        if previous_sunday is None or any(point is None for point in week):
            continue

        complete_week = [point for point in week if point is not None]
        interval = [previous_sunday, *complete_week]
        numeric_values = [
            value
            for point in complete_week
            for value in (
                previous_sunday.depth_mean_cm,
                point.depth_mean_cm,
                point.outdoor_mean_c,
                point.indoor_mean_c,
                point.runtime_max_hours,
            )
        ]
        if not all(math.isfinite(value) for value in numeric_values):
            continue
        if any(
            point.depth_mean_cm <= 0
            or point.runtime_max_hours < 0
            for point in interval
        ):
            continue

        # A refill makes the apparent tank-level change positive. Skip that
        # week rather than treating a refill as negative consumption.
        depth_changes = [
            current.depth_mean_cm - previous.depth_mean_cm
            for previous, current in zip(interval, interval[1:])
        ]
        if any(change <= -8.0 for change in depth_changes):
            continue

        start_litres = depth_to_litres(
            previous_sunday.depth_mean_cm, tank_capacity_litres
        )
        end_litres = depth_to_litres(
            complete_week[-1].depth_mean_cm, tank_capacity_litres
        )
        weekly_usage = start_litres - end_litres
        # Reject implausibly large weekly changes, likely caused by a sensor
        # jump rather than normal consumption.
        if weekly_usage < 0 or weekly_usage > 300:
            continue

        samples.append(
            DailyTrainingSample(
                outdoor_mean_c=sum(p.outdoor_mean_c for p in complete_week) / 7,
                indoor_mean_c=sum(p.indoor_mean_c for p in complete_week) / 7,
                heating_hours=sum(p.runtime_max_hours for p in complete_week) / 7,
                litres_used=weekly_usage / 7,
            )
        )

    return samples


@dataclass(frozen=True)
class UsageModel:
    """Learned relationship between indoor/outdoor delta, runtime, and oil use."""

    runtime_intercept_hours: float
    runtime_hours_per_degree: float
    oil_baseline_litres_per_day: float
    oil_litres_per_heating_hour: float
    runtime_rmse_hours: float
    oil_rmse_litres: float
    sample_count: int


@dataclass(frozen=True)
class ForecastDay:
    """Estimated heating and oil use for one forecast day."""

    outdoor_mean_c: float
    predicted_heating_hours: float
    predicted_litres: float


@dataclass(frozen=True)
class WeeklyForecast:
    """Seven-day estimate with a simple uncertainty range."""

    days: tuple[ForecastDay, ...]
    total_litres: float
    lower_litres: float
    upper_litres: float


def _linear_fit(points: Sequence[tuple[float, float]]) -> tuple[float, float, float]:
    """Fit y = intercept + slope*x and return coefficients and residual RMSE."""
    if not points:
        raise ValueError("At least one training point is required")

    mean_x = sum(x for x, _ in points) / len(points)
    mean_y = sum(y for _, y in points) / len(points)
    variance_x = sum((x - mean_x) ** 2 for x, _ in points)
    slope = (
        sum((x - mean_x) * (y - mean_y) for x, y in points) / variance_x
        if variance_x > 0
        else 0.0
    )
    intercept = mean_y - slope * mean_x
    rmse = math.sqrt(
        sum((y - (intercept + slope * x)) ** 2 for x, y in points) / len(points)
    )
    return intercept, slope, rmse


def fit_usage_model(samples: Sequence[DailyTrainingSample]) -> UsageModel:
    """Fit a two-stage model from cleaned daily tank, temperature, and runtime data.

    Refill weeks, invalid measurements, and periods without a reliable tank
    delta must be removed before calling this function. Fourteen samples are
    the minimum; the Home Assistant pipeline supplies weekly averages, so this
    corresponds to fourteen weeks of history.
    """
    if len(samples) < 14:
        raise ValueError("At least 14 clean samples are required")

    for sample in samples:
        values = (
            sample.outdoor_mean_c,
            sample.indoor_mean_c,
            sample.heating_hours,
            sample.litres_used,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Training values must be finite numbers")
        if sample.heating_hours < 0 or sample.litres_used < 0:
            raise ValueError("Heating hours and oil use cannot be negative")

    runtime_points = [
        (max(0.0, sample.indoor_mean_c - sample.outdoor_mean_c), sample.heating_hours)
        for sample in samples
    ]
    runtime_intercept, runtime_slope, runtime_rmse = _linear_fit(runtime_points)

    # Heating demand and runtime should not decrease as the indoor/outdoor
    # temperature gap grows. Keep the learned model physically plausible.
    runtime_slope = max(0.0, runtime_slope)
    runtime_intercept = max(
        0.0,
        sum(
            observed - runtime_slope * delta
            for delta, observed in runtime_points
        )
        / len(runtime_points),
    )
    runtime_rmse = math.sqrt(
        sum(
            (observed - (runtime_intercept + runtime_slope * delta)) ** 2
            for delta, observed in runtime_points
        )
        / len(runtime_points)
    )

    oil_points = [(sample.heating_hours, sample.litres_used) for sample in samples]
    oil_baseline, oil_rate, _ = _linear_fit(oil_points)
    oil_rate = max(0.0, oil_rate)
    oil_baseline = max(
        0.0,
        sum(
            sample.litres_used - oil_rate * sample.heating_hours
            for sample in samples
        )
        / len(samples),
    )
    oil_rmse = math.sqrt(
        sum(
            (sample.litres_used - (oil_baseline + oil_rate * sample.heating_hours))
            ** 2
            for sample in samples
        )
        / len(samples)
    )

    return UsageModel(
        runtime_intercept_hours=runtime_intercept,
        runtime_hours_per_degree=runtime_slope,
        oil_baseline_litres_per_day=oil_baseline,
        oil_litres_per_heating_hour=oil_rate,
        runtime_rmse_hours=runtime_rmse,
        oil_rmse_litres=oil_rmse,
        sample_count=len(samples),
    )


def forecast_week(
    model: UsageModel,
    forecast_outdoor_mean_c: Sequence[float],
    indoor_target_c: float,
) -> WeeklyForecast:
    """Estimate oil consumption day by day from the coming week's weather."""
    if len(forecast_outdoor_mean_c) != 7:
        raise ValueError("Exactly seven daily outdoor forecasts are required")
    if not math.isfinite(indoor_target_c):
        raise ValueError("Indoor target temperature must be finite")
    if not all(math.isfinite(temp) for temp in forecast_outdoor_mean_c):
        raise ValueError("Outdoor forecast temperatures must be finite")

    days: list[ForecastDay] = []
    for outdoor_mean in forecast_outdoor_mean_c:
        temperature_gap = max(0.0, indoor_target_c - outdoor_mean)
        heating_hours = max(
            0.0,
            model.runtime_intercept_hours
            + model.runtime_hours_per_degree * temperature_gap,
        )
        litres = max(
            0.0,
            model.oil_baseline_litres_per_day
            + model.oil_litres_per_heating_hour * heating_hours,
        )
        days.append(
            ForecastDay(
                outdoor_mean_c=outdoor_mean,
                predicted_heating_hours=heating_hours,
                predicted_litres=litres,
            )
        )

    total = sum(day.predicted_litres for day in days)
    daily_error = math.sqrt(
        model.oil_rmse_litres**2
        + (model.oil_litres_per_heating_hour * model.runtime_rmse_hours) ** 2
    )
    uncertainty = 1.96 * daily_error * math.sqrt(len(days))
    return WeeklyForecast(
        days=tuple(days),
        total_litres=total,
        lower_litres=max(0.0, total - uncertainty),
        upper_litres=total + uncertainty,
    )
