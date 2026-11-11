"""Tests for the pure oil tank calculations."""

from pathlib import Path
from runpy import run_path
from datetime import date, timedelta
import unittest

MODEL_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "oil_predict"
    / "model.py"
)
MODEL = run_path(str(MODEL_PATH))
depth_to_litres = MODEL["depth_to_litres"]
DailyTrainingSample = MODEL["DailyTrainingSample"]
DailyHistoryPoint = MODEL["DailyHistoryPoint"]
build_training_samples = MODEL["build_training_samples"]
fit_usage_model = MODEL["fit_usage_model"]
forecast_week = MODEL["forecast_week"]
parse_sample_timestamp = MODEL["parse_sample_timestamp"]
seasonal_outdoor_c = MODEL["seasonal_outdoor_c"]
days_until_level = MODEL["days_until_level"]


def make_training_samples() -> list[object]:
    """Create deterministic sample days from a known synthetic relationship."""
    samples = []
    for index in range(28):
        outdoor = 2.0 + (index % 14)
        indoor = 20.0
        heating_hours = 0.5 + 0.4 * max(0.0, indoor - outdoor)
        litres_used = 0.8 + 2.0 * heating_hours
        samples.append(
            DailyTrainingSample(
                outdoor_mean_c=outdoor,
                indoor_mean_c=indoor,
                heating_hours=heating_hours,
                litres_used=litres_used,
            )
        )
    return samples


class TestDepthToLitres(unittest.TestCase):
    """Verify the calibrated tank-volume conversion."""

    def test_calibration_endpoints(self) -> None:
        """The calibration endpoints represent full and empty."""
        self.assertEqual(depth_to_litres(9, 1000), 1000)
        self.assertEqual(depth_to_litres(126, 1000), 0)

    def test_interpolates_between_calibration_points(self) -> None:
        """Intermediate depth values use linear interpolation."""
        self.assertAlmostEqual(depth_to_litres(54, 1000), 607.142857, places=6)

    def test_depth_outside_calibration_range_is_clamped(self) -> None:
        """Valid depths beyond the table are clamped to full or empty."""
        self.assertEqual(depth_to_litres(5, 1000), 1000)
        self.assertEqual(depth_to_litres(130, 1000), 0)

    def test_invalid_depth_is_rejected(self) -> None:
        """Invalid depth values must not produce tank volume estimates."""
        for depth in (0, -1, float("nan"), float("inf")):
            with self.subTest(depth=depth), self.assertRaises(ValueError):
                depth_to_litres(depth, 1000)

    def test_sample_timestamp_formats(self) -> None:
        """ISO, seconds, and millisecond sample timestamps parse to UTC."""
        iso = parse_sample_timestamp("2026-11-10T10:00:00+00:00")
        seconds = parse_sample_timestamp("1794304800")
        milliseconds = parse_sample_timestamp("1794304800000")
        self.assertEqual(iso.isoformat(), "2026-11-10T10:00:00+00:00")
        self.assertEqual(seconds, milliseconds)

    def test_invalid_sample_timestamp_is_rejected(self) -> None:
        """Unparseable values cannot be treated as fresh tank samples."""
        self.assertIsNone(parse_sample_timestamp("not-a-timestamp"))


