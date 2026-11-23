"""Boiler analysis from flow (and optionally return) water temperatures.

Pure calculations, kept free of Home Assistant imports so they can be tested
on their own.

Most oil boilers fire at one fixed rate, so oil burned follows how long the
heating runs. Hourly flow statistics (mean and maximum) show that: in an
hour where the heating ran throughout, the mean flow temperature is close to
the hour's maximum; in an hour where it ran for ten minutes the mean stays
close to the idle temperature. From that come:

* heating hours per day, which can stand in for a runtime sensor;
* a heat-delivered index from the radiator law
  (output ∝ (mean water temperature − room temperature)^1.3), which,
  divided by the oil burned, gives a relative boiler efficiency, and,
  divided by the indoor–outdoor gap, a relative house heat-loss figure;
* with a return sensor: the flow–return drop, the share of heating time
  the boiler can condense (return below 55 °C) and how much each degree of
  return temperature costs in oil.

The radiator size is unknown, so efficiency and heat loss are relative
(100 % = this house's usual), not absolute percentages.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import math

RADIATOR_EXPONENT = 1.3
# Without a return sensor, assume this flow–return drop while heating
# (typical radiator systems are designed for 10–20 K).
ASSUMED_DROP_C = 11.0
# Condensing boilers only condense with return water below about this.
CONDENSING_RETURN_C = 55.0
# An hour counts as heating when its peak flow is this far above idle.
HOT_MARGIN_C = 10.0
MAX_IDLE_OFFSET_C = 8.0  # idle pipes rarely sit further above room temperature
MIN_HOT_HOURS = 24  # hours of heating needed before analysing anything
MIN_HOURS_PER_DAY = 20  # hourly statistics needed to summarise a day
MIN_HEATING_HOURS_PER_DAY = 1.5  # weeks lighter than this are too noisy for efficiency
MIN_TREND_WEEKS = 6
MIN_EFFECT_WEEKS = 8
RECENT_WEEKS = 4
EFFICIENCY_ALERT_PERCENT = 92.0  # recent efficiency below this flags a service
WEATHER_COMP_SLOPE = -0.4  # °C of flow per °C outdoor that counts as compensated
DAYS_SHOWN = 60


@dataclass(frozen=True)
class HourPoint:
    """One hour of statistics, in °C."""

    start: datetime
    flow_mean_c: float
    flow_max_c: float
    indoor_c: float
    return_mean_c: float | None = None
    return_max_c: float | None = None


@dataclass(frozen=True)
class HeatingHour:
    """What one hour's statistics say about the heating."""

    point: HourPoint
    fraction: float  # share of the hour the heating ran (0–1)
    heat: float  # heat-delivered index for the hour
    flow_c: float | None  # flow temperature while heating
    return_c: float | None  # return temperature while heating


@dataclass(frozen=True)
class PeriodSummary:
    """Heating over a day or the average day of a week."""

    hours: float  # heating hours per day
    heat: float  # heat-delivered index per day
    indoor_c: float
    flow_c: float | None  # flow while heating, weighted by heating time
    return_c: float | None
    condensing_share: float | None  # share of heating time with return < 55 °C


def _percentile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("No values")
    position = (len(ordered) - 1) * q
    low = math.floor(position)
    high = math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


@dataclass(frozen=True)
class Fit:
    """A straight-line fit y = intercept + slope·x."""

    intercept: float
    slope: float
    r2: float
    t: float  # slope / its standard error (0 when undefined)
    n: int


def linear_fit(points: Sequence[tuple[float, float]]) -> Fit | None:
    """Least-squares line with R² and the slope's t-statistic."""
    n = len(points)
    if n < 3:
        return None
    mean_x = sum(x for x, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    sxx = sum((x - mean_x) ** 2 for x, _ in points)
    syy = sum((y - mean_y) ** 2 for _, y in points)
    if sxx <= 0:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in points) / sxx
    intercept = mean_y - slope * mean_x
    sse = sum((y - intercept - slope * x) ** 2 for x, y in points)
    r2 = 1 - sse / syy if syy > 0 else 0.0
    se = math.sqrt(sse / (n - 2) / sxx) if n > 2 and sse > 0 else 0.0
    t = slope / se if se > 0 else 0.0
    return Fit(intercept, slope, r2, t, n)


def idle_offset_c(hours: Sequence[HourPoint]) -> float | None:
    """How far above room temperature the flow pipe sits when not heating.

    Returns None when the flow never looks hot, e.g. a sensor on the wrong
    pipe or no heating season in the history yet.
    """
    if not hours:
        return None
    # Hours with no heating at all have a low peak; their peaks sit at the
    # bottom of the distribution (nights, summer). Capped so a history with
    # the heating on almost constantly can't push "idle" into heating range.
    idle = min(
        MAX_IDLE_OFFSET_C,
        max(0.0, _percentile([h.flow_max_c - h.indoor_c for h in hours], 0.1)),
    )
    hot = [h for h in hours if h.flow_max_c - h.indoor_c > idle + HOT_MARGIN_C]
    if len(hot) < MIN_HOT_HOURS:
        return None
    return idle


