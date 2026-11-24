"""Tests for the room-by-room heating analysis."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from runpy import run_path
import unittest

ROOMS = run_path(
    str(Path(__file__).parents[1] / "custom_components" / "oil_predict" / "rooms.py")
)
analyse_rooms = ROOMS["analyse_rooms"]
firing_mask = ROOMS["firing_mask"]
burner_cycles = ROOMS["burner_cycles"]

START = datetime(2026, 11, 14, tzinfo=timezone.utc)
MINUTE = timedelta(minutes=1)


def simulate(days=3, burn_on=40, burn_off=20):
    """Heating 06-22 each day, cycling burn_on/burn_off minutes; off overnight.

    Rooms: k is heat loss per hour, gain is °C per hour of warm-up while firing.
    """
    rooms = {
        "sensor.lounge": ("Lounge", 0.03, 1.2),
        "sensor.kitchen": ("Kitchen", 0.03, 1.1),
        "sensor.bedroom": ("Bedroom", 0.03, 1.2),
        "sensor.hall": ("Hall", 0.09, 1.3),  # draughty
        "sensor.study": ("Study", 0.03, 0.4),  # weak radiator
    }
    outdoor = 5.0
    temps = {e: 19.0 for e in rooms}
    series = {e: [] for e in rooms}
    flow, out = [], []
    flow_c = 25.0
    t = START
    end = START + timedelta(days=days)
    while t < end:
        hour = t.hour + t.minute / 60
        schedule = 6 <= hour < 22
        minute_of_cycle = int((t - START).total_seconds() // 60) % (burn_on + burn_off)
        firing = schedule and minute_of_cycle < burn_on
        flow_c += ((70 if firing else 22) - flow_c) * 0.2
        for e, (_, k, gain) in rooms.items():
            temp = temps[e]
            temp -= k * (temp - outdoor) / 60
            if firing and temp < 21.5:
                temp += gain / 60 + k * (temp - outdoor) / 60
            temps[e] = temp
            if t.minute % 5 == 0:
                series[e].append((t, round(temp, 2)))
        if t.minute % 5 == 0:
            flow.append((t, round(flow_c, 1)))
        if t.minute == 0:
            out.append((t, outdoor))
        t += MINUTE
    room_input = {e: (rooms[e][0], series[e]) for e in rooms}
    return room_input, out, flow, START, end


class RoomTests(unittest.TestCase):
    def test_finds_leaky_room_and_slow_radiator(self):
        rooms, out, flow, start, end = simulate()
        result = analyse_rooms(rooms, out, flow, start, end)
        by_name = {r["name"]: r for r in result["rooms"]}
        self.assertTrue(result["has_flow"])
        self.assertGreater(by_name["Hall"]["cooling_vs_house"], 2)
        self.assertAlmostEqual(by_name["Lounge"]["cooling_per_hour"], 0.03, delta=0.01)
        self.assertLess(by_name["Study"]["warming_vs_house"], 0.6)
        texts = " ".join(t["text"] for t in result["advice"])
        self.assertIn("Hall loses heat", texts)
        self.assertIn("Study warms up", texts)
        self.assertNotIn("short-cycling", texts)
        self.assertAlmostEqual(result["house"]["time_constant_hours"], 33, delta=8)

    def test_detects_short_cycling(self):
        rooms, out, flow, start, end = simulate(days=2, burn_on=6, burn_off=12)
        result = analyse_rooms(rooms, out, flow, start, end)
        self.assertIsNotNone(result["cycles"])
        self.assertLessEqual(result["cycles"]["median_burn_minutes"], 10)
        self.assertIn("short-cycling", " ".join(t["text"] for t in result["advice"]))

    def test_works_without_flow_sensor(self):
        rooms, out, _, start, end = simulate()
        result = analyse_rooms(rooms, out, None, start, end)
        self.assertFalse(result["has_flow"])
        hall = next(r for r in result["rooms"] if r["name"] == "Hall")
        self.assertGreater(hall["cooling_vs_house"], 1.4)

    def test_room_with_no_data(self):
        rooms, out, flow, start, end = simulate(days=1)
        rooms["sensor.loft"] = ("Loft", [])
        result = analyse_rooms(rooms, out, flow, start, end)
        loft = next(r for r in result["rooms"] if r["name"] == "Loft")
        self.assertEqual(loft["status"], "no_data")

    def test_flat_flow_gives_no_mask(self):
        mask, threshold = firing_mask([30.0] * 100)
        self.assertIsNone(threshold)
        self.assertIsNone(burner_cycles([False] * 10))


if __name__ == "__main__":
    unittest.main()
