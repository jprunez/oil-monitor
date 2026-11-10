# Oil Monitor for Home Assistant

Oil Monitor is a custom Home Assistant integration that estimates heating-oil tank volume and projects usage from tank, weather, indoor-temperature, and heating-runtime sensors. It adds a sidebar dashboard; users do not need to create Lovelace cards.

## Features

- Estimated litres remaining from an ultrasonic tank-depth sensor.
- A warning when the tank reading is stale.
- A seven-day usage estimate with a lower and upper estimate, when enough history and a supported weather forecast are available.
- A dashboard with tank status and historical oil-use versus outdoor-temperature charts.

This is an experimental project. Predictions are estimates, not a guarantee or a substitute for checking the tank and arranging fuel delivery.

## Important calibration note

The included depth-to-level calibration curve is a prototype for one particular tank. The setup lets you set tank capacity, but it does not yet let you enter a different tank-shape calibration curve. On a differently shaped tank, displayed litres and usage estimates may be inaccurate even if the capacity is correct.

## Install on Home Assistant OS

These steps use the Home Assistant **File editor** add-on. You need access to this GitHub repository to download its files.

1. Make a full Home Assistant backup from **Settings → System → Backups**.
2. On your computer, open this GitHub repository. Select **Code → Download ZIP**, then extract the ZIP file.
3. In the extracted project, open `custom_components`, then `oil_predict`.
4. In Home Assistant, open **Settings → Add-ons → File editor → Open Web UI**. Browse to `/config`. Create a `custom_components` folder if it is missing, then create and open `/config/custom_components/oil_predict`.
5. Upload the files from the extracted `oil_predict` folder into `/config/custom_components/oil_predict`. Inside that folder, also create `translations` and upload `translations/en.json` there. Create `www` and upload `www/oil-monitor-panel.js` there. Do not upload the whole ZIP or the whole project folder.
6. Restart Home Assistant from **Settings → System → Restart**.
7. If the integration is not already set up, go to **Settings → Devices & services → Add integration**, search for **Oil Monitor**, and select the matching tank depth, measurement timestamp, indoor/outdoor temperature, daily heating-runtime, thermostat-target, and weather-forecast sensors from your own Home Assistant.
8. Enter your tank's usable capacity. The default is 1000 L; change it if that is not correct. The default stale-reading timeout is 90 minutes.
9. Open **Oil Monitor** from the Home Assistant sidebar.

The measurement timestamp sensor is used for freshness checks because retained MQTT messages can make `last_updated` look newer than the actual tank measurement. The usage estimate needs at least 14 clean weeks of daily long-term statistics and a weather entity that provides seven daily forecasts.

## HACS

The repository is not currently ready for HACS publication. HACS requires a public GitHub repository; publishing here would make the source available to anyone. The repository also needs HACS-specific metadata and integration branding before it can be submitted or added as a custom HACS repository.
