// Scope / trend view.
//
// Trend:   plots the live values the program sends every monitor period (Settings ->
//          "Update every"), for slow signals such as temperatures or a PID loop.
// Capture: the program records the chosen signals on every scan (or every n-th) into a
//          buffer, optionally waiting for a trigger, and then sends them. This shows fast
//          signals (filters, FFT input, PWM) at the full scan rate.
//
// The host (app.js) supplies the signal list and talks to the server; this module only
// keeps the data and draws it.

const COLORS = ['#2563eb', '#e07b24', '#1f9d55', '#d64545', '#8e5bd6', '#0e9aa7', '#b0782a', '#d63384'];
const MAX_TREND = 50000;  // samples kept per signal
const LANE_H = 18;        // height of a BOOL lane (px)

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'class') el.className = v;
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, v);
  }
  for (const c of children.flat()) if (c != null && c !== false) el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return el;
}

function store(key, value) {
  try {
    if (value === undefined) return JSON.parse(localStorage.getItem(key) || 'null');
    localStorage.setItem(key, JSON.stringify(value));
  } catch { /* private window etc. */ }
  return null;
}

// "Nice" tick step (1, 2 or 5 times a power of ten) for about n ticks over span.
function niceStep(span, n) {
  if (!(span > 0)) return 1;
  const raw = span / Math.max(1, n);
  const p = 10 ** Math.floor(Math.log10(raw));
  const f = raw / p;
  return (f < 1.5 ? 1 : f < 3.5 ? 2 : f < 7.5 ? 5 : 10) * p;
}

function fmtNum(v, step) {
  if (v == null || !Number.isFinite(v)) return '–';
  const d = step ? Math.max(0, Math.min(6, -Math.floor(Math.log10(step) + 1e-9))) : 3;
  return Math.abs(v) >= 1e6 || (Math.abs(v) < 1e-4 && v !== 0) ? v.toExponential(2) : v.toFixed(d);
}

function fmtTime(s, step) {
  const a = Math.abs(step || s);
  if (a < 1e-3) return `${fmtNum(s * 1e6, (step || 0) * 1e6)} µs`;
  if (a < 1) return `${fmtNum(s * 1e3, (step || 0) * 1e3)} ms`;
  return `${fmtNum(s, step)} s`;
}

export class Scope {
  /**
   * host = {
   *   signals(): [{id, label, type, key, sym}]  plotted signals (key: live-value key, sym: symbol index or null)
   *   ports(): [{id, label, type}]               everything that can be plotted
   *   add(id), remove(id)                        change the project's signal list
   *   online(): bool
   *   startCapture(req): Promise, cancelCapture(): Promise
   *   status(text)
   * }
   */
  constructor(root, host) {
    this.root = root;
    this.host = host;
    const saved = store('fbd.scope') || {};
    this.cfg = { mode: 'trend', window: 30, samples: 500, every: 1, trigger: '', edge: 1, level: 0.5, prePct: 10,
      repeat: false, ymin: '', ymax: '', ...saved };
    this.trend = { t: [], v: new Map() };  // t in ms (device clock), v: id -> values
    this.capture = null;                    // {t0, dt, series: [{id, label, type, data}], pre}
    this.capState = 'idle';
    this.paused = false;
    this.view = null;   // zoomed x range [x0, x1] in seconds, or null
    this.hoverX = null;
    this.build();
    new ResizeObserver(() => this.draw()).observe(this.canvas);
  }

  save() { store('fbd.scope', this.cfg); }

