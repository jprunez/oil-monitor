"""Room-by-room heating advice from a few days of detailed history.

Unlike the weekly boiler analysis this needs no oil readings, so it works
within a day or two of choosing the room sensors. It uses Home Assistant's
short-term state history (about ten days by default):

* Cooling rate: while the heating is off, a room cools towards the outdoor
  temperature as ``dT/dt = -k (T_room - T_out)``. ``k`` (per hour) is how
  leaky the room is; ``1/k`` is its time constant.
* Warm-up rate: how fast each room rises while the boiler is firing. A room
  that lags well behind the others usually has a small, unbalanced or
  air-locked radiator.
* Burner cycling, from the flow temperature: many short burns waste oil.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
import math
from statistics import median
from typing import Any

STEP = timedelta(minutes=10)
MAX_GAP = timedelta(hours=1)  # hold a reading at most this long
MIN_COOL_HOURS = 2.0  # a cooling stretch must be this long
MIN_COOL_GAP_C = 3.0  # room must be this much warmer than outside
MIN_WARM_MINUTES = 30
MIN_SEGMENTS = 2  # stretches needed before a room is rated
FAST_COOLING = 1.4  # × the house median: flagged as losing heat fast
SLOW_WARMING = 0.6  # × the house median: flagged as slow to warm
COLD_ROOM_C = 1.5  # this far below the house average while heating
SHORT_BURN_MINUTES = 10
MANY_BURNS_PER_HOUR = 3


Series = Sequence[tuple[datetime, float]]


def resample(points: Series, start: datetime, end: datetime) -> list[float | None]:
    """Values on a 10-minute grid, holding each reading up to an hour."""
    ordered = sorted(points)
    out: list[float | None] = []
    i = -1
    t = start
    while t <= end:
        while i + 1 < len(ordered) and ordered[i + 1][0] <= t:
            i += 1
        if i >= 0 and t - ordered[i][0] <= MAX_GAP:
            out.append(ordered[i][1])
        else:
            out.append(None)
        t += STEP
    return out


def firing_mask(flow: Sequence[float | None]) -> tuple[list[bool | None], float | None]:
    """Whether the boiler is firing at each step, with hysteresis.

    The threshold sits between the idle flow (low percentile) and a typical
    hot flow (high percentile), so it adapts to each system.
    """
    values = sorted(v for v in flow if v is not None)
    if len(values) < 36:
        return [None] * len(flow), None
    low = values[int(0.1 * (len(values) - 1))]
    high = values[int(0.95 * (len(values) - 1))]
    if high - low < 10:
        return [None] * len(flow), None
    on_at = low + 0.5 * (high - low)
    off_at = low + 0.35 * (high - low)
    mask: list[bool | None] = []
    state = False
    for v in flow:
        if v is None:
            mask.append(None)
            continue
        if state and v < off_at:
            state = False
        elif not state and v > on_at:
            state = True
        mask.append(state)
    return mask, on_at


def burner_cycles(mask: Sequence[bool | None]) -> dict[str, Any] | None:
    """Burn count and lengths from the firing mask.

    Flow sensors lag the burner and the 10-minute grid hides very short
    burns, so this undercounts rapid cycling rather than inventing it.
    """
    burns: list[float] = []
    run = 0
    for state in [*mask, None]:
        if state:
            run += 1
        else:
            if run:
                burns.append(run * STEP.total_seconds() / 60)
            run = 0
    firing_hours = sum(1 for s in mask if s) * STEP.total_seconds() / 3600
    if len(burns) < 3 or firing_hours < 1:
        return None
    return {
        "burns": len(burns),
        "firing_hours": round(firing_hours, 1),
        "median_burn_minutes": round(median(burns), 0),
        "burns_per_firing_hour": round(len(burns) / firing_hours, 2),
    }


def _stretches(flags: Sequence[bool]) -> list[tuple[int, int]]:
    out = []
    begin = None
    for i, flag in enumerate([*flags, False]):
        if flag and begin is None:
            begin = i
        elif not flag and begin is not None:
            out.append((begin, i - 1))
            begin = None
    return out


def cooling_rates(
    room: Sequence[float | None],
    outdoor: Sequence[float | None],
    firing: Sequence[bool | None] | None,
) -> list[float]:
    """``k`` per hour for each clean cooling stretch of one room."""
    n = len(room)
    falling = [False] * n
    for i in range(1, n):
        a, b = room[i - 1], room[i]
        if a is None or b is None or outdoor[i] is None:
            continue
        if firing is not None and firing[i] is not False:
            continue
        falling[i] = b <= a + 0.05  # allow sensor jitter
    rates = []
    step_h = STEP.total_seconds() / 3600
    for begin, end in _stretches(falling):
        start = begin - 1
        hours = (end - start) * step_h
        if hours < MIN_COOL_HOURS:
            continue
        out = [o for o in outdoor[start : end + 1] if o is not None]
        t_out = sum(out) / len(out)
        gap0 = room[start] - t_out  # type: ignore[operator]
        gap1 = room[end] - t_out  # type: ignore[operator]
        if gap1 < MIN_COOL_GAP_C or gap0 - gap1 < 0.3:
            continue
        rates.append(math.log(gap0 / gap1) / hours)
    return rates


def warming_rates(
    room: Sequence[float | None], firing: Sequence[bool | None] | None
) -> list[float]:
    """°C per hour over each stretch the room rose while heating."""
    n = len(room)
    # A heating spell includes the short pauses between burns, so slow rooms
    # are measured over the whole spell rather than one burn at a time.
    window = 6
    heating = [
        firing is None or any(f for f in firing[max(0, i - window) : i + 1])
        for i in range(n)
    ]
    rising = [False] * n
    for i in range(1, n):
        a, b = room[i - 1], room[i]
        if a is None or b is None or not heating[i]:
            continue
        rising[i] = b >= a - 0.05
    rates = []
    step_h = STEP.total_seconds() / 3600
    for begin, end in _stretches(rising):
        start = begin - 1
        hours = (end - start) * step_h
        rise = room[end] - room[start]  # type: ignore[operator]
        if hours * 60 < MIN_WARM_MINUTES or rise < 0.3:
            continue
        rates.append(rise / hours)
    return rates


def analyse_rooms(
    rooms: Mapping[str, tuple[str, Series]],
    outdoor: Series,
    flow: Series | None,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Per-room cooling and warm-up, burner cycling and advice.

    ``rooms`` maps entity_id to (display name, readings in °C).
    """
    out_grid = resample(outdoor, start, end)
    firing = None
    cycles = None
    if flow:
        mask, _ = firing_mask(resample(flow, start, end))
        if any(m is not None for m in mask):
            firing = mask
            cycles = burner_cycles(mask)

    results = []
    for entity_id, (name, points) in rooms.items():
        grid = resample(points, start, end)
        known = [v for v in grid if v is not None]
        if not known:
            results.append({"entity_id": entity_id, "name": name, "status": "no_data"})
            continue
        cool = cooling_rates(grid, out_grid, firing)
        warm = warming_rates(grid, firing)
        heated = [
            v for v, f in zip(grid, firing or [None] * len(grid)) if v is not None and f
        ] if firing else []
        results.append(
            {
                "entity_id": entity_id,
                "name": name,
                "status": "ok",
                "hours": round(len(known) * STEP.total_seconds() / 3600, 1),
                "mean_c": round(sum(known) / len(known), 2),
                "heating_mean_c": round(sum(heated) / len(heated), 2) if heated else None,
                "cooling_per_hour": round(median(cool), 4) if len(cool) >= MIN_SEGMENTS else None,
                "cooling_segments": len(cool),
                "warming_c_per_hour": round(median(warm), 2) if len(warm) >= MIN_SEGMENTS else None,
                "warming_segments": len(warm),
            }
        )

    rated = [r for r in results if r.get("cooling_per_hour")]
    warmed = [r for r in results if r.get("warming_c_per_hour")]
    house_k = median(r["cooling_per_hour"] for r in rated) if rated else None
    house_warm = median(r["warming_c_per_hour"] for r in warmed) if warmed else None
    heated_means = [r["heating_mean_c"] for r in results if r.get("heating_mean_c") is not None]
    house_heated = sum(heated_means) / len(heated_means) if heated_means else None

    for r in results:
        if r.get("status") != "ok":
            continue
        k = r.get("cooling_per_hour")
        r["cooling_vs_house"] = round(k / house_k, 2) if k and house_k else None
        w = r.get("warming_c_per_hour")
        r["warming_vs_house"] = round(w / house_warm, 2) if w and house_warm else None
        hm = r.get("heating_mean_c")
        r["vs_house_c"] = round(hm - house_heated, 2) if hm is not None and house_heated is not None and len(heated_means) > 1 else None

    out_known = [v for v in out_grid if v is not None]
    return {
        "status": "ok" if results else "no_rooms",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "has_flow": firing is not None,
        "outdoor_mean_c": round(sum(out_known) / len(out_known), 1) if out_known else None,
        "house": {
            "cooling_per_hour": round(house_k, 4) if house_k else None,
            "time_constant_hours": round(1 / house_k, 1) if house_k else None,
            "warming_c_per_hour": round(house_warm, 2) if house_warm else None,
        },
        "cycles": cycles,
        "rooms": results,
        "advice": advice(results, house_k, cycles, firing is not None),
    }


