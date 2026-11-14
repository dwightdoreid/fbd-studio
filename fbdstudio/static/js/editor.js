// SVG function block diagram editor: rendering, selection, dragging, wiring, pan/zoom, undo.
import { resolvePorts, TYPE_COLORS, formatValue, categoryColor, compatible, zeroValue } from './types.js';

const NS = 'http://www.w3.org/2000/svg';
const HEADER = 22;
const ROW = 18;
const GRID = 10;
const MIN_W = 96;
const WIRE_STUB = 12; // minimum horizontal run out of an output / into an input

function el(tag, attrs = {}, parent = null, text = null) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== undefined && v !== null && v !== false) e.setAttribute(k, v);
  if (text !== null) e.textContent = text;
  if (parent) parent.appendChild(e);
  return e;
}

const snap = (v) => Math.round(v / GRID) * GRID;
export const uid = (p) => p + Math.random().toString(36).slice(2, 9);

export class Editor {
  /**
   * ctx: {
   *   def(type) -> block definition or undefined,
   *   widening: [[src, dst], ...],
   *   onChange(), onSelect(), onSimToggle(inst), onOpenBlock(inst), onConnect(wire)
   * }
   */
  constructor(svg, ctx) {
    this.svg = svg;
    this.ctx = ctx;
    this.project = { blocks: [], wires: [], notes: [] };
    this.view = { x: 60, y: 60, k: 1 };
    this.sel = new Set(); // "b:<id>", "w:<id>", "n:<id>"
    this.diag = { blocks: new Map(), wires: new Map() };
    this.order = new Map();
    this.feedback = new Set();
    this.live = null; // Map "<block>:<port>" -> value, when online
    this.sim = null; // {levels: Map blockId -> 0/1} when simulating
    this.layouts = new Map();
    this.undoStack = [];
    this.redoStack = [];
    this.clipboard = null;
    this.drag = null;
    this._init();
  }

  // ------------------------------------------------------------------ setup
  _init() {
    const svg = this.svg;
    const defs = el('defs', {}, svg);
    const pat = el('pattern', { id: 'grid', width: GRID, height: GRID, patternUnits: 'userSpaceOnUse' }, defs);
    el('circle', { cx: 1, cy: 1, r: 0.8, class: 'grid-dot' }, pat);
    const pat2 = el('pattern', { id: 'grid-major', width: GRID * 10, height: GRID * 10, patternUnits: 'userSpaceOnUse' }, defs);
    el('rect', { width: GRID * 10, height: GRID * 10, fill: 'url(#grid)' }, pat2);
    el('path', { d: `M ${GRID * 10} 0 L 0 0 0 ${GRID * 10}`, class: 'grid-line' }, pat2);
    this.bg = el('rect', { class: 'canvas-bg', width: '100%', height: '100%', fill: 'url(#grid-major)' }, svg);
    this.vp = el('g', { class: 'viewport' }, svg);
    this.gNotes = el('g', {}, this.vp);
    this.gWires = el('g', {}, this.vp);
    this.gBlocks = el('g', {}, this.vp);
    this.gOverlay = el('g', {}, this.vp);

    svg.addEventListener('pointerdown', (e) => this._onDown(e));
    window.addEventListener('pointermove', (e) => this._onMove(e));
    window.addEventListener('pointerup', (e) => this._onUp(e));
    svg.addEventListener('wheel', (e) => this._onWheel(e), { passive: false });
    svg.addEventListener('dblclick', (e) => {
      const b = e.target.closest('.block');
      if (b) this.ctx.onOpenBlock?.(this.block(b.dataset.id));
    });
    svg.addEventListener('contextmenu', (e) => e.preventDefault());
    this._applyView();
  }

  setProject(project, keepView = false) {
    this.project = project;
    project.blocks ||= [];
    project.wires ||= [];
    project.notes ||= [];
    this.sel.clear();
    this.undoStack = [];
    this.redoStack = [];
    if (!keepView) requestAnimationFrame(() => this.fit());
    this.render();
    this.ctx.onSelect?.();
  }

  block(id) { return this.project.blocks.find((b) => b.id === id); }
  wire(id) { return this.project.wires.find((w) => w.id === id); }
  note(id) { return this.project.notes.find((n) => n.id === id); }

