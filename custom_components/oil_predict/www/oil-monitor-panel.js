class OilMonitorPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._rangeDays = 365;
    this._statistics = null;
    this._loading = false;
    this._lastFetch = 0;
    this._boiler = null;
    this._boilerRecent = null;
    this._boilerFetch = 0;
    try {
      this._tab = window.localStorage.getItem("oil-monitor-tab") || "overview";
    } catch (error) {
      this._tab = "overview";
    }
  }

  _setTab(tab) {
    this._tab = tab;
    try {
      window.localStorage.setItem("oil-monitor-tab", tab);
    } catch (error) {
      // Private windows may block storage; the tab still switches.
    }
    if (tab === "boiler") this._loadBoiler();
    this._render();
  }

  async _loadBoiler(force = false) {
    const config = this._panel?.config || {};
    if (!this._hass || !config.flow_entity || this._boilerLoading) return;
    if (!force && Date.now() - this._boilerFetch < 5 * 60 * 1000) return;
    this._boilerLoading = true;
    const end = new Date();
    const start = new Date(end.getTime() - 48 * 3600000);
    try {
      const [analysis, recent] = await Promise.all([
        this._hass.callWS({ type: "oil_predict/boiler" }),
        this._hass.callWS({
          type: "recorder/statistics_during_period",
          start_time: start.toISOString(),
          end_time: end.toISOString(),
          statistic_ids: [config.flow_entity, config.return_entity, config.indoor_entity].filter(Boolean),
          period: "5minute",
          types: ["mean"],
        }),
      ]);
      this._boiler = analysis;
      this._boilerRecent = recent;
      this._boilerError = null;
      this._boilerFetch = Date.now();
    } catch (error) {
      this._boilerError = error.message || "Could not load the boiler analysis.";
    } finally {
      this._boilerLoading = false;
      this._render();
    }
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
    if (this._tab === "boiler") this._loadBoiler();
  }

  set panel(panel) {
    this._panel = panel;
    if (this._tab === "boiler") this._loadBoiler();
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
      "flow_entity", "return_entity", "efficiency_entity",
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
    const all = Array.isArray(points) ? points.filter((point) => point && point.date) : [];
    const isFuture = (point) => Number.isFinite(point.litres) && Number.isFinite(point.mild) && Number.isFinite(point.cold);
    // Recorded levels since the last fill come first, then the projection.
    let past = all.filter((point) => !isFuture(point) && Number.isFinite(point.actual));
    let future = all.filter(isFuture);
    // Stop a couple of weeks after even a mild winter reaches the empty
    // level (or zero), so the useful part of the projection fills the chart.
    const floor = Number.isFinite(emptyLevel) ? emptyLevel : 0;
    const emptyIndex = future.findIndex((point) => point.mild <= floor);
    if (emptyIndex > 0) future = future.slice(0, Math.min(future.length, emptyIndex + 5));
    // Short view: the last two weeks and the next three months, easier to
    // read on a phone than the full season.
    if (this._projectionShort()) {
      past = past.slice(-14);
      future = future.slice(0, 91);
    }
    if (future.length < 2) {
      return this._empty("The projection appears once the 7-day estimate is available.");
    }
    const valid = [...past, ...future];
    const todayIndex = past.length;
    const width = 900;
    const height = 260;
    const left = 52;
    const right = 20;
    const top = 16;
    const bottom = 34;
    const innerW = width - left - right;
    const innerH = height - top - bottom;
    const peak = Math.max(future[0].litres, ...past.map((point) => point.actual));
    const maxY = Math.max(50, peak, Number.isFinite(reorderLevel) ? reorderLevel : 0) * 1.12;
    const x = (index) => left + (index / (valid.length - 1)) * innerW;
    const y = (litres) => top + innerH - (Math.max(0, litres) / maxY) * innerH;
    const pathFor = (key) => future.map((point, i) => `${i ? "L" : "M"}${x(todayIndex + i).toFixed(1)},${y(point[key]).toFixed(1)}`).join(" ");
    const line = pathFor("litres");
    const historyLine = past.length
      ? `<path d="${[...past.map((point, index) => [index, point.actual]), [todayIndex, future[0].litres]].map(([index, value], i) => `${i ? "L" : "M"}${x(index).toFixed(1)},${y(value).toFixed(1)}`).join(" ")}" class="history-line" />`
      : "";
    const lastYearPoints = valid
      .map((point, index) => [index, point.last_year])
      .filter(([, value]) => Number.isFinite(value));
    const lastYearLine = lastYearPoints.length > 1
      ? `<path d="${lastYearPoints.map(([index, value], i) => `${i ? "L" : "M"}${x(index).toFixed(1)},${y(value).toFixed(1)}`).join(" ")}" class="last-year-line" />`
      : "";
    const mildLine = pathFor("mild");
    const coldLine = pathFor("cold");
    const band = `${mildLine} ${future.map((point, i) => [todayIndex + i, point]).reverse().map(([index, point]) => `L${x(index).toFixed(1)},${y(point.cold).toFixed(1)}`).join(" ")} Z`;
    const levelLine = (level, cls, label) =>
      Number.isFinite(level) && level > 0 && level < maxY
        ? `<line x1="${left}" y1="${y(level)}" x2="${width - right}" y2="${y(level)}" class="${cls}" /><text x="${width - right}" y="${y(level) - 5}" text-anchor="end" class="axis-label">${label} ${this._format(level)} L</text>`
        : "";
    const monthFormat = new Intl.DateTimeFormat(this._hass?.locale?.language, { month: "short" });
    let lastMonth = null;
    const monthTicks = valid
      .map((point, index) => {
        const [year, month, day] = point.date.split("-").map(Number);
        if (index === 0 || index === todayIndex || month === lastMonth) {
          lastMonth = month;
          return "";
        }
        lastMonth = month;
        return `<line x1="${x(index)}" y1="${top}" x2="${x(index)}" y2="${top + innerH}" class="grid-line" /><text x="${x(index) + 4}" y="${height - 10}" class="axis-label">${monthFormat.format(new Date(year, month - 1, day))}</text>`;
      })
      .join("");
    // Full-height hover columns: hovering anywhere above a day shows a guide
    // line, a marker on the line and a label with the date and level.
    const step = innerW / (valid.length - 1);
    const hoverColumn = (index, value, label, detail) => {
      const cx = x(index);
      const flip = cx > width - 200;
      const tx = flip ? cx - 8 : cx + 8;
      const anchor = flip ? "end" : "start";
      return `<g class="hover-col"><rect x="${(cx - step / 2).toFixed(1)}" y="${top}" width="${step.toFixed(2)}" height="${innerH}" class="hover-hit" /><line x1="${cx}" y1="${top}" x2="${cx}" y2="${top + innerH}" class="hover-guide" /><circle cx="${cx}" cy="${y(value)}" r="4" class="hover-marker" /><text x="${tx}" y="${top + 14}" text-anchor="${anchor}" class="hover-label">${label}</text><title>${label}${detail}</title></g>`;
    };
    const pastHovers = past
      .map((point, index) => hoverColumn(index, point.actual, `${this._formatDate(point.date)}: ${this._format(point.actual)} L recorded`, ""))
      .join("");
    const hovers = pastHovers + future
      .map((point, i) => [todayIndex + i, point])
      .map(([index, point]) => hoverColumn(index, point.litres, `${this._formatDate(point.date)}: about ${this._format(point.litres)} L`, ` in a typical winter (cold ${this._format(point.cold)} L, mild ${this._format(point.mild)} L${Number.isFinite(point.last_year) ? `; ${this._format(point.last_year)} L at last year's usage` : ""})`))
      .join("");
    return `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Projected oil level over the coming months">
      <line x1="${left}" y1="${top + innerH}" x2="${width - right}" y2="${top + innerH}" class="axis" />
      <line x1="${left}" y1="${top}" x2="${left}" y2="${top + innerH}" class="axis" />
      ${monthTicks}
      <path d="${band}" class="projection-band" />
      ${levelLine(reorderLevel, "reorder-line", "Reorder")}
      ${levelLine(emptyLevel, "empty-line", "Empty")}
      <path d="${mildLine}" class="scenario-line" />
      <path d="${coldLine}" class="scenario-line" />
      ${lastYearLine}
      ${historyLine}
      <path d="${line}" class="oil-line" />
      ${hovers}
      ${todayIndex ? `<line x1="${x(todayIndex)}" y1="${top}" x2="${x(todayIndex)}" y2="${top + innerH}" class="today-line" />` : ""}
      <text x="${x(todayIndex) + (todayIndex ? 4 : 0)}" y="${height - 10}" class="axis-label">Today</text>
      <text x="${left - 8}" y="${y(maxY / 1.12) + 4}" text-anchor="end" class="axis-label">${this._format(maxY / 1.12)}</text>
      <text x="${left - 8}" y="${top + innerH + 4}" text-anchor="end" class="axis-label">0</text>
      <text x="8" y="${top + 8}" class="axis-label">L</text>
    </svg>`;
  }

  _projectionShort() {
    return this._projectionView ? this._projectionView === "short" : Boolean(this._narrow);
  }

  _projectionToggle() {
    const short = this._projectionShort();
    return `<div class="view-toggle" role="group" aria-label="Chart range"><button type="button" data-projection-view="short" aria-pressed="${short}">Next 3 months</button><button type="button" data-projection-view="full" aria-pressed="${!short}">Full season</button></div>`;
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
      const coldDate = this._formatDate(emptyAttrs.earliest_date);
      const mildDate = this._formatDate(emptyAttrs.latest_date);
      let when = `around ${date}`;
      if (coldDate && mildDate && coldDate !== mildDate) when = `between ${coldDate} (cold winter) and ${mildDate} (mild)`;
      else if (coldDate && !mildDate) when = `from ${coldDate} in a cold winter`;
      hints.push(emptyDays <= 0 ? `At or below the empty level (${level(emptyAttrs, 50)} L)` : `Empty (${level(emptyAttrs, 50)} L) ${when}`);
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
    return `<article class="card"><div class="metric-label">Oil lasts about · typical winter</div><div class="metric ${warn ? "warn" : ""}">${metric} <span class="unit">${unit}</span></div>${hints.map((hint) => `<div class="hint">${this._escape(hint)}</div>`).join("")}</article>`;
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

  // A small general chart: lines over a numeric x axis, optional bars on a
  // second axis, an optional reference line and hover columns.
  // opts: { series:[{points:[[x,y]], cls}], bars:{points:[[x,y]], cls, unit},
  //         xTicks:[[x,label]], unit, ref:{y,label}, hover:[[x,y,label]], aria, height }
  _plot(opts) {
    const width = 900;
    const height = opts.height || 240;
    const left = 46;
    const right = opts.bars ? 46 : 18;
    const top = 16;
    const bottom = 30;
    const innerW = width - left - right;
    const innerH = height - top - bottom;
    const all = opts.series.flatMap((s) => s.points).filter((p) => Number.isFinite(p[0]) && Number.isFinite(p[1]));
    if (all.length < 2) return this._empty(opts.emptyText || "Not enough data yet.");
    const xs = [...all.map((p) => p[0]), ...(opts.bars?.points || []).map((p) => p[0])];
    const minX = Math.min(...xs);
    const maxX = Math.max(...xs);
    let minY = Math.min(...all.map((p) => p[1]), opts.ref ? opts.ref.y : Infinity);
    let maxY = Math.max(...all.map((p) => p[1]), opts.ref ? opts.ref.y : -Infinity);
    const pad = Math.max(1, (maxY - minY) * 0.1);
    minY = Math.floor(minY - pad);
    maxY = Math.ceil(maxY + pad);
    const x = (v) => left + ((v - minX) / (maxX - minX || 1)) * innerW;
    const y = (v) => top + innerH - ((v - minY) / (maxY - minY || 1)) * innerH;
    const lines = opts.series
      .map((s) => {
        const pts = s.points.filter((p) => Number.isFinite(p[0]) && Number.isFinite(p[1]));
        if (!pts.length) return "";
        // Break the line where there is a gap in the data.
        const gap = opts.gap ?? Infinity;
        const d = pts.map((p, i) => `${i && p[0] - pts[i - 1][0] <= gap ? "L" : "M"}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join(" ");
        return `<path d="${d}" class="${s.cls}" />`;
      })
      .join("");
    let bars = "";
    if (opts.bars) {
      const bmax = Math.max(1, ...opts.bars.points.map((p) => p[1])) * 1.15;
      const barW = Math.max(2, (innerW / Math.max(1, opts.bars.points.length)) * 0.6);
      const by = (v) => top + innerH - (v / bmax) * innerH;
      bars = opts.bars.points
        .map((p) => `<rect x="${(x(p[0]) - barW / 2).toFixed(1)}" y="${by(p[1]).toFixed(1)}" width="${barW.toFixed(1)}" height="${(top + innerH - by(p[1])).toFixed(1)}" rx="2" class="${opts.bars.cls}" />`)
        .join("");
      bars += `<text x="${width - right + 6}" y="${top + 8}" class="axis-label">${this._escape(opts.bars.unit)}</text><text x="${width - right + 6}" y="${by(bmax / 1.15) + 4}" class="axis-label">${this._format(bmax / 1.15, 0)}</text>`;
    }
    const ticks = (opts.xTicks || [])
      .map(([v, label]) => `<line x1="${x(v)}" y1="${top}" x2="${x(v)}" y2="${top + innerH}" class="grid-line" /><text x="${x(v) + 3}" y="${height - 9}" class="axis-label">${this._escape(label)}</text>`)
      .join("");
    const ref = opts.ref
      ? `<line x1="${left}" y1="${y(opts.ref.y)}" x2="${width - right}" y2="${y(opts.ref.y)}" class="ref-line" /><text x="${width - right - 4}" y="${y(opts.ref.y) - 5}" text-anchor="end" class="axis-label">${this._escape(opts.ref.label)}</text>`
      : "";
    const hoverPts = opts.hover || [];
    const step = hoverPts.length > 1 ? innerW / (hoverPts.length - 1) : innerW;
    const hovers = hoverPts
      .map(([hx, hy, label]) => {
        const cx = x(hx);
        const flip = cx > width - 260;
        return `<g class="hover-col"><rect x="${(cx - step / 2).toFixed(1)}" y="${top}" width="${step.toFixed(2)}" height="${innerH}" class="hover-hit" /><line x1="${cx}" y1="${top}" x2="${cx}" y2="${top + innerH}" class="hover-guide" />${Number.isFinite(hy) ? `<circle cx="${cx}" cy="${y(hy)}" r="4" class="hover-marker" />` : ""}<text x="${flip ? cx - 8 : cx + 8}" y="${top + 14}" text-anchor="${flip ? "end" : "start"}" class="hover-label">${this._escape(label)}</text><title>${this._escape(label)}</title></g>`;
      })
      .join("");
    return `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${this._escape(opts.aria || "Chart")}">
      <line x1="${left}" y1="${top + innerH}" x2="${width - right}" y2="${top + innerH}" class="axis" />
      <line x1="${left}" y1="${top}" x2="${left}" y2="${top + innerH}" class="axis" />
      ${ticks}${bars}${ref}${lines}${hovers}
      <text x="${left - 8}" y="${top + 4}" text-anchor="end" class="axis-label">${this._format(maxY)}</text>
      <text x="${left - 8}" y="${top + innerH + 4}" text-anchor="end" class="axis-label">${this._format(minY)}</text>
      <text x="6" y="${top + innerH / 2}" class="axis-label">${this._escape(opts.unit || "")}</text>
    </svg>`;
  }

  _dayNumber(iso) {
    const [year, month, day] = String(iso).split("-").map(Number);
    return Date.UTC(year, month - 1, day) / 86400000;
  }

  _dateTicks(isoDates, everyDays) {
    const fmt = new Intl.DateTimeFormat(this._hass?.locale?.language, { day: "numeric", month: "short", timeZone: "UTC" });
    const ticks = [];
    let last = -Infinity;
    for (const iso of isoDates) {
      const n = this._dayNumber(iso);
      if (n - last >= everyDays) {
        ticks.push([n, fmt.format(new Date(n * 86400000))]);
        last = n;
      }
    }
    return ticks;
  }

  // Live and 48-hour flow / return / room temperatures.
  _boilerRecentChart() {
    const config = this._panel?.config || {};
    const stats = this._boilerRecent;
    if (!stats) return this._empty(this._boilerLoading ? "Loading…" : "Recent history has not loaded yet.");
    const fahrenheit = (id) => this._state(id)?.attributes?.unit_of_measurement === "°F";
    const toC = (id, v) => (fahrenheit(id) ? ((v - 32) * 5) / 9 : v);
    const pts = (id) =>
      (stats[id] || [])
        .map((row) => {
          const start = Number(row.start);
          const ms = start > 1e11 ? start : start * 1000;
          return [ms / 3600000, Number.isFinite(row.mean) ? toC(id, row.mean) : NaN];
        })
        .filter((p) => Number.isFinite(p[1]));
    const flow = pts(config.flow_entity);
    const ret = config.return_entity ? pts(config.return_entity) : [];
    const room = pts(config.indoor_entity);
    if (flow.length < 2) {
      return this._empty("No 5-minute statistics for the flow sensor in the last 48 hours. It needs a state class of \"measurement\".");
    }
    const timeFmt = new Intl.DateTimeFormat(this._hass?.locale?.language, { weekday: "short", hour: "2-digit", minute: "2-digit", timeZone: this._hass?.config?.time_zone });
    const ticks = [];
    const first = Math.ceil(flow[0][0] / 6) * 6;
    for (let h = first; h <= flow.at(-1)[0]; h += 6) ticks.push([h, timeFmt.format(new Date(h * 3600000))]);
    const retAt = new Map(ret.map((p) => [p[0], p[1]]));
    const roomAt = new Map(room.map((p) => [p[0], p[1]]));
    // One hover column per 30 minutes keeps the chart light.
    const hover = flow
      .filter((_, i) => i % 6 === 0)
      .map(([h, v]) => {
        const parts = [`${timeFmt.format(new Date(h * 3600000))}: flow ${this._format(v, 1)} °C`];
        if (retAt.has(h)) parts.push(`return ${this._format(retAt.get(h), 1)} °C`);
        if (roomAt.has(h)) parts.push(`room ${this._format(roomAt.get(h), 1)} °C`);
        return [h, v, parts.join(" · ")];
      });
    return this._plot({
      series: [
        { points: room, cls: "room-line" },
        { points: ret, cls: "return-line" },
        { points: flow, cls: "flow-line" },
      ],
      xTicks: ticks,
      unit: "°C",
      gap: 0.5,
      hover,
      aria: "Flow, return and room temperature over the last 48 hours",
      height: 230,
    });
  }

  _boilerAdvice(b) {
    const tips = [];
    const s = b.summary || {};
    const eff = b.efficiency;
    if (b.efficiency_alert && eff) {
      tips.push(["warn", `Efficiency over the last 4 weeks is ${this._format(eff.latest_percent)} % of your usual: each litre is giving less heat than before. A service (nozzle, cleaning the heat exchanger) may be due.`]);
    } else if (eff && eff.change_per_year_percent <= -5 && eff.trend_t <= -2) {
      tips.push(["warn", `Efficiency has been drifting down by about ${this._format(-eff.change_per_year_percent)} % a year. Worth mentioning at the next service.`]);
    }
    const effect = b.return_effect;
    if (effect?.significant) {
      tips.push(["ok", `In your history, each 1 °C cooler return water gave about ${this._format(-effect.percent_per_c, 1)} % more heat per litre. A return 5 °C cooler would save roughly ${this._format(-effect.percent_per_c * 5)} % of heating oil.`]);
    }
    if (b.has_return && Number.isFinite(s.drop_c)) {
      if (s.drop_c < 8) {
        tips.push(["note", `The flow–return difference is small (${this._format(s.drop_c, 1)} °C): water is going round faster than the radiators need. Balancing the radiators or a lower pump speed would cool the return.`]);
      } else if (s.drop_c > 25) {
        tips.push(["note", `The flow–return difference is large (${this._format(s.drop_c, 1)} °C): the radiators may be starved of flow (pump speed, a sticking valve or sludge).`]);
      }
    }
    if (b.has_return && Number.isFinite(s.condensing_share) && s.condensing_share < 0.5) {
      tips.push(["note", `The return water was below 55 °C for only ${this._format(s.condensing_share * 100)} % of heating time. If your boiler is a condensing one, it condenses (and saves about 10 %) only below that, so a lower flow temperature would help.`]);
    }
    const wc = b.weather_comp;
    if (wc && !wc.compensated && wc.slope > -0.2 && Number.isFinite(s.flow_c)) {
      tips.push(["note", `The flow temperature stays near ${this._format(s.flow_c)} °C whatever the weather. Lowering it in mild weather (weather compensation) lets the boiler run cooler and more efficiently for most of the season.`]);
    }
    const use = b.forecast_use || {};
    if (use.status === "on") {
      tips.push(["ok", `Heating hours from the flow sensor are used in the oil estimates: they made past weekly forecasts ${this._format(use.gain_percent)} % more accurate.`]);
    } else if (use.status === "off_no_gain") {
      tips.push(["note", "Heating hours from the flow sensor were tested in the oil estimates but didn't improve past forecasts, so they are not used yet. This is re-checked daily."]);
    }
    if (b.runtime_check) {
      tips.push(["note", `Your runtime sensor reports ${this._format(b.runtime_check.measured_per_derived, 2)}× the heating hours seen at the flow pipe (R² ${this._format(b.runtime_check.r2, 2)} over ${b.runtime_check.days} days). Close to 1 means both agree.`]);
    }
    if (!tips.length) tips.push(["ok", "Nothing stands out: the boiler is working as it usually does."]);
    const icon = { ok: "✓", warn: "!", note: "i" };
    return `<div class="checks">${tips.map(([kind, text]) => `<div class="check ${kind === "warn" ? "warning" : ""}"><span class="check-icon ${kind === "note" ? "note" : ""}">${icon[kind]}</span><div class="check-message">${this._escape(text)}</div></div>`).join("")}</div>`;
  }

  _boilerTab() {
    const config = this._panel?.config || {};
    if (!config.flow_entity) {
      return `<section class="section"><h2>Boiler</h2><p>See how long your heating runs, how efficiently oil turns into heat, and how fast the house loses it.</p>
        <article class="card"><div class="metric-label">Add a flow temperature sensor</div><div class="hint" style="margin-top:8px;line-height:1.5">Fit a temperature sensor to the pipe leaving the boiler (a clamp-on or strap-on probe works) and, if you can, one on the return pipe. Then choose them under Settings → Devices &amp; services → Oil Monitor → Configure. Both need a state class of "measurement" so Home Assistant keeps their history.</div></article></section>`;
    }
    const b = this._boiler;
    const flowState = this._state(config.flow_entity);
    const retState = this._state(config.return_entity);
    const indoor = this._state(config.indoor_entity);
    const unitOf = (st) => st?.attributes?.unit_of_measurement || "°C";
    const asC = (st) => {
      const v = Number(st?.state);
      if (!Number.isFinite(v)) return NaN;
      return unitOf(st) === "°F" ? ((v - 32) * 5) / 9 : v;
    };
    const flowC = asC(flowState);
    const retC = asC(retState);
    const roomC = asC(indoor);
    const idle = Number(b?.idle_offset_c) || 0;
    const heatingNow = Number.isFinite(flowC) && Number.isFinite(roomC) ? flowC - roomC > idle + 10 : null;
    const s = b?.summary || {};
    const eff = b?.efficiency;
    const burn = b?.burn;
    const loss = b?.heat_loss;
    const status = this._boilerError
      ? `Boiler analysis: ${this._boilerError}`
      : !b
        ? "Loading the boiler analysis…"
        : b.status !== "ok"
          ? b.message || "The boiler analysis isn't ready yet."
          : `Analysed ${this._format(b.hours_analysed)} hours of history${b.has_return ? "" : `. Without a return sensor, the water is assumed to cool by ${this._format(b.assumed_drop_c)} °C in the radiators`}.`;
    const trendText = (t) =>
      !t || !Number.isFinite(t.change_per_year_percent)
        ? ""
        : Math.abs(t.trend_t) < 2
          ? "steady"
          : `${t.change_per_year_percent > 0 ? "rising" : "falling"} about ${this._format(Math.abs(t.change_per_year_percent))} % a year`;

    const weeks = (b?.weeks || []).filter((w) => Number.isFinite(w.efficiency) || Number.isFinite(w.heat_loss));
    const weekTicks = this._dateTicks(weeks.map((w) => w.week_start), 28);
    const weekLabel = (w) => {
      const parts = [`Week of ${this._formatDate(w.week_start)}`];
      if (Number.isFinite(w.efficiency)) parts.push(`efficiency ${this._format(w.efficiency)} %`);
      if (Number.isFinite(w.heat_loss)) parts.push(`heat loss ${this._format(w.heat_loss)} %`);
      parts.push(`${this._format(w.hours_per_day, 1)} h/day heating`);
      if (Number.isFinite(w.flow_c)) parts.push(`flow ${this._format(w.flow_c)} °C`);
      if (Number.isFinite(w.return_c)) parts.push(`return ${this._format(w.return_c)} °C`);
      return parts.join(" · ");
    };
    const effChart = this._plot({
      series: [
        { points: weeks.map((w) => [this._dayNumber(w.week_start), w.heat_loss]), cls: "loss-line" },
        { points: weeks.map((w) => [this._dayNumber(w.week_start), w.efficiency]), cls: "eff-line" },
      ],
      xTicks: weekTicks,
      unit: "%",
      gap: 15,
      ref: { y: 100, label: "your usual" },
      hover: weeks.map((w) => [this._dayNumber(w.week_start), Number.isFinite(w.efficiency) ? w.efficiency : w.heat_loss, weekLabel(w)]),
      aria: "Weekly heating efficiency and house heat loss, relative to usual",
      emptyText: "Needs a few weeks of heating with clean tank history.",
    });

    const days = b?.days || [];
    const dayTicks = this._dateTicks(days.map((d) => d.date), 7);
    const daysChart = this._plot({
      series: [
        { points: days.map((d) => [this._dayNumber(d.date), d.outdoor_c]), cls: "temp-line" },
        { points: days.map((d) => [this._dayNumber(d.date), d.flow_c]), cls: "flow-line" },
        { points: days.map((d) => [this._dayNumber(d.date), d.return_c]), cls: "return-line" },
      ],
      bars: { points: days.map((d) => [this._dayNumber(d.date), d.hours]), cls: "hours-bar", unit: "h/day" },
      xTicks: dayTicks,
      unit: "°C",
      gap: 1.5,
      hover: days.map((d) => [this._dayNumber(d.date), d.flow_c, `${this._formatDate(d.date)}: ${this._format(d.hours, 1)} h heating${Number.isFinite(d.flow_c) ? ` · flow ${this._format(d.flow_c)} °C` : ""}${Number.isFinite(d.return_c) ? ` · return ${this._format(d.return_c)} °C` : ""}${Number.isFinite(d.outdoor_c) ? ` · outdoor ${this._format(d.outdoor_c, 1)} °C` : ""}`]),
      aria: "Daily heating hours and flow temperature",
      emptyText: "Needs a few complete days of hourly flow statistics.",
    });

    const wc = b?.weather_comp;
    let compChart = this._empty("Needs at least 10 heating days with outdoor temperatures.");
    if (wc?.points?.length) {
      const w = 520;
      const h = 230;
      const L = 44;
      const R = 14;
      const T = 14;
      const B = 30;
      const xsC = wc.points.map((p) => p[0]);
      const ysC = wc.points.map((p) => p[1]);
      const minX = Math.floor(Math.min(...xsC)) - 1;
      const maxX = Math.ceil(Math.max(...xsC)) + 1;
      const minY = Math.floor(Math.min(...ysC)) - 3;
      const maxY = Math.ceil(Math.max(...ysC)) + 3;
      const px = (v) => L + ((v - minX) / (maxX - minX || 1)) * (w - L - R);
      const py = (v) => T + ((maxY - v) / (maxY - minY || 1)) * (h - T - B);
      const fit = (v) => wc.intercept + wc.slope * v;
      compChart = `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="Flow temperature while heating against outdoor temperature">
        <line x1="${L}" y1="${h - B}" x2="${w - R}" y2="${h - B}" class="axis" /><line x1="${L}" y1="${T}" x2="${L}" y2="${h - B}" class="axis" />
        ${wc.points.map(([ox, fy]) => `<circle cx="${px(ox)}" cy="${py(fy)}" r="4" class="flow-dot"><title>${this._format(ox, 1)} °C outdoor: flow ${this._format(fy, 1)} °C</title></circle>`).join("")}
        <path d="M${px(minX)},${py(fit(minX))} L${px(maxX)},${py(fit(maxX))}" class="trend-line" />
        <text x="${L}" y="${h - 8}" class="axis-label">${minX}°</text><text x="${w - R}" y="${h - 8}" text-anchor="end" class="axis-label">${maxX}° outdoor</text>
        <text x="${L - 6}" y="${T + 8}" text-anchor="end" class="axis-label">${maxY}°</text><text x="${L - 6}" y="${h - B}" text-anchor="end" class="axis-label">${minY}°</text>
      </svg>`;
    }
    const compText = !wc
      ? ""
      : wc.compensated
        ? `Weather compensated: the flow drops about ${this._format(-wc.slope, 1)} °C for each degree warmer outside.`
        : `Fixed flow temperature: it changes only ${this._format(Math.abs(wc.slope), 1)} °C per degree outside.`;

    const drop = Number.isFinite(flowC) && Number.isFinite(retC) ? flowC - retC : NaN;
    const legend = (items) => `<div class="legend">${items.map(([cls, label]) => `<span><i class="key ${cls}"></i>${label}</span>`).join("")}</div>`;
    return `
      <section class="section"><h2>Right now</h2><p>From the flow${config.return_entity ? " and return" : ""} pipe sensors.</p><div class="grid">
        <article class="card"><div class="metric-label">Flow temperature</div><div class="metric">${this._format(flowC, 1)} <span class="unit">°C</span></div><div class="hint">${heatingNow === null ? "—" : heatingNow ? "Heating now" : "Heating idle"}</div></article>
        <article class="card"><div class="metric-label">Return temperature</div>${config.return_entity ? `<div class="metric">${this._format(retC, 1)} <span class="unit">°C</span></div><div class="hint">${Number.isFinite(drop) && heatingNow ? `${this._format(drop, 1)} °C cooler than the flow` : Number.isFinite(s.drop_c) ? `Usually ${this._format(s.drop_c, 1)} °C cooler while heating` : "—"}</div>` : `<div class="metric">—</div><div class="hint">Add a return sensor under Configure for condensing and balancing checks</div>`}</article>
        <article class="card"><div class="metric-label">Heating hours · last 4 weeks</div><div class="metric">${this._format(s.hours_per_day, 1)} <span class="unit">h/day</span></div><div class="hint">${Number.isFinite(s.flow_c) ? `Flow about ${this._format(s.flow_c)} °C while heating` : "Worked out from the flow temperature"}</div></article>
        <article class="card"><div class="metric-label">Heating efficiency</div><div class="metric ${b?.efficiency_alert ? "warn" : ""}">${eff ? this._format(eff.latest_percent) : "—"} <span class="unit">${eff ? "%" : ""}</span></div><div class="hint">${eff ? `Heat per litre vs your usual · ${trendText(eff)}` : "Needs 6 heating weeks with clean tank history"}</div></article>
      </div>
      <div class="status ${b?.efficiency_alert || (b && b.status !== "ok") ? "warn" : ""}">${this._escape(status)}</div></section>

      <section class="section"><h2>What it shows</h2><p>Findings from your history, re-checked every day.</p>${b?.status === "ok" ? this._boilerAdvice(b) : this._empty("Findings appear once the analysis has run.")}</section>

      <article class="chart-card wide"><h3>Last 48 hours</h3><p>Water temperatures leaving${config.return_entity ? " and returning to" : ""} the boiler, with the room temperature. Hover for values.</p>${this._boilerRecentChart()}${legend([["flow", "Flow"], ...(config.return_entity ? [["return", "Return"]] : []), ["room", "Room"]])}</article>

      <section class="section"><h2>Efficiency and heat loss</h2><p>Heat delivered is worked out from the water temperatures with the radiator law. Divided by oil burned it gives efficiency; divided by the indoor–outdoor gap it shows how fast the house loses heat. Both are relative: 100 % is this house's usual.</p>
        <div class="grid">
          <article class="card"><div class="metric-label">Oil per heating hour</div><div class="metric">${burn ? this._format(burn.litres_per_hour, 2) : "—"} <span class="unit">L/h</span></div><div class="hint">${burn ? `About the burner nozzle rate (fit R² ${this._format(burn.r2, 2)}, ${burn.weeks} weeks)` : "Needs 6 clean weeks"}</div></article>
          <article class="card"><div class="metric-label">Hot water and standing use</div><div class="metric">${burn ? this._format(burn.baseline_litres_per_day, 1) : "—"} <span class="unit">L/day</span></div><div class="hint">Oil used with no space heating</div></article>
          <article class="card"><div class="metric-label">House heat loss</div><div class="metric">${loss ? this._format(loss.latest_percent) : "—"} <span class="unit">${loss ? "%" : ""}</span></div><div class="hint">${loss ? `Last 4 weeks vs usual · ${trendText(loss)}. Lower is better: windy, wet weeks run high.` : "Needs 6 heating weeks"}</div></article>
          <article class="card"><div class="metric-label">Condensing time</div><div class="metric">${Number.isFinite(s.condensing_share) ? this._format(s.condensing_share * 100) : "—"} <span class="unit">${Number.isFinite(s.condensing_share) ? "%" : ""}</span></div><div class="hint">${b?.has_return ? "Share of heating time with return water below 55 °C" : "Needs a return sensor"}</div></article>
        </div>
        <article class="chart-card wide"><h3>Weekly efficiency and heat loss</h3><p>A falling efficiency line means each litre gives less heat: a service may be due. Weeks without clean tank data show heat loss only.</p>${effChart}${legend([["eff", "Efficiency (heat per litre)"], ["loss", "Heat loss (heat per degree of gap)"]])}</article>
      </section>

      <section class="section"><h2>Flow temperature</h2><p>How hard and how hot the boiler runs.</p><div class="charts">
        <article class="chart-card"><h3>Daily heating hours</h3><p>Bars: hours of heating a day · lines: flow${b?.has_return ? ", return" : ""} and outdoor temperature.</p>${daysChart}${legend([["hours", "Heating hours"], ["flow", "Flow"], ...(b?.has_return ? [["return", "Return"]] : []), ["temp", "Outdoor"]])}</article>
        <article class="chart-card"><h3>Flow vs outdoor temperature</h3><p>${this._escape(compText || "Each point is a heating day.")}</p>${compChart}</article>
      </div></section>
      <p class="foot">Heating hours are estimated from hourly flow statistics: in an hour the heating ran throughout, the average flow temperature is close to the hour's peak. Efficiency compares heat delivered with oil burned on weeks with clean tank readings, after taking off hot-water use. Figures are relative because the radiator size isn't known.</p>`;
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
    const backtest = staleAttrs.backtest;
    const accuracyNote = backtest && Number.isFinite(backtest.typical_error_litres)
      ? ` · past weekly forecasts were typically within ±${this._format(Math.max(1, Math.round(backtest.typical_error_litres)))} L of actual use (checked on ${this._format(backtest.weeks)} weeks)`
      : "";
    const estimateHint = hasEstimate
      ? `Forecast range ${this._format(lower)}–${this._format(upper)} L${accuracyNote}${early ? ` · early estimate from ${this._format(trainingWeeks)} weeks of history, improving as more is recorded` : ""}`
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
        *{box-sizing:border-box}.page{max-width:1180px;margin:0 auto;padding:28px 24px 48px}.top{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:22px}.eyebrow{color:var(--secondary-text-color);font-size:12px;letter-spacing:.11em;text-transform:uppercase}.title{font-size:clamp(28px,4vw,38px);font-weight:650;margin:5px 0}.subtitle{color:var(--secondary-text-color);margin:0}.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px}.card{background:var(--card-background-color,var(--secondary-background-color));border:1px solid var(--divider-color);border-radius:16px;padding:18px;min-width:0}.metric-label{color:var(--secondary-text-color);font-size:13px}.metric{font-size:clamp(26px,3vw,34px);font-weight:650;margin:8px 0 3px}.unit{font-size:15px;font-weight:450;color:var(--secondary-text-color)}.hint{color:var(--secondary-text-color);font-size:12px}.level{height:6px;border-radius:6px;background:var(--divider-color);margin-top:14px;overflow:hidden}.level i{display:block;height:100%;background:var(--accent-color,#03a9f4);border-radius:inherit}.status{margin:14px 0;padding:12px 14px;border-radius:12px;background:var(--secondary-background-color);font-size:13px}.warn{color:var(--warning-color,#e6a700)}.section{margin-top:18px}.section h2{font-size:18px;margin:0 0 4px}.section p{color:var(--secondary-text-color);margin:0 0 12px;font-size:13px}.charts{display:grid;grid-template-columns:1.35fr 1fr;gap:14px}.chart-card{background:var(--card-background-color,var(--secondary-background-color));border:1px solid var(--divider-color);border-radius:16px;padding:18px;min-width:0}.chart-card h3{margin:0;font-size:16px}.chart-card p{margin:5px 0 12px;color:var(--secondary-text-color);font-size:12px}svg{display:block;width:100%;height:auto;overflow:visible}.axis{stroke:var(--divider-color);stroke-width:1}.axis-label{fill:var(--secondary-text-color);font-size:11px}.oil-line{fill:none;stroke:var(--accent-color,#03a9f4);stroke-width:3}.history-line{fill:none;stroke:var(--primary-text-color);stroke-width:2.5}.today-line{stroke:var(--secondary-text-color);stroke-width:1;stroke-dasharray:3 3}.temp-line{fill:none;stroke:var(--orange-color,#ff9800);stroke-width:2.4;stroke-dasharray:6 4}.oil-bar{fill:var(--accent-color,#03a9f4);opacity:.2}.temp-dot{fill:var(--orange-color,#ff9800)}.scatter-dot{fill:var(--accent-color,#03a9f4);opacity:.75}.trend-line{fill:none;stroke:var(--orange-color,#ff9800);stroke-width:2;stroke-dasharray:5 4}.knee-line{stroke:var(--divider-color);stroke-width:1;stroke-dasharray:2 3}.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--secondary-text-color);margin-top:8px}.key{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;background:var(--accent-color,#03a9f4)}.key.temp{background:var(--orange-color,#ff9800)}.toolbar{display:flex;align-items:center;gap:8px}.toolbar select,.toolbar button{color:var(--primary-text-color);background:var(--card-background-color,var(--secondary-background-color));border:1px solid var(--divider-color);border-radius:9px;padding:9px 11px;font:inherit}.empty{padding:24px 8px;color:var(--secondary-text-color);font-size:13px}.details{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.detail{padding:13px;background:var(--secondary-background-color);border-radius:12px}.detail b{display:block;font-size:18px;margin-top:5px}.grid.three{grid-template-columns:repeat(3,minmax(0,1fr))}.chart-card.wide{margin-top:14px}.setting .hint{margin-top:8px;line-height:1.4}.level-input{display:flex;align-items:center;gap:8px;margin:10px 0 2px;font-size:15px;color:var(--secondary-text-color)}.level-input input{width:7.5em;font:inherit;font-size:22px;font-weight:600;color:var(--primary-text-color);background:var(--secondary-background-color);border:1px solid var(--divider-color);border-radius:9px;padding:6px 10px}.stepper{display:flex;align-items:center;gap:12px;margin:10px 0 2px}.stepper b{font-size:22px;min-width:4.5em;text-align:center}.stepper button{width:40px;height:40px;border-radius:50%;font-size:20px;line-height:1;cursor:pointer;color:var(--primary-text-color);background:var(--secondary-background-color);border:1px solid var(--divider-color)}.last-year-line{fill:none;stroke:var(--secondary-text-color);stroke-width:1.8;opacity:.8}.key.last-year{background:var(--secondary-text-color)}.scenario-line{fill:none;stroke:var(--accent-color,#03a9f4);stroke-width:1.2;stroke-dasharray:4 4;opacity:.7}.projection-band{fill:var(--accent-color,#03a9f4);opacity:.15}.reorder-line{stroke:var(--warning-color,#e6a700);stroke-width:1.5;stroke-dasharray:6 4}.empty-line{stroke:var(--error-color,#db4437);stroke-width:1.5;stroke-dasharray:6 4}.grid-line{stroke:var(--divider-color);stroke-width:1;opacity:.5}.hover-dot{fill:transparent}.hover-hit{fill:transparent}.hover-col .hover-guide,.hover-col .hover-marker,.hover-col .hover-label{visibility:hidden;pointer-events:none}.hover-col:hover .hover-guide,.hover-col:hover .hover-marker,.hover-col:hover .hover-label{visibility:visible}.hover-guide{stroke:var(--secondary-text-color);stroke-width:1}.hover-marker{fill:var(--primary-color);stroke:var(--card-background-color,#fff);stroke-width:2}.hover-label{fill:var(--primary-text-color);font-size:12px;font-weight:600;paint-order:stroke;stroke:var(--card-background-color,#fff);stroke-width:4px;stroke-linejoin:round}.key.band{background:var(--accent-color,#03a9f4);opacity:.3}.key.reorder{background:var(--warning-color,#e6a700)}.key.stop{background:var(--error-color,#db4437)}.checks{display:grid;gap:8px}.check{display:flex;gap:12px;align-items:flex-start;padding:12px 14px;border-radius:12px;background:var(--card-background-color,var(--secondary-background-color));border:1px solid var(--divider-color)}.check-icon{flex:none;width:24px;height:24px;border-radius:50%;display:grid;place-items:center;font-weight:700;font-size:13px;color:#fff;background:var(--success-color,#43a047)}.check.warning .check-icon{background:var(--warning-color,#e6a700)}.check.problem .check-icon{background:var(--error-color,#db4437)}.check-entity{color:var(--secondary-text-color);font-size:12px;margin-left:4px;word-break:break-all}.check-message{color:var(--secondary-text-color);font-size:13px;margin-top:3px;line-height:1.4}.chart-head{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}.view-toggle{display:inline-flex;border:1px solid var(--divider-color);border-radius:9px;overflow:hidden}.view-toggle button{font:inherit;font-size:12px;padding:6px 10px;border:0;cursor:pointer;color:var(--secondary-text-color);background:transparent}.view-toggle button[aria-pressed="true"]{color:var(--text-primary-color,#fff);background:var(--accent-color,#03a9f4)}.tabs{display:flex;gap:4px;border-bottom:1px solid var(--divider-color);margin:0 0 6px}.tabs button{font:inherit;font-size:14px;font-weight:550;padding:10px 16px;border:0;border-bottom:3px solid transparent;margin-bottom:-1px;background:transparent;color:var(--secondary-text-color);cursor:pointer}.tabs button[aria-selected="true"]{color:var(--primary-text-color);border-bottom-color:var(--accent-color,#03a9f4)}.flow-line{fill:none;stroke:var(--error-color,#db4437);stroke-width:2.4}.return-line{fill:none;stroke:var(--accent-color,#03a9f4);stroke-width:2.2}.room-line{fill:none;stroke:var(--secondary-text-color);stroke-width:1.6;stroke-dasharray:4 3}.eff-line{fill:none;stroke:var(--success-color,#43a047);stroke-width:2.6}.loss-line{fill:none;stroke:var(--orange-color,#ff9800);stroke-width:2;stroke-dasharray:6 4}.ref-line{stroke:var(--secondary-text-color);stroke-width:1;stroke-dasharray:2 4;opacity:.8}.hours-bar{fill:var(--accent-color,#03a9f4);opacity:.22}.flow-dot{fill:var(--error-color,#db4437);opacity:.7}.key.flow{background:var(--error-color,#db4437)}.key.return{background:var(--accent-color,#03a9f4)}.key.room{background:var(--secondary-text-color)}.key.eff{background:var(--success-color,#43a047)}.key.loss{background:var(--orange-color,#ff9800)}.key.hours{background:var(--accent-color,#03a9f4);opacity:.35}.check-icon.note{background:var(--info-color,#039be5)}.foot{margin-top:16px;color:var(--secondary-text-color);font-size:12px;line-height:1.5}@media(max-width:850px){.grid,.grid.three{grid-template-columns:repeat(2,minmax(0,1fr))}.charts{grid-template-columns:1fr}}@media(max-width:520px){.grid.three{grid-template-columns:1fr}.page{padding:20px 14px 36px}.grid{gap:9px}.card{padding:14px}.metric{font-size:25px}.details{grid-template-columns:repeat(2,1fr)}.top{align-items:center}}
      </style>
      <main class="page">
        <header class="top"><div><div class="eyebrow">Home energy · heating oil</div><h1 class="title">Oil Monitor</h1><p class="subtitle">Your tank, usage trends and weather-aware outlook</p></div><div class="toolbar"><button type="button" class="refresh" title="Refresh history">↻</button></div></header>
        <nav class="tabs" role="tablist">${[["overview", "Overview"], ["boiler", "Boiler"]].map(([key, label]) => `<button type="button" role="tab" data-tab="${key}" aria-selected="${this._tab === key}">${label}</button>`).join("")}</nav>
        ${this._tab === "boiler" ? this._boilerTab() : `
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
        <article class="chart-card wide"><div class="chart-head"><h3>Projected oil level</h3>${this._projectionToggle()}</div><p>Expected litres over the coming months. The solid line is a typical winter; the shaded band runs from a mild winter (top) to a cold one (bottom). The grey line shows the same months last year: what you actually used, starting from today's level.</p>${this._projectionChart(emptyState?.attributes?.projection, Number(reorderState?.attributes?.level_litres), Number(emptyState?.attributes?.level_litres))}<div class="legend"><span><i class="key"></i>Typical winter</span><span><i class="key band"></i>Mild to cold winter</span><span><i class="key last-year"></i>Last year's usage, from today's level</span><span><i class="key reorder"></i>Reorder level</span><span><i class="key stop"></i>Empty level</span></div></article>
        <p class="foot">Estimates are learned from your tank, temperature and heating history. The first week follows the weather forecast; later weeks are projected for mild, typical and cold winters. Use them as a planning aid, not as the only basis for ordering oil.</p></section>

        <section class="section"><h2>Settings</h2><p>Changes apply to the estimates straight away.</p><div class="grid three">
          ${this._levelSetting(config.reorder_level_entity, "Reorder level", "Order more oil when the tank reaches this level.")}
          ${this._levelSetting(config.empty_level_entity, "Empty level", "Treat the tank as empty here (the pump/sludge line).")}
          <article class="card setting"><div class="metric-label">Forecast temperature adjustment</div><div class="stepper"><button type="button" data-offset-step="-0.5" aria-label="Decrease adjustment">−</button><b>${Number.isFinite(offset) ? `${offset > 0 ? "+" : ""}${this._format(offset, 1)}` : "—"} °C</b><button type="button" data-offset-step="0.5" aria-label="Increase adjustment">+</button></div><div class="hint">"What if" the house were warmer or cooler than the thermostat. Leave at 0 for normal estimates.</div></article>
        </div></section>

        <section class="section"><h2>Setup check</h2><p>Each sensor Oil Monitor uses, and anything that stops it working well. To report a problem, use Download diagnostics on the Oil Monitor integration page.</p>${this._setupCheck(checks)}</section>

        <section class="section"><h2>History</h2><p>Weekly tank-level changes are converted with your tank calibration and compared with weekly outdoor temperatures. Refill and suspect weeks are excluded.</p><div class="charts">
          <article class="chart-card"><h3>Weekly oil use & outdoor temperature</h3><p>Bars and blue line: litres used · orange: mean outdoor temperature</p>${this._chart(weeks)}<div class="legend"><span><i class="key"></i>Oil use (L/week)</span><span><i class="key temp"></i>Outdoor temperature</span></div></article>
          <article class="chart-card"><h3>Oil use vs outdoor temperature</h3><p>Each point is a clean week. The dashed curve flattens to hot-water-only use above the temperature where heating switches off.</p>${this._scatter(weeks)}</article>
        </div></section>`}
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
    this.shadowRoot.querySelectorAll("[data-projection-view]").forEach((button) => {
      button.addEventListener("click", () => {
        this._projectionView = button.dataset.projectionView;
        this._render();
      });
    });
    this.shadowRoot.querySelector(".refresh")?.addEventListener("click", () => {
      this._lastFetch = 0;
      this._loadStatistics();
      if (this._tab === "boiler") this._loadBoiler(true);
    });
    this.shadowRoot.querySelectorAll("[data-tab]").forEach((button) => {
      button.addEventListener("click", () => this._setTab(button.dataset.tab));
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