def advice(
    rooms: Sequence[Mapping[str, Any]],
    house_k: float | None,
    cycles: Mapping[str, Any] | None,
    has_flow: bool,
) -> list[dict[str, str]]:
    """Plain-language tips on cutting heat waste, most useful first."""
    tips: list[dict[str, str]] = []

    if cycles and (
        cycles["median_burn_minutes"] <= SHORT_BURN_MINUTES
        or cycles["burns_per_firing_hour"] >= MANY_BURNS_PER_HOUR
    ):
        tips.append({
            "kind": "warn",
            "text": (
                f"The burner is short-cycling: {cycles['burns']} burns in {cycles['firing_hours']} firing hours, "
                f"typically {cycles['median_burn_minutes']:.0f} minutes each. Every start wastes oil and adds soot. "
                "Lowering the boiler thermostat, widening the room thermostat's switching band, or opening more "
                "radiator valves (so the boiler has somewhere to send heat) gives longer, more efficient burns."
            ),
        })

    leaky = sorted(
        (r for r in rooms if (r.get("cooling_vs_house") or 0) >= FAST_COOLING),
        key=lambda r: -r["cooling_vs_house"],
    )
    for r in leaky[:3]:
        tips.append({
            "kind": "warn",
            "text": (
                f"{r['name']} loses heat {r['cooling_vs_house']:.1f}× faster than the rest of the house. "
                "Check for draughts around windows and doors, an open vent or chimney, single glazing or an "
                "uninsulated outside wall. Closing curtains at dusk and a draught excluder are cheap first steps; "
                "if it's rarely used, turn its radiator valve down and keep the door shut."
            ),
        })

    slow = sorted(
        (r for r in rooms if r.get("warming_vs_house") and r["warming_vs_house"] <= SLOW_WARMING),
        key=lambda r: r["warming_vs_house"],
    )
    for r in slow[:3]:
        cold = r.get("vs_house_c")
        extra = f" and sits {-cold:.1f} °C below the house average while heating" if cold is not None and cold < -0.5 else ""
        tips.append({
            "kind": "note",
            "text": (
                f"{r['name']} warms up at only {r['warming_vs_house'] * 100:.0f} % of the house's usual rate{extra}. "
                "Bleed its radiator, check the valve (TRV pin) isn't stuck, and feel whether the radiator is cold at the "
                "bottom (sludge). If it's all fine, the system may need balancing: close the lockshield valves a little "
                "on the fastest rooms so more hot water reaches this one."
            ),
        })

    cold_rooms = [
        r for r in rooms
        if r.get("vs_house_c") is not None and r["vs_house_c"] <= -COLD_ROOM_C and r not in slow
    ]
    for r in cold_rooms[:2]:
        tips.append({
            "kind": "note",
            "text": (
                f"{r['name']} averages {-r['vs_house_c']:.1f} °C below the rest of the house while the heating runs. "
                "If the thermostat is in a warmer room, the boiler stops before this room is warm; if the thermostat is "
                "here, the rest of the house is being overheated to satisfy it."
            ),
        })

    warm_rooms = [r for r in rooms if r.get("vs_house_c") is not None and r["vs_house_c"] >= COLD_ROOM_C]
    for r in warm_rooms[:2]:
        tips.append({
            "kind": "note",
            "text": (
                f"{r['name']} runs {r['vs_house_c']:.1f} °C warmer than the rest of the house while heating. "
                "Turning its radiator valve down a notch saves roughly 3 % of that room's heat per degree."
            ),
        })

    if house_k:
        tau = 1 / house_k
        if tau >= 40:
            tips.append({
                "kind": "ok",
                "text": (
                    f"The house holds heat well (time constant about {tau:.0f} hours). Overnight setbacks save little "
                    "here; a steady, slightly lower temperature is usually cheapest."
                ),
            })
        elif tau <= 15:
            tips.append({
                "kind": "note",
                "text": (
                    f"The house cools quickly (time constant about {tau:.0f} hours), so setting the heating back at "
                    "night and when you're out saves real oil. Loft and cavity insulation would make the biggest difference."
                ),
            })
        else:
            tips.append({
                "kind": "ok",
                "text": (
                    f"The house cools at a typical rate (time constant about {tau:.0f} hours). Setting back a few degrees "
                    "overnight and when out saves oil without long warm-ups."
                ),
            })

    if not has_flow:
        tips.append({
            "kind": "note",
            "text": "Without a flow temperature sensor, heating-on and heating-off times are guessed from the rooms themselves, so warm-up figures are rougher.",
        })
    if not any(r.get("cooling_per_hour") for r in rooms):
        tips.append({
            "kind": "note",
            "text": "No room has cooled for long enough yet. A night or a few hours with the heating off gives the first heat-loss figures.",
        })
    return tips