  // ------------------------------------------------------------------ undo
  checkpoint() {
    this.undoStack.push(JSON.stringify(this._doc()));
    if (this.undoStack.length > 200) this.undoStack.shift();
    this.redoStack = [];
  }
  _doc() { return { blocks: this.project.blocks, wires: this.project.wires, notes: this.project.notes }; }
  _restore(json) {
    const d = JSON.parse(json);
    Object.assign(this.project, d);
    for (const k of [...this.sel]) {
      const [t, id] = k.split(':');
      if ((t === 'b' && !this.block(id)) || (t === 'w' && !this.wire(id)) || (t === 'n' && !this.note(id))) this.sel.delete(k);
    }
    this.render();
    this.ctx.onChange?.();
    this.ctx.onSelect?.();
  }
  undo() {
    if (!this.undoStack.length) return;
    this.redoStack.push(JSON.stringify(this._doc()));
    this._restore(this.undoStack.pop());
  }
  redo() {
    if (!this.redoStack.length) return;
    this.undoStack.push(JSON.stringify(this._doc()));
    this._restore(this.redoStack.pop());
  }
  changed() {
    this.render();
    this.ctx.onChange?.();
  }

  // ------------------------------------------------------------------ edits
  uniqueName(type) {
    const used = new Set(this.project.blocks.map((b) => b.name));
    let base = type.replace(/[^A-Za-z0-9_]/g, '_');
    for (let i = 1; ; i++) if (!used.has(`${base}_${i}`)) return `${base}_${i}`;
  }

  addBlock(type, x, y) {
    const def = this.ctx.def(type);
    if (!def) return null;
    this.checkpoint();
    x = snap(x); y = snap(y);
    const inst = { id: uid('b'), type, name: this.uniqueName(type), x, y, values: {}, params: {}, type_args: {}, counts: {}, comment: '' };
    // don't place the block over another one (incl. its name label above and
    // comment below): move it down until the spot is free
    const me = this.layout(inst);
    const MARGIN = 24;
    const overlaps = () => this.project.blocks.some((b) => {
      const L = this.layouts.get(b.id) || this.layout(b);
      return x < b.x + L.w + MARGIN && b.x < x + me.w + MARGIN &&
        y - 20 < b.y + L.h + (b.comment ? 18 : 0) + MARGIN && b.y - 20 < y + me.h + MARGIN;
    });
    for (let i = 0; i < 100 && overlaps(); i++) y = snap(y + 20);
    inst.x = x; inst.y = y;
    this.project.blocks.push(inst);
    this.sel = new Set([`b:${inst.id}`]);
    this.changed();
    this.ctx.onSelect?.();
    return inst;
  }

  addNote(x, y) {
    this.checkpoint();
    const n = { id: uid('n'), x: snap(x), y: snap(y), text: 'Note' };
    this.project.notes.push(n);
    this.sel = new Set([`n:${n.id}`]);
    this.changed();
    this.ctx.onSelect?.();
  }

  deleteSelection() {
    if (!this.sel.size) return;
    this.checkpoint();
    const blocks = new Set([...this.sel].filter((k) => k.startsWith('b:')).map((k) => k.slice(2)));
    const wires = new Set([...this.sel].filter((k) => k.startsWith('w:')).map((k) => k.slice(2)));
    const notes = new Set([...this.sel].filter((k) => k.startsWith('n:')).map((k) => k.slice(2)));
    this.project.blocks = this.project.blocks.filter((b) => !blocks.has(b.id));
    this.project.wires = this.project.wires.filter((w) => !wires.has(w.id) && !blocks.has(w.src.block) && !blocks.has(w.dst.block));
    this.project.notes = this.project.notes.filter((n) => !notes.has(n.id));
    this.sel.clear();
    this.changed();
    this.ctx.onSelect?.();
  }

  selectAll() {
    this.sel = new Set([...this.project.blocks.map((b) => `b:${b.id}`), ...this.project.notes.map((n) => `n:${n.id}`)]);
    this.render();
    this.ctx.onSelect?.();
  }

  copy() {
    const ids = new Set([...this.sel].filter((k) => k.startsWith('b:')).map((k) => k.slice(2)));
    const notes = [...this.sel].filter((k) => k.startsWith('n:')).map((k) => this.note(k.slice(2)));
    if (!ids.size && !notes.length) return false;
    this.clipboard = JSON.stringify({
      blocks: this.project.blocks.filter((b) => ids.has(b.id)),
      wires: this.project.wires.filter((w) => ids.has(w.src.block) && ids.has(w.dst.block)),
      notes,
    });
    return true;
  }

  paste(offset = 30) {
    if (!this.clipboard) return;
    this.checkpoint();
    const data = JSON.parse(this.clipboard);
    const map = new Map();
    this.sel.clear();
    for (const b of data.blocks) {
      const nb = { ...b, id: uid('b'), x: b.x + offset, y: b.y + offset };
      nb.name = this.project.blocks.some((x) => x.name === b.name) ? this.uniqueName(b.type) : b.name;
      map.set(b.id, nb.id);
      this.project.blocks.push(nb);
      this.sel.add(`b:${nb.id}`);
    }
    for (const w of data.wires) {
      this.project.wires.push({ id: uid('w'), src: { block: map.get(w.src.block), port: w.src.port }, dst: { block: map.get(w.dst.block), port: w.dst.port } });
    }
    for (const n of data.notes) {
      const nn = { ...n, id: uid('n'), x: n.x + offset, y: n.y + offset };
      this.project.notes.push(nn);
      this.sel.add(`n:${nn.id}`);
    }
    this.clipboard = JSON.stringify({ ...data, blocks: data.blocks.map((b) => ({ ...b, x: b.x + offset, y: b.y + offset })), notes: data.notes.map((n) => ({ ...n, x: n.x + offset, y: n.y + offset })) });
    this.changed();
    this.ctx.onSelect?.();
  }