def heating_hours(hours: Sequence[HourPoint], idle_c: float) -> list[HeatingHour]:
    """Estimate, for each hour, how long the heating ran and the heat it gave."""
    result: list[HeatingHour] = []
    for h in hours:
        idle = h.indoor_c + idle_c
        span = h.flow_max_c - idle
        if span <= HOT_MARGIN_C:
            result.append(HeatingHour(h, 0.0, 0.0, None, None))
            continue
        fraction = min(1.0, max(0.0, (h.flow_mean_c - idle) / span))
        if fraction <= 0:
            result.append(HeatingHour(h, 0.0, 0.0, None, None))
            continue
        flow = h.flow_max_c
        ret = h.return_max_c
        if ret is not None and not (h.indoor_c < ret <= flow):
            ret = None  # a return hotter than the flow is a mislabelled pair
        mean_water = (flow + ret) / 2 if ret is not None else flow - ASSUMED_DROP_C / 2
        heat = fraction * max(0.0, mean_water - h.indoor_c) ** RADIATOR_EXPONENT
        result.append(HeatingHour(h, fraction, heat, flow, ret))
    return result


def summarise(rows: Sequence[HeatingHour], days: int = 1) -> PeriodSummary:
    """Per-day averages over a set of hours spanning ``days`` days."""
    on = sum(r.fraction for r in rows)
    with_return = [r for r in rows if r.fraction > 0 and r.return_c is not None]
    on_return = sum(r.fraction for r in with_return)
    return PeriodSummary(
        hours=on / days,
        heat=sum(r.heat for r in rows) / days,
        indoor_c=sum(r.point.indoor_c for r in rows) / len(rows),
        flow_c=(
            sum(r.fraction * r.flow_c for r in rows if r.flow_c is not None) / on
            if on > 0
            else None
        ),
        return_c=(
            sum(r.fraction * r.return_c for r in with_return) / on_return  # type: ignore[operator]
            if on_return > 0
            else None
        ),
        condensing_share=(
            sum(r.fraction for r in with_return if r.return_c < CONDENSING_RETURN_C)  # type: ignore[operator]
            / on_return
            if on_return > 0
            else None
        ),
    )


def daily_summaries(
    rows: Sequence[HeatingHour], local_day: Callable[[datetime], date]
) -> dict[date, PeriodSummary]:
    """Summaries for each day with enough hourly statistics."""
    by_day: dict[date, list[HeatingHour]] = defaultdict(list)
    for row in rows:
        by_day[local_day(row.point.start)].append(row)
    result: dict[date, PeriodSummary] = {}
    for day, day_rows in by_day.items():
        if len(day_rows) < MIN_HOURS_PER_DAY:
            continue
        # Scale for a missing hour or two so days stay comparable.
        summary = summarise(day_rows)
        scale = 24 / len(day_rows)
        result[day] = PeriodSummary(
            hours=summary.hours * scale,
            heat=summary.heat * scale,
            indoor_c=summary.indoor_c,
            flow_c=summary.flow_c,
            return_c=summary.return_c,
            condensing_share=summary.condensing_share,
        )
    return result


def weekly_summaries(daily: Mapping[date, PeriodSummary]) -> dict[date, PeriodSummary]:
    """Average-day summaries for complete Monday-to-Sunday weeks."""
    mondays = {day - timedelta(days=day.weekday()) for day in daily}
    result: dict[date, PeriodSummary] = {}
    for monday in sorted(mondays):
        week = [daily.get(monday + timedelta(days=offset)) for offset in range(7)]
        if any(day is None for day in week):
            continue
        days = [day for day in week if day is not None]
        on = sum(d.hours for d in days)

        def weighted(values: list[tuple[float, float | None]]) -> float | None:
            known = [(w, v) for w, v in values if v is not None and w > 0]
            total = sum(w for w, _ in known)
            return sum(w * v for w, v in known) / total if total > 0 else None

        result[monday] = PeriodSummary(
            hours=on / 7,
            heat=sum(d.heat for d in days) / 7,
            indoor_c=sum(d.indoor_c for d in days) / 7,
            flow_c=weighted([(d.hours, d.flow_c) for d in days]),
            return_c=weighted([(d.hours, d.return_c) for d in days]),
            condensing_share=weighted([(d.hours, d.condensing_share) for d in days]),
        )
    return result


