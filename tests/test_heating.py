"""Tests for the boiler flow/return analysis."""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import math
from runpy import run_path
import unittest

HEATING = run_path(
    str(Path(__file__).parents[1] / "custom_components" / "oil_predict" / "heating.py")
)
HourPoint = HEATING["HourPoint"]
idle_offset_c = HEATING["idle_offset_c"]
heating_hours = HEATING["heating_hours"]
daily_summaries = HEATING["daily_summaries"]
weekly_summaries = HEATING["weekly_summaries"]
analyse_boiler = HEATING["analyse_boiler"]
linear_fit = HEATING["linear_fit"]

START = datetime(2025, 10, 6, tzinfo=timezone.utc)  # a Monday


def local_day(moment: datetime) -> date:
    return moment.date()


def outdoor_for(day_index: int) -> float:
    """A cooling autumn into winter, with a weekly wobble."""
    return 12 - day_index * 0.12 + 2 * math.sin(day_index / 3)


def simulate(
    days: int,
    *,
    room: float = 20.0,
    idle_above_room: float = 2.0,
    with_return: bool = True,
    flow_for=lambda outdoor: 70.0,
    drop: float = 15.0,
    efficiency_for=lambda day: 1.0,
    litres_per_hour: float = 2.2,
    hot_water: float = 1.0,
):
    """Hourly flow statistics for a house whose heating runs in proportion to the gap.

    Returns the hours, daily outdoor temperatures and weekly litres per day.
    Oil per heat unit is divided by ``efficiency_for(day)``.
    """
    hours = []
    outdoor = {}
    litres_by_day = {}
    for d in range(days):
        out = outdoor_for(d)
        day = (START + timedelta(days=d)).date()
        outdoor[day] = out
        flow = flow_for(out)
        ret = flow - drop
        # Heating fraction each hour from the gap: about 1 at -5 °C outside.
        fraction = min(1.0, max(0.0, (room - out - 3) / 22))
        # The heating runs in one block from 06:00: full hours, then a part hour.
        remaining = 24 * fraction
        on_hours = 0.0
        idle = room + idle_above_room
        for h in range(24):
            share = min(1.0, max(0.0, remaining)) if h >= 6 else 0.0
            remaining -= share if h >= 6 else 0.0
            hot = share > 0
            hours.append(
                HourPoint(
                    START + timedelta(days=d, hours=h),
                    idle + share * (flow - idle),
                    flow if hot else idle + 0.5,
                    room,
                    (idle + share * (ret - idle)) if with_return else None,
                    (ret if hot else idle + 0.3) if with_return else None,
                )
            )
            on_hours += share
        litres_by_day[day] = hot_water + litres_per_hour * on_hours / efficiency_for(d)
    weekly = {}
    for d in range(0, days - 6, 7):
        monday = (START + timedelta(days=d)).date()
        weekly[monday] = sum(
            litres_by_day[monday + timedelta(days=o)] for o in range(7)
        ) / 7
    return hours, outdoor, weekly


class HeatingHoursTest(unittest.TestCase):
    def test_idle_needs_some_heating(self) -> None:
        cold = [HourPoint(START + timedelta(hours=h), 21.0, 22.0, 20.0) for h in range(200)]
        self.assertIsNone(idle_offset_c(cold))

    def test_fraction_follows_mean_between_idle_and_peak(self) -> None:
        hours, _, _ = simulate(10)
        idle = idle_offset_c(hours)
        self.assertAlmostEqual(idle, 2.0, delta=0.6)
        rows = heating_hours(hours, idle)
        # Day 0 at ~12 °C: fraction (20-12-3)/22 ≈ 0.23 → about 5.5 h.
        days = daily_summaries(rows, local_day)
        first = days[START.date()]
        self.assertAlmostEqual(first.hours, 24 * (20 - outdoor_for(0) - 3) / 22, delta=0.6)
        self.assertAlmostEqual(first.flow_c, 70.0, delta=0.01)
        self.assertAlmostEqual(first.return_c, 55.0, delta=0.01)

    def test_incomplete_days_and_weeks_are_left_out(self) -> None:
        hours, _, _ = simulate(10)
        hours = [h for h in hours if not (h.start.date() == START.date() and h.start.hour < 10)]
        rows = heating_hours(hours, 2.0)
        days = daily_summaries(rows, local_day)
        self.assertNotIn(START.date(), days)
        self.assertEqual(weekly_summaries(days), {})

    def test_return_hotter_than_flow_is_ignored(self) -> None:
        point = HourPoint(START, 60.0, 70.0, 20.0, 75.0, 80.0)
        row = heating_hours([point], 2.0)[0]
        self.assertIsNone(row.return_c)
        self.assertGreater(row.heat, 0)