  // ------------------------------------------------------------------ UI
  build() {
    const c = this.cfg;
    const num = (key, attrs, onchange) => h('input', {
      type: 'number', value: c[key], ...attrs,
      onchange: (e) => { const v = e.target.value === '' ? '' : Number(e.target.value); c[key] = v; this.save(); onchange?.(); this.draw(); },
    });
    this.modeBtns = ['trend', 'capture'].map((m) => h('button', {
      class: 'small', 'data-mode': m, onclick: () => { c.mode = m; this.view = null; this.save(); this.refresh(); },
      title: m === 'trend' ? 'Plot the live values (one point per monitor update)' : 'Record every scan on the board and plot it (for fast signals)',
    }, m === 'trend' ? 'Trend' : 'Capture'));
    this.windowSel = h('select', { onchange: (e) => { c.window = Number(e.target.value); this.view = null; this.save(); this.draw(); } },
      [10, 30, 60, 300, 900].map((s) => h('option', { value: s, selected: s === c.window }, s < 60 ? `${s} s` : `${s / 60} min`)));
    this.pauseBtn = h('button', { class: 'small', onclick: () => this.setPaused(!this.paused) }, 'Pause');
    this.rateInfo = h('span', { class: 'scope-status', title: 'Trend points arrive with the online monitoring updates (Project settings → Update every). Use Capture for faster signals.' });
    this.trendBar = h('span', { class: 'scope-group' },
      h('label', {}, 'Window ', this.windowSel), this.pauseBtn,
      h('button', { class: 'small', onclick: () => { this.trend = { t: [], v: new Map() }; this.view = null; this.draw(); } }, 'Clear'),
      this.rateInfo);

    this.trigSel = h('select', { onchange: (e) => { c.trigger = e.target.value; this.save(); this.draw(); }, title: 'Start the recording when this signal crosses the level' });
    this.edgeSel = h('select', { onchange: (e) => { c.edge = Number(e.target.value); this.save(); } },
      [[1, '↗ rising'], [2, '↘ falling'], [3, '↕ both']].map(([v, t]) => h('option', { value: v, selected: v === c.edge }, t)));
    this.singleBtn = h('button', { class: 'small primary', onclick: () => this.arm() }, 'Single');
    this.stopBtn = h('button', { class: 'small', onclick: () => this.stopCapture(), hidden: true }, 'Stop');
    this.repeatChk = h('input', { type: 'checkbox', checked: c.repeat, onchange: (e) => { c.repeat = e.target.checked; this.save(); } });
    this.capStatus = h('span', { class: 'scope-status' });
    this.captureBar = h('span', { class: 'scope-group' },
      h('label', { title: 'Samples per capture (the buffer holds 4096 values: samples x signals)' }, 'Samples ', num('samples', { min: 2, max: 4096, step: 1, class: 'n5' })),
      h('label', { title: 'Record every n-th scan (1 = every scan)' }, 'every ', num('every', { min: 1, max: 60000, step: 1, class: 'n3' }), ' scans'),
      h('label', {}, 'Trigger ', this.trigSel), this.edgeSel,
      h('label', { title: 'Trigger level' }, 'at ', num('level', { step: 'any', class: 'n4' })),
      h('label', { title: 'Part of the capture before the trigger' }, 'pre ', num('prePct', { min: 0, max: 95, step: 5, class: 'n3' }), ' %'),
      this.singleBtn, this.stopBtn,
      h('label', { title: 'Arm again after each capture' }, this.repeatChk, ' repeat'), this.capStatus);

    const bar = h('div', { class: 'scope-bar' },
      h('span', { class: 'seg' }, this.modeBtns), this.trendBar, this.captureBar, h('span', { class: 'spacer' }),
      h('label', { title: 'Y axis range (empty = automatic)' }, 'Y ', num('ymin', { step: 'any', class: 'n4', placeholder: 'auto' }), ' … ',
        num('ymax', { step: 'any', class: 'n4', placeholder: 'auto' })),
      h('button', { class: 'small', onclick: () => this.exportCsv(), title: 'Download the plotted data as CSV' }, 'CSV'));

    this.canvas = h('canvas', { class: 'scope-canvas' });
    this.legend = h('div', { class: 'scope-legend' });
    this.addSel = h('select', { class: 'scope-add', onchange: (e) => { if (e.target.value) this.host.add(e.target.value); e.target.value = ''; } });
    this.empty = h('div', { class: 'scope-empty' },
      'Choose signals to plot: use “+ signal” on the right, or 📈 next to a value in the block properties. ',
      'Then start the simulator or connect to the board.');
    this.root.replaceChildren(bar, h('div', { class: 'scope-main' },
      h('div', { class: 'scope-plot' }, this.canvas, this.empty), h('div', { class: 'scope-side' }, this.legend, this.addSel)));

    const cv = this.canvas;
    cv.addEventListener('mousemove', (e) => { this.hoverX = e.offsetX; if (this.drag) this.pan(e); this.draw(); });
    cv.addEventListener('mouseleave', () => { this.hoverX = null; this.drag = null; this.draw(); });
    cv.addEventListener('mousedown', (e) => { const r = this.xRange(); if (r) this.drag = { x: e.offsetX, r }; });
    window.addEventListener('mouseup', () => { this.drag = null; });
    cv.addEventListener('dblclick', () => { this.view = null; this.draw(); });
    cv.addEventListener('wheel', (e) => { e.preventDefault(); this.zoom(e.offsetX, e.deltaY > 0 ? 1.25 : 0.8); }, { passive: false });
    this.refresh();
  }

