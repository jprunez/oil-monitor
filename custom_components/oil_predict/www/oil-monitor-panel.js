class OilMonitorPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._rangeDays = 365;
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
      this._tempCorrection()?.entity_id,
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
      "reorder_entity", "empty_entity", "reorder_level_entity", "empty_level_entity",
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

  // Parse the tank sensor's timestamp: ISO text, or Unix seconds/milliseconds.
  _parseSampleTime(value) {
    if (value === null || value === undefined || value === "") return null;
    const numeric = Number(value);
    let date;
    if (Number.isFinite(numeric) && numeric > 0) {
      date = new Date(numeric > 100_000_000_000 ? numeric : numeric * 1000);
    } else {
      date = new Date(String(value));
    }
    return Number.isNaN(date.getTime()) ? null : date;
  }

  // e.g. "Measured 12 min ago · 07:58", "Measured yesterday at 22:10".
  _friendlySampleTime(value) {
    const date = this._parseSampleTime(value);
    if (!date) return "No measurement time available";
    const language = this._hass?.locale?.language;
    const timeZone = this._hass?.config?.time_zone;
    const time = new Intl.DateTimeFormat(language, { hour: "2-digit", minute: "2-digit", timeZone }).format(date);
    const dayKey = (d) => new Intl.DateTimeFormat("en-CA", { timeZone }).format(d);
    const minutes = Math.round((Date.now() - date.getTime()) / 60000);
    if (minutes < 0) return `Measured at ${time}`;
    if (minutes < 1) return `Measured just now · ${time}`;
    if (minutes < 60) return `Measured ${minutes} min ago · ${time}`;
    if (dayKey(date) === dayKey(new Date())) {
      const hours = Math.floor(minutes / 60);
      const rest = minutes % 60;
      return `Measured ${hours} h${rest ? ` ${rest} min` : ""} ago · ${time}`;
    }
    if (dayKey(date) === dayKey(new Date(Date.now() - 86400000))) {
      return `Measured yesterday at ${time}`;
    }
    const day = new Intl.DateTimeFormat(language, { weekday: "short", day: "numeric", month: "short", timeZone }).format(date);
    return `Measured ${day} at ${time}`;
  }

  _levelSetting(entityId, label, help) {
    const state = this._state(entityId);
    const value = Number(state?.state);
    const max = Number(state?.attributes?.max ?? this._panel?.config?.tank_capacity ?? 10000);
    const control = state
      ? `<label class="level-input"><input type="number" inputmode="numeric" min="0" max="${max}" step="10" value="${Number.isFinite(value) ? value : ""}" data-level-entity="${this._escape(entityId)}" aria-label="${this._escape(label)} in litres"><span>L</span></label>`
      : `<div class="hint">Setting not found</div>`;
    return `<article class="card setting"><div class="metric-label">${this._escape(label)}</div>${control}<div class="hint">${this._escape(help)}</div></article>`;
  }

  async _setNumber(entityId, value) {
    if (!entityId || !Number.isFinite(value)) return;
    try {
      await this._hass.callService("number", "set_value", { entity_id: entityId, value });
      this._settingError = null;
    } catch (error) {
      this._settingError = error.message || "Could not save the setting.";
      this._render();
    }
  }

  _projectionChart(points, reorderLevel, emptyLevel) {
    let valid = Array.isArray(points)
      ? points.filter((point) => Number.isFinite(point.litres) && Number.isFinite(point.cautious) && point.date)
      : [];
    // Stop a couple of weeks after the expected level reaches the empty level
    // (or zero), so the useful part of the projection fills the chart.
    const floor = Number.isFinite(emptyLevel) ? emptyLevel : 0;
    const emptyIndex = valid.findIndex((point) => point.litres <= floor);
    if (emptyIndex > 0) valid = valid.slice(0, Math.min(valid.length, emptyIndex + 5));
    if (valid.length < 2) {
      return this._empty("The projection appears once the 7-day estimate is available.");
    }
    const width = 900;
    const height = 260;
    const left = 52;
    const right = 20;
    const top = 16;
    const bottom = 34;
    const innerW = width - left - right;
    const innerH = height - top - bottom;
    const maxY = Math.max(50, valid[0].litres, Number.isFinite(reorderLevel) ? reorderLevel : 0) * 1.12;
    const x = (index) => left + (index / (valid.length - 1)) * innerW;
    const y = (litres) => top + innerH - (Math.max(0, litres) / maxY) * innerH;
    const line = valid.map((point, index) => `${index ? "L" : "M"}${x(index).toFixed(1)},${y(point.litres).toFixed(1)}`).join(" ");
    const band = `${valid.map((point, index) => `${index ? "L" : "M"}${x(index).toFixed(1)},${y(point.litres).toFixed(1)}`).join(" ")} ${valid.map((point, index) => [index, point]).reverse().map(([index, point]) => `L${x(index).toFixed(1)},${y(point.cautious).toFixed(1)}`).join(" ")} Z`;
    const levelLine = (level, cls, label) =>
      Number.isFinite(level) && level > 0 && level < maxY
        ? `<line x1="${left}" y1="${y(level)}" x2="${width - right}" y2="${y(level)}" class="${cls}" /><text x="${width - right}" y="${y(level) - 5}" text-anchor="end" class="axis-label">${label} ${this._format(level)} L</text>`
        : "";
    const monthFormat = new Intl.DateTimeFormat(this._hass?.locale?.language, { month: "short" });
    let lastMonth = null;
    const monthTicks = valid
      .map((point, index) => {
        const [year, month, day] = point.date.split("-").map(Number);
        if (index === 0 || month === lastMonth) {
          lastMonth = month;
          return "";
        }
        lastMonth = month;
        return `<line x1="${x(index)}" y1="${top}" x2="${x(index)}" y2="${top + innerH}" class="grid-line" /><text x="${x(index) + 4}" y="${height - 10}" class="axis-label">${monthFormat.format(new Date(year, month - 1, day))}</text>`;
      })
      .join("");
    const hovers = valid
      .map((point, index) => `<circle cx="${x(index)}" cy="${y(point.litres)}" r="6" class="hover-dot"><title>${this._formatDate(point.date)}: about ${this._format(point.litres)} L (cautious ${this._format(point.cautious)} L)</title></circle>`)
      .join("");
    return `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Projected oil level over the coming months">
      <line x1="${left}" y1="${top + innerH}" x2="${width - right}" y2="${top + innerH}" class="axis" />
      <line x1="${left}" y1="${top}" x2="${left}" y2="${top + innerH}" class="axis" />
      ${monthTicks}
      <path d="${band}" class="projection-band" />
      ${levelLine(reorderLevel, "reorder-line", "Reorder")}
      ${levelLine(emptyLevel, "empty-line", "Empty")}
      <path d="${line}" class="oil-line" />
      ${hovers}
      <text x="${left}" y="${height - 10}" class="axis-label">Today</text>
      <text x="${left - 8}" y="${y(maxY / 1.12) + 4}" text-anchor="end" class="axis-label">${this._format(maxY / 1.12)}</text>
      <text x="${left - 8}" y="${top + innerH + 4}" text-anchor="end" class="axis-label">0</text>
      <text x="8" y="${top + 8}" class="axis-label">L</text>
    </svg>`;
  }

  _setupCheck(rows) {
    if (!Array.isArray(rows) || !rows.length) {
      return `<div class="empty">The setup check appears after Oil Monitor's next update.</div>`;
    }
    const icon = { ok: "✓", warning: "!", problem: "✕" };
    return `<div class="checks">${rows
      .map(
        (row) => `<div class="check ${this._escape(row.status)}"><span class="check-icon" aria-label="${this._escape(row.status)}">${icon[row.status] || "?"}</span><div><b>${this._escape(row.label)}</b> <span class="check-entity">${this._escape(row.entity_id)}</span><div class="check-message">${this._escape(row.message)}</div></div></div>`,
      )
      .join("")}</div>`;
  }

  _formatDate(isoDate) {
    if (!isoDate) return null;
    const [year, month, day] = String(isoDate).split("-").map(Number);
    if (!year || !month || !day) return null;
    return new Intl.DateTimeFormat(this._hass?.locale?.language, {
      weekday: "short",
      day: "numeric",
      month: "short",
    }).format(new Date(year, month - 1, day));
  }

  // Card answering "how long will the oil last?" from the run-out sensors.
  _runOutCard(reorderState, emptyState) {
    const emptyAttrs = emptyState?.attributes || {};
    const reorderAttrs = reorderState?.attributes || {};
    const emptyDays = Number(emptyState?.state);
    const reorderDays = Number(reorderState?.state);
    const hasEmpty = emptyState && Number.isFinite(emptyDays) && emptyState.state !== "";
    const hasReorder = reorderState && Number.isFinite(reorderDays) && reorderState.state !== "";
    const level = (attrs, fallback) => this._format(attrs.level_litres ?? fallback);

    let metric = "—";
    let unit = "";
    let warn = false;
    const hints = [];
    if (hasEmpty) {
      metric = this._format(Math.floor(emptyDays));
      unit = Math.floor(emptyDays) === 1 ? "day" : "days";
      warn = emptyDays < 14;
      const date = this._formatDate(emptyAttrs.date);
      hints.push(emptyDays <= 0 ? `At or below the empty level (${level(emptyAttrs, 50)} L)` : `Empty (${level(emptyAttrs, 50)} L) around ${date}`);
    } else if (emptyAttrs.more_than_a_year) {
      metric = "Over a year";
    } else {
      hints.push("Needs the 7-day estimate to project");
    }
    if (hasReorder) {
      if (reorderDays <= 0) {
        hints.push(`Below the reorder level (${level(reorderAttrs, 250)} L): order now`);
        warn = true;
      } else {
        const orderBy = this._formatDate(reorderAttrs.earliest_date || reorderAttrs.date);
        hints.push(`Order by ${orderBy} (${level(reorderAttrs, 250)} L reorder level)`);
      }
    }
    return `<article class="card"><div class="metric-label">Oil lasts about</div><div class="metric ${warn ? "warn" : ""}">${metric} <span class="unit">${unit}</span></div>${hints.map((hint) => `<div class="hint">${this._escape(hint)}</div>`).join("")}</article>`;
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

  // Convert a tank sensor reading to litres, mirroring the integration's
  // model.reading_to_litres so history charts match the sensors.
  _readingToLitres(value) {
    const config = this._panel?.config || {};
    const tank = config.tank || {};
    const capacity = Number(config.tank_capacity || 1000);
    if (!Number.isFinite(value)) return null;
    const type = tank.level_type || "distance";
    if (type === "litres") return value >= 0 ? value : null;
    if (type === "percent") return value >= 0 && value <= 110 ? (capacity * Math.min(100, value)) / 100 : null;
    if (value <= 0) return null;
    let percent;
    if ((tank.shape || "custom") === "custom") {
      const table = (tank.calibration || []).map((pair) => pair.map(Number));
      if (table.length < 2) return null;
      if (value <= table[0][0]) percent = table[0][1];
      else if (value >= table.at(-1)[0]) percent = table.at(-1)[1];
      else {
        for (let i = 0; i < table.length - 1; i += 1) {
          const [d0, p0] = table[i];
          const [d1, p1] = table[i + 1];
          if (value >= d0 && value <= d1) {
            percent = p0 + ((value - d0) / (d1 - d0)) * (p1 - p0);
            break;
          }
        }
      }
    } else {
      const full = Number(tank.full_distance_cm);
      const empty = Number(tank.empty_distance_cm);
      if (!(empty > full)) return null;
      const h = Math.min(1, Math.max(0, (empty - value) / (empty - full)));
      if (tank.shape === "horizontal_cylinder") {
        const angle = 2 * Math.acos(1 - 2 * h);
        percent = (100 * (angle - Math.sin(angle))) / (2 * Math.PI);
      } else {
        percent = 100 * h;
      }
    }
    return (capacity * percent) / 100;
  }

  // The ultrasonic temperature correction the integration decided on, if any.
  _tempCorrection() {
    const config = this._panel?.config || {};
    const correction = this._state(this._findEntity(config.stale_entity, "tank sensor stale"))
      ?.attributes?.temp_correction;
    return correction?.active && Number.isFinite(correction.reference_c) ? correction : null;
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
      ["air", this._tempCorrection()?.entity_id],
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
    const correction = this._tempCorrection();
    const airIsFahrenheit =
      correction && this._state(correction.entity_id)?.attributes?.unit_of_measurement === "°F";
    for (const point of byDay.values()) {
      let reading = point.depth;
      if (correction && Number.isFinite(reading) && Number.isFinite(point.air)) {
        const airC = airIsFahrenheit ? ((point.air - 32) * 5) / 9 : point.air;
        reading *= Math.sqrt((273.15 + airC) / (273.15 + correction.reference_c));
      }
      point.litres = this._readingToLitres(reading);
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
            index > 0 &&
            point.litres - interval[index - 1].litres >
              capacity * 0.05,
        );
        const oilUsed =
          anchor.litres - days.at(-1).litres;
        if (!refill && Number.isFinite(oilUsed) && oilUsed >= 0 && oilUsed <= capacity * 0.3) {
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

  // Fit litres = base + slope * max(0, switchOff - outdoor): flat hot-water use
  // above the heating switch-off temperature, rising linearly below it. The
  // switch-off temperature is chosen by grid search for the best fit.
  _switchOffFit(rows) {
    const temps = rows.map((row) => row.outdoor);
    const lowest = Math.min(...temps);
    const highest = Math.max(...temps);
    let best = null;
    for (let candidate = lowest; candidate <= highest + 0.01; candidate += 0.25) {
      // Need a few heating weeks below the switch-off to estimate the slope.
      if (rows.filter((row) => row.outdoor < candidate).length < 3) continue;
      const xs = rows.map((row) => Math.max(0, candidate - row.outdoor));
      const meanX = xs.reduce((a, b) => a + b, 0) / xs.length;
      const meanY = rows.reduce((a, row) => a + row.litres, 0) / rows.length;
      const varX = xs.reduce((a, x) => a + (x - meanX) ** 2, 0);
      let slope = varX ? xs.reduce((a, x, i) => a + (x - meanX) * (rows[i].litres - meanY), 0) / varX : 0;
      slope = Math.max(0, slope);
      const base = Math.max(0, meanY - slope * meanX);
      const sse = rows.reduce((a, row, i) => a + (row.litres - (base + slope * xs[i])) ** 2, 0);
      if (!best || sse < best.sse - 1e-9 * (1 + best.sse)) best = { base, slope, switchOff: candidate, sse };
    }
    return best || { base: rows.reduce((a, row) => a + row.litres, 0) / rows.length, slope: 0, switchOff: highest, sse: 0 };
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
    const fit = this._switchOffFit(valid);
    const curveAt = (temp) => fit.base + fit.slope * Math.max(0, fit.switchOff - temp);
    const knee = Math.min(maxX, Math.max(minX, fit.switchOff));
    const trend = `M${px(minX)},${py(curveAt(minX))} L${px(knee)},${py(curveAt(knee))} L${px(maxX)},${py(curveAt(maxX))}`;
    const showsSwitchOff = fit.switchOff < maxX - 1;
    const switchOffLabel = showsSwitchOff
      ? `<line x1="${px(knee)}" y1="${top}" x2="${px(knee)}" y2="${height - bottom}" class="knee-line" />
      <text x="${px(knee) + 4}" y="${top + 8}" class="axis-label">heating off ≈ ${this._format(fit.switchOff, 0)}°</text>
      <text x="${width - right}" y="${py(fit.base) - 6}" text-anchor="end" class="axis-label">hot water ≈ ${this._format(fit.base, 0)} L/week</text>`
      : "";
    const dots = valid.map((row) => `<circle cx="${px(row.outdoor)}" cy="${py(row.litres)}" r="5" class="scatter-dot"><title>${row.day}: ${this._format(row.litres, 1)} L/week at ${this._format(row.outdoor, 1)} °C</title></circle>`).join("");
    return `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Weekly oil use compared with outdoor temperature">
      <line x1="${left}" y1="${height - bottom}" x2="${width - right}" y2="${height - bottom}" class="axis" />
      <line x1="${left}" y1="${top}" x2="${left}" y2="${height - bottom}" class="axis" />
      <path d="${trend}" class="trend-line" />${switchOffLabel}${dots}
      <text x="${left}" y="${height - 8}" class="axis-label">${this._format(minX, 0)}°</text>
      <text x="${width - right}" y="${height - 8}" text-anchor="end" class="axis-label">${this._format(maxX, 0)}° outdoor</text>
      <text x="8" y="${top + 8}" class="axis-label">L/week</text>
    </svg>`;
  }

  _render() {
    if (!this.shadowRoot || !this._hass || !this._panel) return;
    // Don't replace an input the user is typing in; redraw once they leave it.
    if (this._editing) {
      this._renderPending = true;
      return;
    }
    this._renderPending = false;
    const config = this._panel.config || {};
    const litresId = this._findEntity(config.litres_entity, "litres remaining");
    const weeklyId = this._findEntity(config.weekly_entity, "oil use next 7 days");
    const lowerId = this._findEntity(config.lower_entity, "lower estimate");
    const upperId = this._findEntity(config.upper_entity, "upper estimate");
    const staleId = this._findEntity(config.stale_entity, "tank sensor stale");
    const litresState = this._state(litresId);
    const weekly = this._state(weeklyId)?.state;
    const lower = this._state(lowerId)?.state;
    const upper = this._state(upperId)?.state;
    const stale = this._state(staleId)?.state === "on";
    const outdoor = this._state(config.outdoor_entity);
    const indoor = this._state(config.indoor_entity);
    const runtime = this._state(config.runtime_entity);
    const target = this._state(config.target_entity);
    const capacity = Number(config.tank_capacity || 1000);
    const litres = Number(litresState?.state);
    const percent = Number.isFinite(Number(litres)) ? Math.min(100, (Number(litres) / capacity) * 100) : 0;
    let weeks = [];
    this._chartError = null;
    try {
      weeks = this._weeklyRows();
    } catch (error) {
      this._chartError = error.message || String(error);
    }
    const staleAttrs = this._state(staleId)?.attributes || {};
    const sampleTime = staleAttrs.sample_time || this._state(config.timestamp_entity)?.state;
    const checks = Array.isArray(staleAttrs.setup_check) ? staleAttrs.setup_check : [];
    const problemCount = checks.filter((row) => row.status === "problem").length;
    const warningCount = checks.filter((row) => row.status === "warning").length;
    const checkSummary = problemCount
      ? `Setup check found ${problemCount} problem${problemCount === 1 ? "" : "s"}; see Setup check below.`
      : warningCount
        ? `Setup check has ${warningCount} note${warningCount === 1 ? "" : "s"}; see Setup check below.`
        : "";
    const hasEstimate = Number.isFinite(Number(weekly)) && weekly !== "";
    const trainingWeeks = Number(staleAttrs.training_weeks);
    const weeksNeeded = Number(staleAttrs.training_weeks_needed) || 6;
    const early = Boolean(this._state(weeklyId)?.attributes?.early_estimate);
    const estimateHint = hasEstimate
      ? `Forecast range ${this._format(lower)}–${this._format(upper)} L${early ? ` · early estimate from ${this._format(trainingWeeks)} weeks of history, improving as more is recorded` : ""}`
      : this._escape(
          [
            Number.isFinite(trainingWeeks) ? `${trainingWeeks} of ${weeksNeeded} clean weeks of history so far.` : "",
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
    const reorderState = this._state(this._findEntity(config.reorder_entity, "days until reorder"));
    const emptyState = this._state(this._findEntity(config.empty_entity, "days until empty"));
    const targetNumber = Number(targetValue);
    const offsetValue = Number.isFinite(offset) ? offset : 0;
    const assumedIndoor = Number.isFinite(targetNumber) ? targetNumber + offsetValue : null;
    const assumedHint = !Number.isFinite(targetNumber)
      ? "Thermostat target not available"
      : offsetValue
        ? `Thermostat ${this._format(targetNumber, 1)} ${targetUnit} ${offsetValue > 0 ? "+" : "−"} ${this._format(Math.abs(offsetValue), 1)} °C adjustment`
        : "Your current thermostat setting";
    const runtimeValue =runtime && runtime.state !== "unknown" && runtime.state !== "unavailable" ? `${this._format(runtime.state, 1)} ${runtime.attributes.unit_of_measurement || "h"}` : "—";

    this.shadowRoot.innerHTML = `
      <style>
        :host{display:block;color:var(--primary-text-color);background:var(--primary-background-color);min-height:100%;font-family:var(--paper-font-body1_-_font-family,Roboto,"Noto",sans-serif)}
        *{box-sizing:border-box}.page{max-width:1180px;margin:0 auto;padding:28px 24px 48px}.top{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:22px}.eyebrow{color:var(--secondary-text-color);font-size:12px;letter-spacing:.11em;text-transform:uppercase}.title{font-size:clamp(28px,4vw,38px);font-weight:650;margin:5px 0}.subtitle{color:var(--secondary-text-color);margin:0}.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px}.card{background:var(--card-background-color,var(--secondary-background-color));border:1px solid var(--divider-color);border-radius:16px;padding:18px;min-width:0}.metric-label{color:var(--secondary-text-color);font-size:13px}.metric{font-size:clamp(26px,3vw,34px);font-weight:650;margin:8px 0 3px}.unit{font-size:15px;font-weight:450;color:var(--secondary-text-color)}.hint{color:var(--secondary-text-color);font-size:12px}.level{height:6px;border-radius:6px;background:var(--divider-color);margin-top:14px;overflow:hidden}.level i{display:block;height:100%;background:var(--accent-color,#03a9f4);border-radius:inherit}.status{margin:14px 0;padding:12px 14px;border-radius:12px;background:var(--secondary-background-color);font-size:13px}.warn{color:var(--warning-color,#e6a700)}.section{margin-top:18px}.section h2{font-size:18px;margin:0 0 4px}.section p{color:var(--secondary-text-color);margin:0 0 12px;font-size:13px}.charts{display:grid;grid-template-columns:1.35fr 1fr;gap:14px}.chart-card{background:var(--card-background-color,var(--secondary-background-color));border:1px solid var(--divider-color);border-radius:16px;padding:18px;min-width:0}.chart-card h3{margin:0;font-size:16px}.chart-card p{margin:5px 0 12px;color:var(--secondary-text-color);font-size:12px}svg{display:block;width:100%;height:auto;overflow:visible}.axis{stroke:var(--divider-color);stroke-width:1}.axis-label{fill:var(--secondary-text-color);font-size:11px}.oil-line{fill:none;stroke:var(--accent-color,#03a9f4);stroke-width:3}.temp-line{fill:none;stroke:var(--orange-color,#ff9800);stroke-width:2.4;stroke-dasharray:6 4}.oil-bar{fill:var(--accent-color,#03a9f4);opacity:.2}.temp-dot{fill:var(--orange-color,#ff9800)}.scatter-dot{fill:var(--accent-color,#03a9f4);opacity:.75}.trend-line{fill:none;stroke:var(--orange-color,#ff9800);stroke-width:2;stroke-dasharray:5 4}.knee-line{stroke:var(--divider-color);stroke-width:1;stroke-dasharray:2 3}.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--secondary-text-color);margin-top:8px}.key{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;background:var(--accent-color,#03a9f4)}.key.temp{background:var(--orange-color,#ff9800)}.toolbar{display:flex;align-items:center;gap:8px}.toolbar select,.toolbar button{color:var(--primary-text-color);background:var(--card-background-color,var(--secondary-background-color));border:1px solid var(--divider-color);border-radius:9px;padding:9px 11px;font:inherit}.empty{padding:24px 8px;color:var(--secondary-text-color);font-size:13px}.details{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.detail{padding:13px;background:var(--secondary-background-color);border-radius:12px}.detail b{display:block;font-size:18px;margin-top:5px}.grid.three{grid-template-columns:repeat(3,minmax(0,1fr))}.chart-card.wide{margin-top:14px}.setting .hint{margin-top:8px;line-height:1.4}.level-input{display:flex;align-items:center;gap:8px;margin:10px 0 2px;font-size:15px;color:var(--secondary-text-color)}.level-input input{width:7.5em;font:inherit;font-size:22px;font-weight:600;color:var(--primary-text-color);background:var(--secondary-background-color);border:1px solid var(--divider-color);border-radius:9px;padding:6px 10px}.stepper{display:flex;align-items:center;gap:12px;margin:10px 0 2px}.stepper b{font-size:22px;min-width:4.5em;text-align:center}.stepper button{width:40px;height:40px;border-radius:50%;font-size:20px;line-height:1;cursor:pointer;color:var(--primary-text-color);background:var(--secondary-background-color);border:1px solid var(--divider-color)}.projection-band{fill:var(--accent-color,#03a9f4);opacity:.15}.reorder-line{stroke:var(--warning-color,#e6a700);stroke-width:1.5;stroke-dasharray:6 4}.empty-line{stroke:var(--error-color,#db4437);stroke-width:1.5;stroke-dasharray:6 4}.grid-line{stroke:var(--divider-color);stroke-width:1;opacity:.5}.hover-dot{fill:transparent}.key.band{background:var(--accent-color,#03a9f4);opacity:.3}.key.reorder{background:var(--warning-color,#e6a700)}.key.stop{background:var(--error-color,#db4437)}.checks{display:grid;gap:8px}.check{display:flex;gap:12px;align-items:flex-start;padding:12px 14px;border-radius:12px;background:var(--card-background-color,var(--secondary-background-color));border:1px solid var(--divider-color)}.check-icon{flex:none;width:24px;height:24px;border-radius:50%;display:grid;place-items:center;font-weight:700;font-size:13px;color:#fff;background:var(--success-color,#43a047)}.check.warning .check-icon{background:var(--warning-color,#e6a700)}.check.problem .check-icon{background:var(--error-color,#db4437)}.check-entity{color:var(--secondary-text-color);font-size:12px;margin-left:4px;word-break:break-all}.check-message{color:var(--secondary-text-color);font-size:13px;margin-top:3px;line-height:1.4}.foot{margin-top:16px;color:var(--secondary-text-color);font-size:12px;line-height:1.5}@media(max-width:850px){.grid,.grid.three{grid-template-columns:repeat(2,minmax(0,1fr))}.charts{grid-template-columns:1fr}}@media(max-width:520px){.grid.three{grid-template-columns:1fr}.page{padding:20px 14px 36px}.grid{gap:9px}.card{padding:14px}.metric{font-size:25px}.details{grid-template-columns:repeat(2,1fr)}.top{align-items:center}}
      </style>
      <main class="page">
        <header class="top"><div><div class="eyebrow">Home energy · heating oil</div><h1 class="title">Oil Monitor</h1><p class="subtitle">Your tank, usage trends and weather-aware outlook</p></div><div class="toolbar"><button type="button" class="refresh" title="Refresh history">↻</button></div></header>
        <section class="section"><h2>Right now</h2><p>Measured by your sensors.</p><div class="grid">
          <article class="card"><div class="metric-label">${stale ? "Last known oil remaining" : "Oil remaining"}</div><div class="metric">${this._format(litres)} <span class="unit">L</span></div><div class="hint">of ${this._format(capacity)} L usable capacity</div><div class="level"><i style="width:${percent}%"></i></div></article>
          <article class="card"><div class="metric-label">Tank reading</div><div class="metric ${stale ? "warn" : ""}">${stale ? "Check sensor" : "Up to date"}</div><div class="hint" title="${this._escape(sampleTime || "")}">${this._escape(this._friendlySampleTime(sampleTime))}</div></article>
          <article class="card"><div class="metric-label">Indoor temperature</div><div class="metric">${this._format(indoor?.state, 1)} <span class="unit">${indoor?.attributes?.unit_of_measurement || "°C"}</span></div><div class="hint">Thermostat set to ${this._format(targetValue, 1)} ${targetUnit}</div></article>
          <article class="card"><div class="metric-label">Outdoor temperature</div><div class="metric">${this._format(outdoor?.state, 1)} <span class="unit">${outdoor?.attributes?.unit_of_measurement || "°C"}</span></div><div class="hint">Heating ran ${runtimeValue} today</div></article>
        </div>
        <div class="status ${stale || problemCount ? "warn" : ""}">${status}${checkSummary ? ` ${this._escape(checkSummary)}` : ""}${this._loading ? " Loading recorder history…" : ""}${this._error ? ` History: ${this._escape(this._error)}` : ""}${this._settingError ? ` Settings: ${this._escape(this._settingError)}` : ""}</div></section>

        <section class="section"><h2>Estimates</h2><p>Predicted from your history and the weather forecast.</p><div class="grid three">
          ${this._runOutCard(reorderState, emptyState)}
          <article class="card"><div class="metric-label">Estimated use · next 7 days</div><div class="metric">${this._format(weekly)} <span class="unit">L</span></div><div class="hint">${estimateHint}</div></article>
          <article class="card"><div class="metric-label">Estimates assume indoors</div><div class="metric">${this._format(assumedIndoor, 1)} <span class="unit">${targetUnit}</span></div><div class="hint">${assumedHint}</div></article>
        </div>
        <article class="chart-card wide"><h3>Projected oil level</h3><p>Expected litres over the coming months. The shaded band runs down to a cautious estimate using the high end of the usage range.</p>${this._projectionChart(emptyState?.attributes?.projection, Number(reorderState?.attributes?.level_litres), Number(emptyState?.attributes?.level_litres))}<div class="legend"><span><i class="key"></i>Expected level</span><span><i class="key band"></i>Cautious range</span><span><i class="key reorder"></i>Reorder level</span><span><i class="key stop"></i>Empty level</span></div></article>
        <p class="foot">Estimates are learned from your tank, temperature and heating history. The first week follows the weather forecast; later weeks assume temperatures like the same time last year. Use them as a planning aid, not as the only basis for ordering oil.</p></section>

        <section class="section"><h2>Settings</h2><p>Changes apply to the estimates straight away.</p><div class="grid three">
          ${this._levelSetting(config.reorder_level_entity, "Reorder level", "Order more oil when the tank reaches this level.")}
          ${this._levelSetting(config.empty_level_entity, "Empty level", "Treat the tank as empty here (the pump/sludge line).")}
          <article class="card setting"><div class="metric-label">Forecast temperature adjustment</div><div class="stepper"><button type="button" data-offset-step="-0.5" aria-label="Decrease adjustment">−</button><b>${Number.isFinite(offset) ? `${offset > 0 ? "+" : ""}${this._format(offset, 1)}` : "—"} °C</b><button type="button" data-offset-step="0.5" aria-label="Increase adjustment">+</button></div><div class="hint">"What if" the house were warmer or cooler than the thermostat. Leave at 0 for normal estimates.</div></article>
        </div></section>

        <section class="section"><h2>Setup check</h2><p>Each sensor Oil Monitor uses, and anything that stops it working well. To report a problem, use Download diagnostics on the Oil Monitor integration page.</p>${this._setupCheck(checks)}</section>

        <section class="section"><h2>History</h2><p>Weekly tank-level changes are converted with your tank calibration and compared with weekly outdoor temperatures. Refill and suspect weeks are excluded.</p><div class="charts">
          <article class="chart-card"><h3>Weekly oil use & outdoor temperature</h3><p>Bars and blue line: litres used · orange: mean outdoor temperature</p>${this._chart(weeks)}<div class="legend"><span><i class="key"></i>Oil use (L/week)</span><span><i class="key temp"></i>Outdoor temperature</span></div></article>
          <article class="chart-card"><h3>Oil use vs outdoor temperature</h3><p>Each point is a clean week. The dashed curve flattens to hot-water-only use above the temperature where heating switches off.</p>${this._scatter(weeks)}</article>
        </div></section>
        <p class="foot">Oil Monitor ${config.version ? `v${this._escape(config.version)}` : ""} · build ${this._escape(config.build || "unknown")}</p>
      </main>`;

    this.shadowRoot.querySelectorAll("[data-level-entity]").forEach((input) => {
      input.addEventListener("focus", () => { this._editing = true; });
      input.addEventListener("blur", () => {
        this._editing = false;
        if (this._renderPending) this._render();
      });
      input.addEventListener("change", () => {
        const max = Number(input.max);
        const value = Math.min(Number.isFinite(max) ? max : Infinity, Math.max(0, Number(input.value)));
        this._setNumber(input.dataset.levelEntity, value);
      });
      input.addEventListener("keydown", (event) => { if (event.key === "Enter") input.blur(); });
    });
    this.shadowRoot.querySelectorAll("[data-offset-step]").forEach((button) => {
      button.addEventListener("click", () => {
        const current = Number(this._state(config.offset_entity)?.state) || 0;
        const next = Math.min(3, Math.max(-3, current + Number(button.dataset.offsetStep)));
        this._setNumber(config.offset_entity, next);
      });
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