class TestUsageForecast(unittest.TestCase):
    """Verify learned usage rises with colder forecast conditions."""

    def test_fit_recovers_synthetic_rates(self) -> None:
        """The two-stage fit recovers the known training relationships."""
        model = fit_usage_model(make_training_samples())
        self.assertAlmostEqual(model.runtime_intercept_hours, 0.5)
        self.assertAlmostEqual(model.runtime_hours_per_degree, 0.4)
        self.assertAlmostEqual(model.oil_baseline_litres_per_day, 0.8)
        self.assertAlmostEqual(model.oil_litres_per_heating_hour, 2.0)
        self.assertEqual(model.sample_count, 28)

    def test_colder_week_predicts_higher_oil_use(self) -> None:
        """Daily forecast temperatures drive a higher estimate for colder weather."""
        model = fit_usage_model(make_training_samples())
        cold = forecast_week(model, [0.0] * 7, indoor_target_c=20.0)
        mild = forecast_week(model, [10.0] * 7, indoor_target_c=20.0)
        self.assertGreater(cold.total_litres, mild.total_litres)
        self.assertTrue(all(day.predicted_litres > 0 for day in cold.days))
        self.assertEqual(len(cold.days), 7)

    def test_fit_finds_heating_switch_off(self) -> None:
        """Warm weeks use only hot water; the fit finds where heating stops."""
        samples = []
        for index in range(30):
            outdoor = -2.0 + index * 0.75  # -2 to about 20 degrees
            indoor = 20.0
            gap = max(0.0, indoor - outdoor)
            heating_hours = 0.6 * max(0.0, gap - 5.0)  # off above 15 degrees
            samples.append(
                DailyTrainingSample(
                    outdoor_mean_c=outdoor,
                    indoor_mean_c=indoor,
                    heating_hours=heating_hours,
                    litres_used=1.2 + 1.5 * heating_hours,
                )
            )
        model = fit_usage_model(samples)
        self.assertAlmostEqual(model.runtime_switch_off_gap_c, 5.0)
        self.assertAlmostEqual(model.runtime_hours_per_degree, 0.6)
        self.assertAlmostEqual(model.runtime_intercept_hours, 0.0, places=6)

        warm = forecast_week(model, [17.0] * 7, indoor_target_c=20.0)
        warmer = forecast_week(model, [22.0] * 7, indoor_target_c=20.0)
        # Above the switch-off temperature only hot-water use remains.
        self.assertAlmostEqual(warm.total_litres, 1.2 * 7, places=4)
        self.assertAlmostEqual(warmer.total_litres, warm.total_litres, places=6)
        cold = forecast_week(model, [5.0] * 7, indoor_target_c=20.0)
        self.assertAlmostEqual(cold.total_litres, 7 * (1.2 + 1.5 * 0.6 * 10), places=4)

    def test_rejects_insufficient_training_history(self) -> None:
        """Do not fit a personalized model from too few noisy daily samples."""
        with self.assertRaisesRegex(ValueError, "14 clean samples"):
            fit_usage_model(make_training_samples()[:13])

    def test_rejects_invalid_forecast_length(self) -> None:
        """A weekly projection must receive seven daily forecast values."""
        model = fit_usage_model(make_training_samples())
        with self.assertRaisesRegex(ValueError, "Exactly seven"):
            forecast_week(model, [5.0] * 6, indoor_target_c=20.0)

    def test_builds_weekly_samples_and_skips_refill_week(self) -> None:
        """Aggregate daily stats into weeks and exclude a refill discontinuity."""
        first_monday = date(2026, 1, 5)
        points = []
        for offset in range(-1, 21):
            if offset < 7:
                depth = 40 + offset
            elif offset < 14:
                depth = 47 + offset - 7 if offset < 10 else 20 + offset - 10
            else:
                depth = 24 + offset - 14
            points.append(
                DailyHistoryPoint(
                    day=first_monday + timedelta(days=offset),
                    depth_mean_cm=float(depth),
                    outdoor_mean_c=5.0,
                    indoor_mean_c=19.0,
                    runtime_max_hours=4.0,
                )
            )

        samples = build_training_samples(points, 1000)
        self.assertEqual(len(samples), 2)
        self.assertGreater(samples[0].litres_used, 0)
        self.assertAlmostEqual(samples[0].heating_hours, 4.0)


class TestRunOutProjection(unittest.TestCase):
    """Verify how long the remaining oil is projected to last."""

    def test_days_until_level_interpolates_within_a_day(self) -> None:
        """600 L down to 250 L at 10 L/day takes 35 days."""
        self.assertAlmostEqual(days_until_level(600, 250, [10.0] * 100), 35.0)
        self.assertAlmostEqual(days_until_level(605, 250, [10.0] * 100), 35.5)

    def test_rising_usage_shortens_the_projection(self) -> None:
        """Colder days later on use oil faster than a flat projection."""
        flat = days_until_level(600, 50, [10.0] * 200)
        rising = days_until_level(600, 50, [10.0 + day * 0.2 for day in range(200)])
        self.assertLess(rising, flat)

    def test_already_below_level_and_beyond_horizon(self) -> None:
        """At or below the level is zero days; never reaching it is None."""
        self.assertEqual(days_until_level(200, 250, [10.0] * 10), 0.0)
        self.assertIsNone(days_until_level(600, 50, [1.0] * 30))

    def test_seasonal_temperature_uses_last_year(self) -> None:
        """A future day's typical temperature comes from the same dates last year."""
        history = {
            date(2025, 12, 1) + timedelta(days=offset): float(offset)
            for offset in range(-3, 4)
        }
        self.assertAlmostEqual(seasonal_outdoor_c(history, date(2026, 12, 1)), 0.0)
        self.assertIsNone(seasonal_outdoor_c(history, date(2026, 6, 1)))
