# Oil Monitor for Home Assistant

Oil Monitor tells you how much heating oil is in your tank, how much you are likely to use in the coming week, and roughly when you will need to order more. It learns from your own tank, temperature and heating history and uses the weather forecast. It adds an **Oil Monitor** page to the Home Assistant sidebar, so you don't need to build any dashboard cards.

> This is an experimental project. Estimates are a planning aid, not a guarantee. Keep checking your tank and order oil in good time.

## What you get

- **Litres remaining**, from a distance, percent or litres sensor on your tank.
- **A warning when the tank reading is out of date**, for example when the sensor's battery is flat.
- **Estimated oil use for the next 7 days**, with a likely range, based on the weather forecast.
- **How long the oil will last**: days until your reorder level and until empty, with dates and a projected-level chart for **mild, typical and cold winters**. The first week follows the weather forecast. Later weeks use about 25 years of real temperatures for your area.
- **History charts** of weekly oil use against outdoor temperature, including the temperature at which your heating switches off.
- **Settings on the dashboard** for the reorder level, the empty level and a "what if it were warmer or cooler indoors" adjustment.

## What you need

| | Required? | Notes |
|---|---|---|
| A sensor on the oil tank | Yes | Reports one of: **distance** from the sensor down to the oil (e.g. an ultrasonic sensor), **percent full**, or **litres**. |
| Outdoor temperature sensor | Yes | °C or °F. |
| Indoor temperature sensor | Yes | A room or thermostat temperature. |
| Thermostat | Yes | Your thermostat (climate entity) or a sensor holding its target temperature. |
| Weather forecast | Yes | A weather entity with 7 daily forecasts (most weather integrations provide this). |
| Daily heating runtime sensor | Optional | Hours the heating has run today, resetting each day. Makes estimates more accurate. |
| Tank temperature sensor | Optional | Air temperature at the tank sensor. Used to correct ultrasonic readings, which read slightly long in cold air (about 2 cm over a 20 °C swing). Oil Monitor checks your history and only corrects if your sensor actually drifts. Without this sensor the outdoor temperature is used. |
| Tank measurement time sensor | Optional | When the tank sensor actually took its reading. Useful for radio/MQTT sensors that resend old readings. |

**Important:** the tank, outdoor and indoor sensors (and the runtime sensor if you use one) must keep **long-term statistics**. That means they need a *state class* of `measurement`. You can check under **Developer tools → Statistics**: each sensor should be listed there. Without statistics there is no history, so no charts or estimates.

Estimates start once there are about **6 complete, clean weeks** of history. A clean week has data for every day and no oil delivery. Until about 14 weeks they are marked as early estimates, with a wider range. If your sensors have already been recording, this history is used straight away.

## Install