class AnalysisTest(unittest.TestCase):
    def test_burn_rate_and_hot_water_recovered(self) -> None:
        hours, outdoor, weekly = simulate(120)
        result = analyse_boiler(hours, local_day, weekly, outdoor)
        self.assertEqual(result["status"], "ok")
        self.assertAlmostEqual(result["burn"]["litres_per_hour"], 2.2, delta=0.2)
        self.assertAlmostEqual(result["burn"]["baseline_litres_per_day"], 1.0, delta=0.4)
        self.assertGreater(result["burn"]["r2"], 0.95)

    def test_steady_boiler_has_steady_efficiency(self) -> None:
        hours, outdoor, weekly = simulate(120)
        result = analyse_boiler(hours, local_day, weekly, outdoor)
        self.assertAlmostEqual(result["efficiency"]["latest_percent"], 100, delta=4)
        self.assertFalse(result["efficiency_alert"])

    def test_falling_efficiency_raises_the_service_alert(self) -> None:
        hours, outdoor, weekly = simulate(
            140, efficiency_for=lambda day: 1.0 if day < 90 else 0.8
        )
        result = analyse_boiler(hours, local_day, weekly, outdoor, hot_water_litres_per_day=1.0)
        self.assertLess(result["efficiency"]["latest_percent"], 90)
        self.assertTrue(result["efficiency_alert"])

    def test_weather_compensation_detected(self) -> None:
        hours, outdoor, weekly = simulate(90, flow_for=lambda out: 55 - 1.0 * out)
        result = analyse_boiler(hours, local_day, weekly, outdoor)
        self.assertTrue(result["weather_comp"]["compensated"])
        self.assertAlmostEqual(result["weather_comp"]["slope"], -1.0, delta=0.05)

        hours, outdoor, weekly = simulate(90)
        fixed = analyse_boiler(hours, local_day, weekly, outdoor)
        self.assertFalse(fixed["weather_comp"]["compensated"])

    def test_condensing_share_and_drop(self) -> None:
        hours, outdoor, weekly = simulate(60, flow_for=lambda out: 60.0, drop=15.0)
        result = analyse_boiler(hours, local_day, weekly, outdoor)
        self.assertAlmostEqual(result["summary"]["drop_c"], 15.0, delta=0.1)
        self.assertEqual(result["summary"]["condensing_share"], 1.0)  # return 45 °C

        hours, outdoor, weekly = simulate(60, flow_for=lambda out: 75.0, drop=10.0)
        hot = analyse_boiler(hours, local_day, weekly, outdoor)
        self.assertEqual(hot["summary"]["condensing_share"], 0.0)  # return 65 °C

    def test_without_return_assumes_a_drop(self) -> None:
        hours, outdoor, weekly = simulate(60, with_return=False)
        result = analyse_boiler(hours, local_day, weekly, outdoor)
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["has_return"])
        self.assertIsNone(result["summary"]["condensing_share"])
        self.assertIsNotNone(result["assumed_drop_c"])

    def test_weekly_hours_line_up_with_weeks(self) -> None:
        hours, outdoor, weekly = simulate(56)
        result = analyse_boiler(hours, local_day, weekly, outdoor)
        self.assertEqual(set(result["weekly_hours"]), {d.isoformat() for d in weekly})

    def test_runtime_sensor_comparison(self) -> None:
        hours, outdoor, weekly = simulate(40)
        days = daily_summaries(heating_hours(hours, idle_offset_c(hours)), local_day)
        runtime = {day: summary.hours * 1.1 for day, summary in days.items()}
        result = analyse_boiler(hours, local_day, weekly, outdoor, runtime_daily_hours=runtime)
        self.assertAlmostEqual(result["runtime_check"]["measured_per_derived"], 1.1, delta=0.02)

    def test_no_history(self) -> None:
        result = analyse_boiler([], local_day, {}, {})
        self.assertEqual(result["status"], "no_history")


class FitTest(unittest.TestCase):
    def test_linear_fit(self) -> None:
        fit = linear_fit([(x, 2 + 3 * x + (0.1 if x % 2 else -0.1)) for x in range(10)])
        self.assertAlmostEqual(fit.slope, 3, delta=0.05)
        self.assertGreater(fit.r2, 0.99)
        self.assertGreater(fit.t, 10)
        self.assertIsNone(linear_fit([(1, 1), (1, 2), (1, 3)]))


if __name__ == "__main__":
    unittest.main()