  duplicate() { if (this.copy()) this.paste(); }

  connect(src, dst) {
    // src = {block, port} output, dst = {block, port} input; replaces any wire already on dst
    this.checkpoint();
    this.project.wires = this.project.wires.filter((w) => !(w.dst.block === dst.block && w.dst.port === dst.port));
    const wire = { id: uid('w'), src, dst };
    this.project.wires.push(wire);
    this.ctx.onConnect?.(wire);
    this.sel = new Set([`w:${wire.id}`]);
    this.changed();
    this.ctx.onSelect?.();
  }

  // ------------------------------------------------------------------ geometry
  layout(inst) {
    const def = this.ctx.def(inst.type);
    if (!def) {
      return { w: 120, h: HEADER + ROW, inputs: [], outputs: [], def: null };
    }
    const { inputs, outputs, typeArgs } = resolvePorts(inst, def);
    const lw = Math.max(0, ...inputs.map((p) => p.name.length));
    const rw = Math.max(0, ...outputs.map((p) => p.name.length));
    const isSimIn = def.type === 'DI' || def.type === 'AI';
    const w = Math.max(MIN_W, (lw + rw) * 7 + 34 + (isSimIn ? 30 : 0), inst.type.length * 8 + 40);
    const rows = Math.max(inputs.length, outputs.length, 1);
    const h = HEADER + rows * ROW + 4;
    inputs.forEach((p, i) => { p.y = HEADER + ROW / 2 + i * ROW + 2; });
    outputs.forEach((p, i) => { p.y = HEADER + ROW / 2 + i * ROW + 2; });
    return { w, h, inputs, outputs, typeArgs, def };
  }

  portPos(blockId, port, dir) {
    const b = this.block(blockId);
    const L = this.layouts.get(blockId);
    if (!b || !L) return null;
    const list = dir === 'out' ? L.outputs : L.inputs;
    const p = list.find((x) => x.name === port);
    if (!p) return null;
    return { x: b.x + (dir === 'out' ? L.w : 0), y: b.y + p.y, type: p.type, port: p };
  }

  // ------------------------------------------------------------------ view
  _applyView() {
    const { x, y, k } = this.view;
    this.vp.setAttribute('transform', `translate(${x},${y}) scale(${k})`);
    for (const id of ['grid', 'grid-major']) {
      this.svg.querySelector(`#${id}`).setAttribute('patternTransform', `translate(${x},${y}) scale(${k})`);
    }
    this.ctx.onView?.(this.view);
  }

  toWorld(clientX, clientY) {
    const r = this.svg.getBoundingClientRect();
    return { x: (clientX - r.left - this.view.x) / this.view.k, y: (clientY - r.top - this.view.y) / this.view.k };
  }

  zoomAt(factor, clientX, clientY) {
    const r = this.svg.getBoundingClientRect();
    const cx = clientX ?? r.left + r.width / 2;
    const cy = clientY ?? r.top + r.height / 2;
    const k = Math.min(3, Math.max(0.2, this.view.k * factor));
    const wx = (cx - r.left - this.view.x) / this.view.k;
    const wy = (cy - r.top - this.view.y) / this.view.k;
    this.view.k = k;
    this.view.x = cx - r.left - wx * k;
    this.view.y = cy - r.top - wy * k;
    this._applyView();
  }

  fit() {
    const r = this.svg.getBoundingClientRect();
    const items = [
      ...this.project.blocks.map((b) => { const L = this.layouts.get(b.id) || this.layout(b); return [b.x - 60, b.y - 20, b.x + L.w + 60, b.y + L.h + 20]; }),
      ...this.project.notes.map((n) => [n.x, n.y, n.x + 240, n.y + 40]),
    ];
    if (!items.length || !r.width) { this.view = { x: 60, y: 60, k: 1 }; this._applyView(); return; }
    const x0 = Math.min(...items.map((i) => i[0])), y0 = Math.min(...items.map((i) => i[1]));
    const x1 = Math.max(...items.map((i) => i[2])), y1 = Math.max(...items.map((i) => i[3]));
    const k = Math.min(1.4, Math.max(0.25, Math.min(r.width / (x1 - x0), r.height / (y1 - y0))));
    this.view = { k, x: (r.width - (x1 - x0) * k) / 2 - x0 * k, y: (r.height - (y1 - y0) * k) / 2 - y0 * k };
    this._applyView();
  }