def _normalise(values: Mapping[date, float]) -> dict[date, float]:
    """Scale so this house's median is 100."""
    if not values:
        return {}
    median = _percentile(list(values.values()), 0.5)
    if median <= 0:
        return {}
    return {key: 100 * value / median for key, value in values.items()}


def _trend(series: Mapping[date, float]) -> dict[str, float | int | None] | None:
    """Change per year and how the latest weeks compare with the rest."""
    if len(series) < MIN_TREND_WEEKS:
        return None
    ordered = sorted(series.items())
    first = ordered[0][0]
    fit = linear_fit([((day - first).days / 365.25, value) for day, value in ordered])
    recent = [value for _, value in ordered[-RECENT_WEEKS:]]
    return {
        "weeks": len(ordered),
        "latest_percent": round(sum(recent) / len(recent), 1),
        "change_per_year_percent": round(fit.slope, 1) if fit else None,
        "trend_t": round(fit.t, 2) if fit else None,
    }


def analyse_boiler(
    hours: Sequence[HourPoint],
    local_day: Callable[[datetime], date],
    weekly_litres_per_day: Mapping[date, float],
    outdoor_daily_c: Mapping[date, float],
    hot_water_litres_per_day: float | None = None,
    runtime_daily_hours: Mapping[date, float] | None = None,
) -> dict:
    """Everything the Boiler tab shows, as plain JSON-ready values.

    ``weekly_litres_per_day`` is measured oil use for clean weeks, keyed by
    the week's Monday. ``hot_water_litres_per_day`` is the forecast model's
    baseline, used when the flow data can't separate hot water itself.
    """
    has_return = any(h.return_mean_c is not None for h in hours)
    base: dict = {"has_return": has_return, "hours_analysed": len(hours)}
    idle = idle_offset_c(hours)
    if idle is None:
        return {
            **base,
            "status": "never_hot" if hours else "no_history",
            "message": (
                "No hourly flow-temperature history yet. Analysis starts once "
                "Home Assistant has recorded some heating."
                if not hours
                else "The flow temperature never rises far above room temperature in "
                "the history, so heating can't be detected. Check the sensor is on "
                "the boiler's flow pipe, or wait for the heating season."
            ),
        }

    rows = heating_hours(hours, idle)
    daily = daily_summaries(rows, local_day)
    weekly = weekly_summaries(daily)

    # Oil per heating hour (≈ the burner nozzle rate) and hot-water use.
    burn_points = [
        (weekly[monday].hours, litres)
        for monday, litres in weekly_litres_per_day.items()
        if monday in weekly
    ]
    burn_fit = linear_fit(burn_points)
    burn = None
    if burn_fit is not None and burn_fit.slope > 0 and burn_fit.n >= MIN_TREND_WEEKS:
        burn = {
            "litres_per_hour": round(burn_fit.slope, 2),
            "baseline_litres_per_day": round(max(0.0, burn_fit.intercept), 2),
            "r2": round(burn_fit.r2, 2),
            "weeks": burn_fit.n,
        }
    hot_water = (
        max(0.0, burn_fit.intercept)
        if burn is not None and burn_fit.r2 >= 0.5
        else hot_water_litres_per_day or 0.0
    )

    # Efficiency: heat delivered per litre burned for space heating.
    raw_efficiency: dict[date, float] = {}
    for monday, litres in weekly_litres_per_day.items():
        week = weekly.get(monday)
        if week is None or week.hours < MIN_HEATING_HOURS_PER_DAY:
            continue
        space = litres - hot_water
        if space > 0.2 and week.heat > 0:
            raw_efficiency[monday] = week.heat / space
    efficiency = _normalise(raw_efficiency)

    # House heat loss: heat delivered per degree of indoor–outdoor gap.
    raw_loss: dict[date, float] = {}
    for monday, week in weekly.items():
        outdoor = [outdoor_daily_c.get(monday + timedelta(days=o)) for o in range(7)]
        if any(value is None for value in outdoor) or week.hours < MIN_HEATING_HOURS_PER_DAY:
            continue
        gap = week.indoor_c - sum(outdoor) / 7  # type: ignore[arg-type]
        if gap > 3:
            raw_loss[monday] = week.heat / gap
    heat_loss = _normalise(raw_loss)

    # Oil cost of return temperature (condensing boilers).
    return_effect = None
    effect_points = [
        (weekly[monday].return_c, value)
        for monday, value in efficiency.items()
        if weekly[monday].return_c is not None
    ]
    if len(effect_points) >= MIN_EFFECT_WEEKS:
        fit = linear_fit(effect_points)  # type: ignore[arg-type]
        if fit is not None:
            return_effect = {
                "percent_per_c": round(fit.slope, 2),
                "t": round(fit.t, 2),
                "weeks": fit.n,
                # Only claim an effect that is clear and physically sensible.
                "significant": fit.slope < 0 and fit.t <= -2,
            }

    # Weather compensation: does flow temperature fall as it gets milder?
    comp_points = [
        (outdoor_daily_c[day], summary.flow_c)
        for day, summary in daily.items()
        if day in outdoor_daily_c
        and summary.flow_c is not None
        and summary.hours >= MIN_HEATING_HOURS_PER_DAY
    ]
    comp_fit = linear_fit(comp_points)  # type: ignore[arg-type]
    weather_comp = None
    if comp_fit is not None and comp_fit.n >= 10:
        weather_comp = {
            "slope": round(comp_fit.slope, 2),
            "intercept": round(comp_fit.intercept, 1),
            "r2": round(comp_fit.r2, 2),
            "days": comp_fit.n,
            "compensated": comp_fit.slope <= WEATHER_COMP_SLOPE and comp_fit.r2 >= 0.3,
            "points": [
                [round(x, 1), round(y, 1)] for x, y in sorted(comp_points)  # type: ignore[arg-type]
            ],
        }

    # How the flow-based hours compare with a runtime sensor, when there is one.
    runtime_check = None
    if runtime_daily_hours:
        pairs = [
            (daily[day].hours, runtime_daily_hours[day])
            for day in daily
            if day in runtime_daily_hours and runtime_daily_hours[day] > 0.5
        ]
        fit = linear_fit(pairs)
        if fit is not None and len(pairs) >= 14:
            ratio = _percentile([measured / max(0.1, derived) for derived, measured in pairs], 0.5)
            runtime_check = {
                "days": len(pairs),
                "r2": round(fit.r2, 2),
                "measured_per_derived": round(ratio, 2),
            }

    recent_days = sorted(daily)[-28:]
    recent = [daily[day] for day in recent_days]

    def recent_weighted(attr: str) -> float | None:
        known = [(d.hours, getattr(d, attr)) for d in recent if getattr(d, attr) is not None]
        total = sum(w for w, _ in known)
        return round(sum(w * v for w, v in known) / total, 1) if total > 0 else None

    flow_now = recent_weighted("flow_c")
    return_now = recent_weighted("return_c")
    condensing = recent_weighted("condensing_share")
    summary = {
        "days": len(recent),
        "hours_per_day": round(sum(d.hours for d in recent) / len(recent), 1) if recent else None,
        "flow_c": flow_now,
        "return_c": return_now,
        "drop_c": (
            round(flow_now - return_now, 1)
            if flow_now is not None and return_now is not None
            else None
        ),
        "condensing_share": condensing,
    }

    weeks = []
    for monday, week in sorted(weekly.items()):
        weeks.append(
            {
                "week_start": monday.isoformat(),
                "hours_per_day": round(week.hours, 2),
                "heat_index": round(week.heat, 1),
                "litres_per_day": (
                    round(weekly_litres_per_day[monday], 2)
                    if monday in weekly_litres_per_day
                    else None
                ),
                "efficiency": round(efficiency[monday], 1) if monday in efficiency else None,
                "heat_loss": round(heat_loss[monday], 1) if monday in heat_loss else None,
                "flow_c": None if week.flow_c is None else round(week.flow_c, 1),
                "return_c": None if week.return_c is None else round(week.return_c, 1),
                "condensing_share": (
                    None if week.condensing_share is None else round(week.condensing_share, 2)
                ),
                "indoor_c": round(week.indoor_c, 1),
            }
        )

    days = [
        {
            "date": day.isoformat(),
            "hours": round(daily[day].hours, 2),
            "flow_c": None if daily[day].flow_c is None else round(daily[day].flow_c, 1),
            "return_c": None if daily[day].return_c is None else round(daily[day].return_c, 1),
            "outdoor_c": (
                round(outdoor_daily_c[day], 1) if day in outdoor_daily_c else None
            ),
        }
        for day in sorted(daily)[-DAYS_SHOWN:]
    ]

    efficiency_trend = _trend(efficiency)
    alert = bool(
        efficiency_trend
        and efficiency_trend["weeks"] >= 10  # type: ignore[operator]
        and efficiency_trend["latest_percent"] < EFFICIENCY_ALERT_PERCENT  # type: ignore[operator]
    )
    return {
        **base,
        "status": "ok",
        "message": "",
        "idle_offset_c": round(idle, 1),
        "assumed_drop_c": None if has_return else ASSUMED_DROP_C,
        "summary": summary,
        "burn": burn,
        "efficiency": efficiency_trend,
        "efficiency_alert": alert,
        "heat_loss": _trend(heat_loss),
        "return_effect": return_effect,
        "weather_comp": weather_comp,
        "runtime_check": runtime_check,
        "weeks": weeks,
        "days": days,
        "weekly_hours": {
            monday.isoformat(): round(week.hours, 3) for monday, week in weekly.items()
        },
    }