  setPaused(p) {
    this.paused = p;
    this.pauseBtn.textContent = p ? 'Run' : 'Pause';
    this.pauseBtn.classList.toggle('active', p);
    if (!p) this.view = null;
    this.draw();
  }

  /** Rebuild the signal-dependent parts (legend, selects); call after the signal list or online state changed. */
  refresh() {
    const c = this.cfg;
    for (const b of this.modeBtns) b.classList.toggle('active', b.dataset.mode === c.mode);
    this.trendBar.hidden = c.mode !== 'trend';
    this.captureBar.hidden = c.mode !== 'capture';
    const sigs = this.host.signals();
    this.colors = new Map(sigs.map((s, i) => [s.id, COLORS[i % COLORS.length]]));
    this.legend.replaceChildren(...sigs.map((s) => {
      const val = h('span', { class: 'v' });
      const chip = h('div', { class: 'scope-chip', title: s.sym == null && this.host.online() ? 'Not available in the running program: restart / upload again' : s.label },
        h('span', { class: 'sw', style: `background:${this.colors.get(s.id)}` }), h('span', { class: 'l' }, s.label), val,
        h('button', { class: 'x', title: 'Remove from the plot', onclick: () => this.host.remove(s.id) }, '×'));
      chip.classList.toggle('na', s.sym == null && this.host.online());
      s.valueEl = val;
      return chip;
    }));
    this.sigs = sigs;
    const ports = this.host.ports().filter((p) => !sigs.some((s) => s.id === p.id));
    this.addSel.replaceChildren(h('option', { value: '' }, '+ signal…'), ...ports.map((p) => h('option', { value: p.id }, `${p.label} (${p.type})`)));
    this.trigSel.replaceChildren(h('option', { value: '' }, 'none (at once)'),
      ...sigs.map((s) => h('option', { value: s.id, selected: s.id === c.trigger }, s.label)));
    if (c.trigger && !sigs.some((s) => s.id === c.trigger)) c.trigger = '';
    const ms = this.host.monitorMs?.();
    this.rateInfo.textContent = ms ? `1 point / ${ms} ms – use Capture for faster signals` : 'monitoring is off (Update every = 0)';
    this.updateCaptureUi();
    this.draw();
  }

  updateCaptureUi() {
    const busy = ['arming', 'armed', 'triggered', 'receiving'].includes(this.capState);
    this.singleBtn.hidden = busy;
    this.stopBtn.hidden = !busy;
    this.singleBtn.disabled = !this.host.online();
    this.singleBtn.title = this.host.online() ? 'Record one capture' : 'Start the simulator or connect to the board first';
    const text = { idle: '', arming: 'arming…', armed: this.cfg.trigger ? 'waiting for trigger…' : 'recording…', triggered: 'triggered, recording…',
      receiving: `receiving ${this.capLen || ''} samples…`, done: '' }[this.capState] ?? '';
    this.capStatus.textContent = text;
  }

  // ------------------------------------------------------------------ data in
  /** One monitor update: t in ms of the device clock, live: Map key -> value. */
  onValues(t, live) {
    if (!this.sigs?.length) return;
    const tr = this.trend;
    if (tr.t.length && t < tr.t[tr.t.length - 1]) { tr.t = []; tr.v = new Map(); }  // the program restarted
    tr.t.push(t);
    for (const s of this.sigs) {
      let arr = tr.v.get(s.id);
      if (!arr) { arr = new Array(tr.t.length - 1).fill(null); tr.v.set(s.id, arr); }
      const v = live.get(s.key);
      arr.push(v == null ? null : typeof v === 'boolean' ? (v ? 1 : 0) : Number(v));
    }
    for (const [id, arr] of tr.v) if (arr.length < tr.t.length) arr.push(null);  // signals no longer plotted
    if (tr.t.length > MAX_TREND) {
      const n = tr.t.length - MAX_TREND;
      tr.t.splice(0, n);
      for (const arr of tr.v.values()) arr.splice(0, n);
    }
    if (this.cfg.mode === 'trend' && !this.paused) this.scheduleDraw();
    else if (this.hoverX == null) this.updateLegendValues(null);
  }