  centerOn(blockId) {
    const b = this.block(blockId);
    if (!b) return;
    const r = this.svg.getBoundingClientRect();
    const L = this.layouts.get(b.id) || this.layout(b);
    this.view.x = r.width / 2 - (b.x + L.w / 2) * this.view.k;
    this.view.y = r.height / 2 - (b.y + L.h / 2) * this.view.k;
    this._applyView();
  }

  centerWorld() {
    const r = this.svg.getBoundingClientRect();
    return this.toWorld(r.left + r.width / 2 - 50, r.top + r.height / 2 - 30);
  }

  // ------------------------------------------------------------------ rendering
  render() {
    this.layouts.clear();
    for (const b of this.project.blocks) this.layouts.set(b.id, this.layout(b));
    this.gBlocks.replaceChildren();
    this.gWires.replaceChildren();
    this.gNotes.replaceChildren();
    for (const n of this.project.notes) this._renderNote(n);
    this._planTrunks();
    for (const w of this.project.wires) this._renderWire(w);
    this._renderJunctions();
    for (const b of this.project.blocks) this._renderBlock(b);
    if (this.drag?.kind === 'wire') this._markCompatible(this.drag.from);  // keep highlights across re-renders
  }

  /** Nearest port (in world coordinates) that the wire being dragged may connect to. */
  _dropTarget(from, world) {
    let best = null, bestD = (14 / this.view.k) ** 2;
    for (const b of this.project.blocks) {
      if (b.id === from.block) continue;
      const L = this.layouts.get(b.id);
      if (!L) continue;
      const list = from.dir === 'out' ? L.inputs : L.outputs;
      for (const p of list) {
        const x = b.x + (from.dir === 'out' ? -8 : L.w + 8), y = b.y + p.y;
        const d = (x - world.x) ** 2 + (y - world.y) ** 2;
        if (d >= bestD) continue;
        const other = { block: b.id, port: p.name, type: p.type };
        const ok = from.dir === 'out' ? this._canConnect(from, other) : this._canConnect(other, from);
        if (ok) { best = other; bestD = d; }
      }
    }
    return best;
  }

  _renderNote(n) {
    const g = el('g', { class: `note${this.sel.has(`n:${n.id}`) ? ' selected' : ''}`, 'data-id': n.id, transform: `translate(${n.x},${n.y})` }, this.gNotes);
    const lines = (n.text || '').split('\n');
    const w = Math.max(60, ...lines.map((l) => l.length * 6.6)) + 16;
    el('rect', { width: w, height: lines.length * 16 + 10, rx: 3 }, g);
    const t = el('text', { x: 8, y: 17 }, g);
    lines.forEach((l, i) => el('tspan', { x: 8, dy: i ? 16 : 0 }, t, l || ' '));
  }

  // ------------------------------------------------------------------ orthogonal wire routing
  // Wires leave an output horizontally, run vertically along a "trunk" and enter the input
  // horizontally. All wires from one output share one trunk (like a bus in a schematic).
  // Wires that go backwards (feedback) are routed around underneath both blocks.

  /** End points of a wire at the tips of the port stubs. */
  _ends(w) {
    const a = this.portPos(w.src.block, w.src.port, 'out');
    const b = this.portPos(w.dst.block, w.dst.port, 'in');
    if (!a || !b) return null;
    return { a: { ...a, x: a.x + 8 }, b: { ...b, x: b.x - 8 } };
  }

  _isForward(a, b) { return b.x - a.x >= 2 * WIRE_STUB; }

  /** Choose the trunk x for every output that has forward wires, avoiding overlapping trunks. */
  _planTrunks() {
    const groups = new Map(); // "block:port" -> {a, ys: [], minX}
    for (const w of this.project.wires) {
      const e = this._ends(w);
      if (!e || !this._isForward(e.a, e.b)) continue;
      const key = `${w.src.block}:${w.src.port}`;
      const g = groups.get(key) || { a: e.a, ys: [], maxX: Infinity };
      g.ys.push(e.b.y);
      g.maxX = Math.min(g.maxX, e.b.x - WIRE_STUB);  // trunk must stay left of every input it feeds
      groups.set(key, g);
    }
    const placed = [];
    this.trunks = new Map();
    for (const [key, g] of groups) {
      const lo = g.a.x + WIRE_STUB;
      const ys = [g.a.y, ...g.ys];
      const span = [Math.min(...ys), Math.max(...ys)];
      // preferred: halfway to the nearest input, on the grid
      let x = Math.max(lo, Math.min(g.maxX, snap(g.a.x + (g.maxX + WIRE_STUB - g.a.x) / 2)));
      // nudge sideways while another trunk's vertical run would lie on top of this one
      const clash = (cx) => placed.some((p) => Math.abs(p.x - cx) < 4 && p.span[0] <= span[1] && span[0] <= p.span[1]);
      for (let i = 1; clash(x) && i < 12; i++) {
        const cand = x + (i % 2 ? 1 : -1) * Math.ceil(i / 2) * 8;
        if (cand >= lo && cand <= g.maxX && !clash(cand)) { x = cand; break; }
      }
      placed.push({ x, span });
      this.trunks.set(key, { x, a: g.a, ys: g.ys, span, type: g.a.type });
    }
  }

