"""Pure calculations used by the Oil Monitor integration."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import math

# What kind of value the tank sensor reports.
LEVEL_DISTANCE = "distance"  # cm from the sensor down to the oil surface
LEVEL_PERCENT = "percent"  # percent full
LEVEL_LITRES = "litres"  # litres in the tank

# How a distance reading is turned into a volume.
SHAPE_VERTICAL = "vertical"  # upright cylinder or box: volume follows height
SHAPE_HORIZONTAL_CYLINDER = "horizontal_cylinder"
SHAPE_CUSTOM = "custom"  # a measured distance-to-percent table

# The original prototype tank: distance in cm mapped to percent full.
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

# Training-data limits, as fractions of tank capacity so they suit any tank.
REFILL_RISE_FRACTION = 0.05  # a day-to-day rise above this is a refill
MAX_WEEKLY_USE_FRACTION = 0.30  # more use than this in a week is a sensor fault

# Weeks of clean history needed for an estimate, and for full confidence.
MIN_TRAINING_WEEKS = 6
FULL_CONFIDENCE_WEEKS = 14


@dataclass(frozen=True)
class TankConfig:
    """How to turn a tank sensor reading into litres."""

    capacity_litres: float
    level_type: str = LEVEL_DISTANCE
    shape: str = SHAPE_CUSTOM
    full_distance_cm: float = DEPTH_TO_PERCENT[0][0]
    empty_distance_cm: float = DEPTH_TO_PERCENT[-1][0]
    calibration: tuple[tuple[float, float], ...] = DEPTH_TO_PERCENT


def parse_calibration(text: str) -> tuple[tuple[float, float], ...]:
    """Parse "distance:percent" pairs such as "9:100, 66:50, 126:0".

    Pairs may be separated by commas, semicolons or new lines. Returns the
    pairs sorted by distance. Raises ValueError when the table is unusable.
    """
    pairs: list[tuple[float, float]] = []
    for chunk in text.replace(";", ",").replace("\n", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            raise ValueError(f"'{chunk}' is not in distance:percent form")
        distance_text, percent_text = chunk.split(":", 1)
        distance = float(distance_text)
        percent = float(percent_text)
        if not (math.isfinite(distance) and distance > 0):
            raise ValueError("Distances must be positive numbers")
        if not (math.isfinite(percent) and 0 <= percent <= 100):
            raise ValueError("Percentages must be between 0 and 100")
        pairs.append((distance, percent))
    pairs.sort()
    if len(pairs) < 2:
        raise ValueError("At least two calibration points are needed")
    if len({distance for distance, _ in pairs}) != len(pairs):
        raise ValueError("Each distance may appear only once")
    return tuple(pairs)


def format_calibration(pairs: Sequence[tuple[float, float]]) -> str:
    """Format calibration pairs as text that ``parse_calibration`` accepts."""
    return ", ".join(f"{_short(distance)}:{_short(percent)}" for distance, percent in pairs)


def _short(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


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
    except (TypeError, ValueError, AttributeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _interpolate_table(
    distance_cm: float, table: Sequence[tuple[float, float]]
) -> float:
    """Percent full for a distance, interpolating a sorted distance table."""
    if distance_cm <= table[0][0]:
        return table[0][1]
    if distance_cm >= table[-1][0]:
        return table[-1][1]
    for (low_distance, low_percent), (high_distance, high_percent) in zip(
        table, table[1:]
    ):
        if low_distance <= distance_cm <= high_distance:
            fraction = (distance_cm - low_distance) / (high_distance - low_distance)
            return low_percent + fraction * (high_percent - low_percent)
    return table[-1][1]


def horizontal_cylinder_fraction(height_fraction: float) -> float:
    """Volume fraction of a horizontal cylinder filled to ``height_fraction``."""
    h = min(1.0, max(0.0, height_fraction))
    angle = 2 * math.acos(1 - 2 * h)
    return (angle - math.sin(angle)) / (2 * math.pi)


def reading_to_litres(value: float, tank: TankConfig) -> float:
    """Convert a tank sensor reading to litres according to ``tank``."""
    if not math.isfinite(tank.capacity_litres) or tank.capacity_litres <= 0:
        raise ValueError("Tank capacity must be a finite positive number")
    if not math.isfinite(value):
        raise ValueError("Tank reading must be a finite number")

    if tank.level_type == LEVEL_LITRES:
        if value < 0:
            raise ValueError("Litres cannot be negative")
        return value

    if tank.level_type == LEVEL_PERCENT:
        if value < 0 or value > 110:
            raise ValueError("Percent full must be between 0 and 100")
        return tank.capacity_litres * min(100.0, value) / 100.0

    # Distance from the sensor down to the oil surface.
    if value <= 0:
        raise ValueError("Tank distance must be a positive number")
    if tank.shape == SHAPE_CUSTOM:
        percent_full = _interpolate_table(value, tank.calibration)
    else:
        span = tank.empty_distance_cm - tank.full_distance_cm
        if span <= 0:
            raise ValueError("The empty distance must be greater than the full distance")
        height_fraction = min(1.0, max(0.0, (tank.empty_distance_cm - value) / span))
        if tank.shape == SHAPE_HORIZONTAL_CYLINDER:
            percent_full = 100.0 * horizontal_cylinder_fraction(height_fraction)
        else:
            percent_full = 100.0 * height_fraction
    return tank.capacity_litres * percent_full / 100.0


def depth_to_litres(depth_cm: float, tank_capacity_litres: float) -> float:
    """Convert a distance reading with the prototype tank's calibration table."""
    if not math.isfinite(depth_cm) or depth_cm <= 0:
        raise ValueError("Tank depth must be a finite positive number")
    return reading_to_litres(depth_cm, TankConfig(capacity_litres=tank_capacity_litres))