  scheduleDraw() {
    if (this.pending) return;
    this.pending = requestAnimationFrame(() => { this.pending = null; this.draw(); });
  }

  async arm() {
    const c = this.cfg;
    const sigs = this.sigs.filter((s) => s.sym != null);
    if (!sigs.length) return this.host.status('Add signals to plot first (they must be in the running program)');
    if (sigs.length > 8) return this.host.status('A capture can record at most 8 signals: remove some from the plot');
    const trig = c.trigger ? this.sigs.find((s) => s.id === c.trigger) : null;
    if (c.trigger && trig?.sym == null) return this.host.status('The trigger signal is not in the running program');
    const length = Math.max(2, Math.min(Math.floor(4096 / sigs.length), Math.round(Number(c.samples) || 500)));
    const req = {
      channels: sigs.map((s) => s.sym), every: Math.max(1, Math.round(Number(c.every) || 1)), length,
      pre: trig ? Math.round(length * Math.max(0, Math.min(95, Number(c.prePct) || 0)) / 100) : 0,
      trigger: trig ? trig.sym : -1, edge: c.edge, level: Number(c.level) || 0,
    };
    this.capSigs = sigs.map((s) => ({ id: s.id, label: s.label, type: s.type }));
    this.capTrigger = trig ? { id: trig.id, level: req.level } : null;
    this.capState = 'arming';
    this.updateCaptureUi();
    try { await this.host.startCapture(req); }
    catch (e) { this.capState = 'idle'; this.updateCaptureUi(); this.host.status(e.message); }
  }

  async stopCapture() {
    this.cfg.repeat = false;
    this.repeatChk.checked = false;
    this.capState = 'idle';
    this.updateCaptureUi();
    try { await this.host.cancelCapture(); } catch { /* offline */ }
  }

  onCaptureState(ev) {
    if (this.capState === 'idle') return;  // stopped meanwhile
    this.capState = ev.state;
    if (ev.len) this.capLen = ev.len;
    if (ev.state === 'armed' && ev.len < Math.round(Number(this.cfg.samples) || 0)) {
      this.host.status(`The capture buffer holds ${ev.len} samples of ${ev.channels} signals`);
    }
    this.updateCaptureUi();
  }

  onCapture(ev) {
    if (!this.capSigs) return;
    const dt = ev.period_us / 1e6;
    this.capture = { dt, pre: ev.pre, t0: -ev.pre * dt, trigger: this.capTrigger,
      series: this.capSigs.map((s, i) => ({ ...s, data: ev.data[i] || [] })) };
    this.capState = 'done';
    this.updateCaptureUi();
    const n = ev.data[0]?.length || 0;
    this.capStatus.textContent = `${n} samples, ${fmtTime(dt)} apart (${fmtNum(1 / dt, 0.1)} Hz)`;
    if (this.cfg.mode === 'capture') this.draw();
    if (this.cfg.repeat && this.host.online()) setTimeout(() => { if (this.cfg.repeat && this.capState === 'done') this.arm(); }, 300);
  }

  onOffline() {
    if (this.capState !== 'done') this.capState = 'idle';
    this.refresh();
  }

  // ------------------------------------------------------------------ series in seconds
  /** [{id, label, type, color, t(i), n, y(i)}] for the current mode. */
  series() {
    if (this.cfg.mode === 'capture') {
      const cap = this.capture;
      if (!cap) return [];
      return cap.series.map((s) => ({ ...s, color: this.colors.get(s.id) || '#888', n: s.data.length,
        t: (i) => cap.t0 + i * cap.dt, y: (i) => s.data[i] }));
    }
    const tr = this.trend;
    if (!tr.t.length) return [];
    const t0 = tr.t[0];
    return this.sigs.filter((s) => tr.v.has(s.id)).map((s) => {
      const arr = tr.v.get(s.id);
      return { ...s, color: this.colors.get(s.id), n: tr.t.length, t: (i) => (tr.t[i] - t0) / 1000, y: (i) => arr[i] };
    });
  }