  _routeWire(w, a, b) {
    if (this._isForward(a, b)) {
      if (Math.abs(a.y - b.y) < 0.5) return `M ${a.x} ${a.y} H ${b.x}`;
      const x = this.trunks?.get(`${w.src.block}:${w.src.port}`)?.x ?? snap((a.x + b.x) / 2);
      return `M ${a.x} ${a.y} H ${x} V ${b.y} H ${b.x}`;
    }
    // backwards: out to the right, around the blocks (below or above, whichever is clear
    // and closer), back to the left and into the input
    const x1 = a.x + WIRE_STUB, x2 = b.x - WIRE_STUB;
    const rects = this.project.blocks.map((blk) => {
      const L = this.layouts.get(blk.id);
      return { x0: blk.x - 10, x1: blk.x + L.w + 10, y0: blk.y - 20, y1: blk.y + L.h + (blk.comment ? 18 : 4) };
    });
    const hits = (xa, xb, ya, yb) => rects.some((r) =>
      Math.min(xa, xb) < r.x1 && Math.max(xa, xb) > r.x0 && Math.min(ya, yb) < r.y1 && Math.max(ya, yb) > r.y0);
    const clear = (y) => !hits(x2, x1, y - 1, y + 1) && !hits(x1 - 1, x1 + 1, a.y, y) && !hits(x2 - 1, x2 + 1, y, b.y);
    const ends = [rects[this.project.blocks.indexOf(this.block(w.src.block))], rects[this.project.blocks.indexOf(this.block(w.dst.block))]];
    const below = snap(Math.max(ends[0].y1, ends[1].y1) + WIRE_STUB);
    const above = snap(Math.min(ends[0].y0, ends[1].y0) - WIRE_STUB);
    let y = below;
    for (let k = 0; k < 40; k++) {
      const down = below + k * GRID * 2, up = above - k * GRID * 2;
      const dDown = Math.abs(down - a.y) + Math.abs(down - b.y), dUp = Math.abs(up - a.y) + Math.abs(up - b.y);
      const order = dDown <= dUp ? [down, up] : [up, down];
      const found = order.find(clear);
      if (found !== undefined) { y = found; break; }
    }
    return `M ${a.x} ${a.y} H ${x1} V ${y} H ${x2} V ${b.y} H ${b.x}`;
  }

  /** Temporary wire while dragging: simple right-angle path between two points. */
  _wirePath(a, b) {
    if (this._isForward(a, b)) {
      const x = snap((a.x + b.x) / 2);
      return `M ${a.x} ${a.y} H ${x} V ${b.y} H ${b.x}`;
    }
    const x1 = a.x + WIRE_STUB, x2 = b.x - WIRE_STUB, y = (a.y + b.y) / 2;
    return `M ${a.x} ${a.y} H ${x1} V ${y} H ${x2} V ${b.y} H ${b.x}`;
  }

  /** Dots where three or more wire segments of one trunk meet. */
  _renderJunctions() {
    for (const [key, t] of this.trunks) {
      if (t.ys.length < 2) continue;
      const pts = new Map(); // y -> {left, right}
      const at = (y) => { const k = Math.round(y); if (!pts.has(k)) pts.set(k, { left: 0, right: 0 }); return pts.get(k); };
      at(t.a.y).left = 1;
      for (const y of t.ys) at(y).right = 1;
      const [y0, y1] = [Math.round(t.span[0]), Math.round(t.span[1])];
      const on = this.live && t.type === 'BOOL' && this.live.get(key) === true;
      for (const [y, p] of pts) {
        const n = p.left + p.right + (y > y0 ? 1 : 0) + (y < y1 ? 1 : 0);
        if (n >= 3) el('circle', { class: `junction${on ? ' on' : ''}`, cx: t.x, cy: y, r: on ? 3.5 : 3, fill: TYPE_COLORS[t.type] }, this.gWires);
      }
    }
  }