@dataclass(frozen=True)
class DailyTrainingSample:
    """One clean (average) day of measured oil use and heating conditions.

    ``heating_hours`` is None when no heating-runtime sensor is configured.
    """

    outdoor_mean_c: float
    indoor_mean_c: float
    heating_hours: float | None
    litres_used: float


@dataclass(frozen=True)
class DailyHistoryPoint:
    """One day's recorder statistics used to build weekly training samples."""

    day: date
    litres_mean: float
    outdoor_mean_c: float
    indoor_mean_c: float
    runtime_max_hours: float | None = None


def build_training_samples(
    points: Sequence[DailyHistoryPoint], tank_capacity_litres: float
) -> list[DailyTrainingSample]:
    """Build low-noise daily-average training samples from complete weeks.

    Each sample represents the average day in one complete Monday-to-Sunday
    week. Tank-volume differences are calculated across the full week, reducing
    the impact of sensor quantisation. Weeks with a refill, a missing day or an
    implausible amount of use are skipped.
    """
    by_day = {point.day: point for point in points}
    if len(by_day) != len(points):
        raise ValueError("History must contain at most one point per day")
    if not math.isfinite(tank_capacity_litres) or tank_capacity_litres <= 0:
        raise ValueError("Tank capacity must be a finite positive number")

    if not by_day:
        return []

    refill_rise = REFILL_RISE_FRACTION * tank_capacity_litres
    max_weekly_use = MAX_WEEKLY_USE_FRACTION * tank_capacity_litres
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
            for point in interval
            for value in (point.litres_mean, point.outdoor_mean_c, point.indoor_mean_c)
        ]
        if not all(math.isfinite(value) for value in numeric_values):
            continue
        if any(point.litres_mean < 0 for point in interval):
            continue

        runtimes = [point.runtime_max_hours for point in complete_week]
        has_runtime = all(runtime is not None for runtime in runtimes)
        if has_runtime and not all(
            math.isfinite(runtime) and runtime >= 0 for runtime in runtimes
        ):
            continue

        # A refill makes the tank level rise. Skip that week rather than
        # treating a refill as negative consumption.
        if any(
            current.litres_mean - previous.litres_mean > refill_rise
            for previous, current in zip(interval, interval[1:])
        ):
            continue

        weekly_usage = previous_sunday.litres_mean - complete_week[-1].litres_mean
        # Reject implausibly large weekly changes, likely caused by a sensor
        # jump rather than normal consumption.
        if weekly_usage < 0 or weekly_usage > max_weekly_use:
            continue

        samples.append(
            DailyTrainingSample(
                outdoor_mean_c=sum(p.outdoor_mean_c for p in complete_week) / 7,
                indoor_mean_c=sum(p.indoor_mean_c for p in complete_week) / 7,
                heating_hours=(
                    sum(runtimes) / 7 if has_runtime else None  # type: ignore[arg-type]
                ),
                litres_used=weekly_usage / 7,
            )
        )

    return samples