1. Make sure you have [HACS](https://hacs.xyz) installed.
2. In Home Assistant, open **HACS**, select the **⋮** menu (top right), then **Custom repositories**.
3. Enter `https://github.com/jprunez/oil-monitor`, choose **Integration** as the type, and select **Add**.
4. Find **Oil Monitor** in HACS, select **Download**, then restart Home Assistant.
5. Go to **Settings → Devices & services → Add integration** and search for **Oil Monitor**.

New versions appear as an update notice in Home Assistant. If an update causes problems, **HACS → Oil Monitor → ⋮ → Redownload** lets you go back to an earlier version.

## Setup

Setup takes three short steps.

### 1. Oil tank
Choose your tank sensor, what it reports (distance, percent or litres) and the tank's usable capacity in litres.

### 2. Tank shape (distance sensors only)
A distance sensor measures the gap between the sensor and the oil surface. Oil Monitor needs to know how that gap relates to litres:

- **Distance when full:** the sensor-to-oil distance with a full tank.
- **Distance when empty:** the sensor-to-oil distance at the bottom of the tank, or at the outlet if oil below it can't be used.
- **Shape:**
  - **Upright cylinder or box:** straight sides, so the volume follows the oil height. Most plastic tanks are close to this.
  - **Horizontal cylinder:** a round tank lying on its side. The middle holds more oil per centimetre than the top or bottom, and this is allowed for.
  - **Custom calibration table:** if you have measured your own tank, enter `distance:percent` pairs, e.g. `10:100, 40:70, 75:35, 110:0`. They don't need to be in order.

**Percent sensors:** a ready-made gauge's percentage is only as accurate as the gauge's own tank settings. Many simple gauges report percent of *height*, which is wrong for horizontal round tanks. If yours does that, set it to report distance instead and choose the shape here.

### 3. Temperatures and weather
Choose the temperature sensors, thermostat and weather forecast, and optionally the runtime and measurement-time sensors. Set **Warn when the tank reading is older than** a little longer than your sensor's normal reporting interval.

### Winter scenarios and your location
To project mild, typical and cold winters, Oil Monitor downloads about 25 years of daily temperatures for your area from [Open-Meteo](https://open-meteo.com) (free, no account). You can enter a **town or the first part of your postcode**, or leave it empty to use Home Assistant's home location. Either way, only a location rounded to about 10 km is sent, and for UK postcodes only the first half (e.g. `YO10`) is looked up. The download happens about once a month and is stored in Home Assistant.

The same service provides wind and sunshine for the last 13 months and the coming week. Oil Monitor tests whether including them would have predicted your past weeks more accurately, and only uses them if they do. The **Setup check** shows the result.

You can switch this off in setup. Oil Monitor then stays fully local and bases the scenarios on last year's temperatures, 2 °C warmer and colder.

### Changing settings later
Go to **Settings → Devices & services → Oil Monitor → Configure**. The reorder level, empty level and temperature adjustment are also on the Oil Monitor dashboard under **Settings**.

## Sensors created

| Entity | What it is |
|---|---|
| Litres remaining | Current litres, with the raw sensor reading as an attribute. |
| Tank sensor stale | On when the reading is missing or older than your limit. Its attributes show how many clean weeks of history there are. |
| Estimated oil use next 7 days, lower and upper estimate | The 7-day estimate and its range. |
| Days until reorder level, Days until empty | How long the oil will last in a typical winter, with `date`, `earliest_date` (cold winter) and `latest_date` (mild winter) attributes. Useful for automations, e.g. a phone notification when reorder is under 7 days away. |
| Reorder level, Empty level, Forecast temperature adjustment | Settings (also on the dashboard). Reorder defaults to 25 % of capacity, empty to 5 %. |

## Troubleshooting

Start with the **Setup check** section on the Oil Monitor dashboard. It checks each sensor you chose and explains anything that will stop Oil Monitor working well: a sensor that can't be found, is unavailable, has the wrong unit, or has no long-term statistics. For example, it catches a thermostat chosen where an indoor temperature sensor was expected.

Common problems:

- **No charts, or "No long-term statistics found":** the tank or outdoor sensor isn't keeping statistics. Give it `state_class: measurement`, for example through its integration's settings or a template sensor. Then wait for history to build up.
- **Estimated use stays blank:** the grey text under it gives the reason. Usually it needs more clean weeks of history, or the weather entity doesn't provide 7 daily forecasts.
- **Litres look wrong:** check the sensor type, capacity and full/empty distances under **Configure**. If the tank shape isn't upright or a horizontal cylinder, use a custom table.
- **Dashboard looks out of date after an update:** restart Home Assistant, then close and reopen the browser tab. The footer shows the running version.

### Reporting a problem

[Open an issue](https://github.com/jprunez/oil-monitor/issues/new/choose) and attach a diagnostics file. In Home Assistant, go to **Settings → Devices & services → Oil Monitor**, open the **⋮** menu and choose **Download diagnostics**. The file contains your Oil Monitor settings, the state of each sensor it uses, how much history it found and what it learned. It doesn't contain your location, passwords or tokens.

## Development

- Model tests: `python -m unittest discover -s tests`
- Home Assistant integration tests: `pip install pytest-homeassistant-custom-component`, then `python -m pytest`. These run automatically on GitHub for every push.

### Publishing a release
1. Bump `version` in `custom_components/oil_predict/manifest.json`, for example to `0.4.1`, and commit it to `main`.
2. Tag that commit with the same version and push the tag: `git tag v0.4.1 && git push origin v0.4.1`.
3. The **Release** GitHub Action checks that the tag matches `manifest.json`, runs the model tests and publishes the release with generated notes.