  _renderWire(w) {
    const e = this._ends(w);
    if (!e) return;
    const { a, b } = e;
    const d = this._routeWire(w, a, b);
    const cls = ['wire', `t-${a.type}`];
    if (this.sel.has(`w:${w.id}`)) cls.push('selected');
    if (this.feedback.has(w.id)) cls.push('feedback');
    const lvl = this.diag.wires.get(w.id);
    if (lvl) cls.push(lvl);
    if (a.type !== b.type && !compatible(a.type, b.type, this.ctx.widening)) cls.push('error');
    if (this.live && a.type === 'BOOL' && this.live.get(`${w.src.block}:${w.src.port}`) === true) cls.push('on');
    const g = el('g', { class: cls.join(' '), 'data-id': w.id }, this.gWires);
    el('path', { d, class: 'wire-hit' }, g);
    el('path', { d, class: 'wire-line', stroke: TYPE_COLORS[a.type] }, g);
  }

  _renderBlock(inst) {
    const L = this.layouts.get(inst.id);
    const def = L.def;
    const cls = ['block'];
    if (this.sel.has(`b:${inst.id}`)) cls.push('selected');
    const lvl = this.diag.blocks.get(inst.id);
    if (lvl) cls.push(lvl);
    if (!def) cls.push('unknown');
    const g = el('g', { class: cls.join(' '), 'data-id': inst.id, transform: `translate(${inst.x},${inst.y})` }, this.gBlocks);

    el('text', { class: 'inst-name', x: L.w / 2, y: -6, 'text-anchor': 'middle' }, g, inst.name);
    el('rect', { class: 'body', width: L.w, height: L.h, rx: 4 }, g);
    const color = def ? categoryColor(def) : '#999';
    el('path', { class: 'header', d: `M 0 ${HEADER} V 4 Q 0 0 4 0 H ${L.w - 4} Q ${L.w} 0 ${L.w} 4 V ${HEADER} Z`, fill: color }, g);
    el('text', { class: 'type', x: L.w / 2, y: 15, 'text-anchor': 'middle' }, g, def ? inst.type : `${inst.type} ?`);
    const ord = this.order.get(inst.id);
    if (ord) el('text', { class: 'order', x: L.w - 5, y: 15, 'text-anchor': 'end' }, g, ord);
    if (inst.comment) el('text', { class: 'comment', x: 0, y: L.h + 13 }, g, inst.comment);
    if (!def) return;

    const wiredIn = new Set(this.project.wires.filter((w) => w.dst.block === inst.id).map((w) => w.dst.port));
    for (const p of L.inputs) {
      el('line', { class: 'stub', x1: -8, y1: p.y, x2: 0, y2: p.y, stroke: TYPE_COLORS[p.type] }, g);
      el('circle', { class: 'port', cx: -8, cy: p.y, r: 3.5, fill: TYPE_COLORS[p.type] }, g);
      el('circle', { class: 'port-hit', cx: -8, cy: p.y, r: 9, 'data-port': p.name, 'data-dir': 'in', 'data-block': inst.id, 'data-type': p.type }, g);
      el('text', { class: 'port-label', x: 5, y: p.y + 4 }, g, p.name);
      if (!wiredIn.has(p.name)) {
        let txt, cls2 = 'const';
        const liveV = this.live?.get(`${inst.id}:${p.name}`);
        if (this.live && liveV !== undefined) { txt = formatValue(p.type, liveV); cls2 += ' live'; }
        else {
          const v = inst.values?.[p.name];
          const isSet = v !== undefined && v !== null && v !== '';
          txt = formatValue(p.type, isSet ? v : (p.def.default ?? zeroValue(p.type)));
          if (!isSet) cls2 += ' default';
        }
        el('text', { class: cls2, x: -14, y: p.y + 4, 'text-anchor': 'end' }, g, txt);
      }
    }
    for (const p of L.outputs) {
      el('line', { class: 'stub', x1: L.w, y1: p.y, x2: L.w + 8, y2: p.y, stroke: TYPE_COLORS[p.type] }, g);
      el('circle', { class: 'port', cx: L.w + 8, cy: p.y, r: 3.5, fill: TYPE_COLORS[p.type] }, g);
      el('circle', { class: 'port-hit', cx: L.w + 8, cy: p.y, r: 9, 'data-port': p.name, 'data-dir': 'out', 'data-block': inst.id, 'data-type': p.type }, g);
      el('text', { class: 'port-label', x: L.w - 5, y: p.y + 4, 'text-anchor': 'end' }, g, p.name);
      if (this.live) {
        const v = this.live.get(`${inst.id}:${p.name}`);
        if (v !== undefined) {
          el('text', { class: `live-value${p.type === 'BOOL' ? (v ? ' on' : ' off') : ''}`, x: L.w + 14, y: p.y - 4 }, g, formatValue(p.type, v));
        }
      }
    }
    if (this.live && def.type === 'DO') {
      const v = this.liveInput(inst.id, 'IN');
      el('circle', { class: `lamp${v ? ' on' : ''}`, cx: L.w - 14, cy: HEADER + 11, r: 6 }, g);
    }
    if (this.sim && def.type === 'DI') {
      const level = this.sim.levels.get(inst.id) ?? 0;
      const on = Boolean(level) !== Boolean(inst.params?.invert);
      const t = el('g', { class: `sim-toggle${on ? ' on' : ''}`, 'data-sim': inst.id, transform: `translate(6, ${HEADER + 3})` }, g);
      el('rect', { width: 34, height: 14, rx: 7 }, t);
      el('circle', { cx: on ? 27 : 7, cy: 7, r: 5 }, t);
      el('title', {}, t, 'Click to toggle the simulated input');
    }
  }