@dataclass(frozen=True)
class UsageModel:
    """Learned relationship between indoor/outdoor gap, heating and oil use.

    With a runtime sensor the model has two stages (gap -> heating hours ->
    litres). Without one, "heating hours" are replaced by degrees of gap above
    the switch-off point (slope 1, intercept 0), so the same equations apply.
    """

    runtime_intercept_hours: float
    runtime_hours_per_degree: float
    # Heating only runs once it is this many degrees colder outside than
    # indoors; above that (the switch-off point) only the intercept remains.
    runtime_switch_off_gap_c: float
    oil_baseline_litres_per_day: float
    oil_litres_per_heating_hour: float
    runtime_rmse_hours: float
    oil_rmse_litres: float
    sample_count: int
    uses_runtime: bool = True

    @property
    def is_early(self) -> bool:
        """True while there is less history than needed for full confidence."""
        return self.sample_count < FULL_CONFIDENCE_WEEKS


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
    early: bool = False


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


def _switch_off_fit(
    points: Sequence[tuple[float, float]], step: float = 0.25, min_active: int = 3
) -> tuple[float, float, float, float]:
    """Fit y = intercept + slope*max(0, x - switch_off), with slope >= 0.

    Heating demand is flat (zero, or hot water only) until the indoor/outdoor
    gap exceeds a threshold, then rises linearly. The threshold is chosen by
    grid search; ties prefer the smallest threshold. Returns intercept,
    slope, threshold and residual RMSE.
    """
    if not points:
        raise ValueError("At least one training point is required")

    def fit_at(threshold: float) -> tuple[float, float, float]:
        shifted = [(max(0.0, x - threshold), y) for x, y in points]
        _, slope, _ = _linear_fit(shifted)
        slope = max(0.0, slope)
        intercept = max(
            0.0, sum(y - slope * x for x, y in shifted) / len(shifted)
        )
        sse = sum((y - (intercept + slope * x)) ** 2 for x, y in shifted)
        return intercept, slope, sse

    best_threshold = 0.0
    best = fit_at(0.0)
    highest = max(x for x, _ in points)
    threshold = step
    while threshold < highest:
        if sum(1 for x, _ in points if x > threshold) < min_active:
            break
        candidate = fit_at(threshold)
        if candidate[2] < best[2] - 1e-9 * (1 + best[2]):
            best, best_threshold = candidate, threshold
        threshold += step

    intercept, slope, sse = best
    return intercept, slope, best_threshold, math.sqrt(sse / len(points))


def fit_usage_model(samples: Sequence[DailyTrainingSample]) -> UsageModel:
    """Fit the usage model from cleaned daily-average samples.

    Refill weeks, invalid measurements, and periods without a reliable tank
    delta must be removed before calling this function. The Home Assistant
    pipeline supplies one sample per clean week, so ``MIN_TRAINING_WEEKS``
    samples correspond to that many weeks of history.
    """
    if len(samples) < MIN_TRAINING_WEEKS:
        raise ValueError(f"At least {MIN_TRAINING_WEEKS} clean samples are required")

    for sample in samples:
        values = [sample.outdoor_mean_c, sample.indoor_mean_c, sample.litres_used]
        if sample.heating_hours is not None:
            values.append(sample.heating_hours)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Training values must be finite numbers")
        if (sample.heating_hours or 0) < 0 or sample.litres_used < 0:
            raise ValueError("Heating hours and oil use cannot be negative")

    gaps = [max(0.0, s.indoor_mean_c - s.outdoor_mean_c) for s in samples]
    uses_runtime = all(sample.heating_hours is not None for sample in samples)

    if not uses_runtime:
        # Oil use directly from the temperature gap, with a switch-off point.
        baseline, rate, switch_off_gap, oil_rmse = _switch_off_fit(
            [(gap, sample.litres_used) for gap, sample in zip(gaps, samples)]
        )
        return UsageModel(
            runtime_intercept_hours=0.0,
            runtime_hours_per_degree=1.0,
            runtime_switch_off_gap_c=switch_off_gap,
            oil_baseline_litres_per_day=baseline,
            oil_litres_per_heating_hour=rate,
            runtime_rmse_hours=0.0,
            oil_rmse_litres=oil_rmse,
            sample_count=len(samples),
            uses_runtime=False,
        )

    # Runtime never falls as the gap grows, and stops rising once the
    # weather is warm enough for the heating to switch off.
    runtime_intercept, runtime_slope, switch_off_gap, runtime_rmse = _switch_off_fit(
        [(gap, sample.heating_hours) for gap, sample in zip(gaps, samples)]  # type: ignore[misc]
    )

    oil_points = [(sample.heating_hours, sample.litres_used) for sample in samples]
    oil_baseline, oil_rate, _ = _linear_fit(oil_points)  # type: ignore[arg-type]
    oil_rate = max(0.0, oil_rate)
    oil_baseline = max(
        0.0,
        sum(
            sample.litres_used - oil_rate * sample.heating_hours  # type: ignore[operator]
            for sample in samples
        )
        / len(samples),
    )
    oil_rmse = math.sqrt(
        sum(
            (sample.litres_used - (oil_baseline + oil_rate * sample.heating_hours))  # type: ignore[operator]
            ** 2
            for sample in samples
        )
        / len(samples)
    )

    return UsageModel(
        runtime_intercept_hours=runtime_intercept,
        runtime_hours_per_degree=runtime_slope,
        runtime_switch_off_gap_c=switch_off_gap,
        oil_baseline_litres_per_day=oil_baseline,
        oil_litres_per_heating_hour=oil_rate,
        runtime_rmse_hours=runtime_rmse,
        oil_rmse_litres=oil_rmse,
        sample_count=len(samples),
        uses_runtime=True,
    )