  fullRange(series) {
    if (!series.length || !series[0].n) return null;
    const s = series[0];
    if (this.cfg.mode === 'capture') return [s.t(0), s.t(s.n - 1)];
    const end = s.t(s.n - 1);
    return [end - this.cfg.window, end];
  }

  xRange() {
    const r = this.fullRange(this.series());
    if (!r) return null;
    return this.view || r;
  }

  zoom(px, factor) {
    const r = this.xRange();
    if (!r) return;
    if (this.cfg.mode === 'trend' && !this.paused) this.setPaused(true);
    const { left, w } = this.plotBox();
    const x = r[0] + ((px - left) / w) * (r[1] - r[0]);
    this.view = [x - (x - r[0]) * factor, x + (r[1] - x) * factor];
    this.draw();
  }

  pan(e) {
    const { w } = this.plotBox();
    const r = this.drag.r;
    const dx = ((e.offsetX - this.drag.x) / w) * (r[1] - r[0]);
    if (Math.abs(e.offsetX - this.drag.x) < 3) return;
    if (this.cfg.mode === 'trend' && !this.paused) this.setPaused(true);
    this.view = [r[0] - dx, r[1] - dx];
  }

  plotBox() {
    const W = this.canvas.clientWidth, H = this.canvas.clientHeight;
    const lanes = (this.lastSeries || []).filter((s) => s.type === 'BOOL').length;
    const left = 58, right = 10, top = 8, bottom = 22;
    const laneH = lanes * LANE_H;
    return { W, H, left, w: Math.max(10, W - left - right), top, h: Math.max(10, H - top - bottom - laneH - (lanes ? 6 : 0)), laneTop: H - bottom - laneH, lanes };
  }

