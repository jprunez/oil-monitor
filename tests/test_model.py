"""Tests for the pure oil tank calculations."""

from datetime import date, timedelta
from pathlib import Path
from runpy import run_path
import unittest

MODEL_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "oil_predict"
    / "model.py"
)
MODEL = run_path(str(MODEL_PATH))
depth_to_litres = MODEL["depth_to_litres"]
reading_to_litres = MODEL["reading_to_litres"]
horizontal_cylinder_fraction = MODEL["horizontal_cylinder_fraction"]
parse_calibration = MODEL["parse_calibration"]
format_calibration = MODEL["format_calibration"]
TankConfig = MODEL["TankConfig"]
DEPTH_TO_PERCENT = MODEL["DEPTH_TO_PERCENT"]
DailyTrainingSample = MODEL["DailyTrainingSample"]
DailyHistoryPoint = MODEL["DailyHistoryPoint"]
build_training_samples = MODEL["build_training_samples"]
classify_weeks = MODEL["classify_weeks"]
fit_usage_model = MODEL["fit_usage_model"]
forecast_week = MODEL["forecast_week"]
parse_sample_timestamp = MODEL["parse_sample_timestamp"]
seasonal_outdoor_c = MODEL["seasonal_outdoor_c"]
days_until_level = MODEL["days_until_level"]
MIN_TRAINING_WEEKS = MODEL["MIN_TRAINING_WEEKS"]
estimate_temperature_drift = MODEL["estimate_temperature_drift"]
climate_offset = MODEL["climate_offset"]
historical_year_temperatures = MODEL["historical_year_temperatures"]
pick_scenarios = MODEL["pick_scenarios"]
backtest_weekly = MODEL["backtest_weekly"]
fit_weather_terms = MODEL["fit_weather_terms"]
weather_terms_help = MODEL["weather_terms_help"]
temperature_corrected_distance = MODEL["temperature_corrected_distance"]


def make_training_samples(with_runtime: bool = True) -> list[object]:
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
                heating_hours=heating_hours if with_runtime else None,
                litres_used=litres_used,
            )
        )
    return samples


class TestDepthToLitres(unittest.TestCase):
    """Verify the prototype tank's calibrated volume conversion."""

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


