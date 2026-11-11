const DEPTH_TO_PERCENT = [
  [9, 100],
  [17, 90],
  [30, 80],
  [41, 70],
  [55, 60],
  [66, 50],
  [78, 40],
  [90, 30],
  [101, 20],
  [113, 10],
  [126, 0],
];

class OilMonitorPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._rangeDays = 90;
    this._statistics = null;
    this._loading = false;
    this._lastFetch = 0;
  }

  set hass(hass) {
    const previous = this._hass;
    this._hass = hass;
    // Home Assistant pushes a new hass object on every state change anywhere.
    // Only redraw when one of the entities this panel shows has changed.
    if (!previous || this._relevantIds().some((id) => previous.states[id] !== hass.states[id])) {
      this._render();
    }
    if (!this._loading && Date.now() - this._lastFetch > 15 * 60 * 1000) {
      this._loadStatistics();
    }
  }

  set panel(panel) {
    this._panel = panel;
    this._render();
  }

  set narrow(narrow) {
    this._narrow = narrow;
    this._render();
  }

  async _loadStatistics() {
    if (!this._hass || !this._panel) return;
    this._loading = true;
    const end = new Date();
    const start = new Date(end.getTime() - this._rangeDays * 86400000);
    const config = this._panel.config || {};
    const statisticIds = [
      config.depth_entity,
      config.outdoor_entity,
      config.indoor_entity,
      config.runtime_entity,
    ].filter(Boolean);
    try {
      this._statistics = await this._hass.callWS({
        type: "recorder/statistics_during_period",
        start_time: start.toISOString(),
        end_time: end.toISOString(),
        statistic_ids: statisticIds,
        period: "day",
        types: ["mean", "max"],
      });
      this._lastFetch = Date.now();
      this._error = null;
    } catch (error) {
      this._error = error.message || "Could not load Home Assistant history.";
      this._statistics = null;
    } finally {
      this._loading = false;
      this._render();
    }
  }

  _relevantIds() {
    const config = this._panel?.config || {};
    return [
      "depth_entity", "timestamp_entity", "outdoor_entity", "indoor_entity",
      "runtime_entity", "target_entity", "litres_entity", "weekly_entity",
      "lower_entity", "upper_entity", "stale_entity", "offset_entity",
    ]
      .map((key) => config[key])
      .filter(Boolean);
  }

  _state(entityId) {
    return entityId && this._hass?.states?.[entityId];
  }

  _findEntity(configuredId, namePart) {
    if (this._state(configuredId)) return configuredId;
    const match = Object.values(this._hass?.states || {}).find((state) =>
      (state.attributes?.friendly_name || "").toLowerCase().includes(namePart),
    );
    return match?.entity_id;
  }

  _format(value, digits = 0) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) {
      return "—";
    }
    return Number(value).toLocaleString(this._hass?.locale?.language, {
      maximumFractionDigits: digits,
      minimumFractionDigits: digits,
    });
  }

  _litresAtDepth(depth, capacity) {
    if (!Number.isFinite(depth) || depth <= 0) return null;
    let percent = depth <= DEPTH_TO_PERCENT[0][0] ? 100 : 0;
    for (let index = 0; index < DEPTH_TO_PERCENT.length - 1; index += 1) {
      const [lowDepth, lowPercent] = DEPTH_TO_PERCENT[index];
      const [highDepth, highPercent] = DEPTH_TO_PERCENT[index + 1];
      if (depth >= lowDepth && depth <= highDepth) {
        const fraction = (depth - lowDepth) / (highDepth - lowDepth);
        percent = lowPercent + fraction * (highPercent - lowPercent);
        break;
      }
    }
    if (depth >= DEPTH_TO_PERCENT.at(-1)[0]) percent = 0;
    return (capacity * percent) / 100;
  }

  _dailyRows() {
    const config = this._panel?.config || {};
    const depthId = config.depth_entity;
    const outdoorId = config.outdoor_entity;
    const ids = [depthId, outdoorId];
    if (!this._statistics || ids.some((id) => !id || !this._statistics[id])) return [];

    const byDay = new Map();
    const dayFormat = new Intl.DateTimeFormat("en-CA", {
      timeZone: this._hass.config.time_zone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    });
    for (const [key, id] of [
      ["depth", depthId],
      ["outdoor", outdoorId],
      ["indoor", config.indoor_entity],
    ]) {
      for (const row of this._statistics[id] || []) {
        const start = Number(row.start);
        if (!Number.isFinite(start)) continue;
        // The websocket API reports milliseconds; older versions used seconds.
        const day = dayFormat.format(new Date(start > 1e11 ? start : start * 1000));
        const point = byDay.get(day) || { day };
        point[key] = row.mean;
        byDay.set(day, point);
      }
    }
    return [...byDay.values()].sort((a, b) => a.day.localeCompare(b.day));
  }

  _weeklyRows() {
    const config = this._panel.config || {};
    const capacity = Number(config.tank_capacity || 1000);
    const daily = this._dailyRows();
    const byDay = new Map(daily.map((row) => [row.day, row]));
    const weekly = [];
    const dateFor = (key) => new Date(`${key}T00:00:00Z`);
    const first = daily[0] && dateFor(daily[0].day);
    const last = daily.at(-1) && dateFor(daily.at(-1).day);
    if (!first || !last) return weekly;
    const monday = new Date(first);
    monday.setUTCDate(monday.getUTCDate() - ((monday.getUTCDay() + 6) % 7));
    const dateKey = (date) => date.toISOString().slice(0, 10);

    while (monday <= last) {
      const sunday = new Date(monday);
      sunday.setUTCDate(sunday.getUTCDate() + 6);
      const priorSunday = new Date(monday);
      priorSunday.setUTCDate(priorSunday.getUTCDate() - 1);
      const anchor = byDay.get(dateKey(priorSunday));
      const days = [];
      for (let offset = 0; offset < 7; offset += 1) {
        const date = new Date(monday);
        date.setUTCDate(date.getUTCDate() + offset);
        const point = byDay.get(dateKey(date));
        if (point) days.push(point);
      }
      if (
        anchor &&
        days.length === 7 &&
        [anchor, ...days].every(
          (point) => Number.isFinite(point.depth) && Number.isFinite(point.outdoor),
        )
      ) {
        const interval = [anchor, ...days];
        const refill = interval.some(
          (point, index) =>
            index > 0 && point.depth - interval[index - 1].depth <= -8,
        );
        const oilUsed =
          this._litresAtDepth(anchor.depth, capacity) -
          this._litresAtDepth(days.at(-1).depth, capacity);
        if (!refill && oilUsed >= 0 && oilUsed <= 300) {
          const indoorDays = days.filter((point) => Number.isFinite(point.indoor));
          weekly.push({
            day: dateKey(sunday),
            outdoor: days.reduce((sum, point) => sum + point.outdoor, 0) / 7,
            indoor: indoorDays.length
              ? indoorDays.reduce((sum, point) => sum + point.indoor, 0) / indoorDays.length
              : null,
            litres: oilUsed,
          });
        }
      }
      monday.setUTCDate(monday.getUTCDate() + 7);
    }
    return weekly;
  }

  _path(points, xFn, yFn) {
    return points
      .map((point, index) => `${index ? "L" : "M"}${xFn(point).toFixed(1)},${yFn(point).toFixed(1)}`)
      .join(" ");
  }

  _escape(text) {
    return String(text).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
  }

  _chartProblem(weeks) {
    if (this._chartError) return `Could not draw the charts: ${this._chartError}`;
    if (!this._statistics) {
      return this._loading ? "Loading history…" : "History has not loaded yet.";
    }
    const config = this._panel?.config || {};
    const missing = [config.depth_entity, config.outdoor_entity].filter(
      (id) => id && !this._statistics[id]?.length,
    );
    if (missing.length) {
      return `No long-term statistics found for ${missing.join(" and ")}. Home Assistant only keeps daily statistics for sensors with a state class of "measurement" (check under Developer tools → Statistics).`;
    }
    if (!weeks.length) {
      const days = this._dailyRows().filter(
        (row) => Number.isFinite(row.depth) && Number.isFinite(row.outdoor),
      ).length;
      return `Found ${days} day${days === 1 ? "" : "s"} of tank and temperature history in this range, but no complete clean week yet. A week needs daily data for Monday–Sunday plus the Sunday before, with no refill.`;
    }
    return null;
  }

  _empty(message) {
    return `<div class="empty">${this._escape(message)}</div>`;
  }

  _chart(weeks) {
    if (!weeks.length) {
      return this._empty(this._chartProblem(weeks) || "No weekly data to show.");
    }
    const width = 900;
    const height = 270;
    const left = 52;
    const right = 46;
    const top = 18;
    const bottom = 38;
    const innerW = width - left - right;
    const innerH = height - top - bottom;
    const maxLitres = Math.max(10, ...weeks.map((row) => row.litres)) * 1.18;
    const minTemp = Math.min(...weeks.map((row) => row.outdoor));
    const maxTemp = Math.max(...weeks.map((row) => row.outdoor));
    const tempSpan = Math.max(2, maxTemp - minTemp);
    const x = (_, index) => left + (index / Math.max(1, weeks.length - 1)) * innerW;
    const yOil = (row) => top + innerH - (row.litres / maxLitres) * innerH;
    const yTemp = (row) => top + innerH - ((row.outdoor - minTemp) / tempSpan) * innerH;
    const oilPath = this._path(weeks, (row, index) => x(row, index), yOil);
    const tempPath = this._path(weeks, (row, index) => x(row, index), yTemp);
    const bars = weeks
      .map((row, index) => {
        const barX = x(row, index) - Math.max(2, innerW / weeks.length / 4);
        const barW = Math.max(3, innerW / weeks.length / 2);
        return `<rect x="${barX}" y="${yOil(row)}" width="${barW}" height="${top + innerH - yOil(row)}" rx="3" class="oil-bar"><title>${row.day}: ${this._format(row.litres, 1)} L; ${this._format(row.outdoor, 1)} °C</title></rect>`;
      })
      .join("");
    const labels = [weeks[0], weeks[Math.floor(weeks.length / 2)], weeks.at(-1)]
      .filter(Boolean)
      .map((row, index, list) => {
        const rowIndex = weeks.indexOf(row);
        return `<text x="${x(row, rowIndex)}" y="${height - 8}" text-anchor="${index === 0 ? "start" : index === list.length - 1 ? "end" : "middle"}" class="axis-label">${row.day.slice(5)}</text>`;
      })
      .join("");
    return `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Weekly estimated oil use and outdoor temperature">
      <line x1="${left}" y1="${top + innerH}" x2="${width - right}" y2="${top + innerH}" class="axis" />
      <line x1="${left}" y1="${top}" x2="${left}" y2="${top + innerH}" class="axis" />
      ${bars}
      <path d="${tempPath}" class="temp-line" />
      <path d="${oilPath}" class="oil-line" />
      ${weeks.map((row, index) => `<circle cx="${x(row, index)}" cy="${yTemp(row)}" r="3" class="temp-dot"><title>${this._format(row.outdoor, 1)} °C</title></circle>`).join("")}
      ${labels}
      <text x="10" y="${top + 8}" class="axis-label">L/week</text>
      <text x="${width - right + 8}" y="${top + 8}" class="axis-label">°C</text>
    </svg>`;
  }

  _scatter(weeks) {
    const valid = weeks.filter((row) => row.litres >= 0 && Number.isFinite(row.outdoor));
    if (valid.length < 4) {
      return this._empty(
        this._chartProblem(weeks) ||
          `${valid.length} clean week${valid.length === 1 ? "" : "s"} so far; the comparison needs at least 4.`,
      );
    }
    const width = 520;
    const height = 230;
    const left = 48;
    const right = 18;
    const top = 16;
    const bottom = 34;
    const minX = Math.min(...valid.map((row) => row.outdoor)) - 1;
    const maxX = Math.max(...valid.map((row) => row.outdoor)) + 1;
    const minY = 0;
    const maxY = Math.max(10, ...valid.map((row) => row.litres)) * 1.15;
    const px = (value) => left + ((value - minX) / (maxX - minX || 1)) * (width - left - right);
    const py = (value) => top + ((maxY - value) / (maxY - minY)) * (height - top - bottom);
    const meanX = valid.reduce((sum, row) => sum + row.outdoor, 0) / valid.length;
    const meanY = valid.reduce((sum, row) => sum + row.litres, 0) / valid.length;
    const denominator = valid.reduce((sum, row) => sum + (row.outdoor - meanX) ** 2, 0);
    const slope = denominator ? valid.reduce((sum, row) => sum + (row.outdoor - meanX) * (row.litres - meanY), 0) / denominator : 0;
    const intercept = meanY - slope * meanX;
    const trend = `M${px(minX)},${py(intercept + slope * minX)} L${px(maxX)},${py(intercept + slope * maxX)}`;
    const dots = valid.map((row) => `<circle cx="${px(row.outdoor)}" cy="${py(row.litres)}" r="5" class="scatter-dot"><title>${row.day}: ${this._format(row.litres, 1)} L/week at ${this._format(row.outdoor, 1)} °C</title></circle>`).join("");
    return `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Weekly oil use compared with outdoor temperature">
      <line x1="${left}" y1="${height - bottom}" x2="${width - right}" y2="${height - bottom}" class="axis" />
      <line x1="${left}" y1="${top}" x2="${left}" y2="${height - bottom}" class="axis" />
      <path d="${trend}" class="trend-line" />${dots}
      <text x="${left}" y="${height - 8}" class="axis-label">${this._format(minX, 0)}°</text>
      <text x="${width - right}" y="${height - 8}" text-anchor="end" class="axis-label">${this._format(maxX, 0)}° outdoor</text>
      <text x="8" y="${top + 8}" class="axis-label">L/week</text>
    </svg>`;
  }

  _render() {
    if (!this.shadowRoot || !this._hass || !this._panel) return;
    const config = this._panel.config || {};
    const litresId = this._findEntity(config.litres_entity, "litres remaining");
    const weeklyId = this._findEntity(config.weekly_entity, "oil use next 7 days");
    const lowerId = this._findEntity(config.lower_entity, "lower estimate");
    const upperId = this._findEntity(config.upper_entity, "upper estimate");
    const staleId = this._findEntity(config.stale_entity, "tank sensor stale");
    const litresState = this._state(litresId);
    const depthState = this._state(config.depth_entity);
    const weekly = this._state(weeklyId)?.state;
    const lower = this._state(lowerId)?.state;
    const upper = this._state(upperId)?.state;
    const stale = this._state(staleId)?.state === "on";
    const outdoor = this._state(config.outdoor_entity);
    const indoor = this._state(config.indoor_entity);
    const runtime = this._state(config.runtime_entity);
    const target = this._state(config.target_entity);
    const timestamp = this._state(config.timestamp_entity);
    const capacity = Number(config.tank_capacity || 1000);
    const integrationLitres = Number(litresState?.state);
    const depthCm = Number(depthState?.state);
    const depthEstimate = this._litresAtDepth(depthCm, capacity);
    const litres = Number.isFinite(integrationLitres)
      ? integrationLitres
      : depthEstimate;
    const usingDepthFallback = !Number.isFinite(integrationLitres) && depthEstimate !== null;
    const percent = Number.isFinite(Number(litres)) ? Math.min(100, (Number(litres) / capacity) * 100) : 0;
    let weeks = [];
    this._chartError = null;
    try {
      weeks = this._weeklyRows();
    } catch (error) {
      this._chartError = error.message || String(error);
    }
    const staleAttrs = this._state(staleId)?.attributes || {};
    const hasEstimate = Number.isFinite(Number(weekly)) && weekly !== "";
    const trainingWeeks = Number(staleAttrs.training_weeks);
    const estimateHint = hasEstimate
      ? `Forecast range ${this._format(lower)}–${this._format(upper)} L`
      : this._escape(
          [
            Number.isFinite(trainingWeeks) ? `${trainingWeeks} clean weeks of history (14 needed).` : "",
            staleAttrs.forecast_warning || "Not available yet.",
          ].filter(Boolean).join(" "),
        );
    const status = stale ? "Tank reading is stale — forecast confidence is reduced." : "Tank reading is current.";
    const isClimate = target?.entity_id?.startsWith("climate.");
    const targetValue = isClimate ? target.attributes.temperature : target?.state;
    const targetUnit = isClimate
      ? this._hass.config.unit_system?.temperature || "°C"
      : target?.attributes?.unit_of_measurement || "°C";
    const offset = Number(this._state(config.offset_entity)?.state);
    const offsetNote = Number.isFinite(offset) && offset !== 0
      ? ` · assumes ${offset > 0 ? "+" : ""}${this._format(offset, 1)} °C indoors`
      : "";
    const runtimeValue =runtime && runtime.state !== "unknown" && runtime.state !== "unavailable" ? `${this._format(runtime.state, 1)} ${runtime.attributes.unit_of_measurement || "h"}` : "—";

    this.shadowRoot.innerHTML = `
      <style>
        :host{display:block;color:var(--primary-text-color);background:var(--primary-background-color);min-height:100%;font-family:var(--paper-font-body1_-_font-family,Roboto,"Noto",sans-serif)}
        *{box-sizing:border-box}.page{max-width:1180px;margin:0 auto;padding:28px 24px 48px}.top{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:22px}.eyebrow{color:var(--secondary-text-color);font-size:12px;letter-spacing:.11em;text-transform:uppercase}.title{font-size:clamp(28px,4vw,38px);font-weight:650;margin:5px 0}.subtitle{color:var(--secondary-text-color);margin:0}.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px}.card{background:var(--card-background-color,var(--secondary-background-color));border:1px solid var(--divider-color);border-radius:16px;padding:18px;min-width:0}.metric-label{color:var(--secondary-text-color);font-size:13px}.metric{font-size:clamp(26px,3vw,34px);font-weight:650;margin:8px 0 3px}.unit{font-size:15px;font-weight:450;color:var(--secondary-text-color)}.hint{color:var(--secondary-text-color);font-size:12px}.level{height:6px;border-radius:6px;background:var(--divider-color);margin-top:14px;overflow:hidden}.level i{display:block;height:100%;background:var(--accent-color,#03a9f4);border-radius:inherit}.status{margin:14px 0;padding:12px 14px;border-radius:12px;background:var(--secondary-background-color);font-size:13px}.warn{color:var(--warning-color,#e6a700)}.section{margin-top:18px}.section h2{font-size:18px;margin:0 0 4px}.section p{color:var(--secondary-text-color);margin:0 0 12px;font-size:13px}.charts{display:grid;grid-template-columns:1.35fr 1fr;gap:14px}.chart-card{background:var(--card-background-color,var(--secondary-background-color));border:1px solid var(--divider-color);border-radius:16px;padding:18px;min-width:0}.chart-card h3{margin:0;font-size:16px}.chart-card p{margin:5px 0 12px;color:var(--secondary-text-color);font-size:12px}svg{display:block;width:100%;height:auto;overflow:visible}.axis{stroke:var(--divider-color);stroke-width:1}.axis-label{fill:var(--secondary-text-color);font-size:11px}.oil-line{fill:none;stroke:var(--accent-color,#03a9f4);stroke-width:3}.temp-line{fill:none;stroke:var(--orange-color,#ff9800);stroke-width:2.4;stroke-dasharray:6 4}.oil-bar{fill:var(--accent-color,#03a9f4);opacity:.2}.temp-dot{fill:var(--orange-color,#ff9800)}.scatter-dot{fill:var(--accent-color,#03a9f4);opacity:.75}.trend-line{stroke:var(--orange-color,#ff9800);stroke-width:2;stroke-dasharray:5 4}.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--secondary-text-color);margin-top:8px}.key{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;background:var(--accent-color,#03a9f4)}.key.temp{background:var(--orange-color,#ff9800)}.toolbar{display:flex;align-items:center;gap:8px}.toolbar select,.toolbar button{color:var(--primary-text-color);background:var(--card-background-color,var(--secondary-background-color));border:1px solid var(--divider-color);border-radius:9px;padding:9px 11px;font:inherit}.empty{padding:24px 8px;color:var(--secondary-text-color);font-size:13px}.details{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.detail{padding:13px;background:var(--secondary-background-color);border-radius:12px}.detail b{display:block;font-size:18px;margin-top:5px}.foot{margin-top:16px;color:var(--secondary-text-color);font-size:12px;line-height:1.5}@media(max-width:850px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.charts{grid-template-columns:1fr}}@media(max-width:520px){.page{padding:20px 14px 36px}.grid{gap:9px}.card{padding:14px}.metric{font-size:25px}.details{grid-template-columns:repeat(2,1fr)}.top{align-items:center}}
      </style>
      <main class="page">
        <header class="top"><div><div class="eyebrow">Home energy · heating oil</div><h1 class="title">Oil Monitor</h1><p class="subtitle">Your tank, usage trends and weather-aware outlook</p></div><div class="toolbar"><select aria-label="History range"><option value="30" ${this._rangeDays === 30 ? "selected" : ""}>30 days</option><option value="90" ${this._rangeDays === 90 ? "selected" : ""}>90 days</option><option value="365" ${this._rangeDays === 365 ? "selected" : ""}>1 year</option></select><button type="button" class="refresh" title="Refresh history">↻</button></div></header>
        <section class="grid" aria-label="Oil summary">
          <article class="card"><div class="metric-label">${stale ? "Last known oil remaining" : "Oil remaining"}</div><div class="metric">${this._format(litres)} <span class="unit">L</span></div><div class="hint">${usingDepthFallback ? `Calculated from ${this._format(depthCm, 0)} cm depth` : "of " + this._format(capacity) + " L usable capacity"}</div><div class="level"><i style="width:${percent}%"></i></div></article>
          <article class="card"><div class="metric-label">Estimated use · next 7 days</div><div class="metric">${this._format(weekly)} <span class="unit">L</span></div><div class="hint">${estimateHint}${hasEstimate ? offsetNote : ""}</div></article>
          <article class="card"><div class="metric-label">Outdoor temperature</div><div class="metric">${this._format(outdoor?.state, 1)} <span class="unit">${outdoor?.attributes?.unit_of_measurement || "°C"}</span></div><div class="hint">Current weather sensor</div></article>
          <article class="card"><div class="metric-label">Tank reading</div><div class="metric ${stale ? "warn" : ""}">${stale ? "Check sensor" : "Current"}</div><div class="hint">Measurement timestamp ${timestamp?.state || "not available"}</div></article>
        </section>
        <div class="status ${stale ? "warn" : ""}">${status}${this._loading ? " Loading recorder history…" : ""}${this._error ? ` History: ${this._escape(this._error)}` : ""}</div>
        <section class="section"><h2>Usage and weather over time</h2><p>Weekly tank-level changes are converted with your tank calibration and compared with weekly outdoor temperatures. Refill and suspect weeks are excluded.</p><div class="charts">
          <article class="chart-card"><h3>Weekly oil use & outdoor temperature</h3><p>Bars and blue line: litres used · orange: mean outdoor temperature</p>${this._chart(weeks)}<div class="legend"><span><i class="key"></i>Oil use (L/week)</span><span><i class="key temp"></i>Outdoor temperature</span></div></article>
          <article class="chart-card"><h3>Oil use vs outdoor temperature</h3><p>Each point is a clean week. Dashed line shows a simple trend, not a guarantee.</p>${this._scatter(weeks)}</article>
        </div></section>
        <section class="section"><h2>Home heating conditions</h2><p>Live context from your selected Home Assistant entities.</p><div class="details"><div class="detail"><span class="metric-label">Indoor temperature</span><b>${this._format(indoor?.state, 1)} ${indoor?.attributes?.unit_of_measurement || "°C"}</b></div><div class="detail"><span class="metric-label">Thermostat target</span><b>${this._format(targetValue, 1)} ${targetUnit}</b></div><div class="detail"><span class="metric-label">Heating runtime today</span><b>${runtimeValue}</b></div><div class="detail"><span class="metric-label">History range</span><b>${this._rangeDays} days</b></div></div></section>
        <p class="foot">Forecasts are estimates learned from available tank, temperature and heating history. A limited or noisy history can make the range wide or misleading. Use this as a planning aid, not as the only basis for ordering oil.<br>Dashboard build ${this._escape(config.build || "unknown")}</p>
      </main>`;

    this.shadowRoot.querySelector("select")?.addEventListener("change", (event) => {
      this._rangeDays = Number(event.target.value);
      this._statistics = null;
      this._lastFetch = 0;
      this._render();
      this._loadStatistics();
    });
    this.shadowRoot.querySelector(".refresh")?.addEventListener("click", () => {
      this._lastFetch = 0;
      this._loadStatistics();
    });
  }
}

// Each build registers its own element name (taken from the ?v= build id in
// this module's URL). An element name can only be defined once per page, so
// a fixed name would let an older cached copy keep rendering after updates.
const BUILD_ID = new URL(import.meta.url).searchParams.get("v");
const ELEMENT_NAME = BUILD_ID ? `oil-monitor-panel-${BUILD_ID}` : "oil-monitor-panel";
if (!customElements.get(ELEMENT_NAME)) {
  customElements.define(ELEMENT_NAME, OilMonitorPanel);
}