  // ------------------------------------------------------------------ drawing
  draw() {
    const cv = this.canvas;
    const dpr = window.devicePixelRatio || 1;
    const W = cv.clientWidth, H = cv.clientHeight;
    if (!W || !H) return;
    if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) { cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr); }
    const g = cv.getContext('2d');
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    const css = getComputedStyle(this.root);
    const col = (v, d) => css.getPropertyValue(v).trim() || d;
    const text = col('--text', '#222'), muted = col('--muted', '#777'), grid = col('--grid-major', '#e5e5e5'), axis = col('--border', '#ccc');
    g.clearRect(0, 0, W, H);

    const series = this.series();
    this.lastSeries = series;
    this.empty.hidden = !!(this.sigs?.length && (series.length || this.host.online()));
    const box = this.plotBox();
    const { left, w, top, h: ph } = box;
    const range = this.xRange();
    g.font = '11px ui-monospace, Menlo, Consolas, monospace';
    g.textBaseline = 'middle';
    if (!range || !series.length) {
      g.fillStyle = muted;
      g.textAlign = 'center';
      if (this.sigs?.length) {
        g.fillText(this.cfg.mode === 'capture' ? (this.host.online() ? 'Press Single to record a capture' : 'Start the simulator or connect to the board, then press Single')
          : (this.host.online() ? 'Waiting for values… (Settings → Update every must not be 0)' : 'Start the simulator or connect to the board'), left + w / 2, top + ph / 2);
      }
      this.updateLegendValues(null);
      return;
    }
    const [x0, x1] = range;
    const sx = (t) => left + ((t - x0) / (x1 - x0 || 1)) * w;

    // visible index range per series (time is monotonic)
    const vis = series.map((s) => {
      let a = 0, b = s.n - 1;
      while (a < b && s.t(a) < x0) a++;
      while (b > a && s.t(b) > x1) b--;
      return [Math.max(0, a - 1), Math.min(s.n - 1, b + 1)];
    });

    // y range of the analog signals
    const analog = series.filter((s) => s.type !== 'BOOL');
    let y0 = Infinity, y1 = -Infinity;
    analog.forEach((s) => {
      const [a, b] = vis[series.indexOf(s)];
      for (let i = a; i <= b; i++) { const v = s.y(i); if (v != null) { if (v < y0) y0 = v; if (v > y1) y1 = v; } }
    });
    if (this.capture?.trigger && this.cfg.mode === 'capture') {
      const ts = analog.find((s) => s.id === this.capture.trigger.id);
      if (ts) { y0 = Math.min(y0, this.capture.trigger.level); y1 = Math.max(y1, this.capture.trigger.level); }
    }
    if (!Number.isFinite(y0)) { y0 = 0; y1 = 1; }
    if (y1 - y0 < 1e-9) { const m = Math.abs(y0) * 0.1 || 1; y0 -= m; y1 += m; }
    else { const m = (y1 - y0) * 0.06; y0 -= m; y1 += m; }
    if (this.cfg.ymin !== '' && Number.isFinite(Number(this.cfg.ymin))) y0 = Number(this.cfg.ymin);
    if (this.cfg.ymax !== '' && Number.isFinite(Number(this.cfg.ymax))) y1 = Number(this.cfg.ymax);
    if (y1 <= y0) y1 = y0 + 1;
    const sy = (v) => top + ph - ((v - y0) / (y1 - y0)) * ph;

    // grid and axes
    g.strokeStyle = grid;
    g.lineWidth = 1;
    g.fillStyle = muted;
    const xs = niceStep(x1 - x0, Math.max(2, w / 90));
    g.textAlign = 'center';
    for (let t = Math.ceil(x0 / xs) * xs; t <= x1 + 1e-12; t += xs) {
      const x = Math.round(sx(t)) + 0.5;
      g.beginPath(); g.moveTo(x, top); g.lineTo(x, box.H - 22); g.stroke();
      const label = this.cfg.mode === 'trend' && !this.view ? fmtTime(t - x1, xs) : fmtTime(t, xs);
      g.fillText(label, x, box.H - 11);
    }
    if (analog.length) {
      const ys = niceStep(y1 - y0, Math.max(2, ph / 40));
      g.textAlign = 'right';
      for (let v = Math.ceil(y0 / ys) * ys; v <= y1 + 1e-12; v += ys) {
        const y = Math.round(sy(v)) + 0.5;
        g.beginPath(); g.moveTo(left, y); g.lineTo(left + w, y); g.stroke();
        g.fillText(fmtNum(v, ys), left - 6, y);
      }
    }
    g.strokeStyle = axis;
    g.strokeRect(left + 0.5, top + 0.5, w, ph);

    // trigger marks
    if (this.cfg.mode === 'capture' && this.capture?.trigger) {
      g.save();
      g.setLineDash([4, 3]);
      g.strokeStyle = muted;
      const xt = Math.round(sx(0)) + 0.5;
      g.beginPath(); g.moveTo(xt, top); g.lineTo(xt, box.H - 22); g.stroke();
      const ts = analog.find((s) => s.id === this.capture.trigger.id);
      if (ts) { const yt = Math.round(sy(this.capture.trigger.level)) + 0.5; g.strokeStyle = ts.color; g.beginPath(); g.moveTo(left, yt); g.lineTo(left + w, yt); g.stroke(); }
      g.restore();
    }

    // analog traces (min/max per pixel column when there are many points)
    g.save();
    g.beginPath(); g.rect(left, top, w, ph); g.clip();
    analog.forEach((s) => {
      const [a, b] = vis[series.indexOf(s)];
      g.strokeStyle = s.color;
      g.lineWidth = 1.5;
      g.beginPath();
      let pen = false;
      if (b - a > w * 2) {
        let col = -1, lo = 0, hi = 0;
        const flush = () => { if (col < 0) return; const x = left + col + 0.5; if (!pen) { g.moveTo(x, sy(lo)); pen = true; } g.lineTo(x, sy(lo)); g.lineTo(x, sy(hi)); };
        for (let i = a; i <= b; i++) {
          const v = s.y(i);
          if (v == null) { flush(); col = -1; pen = false; continue; }
          const c = Math.floor(sx(s.t(i)) - left);
          if (c !== col) { flush(); col = c; lo = hi = v; } else { if (v < lo) lo = v; if (v > hi) hi = v; }
        }
        flush();
      } else {
        for (let i = a; i <= b; i++) {
          const v = s.y(i);
          if (v == null) { pen = false; continue; }
          const x = sx(s.t(i)), y = sy(v);
          if (pen) g.lineTo(x, y); else { g.moveTo(x, y); pen = true; }
        }
      }
      g.stroke();
      if (b - a < w / 8) {  // few points: mark the samples
        g.fillStyle = s.color;
        for (let i = a; i <= b; i++) { const v = s.y(i); if (v != null) g.fillRect(sx(s.t(i)) - 1.5, sy(v) - 1.5, 3, 3); }
      }
    });
    g.restore();

    // BOOL lanes
    const bools = series.filter((s) => s.type === 'BOOL');
    g.save();
    g.beginPath(); g.rect(left, box.laneTop - 2, w, bools.length * LANE_H + 4); g.clip();
    bools.forEach((s, k) => {
      const [a, b] = vis[series.indexOf(s)];
      const yTop = box.laneTop + k * LANE_H + 3, yBot = yTop + LANE_H - 6;
      g.strokeStyle = s.color;
      g.fillStyle = s.color + '33';
      g.lineWidth = 1.5;
      g.beginPath();
      let prevY = null;
      for (let i = a; i <= b; i++) {
        const v = s.y(i);
        if (v == null) { prevY = null; continue; }
        const x = sx(s.t(i)), y = v ? yTop : yBot;
        if (prevY == null) g.moveTo(x, y); else { g.lineTo(x, prevY); g.lineTo(x, y); }
        prevY = y;
        if (v) { const xn = i < b ? sx(s.t(i + 1)) : x; g.fillRect(x, yTop, Math.max(1, xn - x), yBot - yTop); }
      }
      g.stroke();
    });
    g.restore();
    g.textAlign = 'right';
    bools.forEach((s, k) => { g.fillStyle = s.color; g.fillText(s.label.length > 9 ? s.label.slice(0, 8) + '…' : s.label, left - 6, box.laneTop + k * LANE_H + LANE_H / 2); });

    // hover cursor
    let tHover = null;
    if (this.hoverX != null && this.hoverX >= left && this.hoverX <= left + w) {
      tHover = x0 + ((this.hoverX - left) / w) * (x1 - x0);
      g.strokeStyle = text;
      g.globalAlpha = 0.5;
      g.beginPath(); g.moveTo(this.hoverX + 0.5, top); g.lineTo(this.hoverX + 0.5, box.H - 22); g.stroke();
      g.globalAlpha = 1;
      g.fillStyle = text;
      g.textAlign = 'left';
      const label = this.cfg.mode === 'trend' && !this.view ? fmtTime(tHover - x1, xs / 10) : fmtTime(tHover, xs / 10);
      g.fillText(label, Math.min(this.hoverX + 4, left + w - 70), top + 8);
    }
    this.updateLegendValues(tHover, series);
  }

  updateLegendValues(tHover, series = this.lastSeries || []) {
    for (const s of this.sigs || []) {
      if (!s.valueEl) continue;
      const ser = series.find((x) => x.id === s.id);
      let v = null;
      if (ser && ser.n) {
        if (tHover == null) v = ser.y(ser.n - 1);
        else {  // nearest sample at or before the cursor
          let lo = 0, hi = ser.n - 1;
          while (lo < hi) { const m = (lo + hi + 1) >> 1; if (ser.t(m) <= tHover) lo = m; else hi = m - 1; }
          v = ser.y(lo);
        }
      }
      s.valueEl.textContent = v == null ? '' : s.type === 'BOOL' ? (v ? 'TRUE' : 'FALSE') : fmtNum(v, Math.abs(v) >= 100 ? 0.1 : 0.001);
    }
  }

  exportCsv() {
    const series = this.series();
    if (!series.length) return this.host.status('Nothing to export yet');
    const n = Math.max(...series.map((s) => s.n));
    const rows = [['time_s', ...series.map((s) => s.label)].join(',')];
    for (let i = 0; i < n; i++) {
      rows.push([series[0].t(i).toFixed(6), ...series.map((s) => { const v = s.y(i); return v == null ? '' : String(v); })].join(','));
    }
    const blob = new Blob([rows.join('\n') + '\n'], { type: 'text/csv' });
    const a = h('a', { href: URL.createObjectURL(blob), download: `${this.cfg.mode === 'capture' ? 'capture' : 'trend'}.csv` });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }
}