def predict_day(
    model: UsageModel, outdoor_mean_c: float, indoor_target_c: float
) -> tuple[float, float]:
    """Return predicted heating hours and litres for one day's mean temperature."""
    temperature_gap = max(0.0, indoor_target_c - outdoor_mean_c)
    heating_hours = max(
        0.0,
        model.runtime_intercept_hours
        + model.runtime_hours_per_degree
        * max(0.0, temperature_gap - model.runtime_switch_off_gap_c),
    )
    litres = max(
        0.0,
        model.oil_baseline_litres_per_day
        + model.oil_litres_per_heating_hour * heating_hours,
    )
    return heating_hours, litres


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
        heating_hours, litres = predict_day(model, outdoor_mean, indoor_target_c)
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
    if model.is_early:
        # Few weeks of history understate the error; widen the range.
        uncertainty *= math.sqrt(FULL_CONFIDENCE_WEEKS / model.sample_count)
    return WeeklyForecast(
        days=tuple(days),
        total_litres=total,
        lower_litres=max(0.0, total - uncertainty),
        upper_litres=total + uncertainty,
        early=model.is_early,
    )


def seasonal_outdoor_c(
    history: Mapping[date, float], day: date, window_days: int = 3
) -> float | None:
    """Typical outdoor temperature for a future day, from the same dates a year earlier.

    Averages the recorded daily means within ``window_days`` of the same
    calendar date one year before. Returns None when no history covers it.
    """
    try:
        last_year = day.replace(year=day.year - 1)
    except ValueError:  # 29 February
        last_year = day.replace(year=day.year - 1, day=28)
    values = [
        history[last_year + timedelta(days=offset)]
        for offset in range(-window_days, window_days + 1)
        if last_year + timedelta(days=offset) in history
    ]
    if not values:
        return None
    return sum(values) / len(values)


def days_until_level(
    current_litres: float, level_litres: float, daily_usage: Sequence[float]
) -> float | None:
    """Days until the tank falls to ``level_litres`` given projected daily use.

    Returns 0 when the tank is already at or below the level, a fractional
    number of days when the level is reached within the projection, or None
    when it is not reached within ``len(daily_usage)`` days.
    """
    if not math.isfinite(current_litres) or not math.isfinite(level_litres):
        raise ValueError("Tank levels must be finite numbers")
    remaining = current_litres - level_litres
    if remaining <= 0:
        return 0.0
    for index, use in enumerate(daily_usage):
        if use <= 0:
            continue
        if use >= remaining:
            return index + remaining / use
        remaining -= use
    return None