  setDiagnostics(result) {
    this.diag = { blocks: new Map(), wires: new Map() };
    const rank = { error: 3, warning: 2, info: 1 };
    for (const d of result?.diagnostics || []) {
      if (d.level === 'info') continue;
      if (d.block && (rank[d.level] > (rank[this.diag.blocks.get(d.block)] || 0))) this.diag.blocks.set(d.block, d.level);
      if (d.wire) this.diag.wires.set(d.wire, d.level);
    }
    this.order = new Map((result?.order || []).map((id, i) => [id, `#${i + 1}`]));
    this.feedback = new Set(result?.feedback || []);
    this.render();
  }

  setLive(map) { this.live = map; this.render(); }

  /** Live value of an input: its own value if unconnected, else the value of the output feeding it. */
  liveInput(blockId, port) {
    if (!this.live) return undefined;
    const w = this.project.wires.find((x) => x.dst.block === blockId && x.dst.port === port);
    return w ? this.live.get(`${w.src.block}:${w.src.port}`) : this.live.get(`${blockId}:${port}`);
  }

  // ------------------------------------------------------------------ interaction
  _onWheel(e) {
    e.preventDefault();
    if (e.ctrlKey || !e.shiftKey) {
      this.zoomAt(Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0015)), e.clientX, e.clientY);
    } else {
      this.view.x -= e.deltaY;
      this._applyView();
    }
  }

  _onDown(e) {
    if (e.button === 1 || (e.button === 0 && e.altKey)) {
      this.drag = { kind: 'pan', sx: e.clientX, sy: e.clientY, vx: this.view.x, vy: this.view.y, moved: false };
      e.preventDefault();
      return;
    }
    if (e.button !== 0) return;
    const t = e.target;
    const simT = t.closest('[data-sim]');
    if (simT) { this.ctx.onSimToggle?.(this.block(simT.dataset.sim)); e.stopPropagation(); return; }
    const port = t.closest('[data-port]');
    if (port) {
      const p = { block: port.dataset.block, port: port.dataset.port, dir: port.dataset.dir, type: port.dataset.type };
      const pos = this.portPos(p.block, p.port, p.dir);
      // starting from a connected input: pick the wire up and move it
      if (p.dir === 'in') {
        const existing = this.project.wires.find((w) => w.dst.block === p.block && w.dst.port === p.port);
        if (existing) {
          this.checkpoint();
          this.project.wires = this.project.wires.filter((w) => w !== existing);
          this.changed();
          const src = this.portPos(existing.src.block, existing.src.port, 'out');
          this._startWire({ block: existing.src.block, port: existing.src.port, dir: 'out', type: src.type }, src, e, true);
          return;
        }
      }
      this._startWire(p, pos, e, false);
      return;
    }
    const wireEl = t.closest('.wire');
    if (wireEl) {
      this._select(`w:${wireEl.dataset.id}`, e.shiftKey || e.ctrlKey);
      return;
    }
    const item = t.closest('.block') || t.closest('.note');
    if (item) {
      const key = `${item.classList.contains('block') ? 'b' : 'n'}:${item.dataset.id}`;
      if (e.shiftKey || e.ctrlKey) {
        if (this.sel.has(key)) this.sel.delete(key); else this.sel.add(key);
        this.render();
        this.ctx.onSelect?.();
      } else if (!this.sel.has(key)) {
        this._select(key, false);
      }
      const start = this.toWorld(e.clientX, e.clientY);
      const items = [...this.sel].filter((k) => !k.startsWith('w:')).map((k) => {
        const o = k[0] === 'b' ? this.block(k.slice(2)) : this.note(k.slice(2));
        return { o, x: o.x, y: o.y };
      });
      this.drag = { kind: 'move', start, items, moved: false };
      return;
    }
    // background
    if (e.shiftKey) {
      const start = this.toWorld(e.clientX, e.clientY);
      this.drag = { kind: 'marquee', start, rect: el('rect', { class: 'marquee', x: start.x, y: start.y, width: 0, height: 0 }, this.gOverlay) };
    } else {
      this.drag = { kind: 'pan', sx: e.clientX, sy: e.clientY, vx: this.view.x, vy: this.view.y, moved: false };
    }
  }

  _startWire(p, pos, e, alreadyCheckpointed) {
    const path = el('path', { class: `wire-temp`, stroke: TYPE_COLORS[p.type] }, this.gOverlay);
    this.drag = { kind: 'wire', from: p, pos, path, alreadyCheckpointed };
    this.svg.classList.add('wiring', `wiring-${p.dir}`);
    this._markCompatible(p);
    this._onMove(e);
  }

  _markCompatible(p) {
    for (const h of this.svg.querySelectorAll('.port-hit')) {
      const ok = h.dataset.dir !== p.dir && h.dataset.block !== p.block &&
        (p.dir === 'out' ? this._canConnect(p, h.dataset) : this._canConnect(h.dataset, p));
      h.classList.toggle('compatible', ok);
    }
  }

  _canConnect(out, inp) {
    if (compatible(out.type, inp.type, this.ctx.widening)) return true;
    // generic ports adapt to what they are connected to
    const gen = (blockId, portName, dir) => {
      const L = this.layouts.get(blockId);
      const list = dir === 'out' ? L?.outputs : L?.inputs;
      return list?.find((x) => x.name === portName)?.generic;
    };
    return Boolean(gen(inp.block, inp.port, 'in') || gen(out.block, out.port, 'out'));
  }

  _select(key, add) {
    if (!add) this.sel.clear();
    if (add && this.sel.has(key)) this.sel.delete(key); else this.sel.add(key);
    this.render();
    this.ctx.onSelect?.();
  }

  _onMove(e) {
    const d = this.drag;
    if (!d) return;
    if (d.kind === 'pan') {
      this.view.x = d.vx + e.clientX - d.sx;
      this.view.y = d.vy + e.clientY - d.sy;
      if (Math.abs(e.clientX - d.sx) + Math.abs(e.clientY - d.sy) > 3) d.moved = true;
      this._applyView();
    } else if (d.kind === 'move') {
      const p = this.toWorld(e.clientX, e.clientY);
      const dx = p.x - d.start.x, dy = p.y - d.start.y;
      if (!d.moved && Math.abs(dx) + Math.abs(dy) < 3 / this.view.k) return;
      if (!d.moved) { this.checkpoint(); d.moved = true; }
      for (const it of d.items) { it.o.x = snap(it.x + dx); it.o.y = snap(it.y + dy); }
      this.render();
    } else if (d.kind === 'wire') {
      const m = this.toWorld(e.clientX, e.clientY);
      const a = d.from.dir === 'out' ? d.pos : m;
      const b = d.from.dir === 'out' ? m : d.pos;
      d.path.setAttribute('d', this._wirePath(a, b));
    } else if (d.kind === 'marquee') {
      const p = this.toWorld(e.clientX, e.clientY);
      const x = Math.min(p.x, d.start.x), y = Math.min(p.y, d.start.y);
      Object.assign(d, { x, y, w: Math.abs(p.x - d.start.x), h: Math.abs(p.y - d.start.y) });
      d.rect.setAttribute('x', x); d.rect.setAttribute('y', y);
      d.rect.setAttribute('width', d.w); d.rect.setAttribute('height', d.h);
    }
  }

  _onUp(e) {
    const d = this.drag;
    if (!d) return;
    this.drag = null;
    if (d.kind === 'pan') {
      if (!d.moved && e.target.closest && e.target.closest('svg') === this.svg && this.sel.size) {
        this.sel.clear();
        this.render();
        this.ctx.onSelect?.();
      }
    } else if (d.kind === 'move') {
      if (d.moved) this.ctx.onChange?.();
    } else if (d.kind === 'wire') {
      d.path.remove();
      this.svg.classList.remove('wiring', 'wiring-in', 'wiring-out');
      const target = this._dropTarget(d.from, this.toWorld(e.clientX, e.clientY));
      if (target) {
        const t = { block: target.block, port: target.port };
        const f = { block: d.from.block, port: d.from.port };
        if (d.alreadyCheckpointed) this.undoStack.pop();  // merge "pick up" and "drop" into one undo step
        if (d.from.dir === 'out') this.connect(f, t); else this.connect(t, f);
      }
      for (const h of this.svg.querySelectorAll('.port-hit.compatible')) h.classList.remove('compatible');
    } else if (d.kind === 'marquee') {
      d.rect.remove();
      if (!e.ctrlKey) this.sel.clear();
      for (const b of this.project.blocks) {
        const L = this.layouts.get(b.id);
        if (b.x >= d.x && b.y >= d.y && b.x + L.w <= d.x + d.w && b.y + L.h <= d.y + d.h) this.sel.add(`b:${b.id}`);
      }
      for (const n of this.project.notes) {
        if (n.x >= d.x && n.y >= d.y && n.x <= d.x + d.w && n.y <= d.y + d.h) this.sel.add(`n:${n.id}`);
      }
      this.render();
      this.ctx.onSelect?.();
    }
  }
}