class TestTankTypes(unittest.TestCase):
    """Verify readings from different sensors and tank shapes."""

    def test_default_tank_matches_prototype_table(self) -> None:
        """The default configuration reproduces the original calibration."""
        tank = TankConfig(capacity_litres=1000)
        for depth in (9, 30, 54, 66, 100, 126):
            self.assertAlmostEqual(
                reading_to_litres(depth, tank), depth_to_litres(depth, 1000)
            )

    def test_percent_and_litre_sensors(self) -> None:
        """Percent readings scale by capacity; litre readings pass through."""
        percent = TankConfig(capacity_litres=1200, level_type="percent")
        litres = TankConfig(capacity_litres=1200, level_type="litres")
        self.assertAlmostEqual(reading_to_litres(25, percent), 300)
        self.assertAlmostEqual(reading_to_litres(100, percent), 1200)
        self.assertAlmostEqual(reading_to_litres(642.5, litres), 642.5)
        with self.assertRaises(ValueError):
            reading_to_litres(-5, percent)
        with self.assertRaises(ValueError):
            reading_to_litres(-1, litres)

    def test_vertical_tank_is_linear_in_height(self) -> None:
        """An upright tank's volume follows the oil height."""
        tank = TankConfig(
            capacity_litres=1000,
            shape="vertical",
            full_distance_cm=10,
            empty_distance_cm=110,
        )
        self.assertAlmostEqual(reading_to_litres(10, tank), 1000)
        self.assertAlmostEqual(reading_to_litres(60, tank), 500)
        self.assertAlmostEqual(reading_to_litres(110, tank), 0)
        self.assertAlmostEqual(reading_to_litres(200, tank), 0)

    def test_horizontal_cylinder_volume(self) -> None:
        """Half height is half full; a quarter height holds about 19.6 %."""
        self.assertAlmostEqual(horizontal_cylinder_fraction(0.5), 0.5)
        self.assertAlmostEqual(horizontal_cylinder_fraction(0.25), 0.1955, places=4)
        tank = TankConfig(
            capacity_litres=2000,
            shape="horizontal_cylinder",
            full_distance_cm=5,
            empty_distance_cm=105,
        )
        self.assertAlmostEqual(reading_to_litres(55, tank), 1000)
        self.assertAlmostEqual(reading_to_litres(80, tank), 2000 * 0.19550, delta=0.5)

    def test_calibration_text_round_trip(self) -> None:
        """Calibration tables parse from and format to editable text."""
        text = format_calibration(DEPTH_TO_PERCENT)
        self.assertEqual(parse_calibration(text), DEPTH_TO_PERCENT)
        self.assertEqual(
            parse_calibration("120:0\n10:100; 65:50"),
            ((10.0, 100.0), (65.0, 50.0), (120.0, 0.0)),
        )

    def test_bad_calibration_text_is_rejected(self) -> None:
        """Unusable tables raise a clear error."""
        for text in ("", "10:100", "10-100, 20:0", "10:120, 20:0", "10:100, 10:0"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_calibration(text)


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
        self.assertTrue(model.uses_runtime)

    def test_fit_without_runtime_sensor(self) -> None:
        """Without runtime data, oil use is fitted straight from the gap."""
        model = fit_usage_model(make_training_samples(with_runtime=False))
        self.assertFalse(model.uses_runtime)
        # litres = 0.8 + 2.0 * (0.5 + 0.4 * gap) = 1.8 + 0.8 * gap
        self.assertAlmostEqual(model.oil_baseline_litres_per_day, 1.8)
        self.assertAlmostEqual(model.oil_litres_per_heating_hour, 0.8)
        cold = forecast_week(model, [0.0] * 7, indoor_target_c=20.0)
        self.assertAlmostEqual(cold.total_litres, 7 * (1.8 + 0.8 * 20), places=6)

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

    def test_colder_week_predicts_higher_oil_use(self) -> None:
        """Daily forecast temperatures drive a higher estimate for colder weather."""
        model = fit_usage_model(make_training_samples())
        cold = forecast_week(model, [0.0] * 7, indoor_target_c=20.0)
        mild = forecast_week(model, [10.0] * 7, indoor_target_c=20.0)
        self.assertGreater(cold.total_litres, mild.total_litres)
        self.assertTrue(all(day.predicted_litres > 0 for day in cold.days))
        self.assertEqual(len(cold.days), 7)
        self.assertFalse(cold.early)

    def test_early_estimate_is_flagged_and_wider(self) -> None:
        """With few weeks of history the estimate is flagged and its range widened."""
        noisy = [
            DailyTrainingSample(
                outdoor_mean_c=s.outdoor_mean_c,
                indoor_mean_c=s.indoor_mean_c,
                heating_hours=s.heating_hours,
                litres_used=s.litres_used + (0.3 if i % 2 else -0.3),
            )
            for i, s in enumerate(make_training_samples())
        ]
        early_model = fit_usage_model(noisy[:MIN_TRAINING_WEEKS])
        early = forecast_week(early_model, [5.0] * 7, indoor_target_c=20.0)
        self.assertTrue(early.early)
        unwidened = 1.96 * early_model.oil_rmse_litres * 7**0.5
        self.assertGreater(early.upper_litres - early.total_litres, unwidened)

    def test_rejects_insufficient_training_history(self) -> None:
        """Do not fit a personalized model from too few noisy samples."""
        with self.assertRaisesRegex(ValueError, f"{MIN_TRAINING_WEEKS} clean samples"):
            fit_usage_model(make_training_samples()[: MIN_TRAINING_WEEKS - 1])

    def test_rejects_invalid_forecast_length(self) -> None:
        """A weekly projection must receive seven daily forecast values."""
        model = fit_usage_model(make_training_samples())
        with self.assertRaisesRegex(ValueError, "Exactly seven"):
            forecast_week(model, [5.0] * 6, indoor_target_c=20.0)

    def test_builds_weekly_samples_and_skips_refill_week(self) -> None:
        """Aggregate daily stats into weeks and exclude a refill discontinuity."""
        first_monday = date(2026, 1, 5)
        points = []
        litres = 700.0
        for offset in range(-1, 21):
            if offset == 9:
                litres += 400  # delivery in the second week
            else:
                litres -= 10
            points.append(
                DailyHistoryPoint(
                    day=first_monday + timedelta(days=offset),
                    litres_mean=litres,
                    outdoor_mean_c=5.0,
                    indoor_mean_c=19.0,
                    runtime_max_hours=4.0,
                )
            )

        samples = build_training_samples(points, 1000)
        self.assertEqual(len(samples), 2)
        self.assertAlmostEqual(samples[0].litres_used, 10.0)
        self.assertAlmostEqual(samples[0].heating_hours, 4.0)

    def test_weekly_samples_without_runtime(self) -> None:
        """Weeks without runtime data still train, with heating hours unknown."""
        first_monday = date(2026, 1, 5)
        points = [
            DailyHistoryPoint(
                day=first_monday + timedelta(days=offset),
                litres_mean=800.0 - 12 * offset,
                outdoor_mean_c=3.0,
                indoor_mean_c=20.0,
            )
            for offset in range(-1, 7)
        ]
        samples = build_training_samples(points, 1000)
        self.assertEqual(len(samples), 1)
        self.assertIsNone(samples[0].heating_hours)
        self.assertAlmostEqual(samples[0].litres_used, 12.0)


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



def make_drift_days(drift_per_c: float, days: int = 200) -> list[tuple]:
    """Daily (day, measured distance, air temperature) with known sensor drift."""
    import math
    import random

    rng = random.Random(7)
    out = []
    true_distance = 30.0
    for index in range(days):
        temp = 8 + 6 * math.sin(index / 5.0) + rng.uniform(-3, 3)
        # Colder days burn more oil, so the true distance grows faster.
        true_distance += max(0.0, 0.05 + 0.02 * (18 - temp)) / 3
        measured = true_distance * (1 - drift_per_c * (temp - 10)) + rng.uniform(-0.05, 0.05)
        out.append((date(2026, 1, 1) + timedelta(days=index), measured, temp))
    return out


class TestTemperatureDrift(unittest.TestCase):
    """Verify detection and correction of ultrasonic temperature drift."""

    def test_detects_uncompensated_sensor(self) -> None:
        """A sensor drifting 0.17 %/°C is found and flagged for correction."""
        estimate = estimate_temperature_drift(make_drift_days(0.0017))
        self.assertAlmostEqual(estimate.coefficient_per_c, 0.0017, delta=0.0003)
        self.assertTrue(estimate.needs_correction)

    def test_leaves_compensated_sensor_alone(self) -> None:
        """A sensor that already compensates shows no drift, so no correction."""
        estimate = estimate_temperature_drift(make_drift_days(0.0))
        self.assertFalse(estimate.needs_correction)

    def test_needs_enough_history(self) -> None:
        """Too few days gives no estimate."""
        self.assertIsNone(estimate_temperature_drift(make_drift_days(0.0017, days=10)))

    def test_correction_removes_the_drift(self) -> None:
        """Correcting to a reference temperature undoes the cold-air error."""
        true_distance = 60.0
        cold_reading = true_distance / temperature_corrected_distance(1.0, 0.0, 15.0)
        self.assertGreater(cold_reading, true_distance + 1.0)  # reads over 1 cm long
        self.assertAlmostEqual(
            temperature_corrected_distance(cold_reading, 0.0, 15.0), true_distance
        )
        self.assertAlmostEqual(temperature_corrected_distance(60.0, 15.0, 15.0), 60.0)



class TestWinterScenarios(unittest.TestCase):
    """Verify mild/typical/cold scenarios from long-term weather history."""

    def _archive(self) -> dict:
        # Ten years; year k is uniformly (k - 4.5) degrees off a 5 degree winter.
        archive = {}
        day = date(2016, 1, 1)
        while day < date(2026, 1, 1):
            archive[day] = 5.0 + (day.year - 2016 - 4.5)
            day += timedelta(days=1)
        return archive

    def test_offset_matches_archive_to_local_sensor(self) -> None:
        """The archive is shifted to agree with the user's own sensor."""
        archive = self._archive()
        local = {day: temp + 1.5 for day, temp in archive.items() if day.year == 2025}
        self.assertAlmostEqual(climate_offset(local, archive), 1.5)
        self.assertEqual(climate_offset({date(2025, 1, 1): 9.0}, archive), 0.0)

    def test_one_temperature_series_per_past_year(self) -> None:
        """Each complete past year contributes the same calendar days."""
        years = historical_year_temperatures(self._archive(), date(2026, 1, 10), 60)
        self.assertEqual(len(years), 10)
        self.assertTrue(all(len(year) == 60 for year in years))
        self.assertEqual(years[0][0], 5.0 + 4.5)  # one year back is 2025
        self.assertEqual(years[-1][0], 5.0 - 4.5)  # ten years back is 2016

    def test_incomplete_years_are_left_out(self) -> None:
        """A year with a long gap is not used."""
        archive = self._archive()
        for offset in range(30):
            archive.pop(date(2020, 1, 15) + timedelta(days=offset))
        years = historical_year_temperatures(archive, date(2026, 1, 10), 60)
        self.assertEqual(len(years), 9)

    def test_scenarios_order_mild_typical_cold(self) -> None:
        """Cold uses more than typical, which uses more than mild."""
        yearly = [[10.0 + year] * 200 for year in range(10)]
        typical, mild, cold = pick_scenarios(yearly)
        self.assertAlmostEqual(typical[0], 14.5)
        self.assertEqual(mild[0], 11.0)  # 10th percentile year
        self.assertEqual(cold[0], 18.0)  # 90th percentile year
        self.assertIsNone(pick_scenarios(yearly[:4]))



class TestBacktest(unittest.TestCase):
    """Verify how past weekly forecasts are scored."""

    def test_perfect_relationship_scores_near_zero(self) -> None:
        """Noise-free history is predicted almost exactly."""
        result = backtest_weekly(make_training_samples())
        self.assertEqual(result.weeks, 28)
        self.assertLess(result.typical_error_litres, 1e-6)
        self.assertLess(result.rmse_litres, 1e-6)

    def test_noisy_history_scores_its_noise(self) -> None:
        """Weeks that vary by about 0.5 L/day score roughly 3.5 L/week off."""
        noisy = [
            DailyTrainingSample(
                outdoor_mean_c=s.outdoor_mean_c,
                indoor_mean_c=s.indoor_mean_c,
                heating_hours=s.heating_hours,
                litres_used=s.litres_used + (0.5 if (i * 7) % 3 else -0.5),
            )
            for i, s in enumerate(make_training_samples())
        ]
        result = backtest_weekly(noisy)
        self.assertGreater(result.typical_error_litres, 2.0)
        self.assertLess(result.typical_error_litres, 6.0)
        self.assertGreater(result.mean_weekly_use_litres, 50)
        # The forecast range follows the measured weekly error.
        model = fit_usage_model(noisy)
        scored = forecast_week(model, [5.0] * 7, 20.0, weekly_error_rmse=result.rmse_litres)
        self.assertAlmostEqual(
            scored.upper_litres - scored.total_litres, 1.96 * result.rmse_litres
        )

    def test_needs_one_week_more_than_the_minimum(self) -> None:
        """With only the minimum weeks there is nothing to hold out."""
        self.assertIsNone(backtest_weekly(make_training_samples()[:MIN_TRAINING_WEEKS]))
        self.assertIsNotNone(backtest_weekly(make_training_samples()[: MIN_TRAINING_WEEKS + 1]))



def make_weather_samples(wind_effect: float, sun_effect: float) -> list[object]:
    """Weekly samples whose oil use also depends on wind and sun while heating."""
    import random

    rng = random.Random(3)
    samples = []
    for index in range(40):
        outdoor = 1.0 + (index * 5) % 17
        wind = 6 + rng.uniform(0, 16)
        sun = 1 + rng.uniform(0, 9)
        gap = 20.0 - outdoor
        degrees = max(0.0, gap - 4.0)  # heating off above 16 degrees
        litres = 1.5 + 0.9 * degrees
        litres += degrees * (wind_effect * (wind - 14) + sun_effect * (sun - 5.5))
        litres += rng.uniform(-0.15, 0.15)
        samples.append(
            DailyTrainingSample(
                outdoor_mean_c=outdoor,
                indoor_mean_c=20.0,
                heating_hours=None,
                litres_used=max(0.0, litres),
                wind_kmh=wind,
                sun_mj=sun,
            )
        )
    return samples


class TestWindAndSun(unittest.TestCase):
    """Verify wind and sun effects are learned only when they are real."""

    def test_recovers_wind_and_sun_effects(self) -> None:
        """Known effects are recovered with the right signs and sizes."""
        samples = make_weather_samples(wind_effect=0.012, sun_effect=-0.02)
        model = fit_usage_model(samples)
        terms = fit_weather_terms(model, samples)
        self.assertAlmostEqual(terms.wind_litres_per_degree_kmh, 0.012, delta=0.003)
        self.assertAlmostEqual(terms.sun_litres_per_degree_mj, -0.02, delta=0.005)
        base = backtest_weekly(samples)
        with_weather = backtest_weekly(samples, use_weather=True)
        self.assertLess(with_weather.rmse_litres, base.rmse_litres * 0.6)
        self.assertTrue(weather_terms_help(base, with_weather))

    def test_no_effect_means_terms_stay_off(self) -> None:
        """When wind and sun make no difference, they fail the improvement test."""
        samples = make_weather_samples(wind_effect=0.0, sun_effect=0.0)
        self.assertFalse(
            weather_terms_help(backtest_weekly(samples), backtest_weekly(samples, use_weather=True))
        )

    def test_no_adjustment_when_heating_is_off(self) -> None:
        """Wind and sun change nothing on days too warm for heating."""
        samples = make_weather_samples(wind_effect=0.012, sun_effect=-0.02)
        model = fit_usage_model(samples)
        terms = fit_weather_terms(model, samples)
        self.assertEqual(terms.adjustment(model, 19.0, 20.0, 40.0, 0.0), 0.0)
        self.assertGreater(terms.adjustment(model, 2.0, 20.0, 30.0, 5.5), 0.0)
        self.assertEqual(terms.adjustment(model, 2.0, 20.0, None, None), 0.0)

    def test_needs_enough_weeks_with_weather(self) -> None:
        """Too few weeks with wind and sun data gives no terms."""
        samples = make_weather_samples(0.012, -0.02)
        model = fit_usage_model(samples)
        self.assertIsNone(fit_weather_terms(model, samples[:8]))



class TestWeekReports(unittest.TestCase):
    """Verify each week is labelled with why it was or wasn't used."""

    def _points(self, litres_by_offset, skip=()):
        monday = date(2026, 1, 5)
        return [
            DailyHistoryPoint(
                day=monday + timedelta(days=offset),
                litres_mean=litres,
                outdoor_mean_c=5.0,
                indoor_mean_c=19.0,
            )
            for offset, litres in litres_by_offset.items()
            if offset not in skip
        ]

    def test_statuses_for_each_kind_of_week(self) -> None:
        """Used, missing-day, delivery, implausible and invalid weeks are told apart."""
        litres = {}
        level = 900.0
        for offset in range(-1, 35):
            if offset == 16:
                level += 400  # delivery in week 3
            elif offset == 23:
                level -= 400  # sensor jump in week 4
            else:
                level -= 8
            litres[offset] = level
        litres[30] = float("nan")  # bad value in week 5
        weeks = classify_weeks(self._points(litres, skip={9}), 1000)
        statuses = [report.status for report, _ in weeks]
        # The history starts on a Sunday, so the week before the first Monday
        # is incomplete and reported as missing days.
        self.assertEqual(
            statuses,
            ["missing_days", "used", "missing_days", "refill", "implausible_use", "invalid"],
        )
        self.assertEqual(weeks[1][0].week_start, date(2026, 1, 5))
        self.assertAlmostEqual(weeks[1][0].litres_used, 56.0)
        self.assertIsNotNone(weeks[1][1])
        self.assertTrue(all(sample is None for i, (_, sample) in enumerate(weeks) if i != 1))

    def test_matches_build_training_samples(self) -> None:
        """The samples are exactly those from the weeks marked used."""
        litres = {offset: 900.0 - 8 * offset for offset in range(-1, 28)}
        points = self._points(litres, skip={12})
        used = [sample for report, sample in classify_weeks(points, 1000) if sample]
        self.assertEqual(used, build_training_samples(points, 1000))
        self.assertEqual(len(used), 3)


if __name__ == "__main__":
    unittest.main()


def test_time_weighted_average_counts_night_setback() -> None:
    from datetime import datetime, timedelta, timezone

    from custom_components.oil_predict.model import time_weighted_average

    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    samples = []
    for day in range(2):
        base = start + timedelta(days=day)
        samples.append((base, 7.0))  # midnight-06:00 setback
        samples.append((base + timedelta(hours=6), 20.0))  # 06:00-24:00
    average = time_weighted_average(samples, start + timedelta(days=2), min_seconds=86400)
    assert average == (6 * 7.0 + 18 * 20.0) / 24
    # Unknown spans (thermostat off) are skipped, and too little data gives None.
    assert time_weighted_average([(start, None), (start + timedelta(hours=1), 18.0)], start + timedelta(hours=2)) == 18.0
    assert time_weighted_average([(start, 20.0)], start + timedelta(hours=2), min_seconds=86400) is None


def test_active_setpoints_ignore_night_setback() -> None:
    from datetime import datetime, timedelta, timezone

    from custom_components.oil_predict.model import active_setpoints, time_weighted_average

    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    samples = []
    for day in range(2):
        base = start + timedelta(days=day)
        samples.append((base, 7.0))  # overnight setback
        samples.append((base + timedelta(hours=6), 20.0))
        samples.append((base + timedelta(hours=18), 19.0))  # evening, still heating
    active = active_setpoints(samples, 3.0)
    assert [value for _, value in active] == [None, 20.0, 19.0, None, 20.0, 19.0]
    average = time_weighted_average(active, start + timedelta(days=2))
    assert average == (12 * 20.0 + 6 * 19.0) / 18