# --- Ultrasonic temperature drift -------------------------------------------
# An ultrasonic sensor times an echo and assumes a fixed speed of sound. Sound
# is slower in cold air, so the measured distance grows by about 0.17 % per
# degree Celsius of cooling unless the sensor compensates for it itself.
PHYSICAL_DRIFT_PER_C = 1 / (2 * 293.15)
MIN_DRIFT_PER_C = 0.0006
MAX_DRIFT_PER_C = 0.0030
MIN_DRIFT_T_STAT = 3.0
MIN_DRIFT_DAYS = 30


@dataclass(frozen=True)
class DriftEstimate:
    """How strongly distance readings follow air temperature in the history."""

    coefficient_per_c: float  # fractional distance change per degree of cooling
    t_statistic: float
    day_pairs: int

    @property
    def needs_correction(self) -> bool:
        """True when the drift is clear and close to the physical value."""
        return (
            self.day_pairs >= MIN_DRIFT_DAYS
            and self.t_statistic >= MIN_DRIFT_T_STAT
            and MIN_DRIFT_PER_C <= self.coefficient_per_c <= MAX_DRIFT_PER_C
        )


def temperature_corrected_distance(
    measured_cm: float, air_temp_c: float, reference_temp_c: float
) -> float:
    """Correct an ultrasonic distance for the air temperature it was taken at.

    The result is the distance the sensor would have reported at
    ``reference_temp_c``.
    """
    return measured_cm * math.sqrt(
        (273.15 + air_temp_c) / (273.15 + reference_temp_c)
    )


def _solve_3x3(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    """Solve a 3x3 linear system by Gaussian elimination."""
    a = [row[:] + [value] for row, value in zip(matrix, vector)]
    for col in range(3):
        pivot = max(range(col, 3), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            return None
        a[col], a[pivot] = a[pivot], a[col]
        for row in range(3):
            if row != col:
                factor = a[row][col] / a[col][col]
                a[row] = [x - factor * y for x, y in zip(a[row], a[col])]
    return [a[i][3] / a[i][i] for i in range(3)]


def estimate_temperature_drift(
    days: Sequence[tuple[date, float, float]],
) -> DriftEstimate | None:
    """Estimate temperature drift from daily (day, distance_cm, air_temp_c).

    Looks at consecutive days: real oil use depends on how cold it is, while
    sensor drift follows the *change* in temperature from one day to the
    next. Fits  change/distance = a + b*temperature - k*temperature_change
    and returns k with its t-statistic. Returns None with too little data.
    """
    by_day = {day: (distance, temp) for day, distance, temp in days}
    rows: list[tuple[float, float, float]] = []  # (temp level, temp change, y)
    for day, (distance, temp) in by_day.items():
        previous = by_day.get(day - timedelta(days=1))
        if previous is None:
            continue
        prev_distance, prev_temp = previous
        if not all(
            math.isfinite(v) for v in (distance, temp, prev_distance, prev_temp)
        ) or min(distance, prev_distance) <= 0:
            continue
        mean_distance = (distance + prev_distance) / 2
        change = (distance - prev_distance) / mean_distance
        # Skip deliveries and sensor jumps; drift is a fraction of a percent.
        if abs(change) > 0.05:
            continue
        rows.append(((temp + prev_temp) / 2, temp - prev_temp, change))

    n = len(rows)
    if n < MIN_DRIFT_DAYS:
        return None

    # Ordinary least squares with predictors [1, level, change].
    xtx = [[0.0] * 3 for _ in range(3)]
    xty = [0.0] * 3
    for level, delta, y in rows:
        x = (1.0, level, delta)
        for i in range(3):
            xty[i] += x[i] * y
            for j in range(3):
                xtx[i][j] += x[i] * x[j]
    beta = _solve_3x3(xtx, xty)
    if beta is None:
        return None
    residual = sum(
        (y - (beta[0] + beta[1] * level + beta[2] * delta)) ** 2
        for level, delta, y in rows
    )
    # Variance of the temperature-change coefficient: sigma^2 * (X'X)^-1[2][2].
    inverse_column = _solve_3x3(xtx, [0.0, 0.0, 1.0])
    if inverse_column is None or n <= 3:
        return None
    variance = residual / (n - 3) * inverse_column[2]
    coefficient = -beta[2]
    t_statistic = coefficient / math.sqrt(variance) if variance > 0 else math.inf
    return DriftEstimate(
        coefficient_per_c=coefficient, t_statistic=t_statistic, day_pairs=n
    )
