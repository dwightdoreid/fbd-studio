// FBD Studio web UI: palette, properties, toolbar, problems/output/code panels, online mode.
import { Editor } from './editor.js';
import { Scope } from './scope.js';
import { categoryColor, expandAllowed, formatValue, resolvePorts, TYPE_COLORS, zeroValue } from './types.js';

const $ = (sel) => document.querySelector(sel);

// ===================================================================== helpers
function h(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (k === 'class') e.className = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(e.style, v);
    else if (k in e && typeof v !== 'string') e[k] = v;
    else e.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) if (c !== null && c !== undefined && c !== false) e.append(c instanceof Node ? c : String(c));
  return e;
}

async function api(method, url, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  const r = await fetch(url, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch { /* not json */ }
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
  }
  const ct = r.headers.get('content-type') || '';
  return ct.includes('json') ? r.json() : r.blob();
}

function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

function storage(key, value) {
  try {
    if (value === undefined) return JSON.parse(localStorage.getItem(key) || 'null');
    localStorage.setItem(key, JSON.stringify(value));
  } catch { return null; }
  return null;
}

// ===================================================================== state
const S = {
  lib: new Map(),
  groups: {},
  widening: [],
  libErrors: [],
  userBlocksDir: '',
  boards: new Map(),
  project: null,
  dirty: false,
  compile: null,
  online: null, // {kind: 'sim'|'serial', symbols, hash, startedDoc}
  live: null,
  pinLevels: new Map(), // sim: pin index -> 0/1
  analog: new Map(), // sim: pin index -> value
  propsRefs: { inputs: new Map(), live: new Map() },
};

const editor = new Editor($('#canvas'), {
  def: (t) => S.lib.get(t),
  get widening() { return S.widening; },
  onChange: () => { markDirty(); scheduleCompile(); updateCount(); checkOnlineDrift(); scope.refresh(); },
  onSelect: () => renderProps(),
  onConnect: (w) => inferTypes(w),
  onSimToggle: (inst) => simToggle(inst),
  onOpenBlock: () => $('#props input')?.focus(),
  onView: (v) => { $('#zoom-level').textContent = `${Math.round(v.k * 100)}%`; },
});

function newProject() {
  const board = S.boards.has('esp32dev') ? 'esp32dev' : [...S.boards.keys()][0] || 'esp32dev';
  return { version: 1, name: 'Untitled', description: '', board, settings: { cycle_ms: 10, monitor_ms: 200, baud: 115200 }, blocks: [], wires: [], notes: [] };
}

function setProject(p, dirty = false) {
  if (S.online) stopOnline();
  S.project = p;
  p.settings ||= { cycle_ms: 10, monitor_ms: 200, baud: 115200 };
  if (!Array.isArray(p.trend)) p.trend = [];
  editor.setProject(p);
  $('#project-name').value = p.name;
  S.dirty = dirty;
  $('#dirty').hidden = !dirty;
  updateCount();
  if (S.lib) renderPalette();
  compileNow();
  scope.refresh();
}

function markDirty() {
  S.dirty = true;
  $('#dirty').hidden = false;
  saveDraft();
}

const saveDraft = debounce(() => storage('fbd.draft', { project: S.project, dirty: S.dirty }), 500);

function updateCount() {
  const p = S.project;
  $('#status-count').textContent = `${p.blocks.length} blocks · ${p.wires.length} wires`;
  const b = S.boards.get(p.board);
  $('#status-board').textContent = b ? b.title : `Unknown board: ${p.board}`;
}

// ===================================================================== library / palette
async function loadLibrary(reload = false) {
  const data = await api(reload ? 'POST' : 'GET', reload ? '/api/library/reload' : '/api/library');
  S.lib = new Map(data.blocks.map((b) => [b.type, b]));
  S.groups = data.type_groups;
  S.widening = data.widening;
  S.libErrors = data.errors;
  S.userBlocksDir = data.user_blocks_dir;
  $('#help-blocks-dir').textContent = data.user_blocks_dir;
  const boards = await api('GET', '/api/boards');
  S.boards = new Map(boards.boards.map((b) => [b.id, b]));
  S.libErrors.push(...boards.errors);
  renderPalette();
}

function renderPalette() {
  const q = $('#palette-search').value.trim().toLowerCase();
  const list = $('#palette-list');
  const openState = storage('fbd.palette') || {};
  list.replaceChildren();
  if (S.libErrors.length) list.append(h('div', { class: 'pal-errors' }, 'Block definition problems:\n' + S.libErrors.join('\n')));
  const cats = new Map();
  for (const def of S.lib.values()) {
    const hay = `${def.type} ${def.title} ${def.description} ${def.category}`.toLowerCase();
    if (q && !hay.includes(q)) continue;
    if (!cats.has(def.category)) cats.set(def.category, []);
    cats.get(def.category).push(def);
  }
  for (const [cat, defs] of cats) {
    const det = h('details', { class: 'pal-cat', open: q ? true : openState[cat] !== false },
      h('summary', {}, h('span', { class: 'sw', style: { background: categoryColor(defs[0]) } }), cat));
    det.addEventListener('toggle', () => { if (!q) { openState[cat] = det.open; storage('fbd.palette', openState); } });
    const board = S.boards.get(S.project?.board);
    for (const def of defs) {
      // "a|b" means: one of them is enough
      const missing = (def.requires || []).filter((f) => board && !f.split('|').some((a) => (board.features || []).includes(a)));
      const needs = [
        missing.length ? `Not available on ${board.title} (needs ${missing.join(', ').replaceAll('|', ' or ')}).` : '',
        def.requires_blocks?.length ? `Needs a ${def.requires_blocks.join(' and a ').replaceAll('|', ' or ')} block.` : '',
        def.excludes_blocks?.length ? `Cannot be combined with ${def.excludes_blocks.join(', ')}.` : '',
        def.singleton ? 'Only one per program.' : '',
      ].filter(Boolean).join(' ');
      const item = h('div', {
        class: `pal-item${missing.length ? ' unavailable' : ''}`, draggable: 'true',
        title: `${def.title}\n\n${def.description}${needs ? `\n\n${needs}` : ''}`,
        onclick: () => { const c = editor.centerWorld(); editor.addBlock(def.type, c.x, c.y); },
        ondragstart: (e) => { e.dataTransfer.setData('text/x-fbd-block', def.type); e.dataTransfer.effectAllowed = 'copy'; },
      }, h('b', {}, def.type), h('span', {}, def.title));
      det.append(item);
    }
    list.append(det);
  }
}

const canvas = $('#canvas');
canvas.addEventListener('dragover', (e) => { if (e.dataTransfer.types.includes('text/x-fbd-block')) { e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; } });
canvas.addEventListener('drop', (e) => {
  const type = e.dataTransfer.getData('text/x-fbd-block');
  if (!type) return;
  e.preventDefault();
  const p = editor.toWorld(e.clientX, e.clientY);
  editor.addBlock(type, p.x - 20, p.y - 10);
  canvas.focus();
});

// ===================================================================== type inference
function inferTypes(wire) {
  const src = editor.block(wire.src.block), dst = editor.block(wire.dst.block);
  const sd = S.lib.get(src?.type), dd = S.lib.get(dst?.type);
  if (!sd || !dd) return;
  const sp = resolvePorts(src, sd).outputs.find((p) => p.name === wire.src.port);
  const dp = resolvePorts(dst, dd).inputs.find((p) => p.name === wire.dst.port);
  if (!sp || !dp) return;
  if (dp.generic) trySetType(dst, dd, dp.generic, sp.type, wire);
  else if (sp.generic) trySetType(src, sd, sp.generic, dp.type, wire);
}

function trySetType(inst, def, tp, type, wire) {
  if (!expandAllowed(def.type_params[tp].allowed, S.groups).includes(type)) return;
  const { inputs, outputs } = resolvePorts(inst, def);
  const genIn = new Set(inputs.filter((p) => p.generic === tp).map((p) => p.name));
  const genOut = new Set(outputs.filter((p) => p.generic === tp).map((p) => p.name));
  const others = S.project.wires.filter((w) => w !== wire &&
    ((w.dst.block === inst.id && genIn.has(w.dst.port)) || (w.src.block === inst.id && genOut.has(w.src.port))));
  if (others.length) return; // already pinned by other connections
  inst.type_args ||= {};
  inst.type_args[tp] = type;
}

// ===================================================================== compile
let compileSeq = 0;
async function compileNow() {
  const seq = ++compileSeq;
  try {
    const res = await api('POST', '/api/compile', { project: S.project, target: 'device' });
    if (seq !== compileSeq) return;
    applyCompile(res);
  } catch (e) {
    setStatus(`Check failed: ${e.message}`);
  }
}
const scheduleCompile = debounce(compileNow, 350);

function applyCompile(res) {
  S.compile = res;
  editor.setDiagnostics(res);
  renderProblems(res.diagnostics);
  renderCode(res.code);
  markInvalidFields();
}

function renderProblems(diags) {
  const ul = $('#problems');
  ul.replaceChildren();
  const errs = diags.filter((d) => d.level === 'error').length;
  const warns = diags.filter((d) => d.level === 'warning').length;
  const badge = $('#problem-count');
  badge.textContent = errs || warns || '';
  badge.className = `badge${errs ? ' err' : warns ? ' warn' : ''}`;
  if (!diags.length) { ul.append(h('li', { class: 'none' }, '✓ No problems. The program is ready to simulate or upload.')); return; }
  const rank = { error: 0, warning: 1, info: 2 };
  for (const d of [...diags].sort((a, b) => rank[a.level] - rank[b.level])) {
    ul.append(h('li', {
      onclick: () => {
        if (d.block && editor.block(d.block)) { editor.sel = new Set([`b:${d.block}`]); editor.centerOn(d.block); editor.render(); renderProps(); }
        else if (d.wire && editor.wire(d.wire)) { editor.sel = new Set([`w:${d.wire}`]); editor.render(); renderProps(); }
      },
    }, h('span', { class: `lvl ${d.level}` }, d.level), d.message));
  }
}

function renderCode(code) {
  const pre = $('#code');
  if (!code) { pre.textContent = '// Fix the errors listed under "Problems" to see the generated C++ code.'; return; }
  const esc = code.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const kw = /\b(void|bool|int|float|double|uint8_t|uint16_t|uint32_t|int8_t|int16_t|int32_t|struct|template|typename|static|const|return|if|else|for|while|true|false|include|define|ifndef|endif)\b/g;
  pre.innerHTML = esc.split('\n').map((line) => {
    const i = line.indexOf('//');
    const codePart = i >= 0 ? line.slice(0, i) : line;
    const cm = i >= 0 ? `<span class="cm">${line.slice(i)}</span>` : '';
    return codePart.replace(kw, '<span class="kw">$1</span>').replace(/\b(\d+(\.\d+)?f?)\b/g, '<span class="num">$1</span>') + cm;
  }).join('\n');
}

function markInvalidFields() {
  const sel = selectedBlock();
  if (!sel) return;
  const bad = new Set((S.compile?.diagnostics || []).filter((d) => d.block === sel.id && d.port && d.level === 'error').map((d) => d.port));
  for (const [port, input] of S.propsRefs.inputs) input.classList.toggle('invalid', bad.has(port));
}

// ===================================================================== scope / trend
// Plotted signals are stored in the project as {block, port}. A wired input shows the
// value of the output it is connected to.
function portsOf(inst) {
  const def = S.lib.get(inst.type);
  return def ? resolvePorts(inst, def) : { inputs: [], outputs: [] };
}

function signalSource(ref) {
  const w = S.project.wires.find((x) => x.dst.block === ref.block && x.dst.port === ref.port);
  return w ? w.src : ref;
}

function plotted(blockId, port) {
  return (S.project.trend || []).some((r) => r.block === blockId && r.port === port);
}

function togglePlot(blockId, port) {
  edit(() => {
    const t = S.project.trend ||= [];
    const i = t.findIndex((r) => r.block === blockId && r.port === port);
    if (i >= 0) t.splice(i, 1); else t.push({ block: blockId, port });
  }, true);
  if (!plotted(blockId, port)) return;
  showTab('scope');
}

const scope = new Scope($('#scope'), {
  signals() {
    const out = [];
    for (const ref of S.project?.trend || []) {
      const inst = editor.block(ref.block);
      if (!inst) continue;
      const { inputs, outputs } = portsOf(inst);
      const p = [...outputs, ...inputs].find((x) => x.name === ref.port);
      if (!p) continue;
      const src = signalSource(ref);
      out.push({ id: `${ref.block}:${ref.port}`, label: `${inst.name}.${ref.port}`, type: p.type,
        key: `${src.block}:${src.port}`, sym: symbolFor(src.block, src.port)?.idx ?? null });
    }
    return out;
  },
  ports() {
    const out = [];
    for (const inst of S.project?.blocks || []) {
      const { inputs, outputs } = portsOf(inst);
      for (const p of outputs) out.push({ id: `${inst.id}:${p.name}`, label: `${inst.name}.${p.name}`, type: p.type });
      for (const p of inputs) {
        if (!S.project.wires.some((w) => w.dst.block === inst.id && w.dst.port === p.name)) {
          out.push({ id: `${inst.id}:${p.name}`, label: `${inst.name}.${p.name}`, type: p.type });
        }
      }
    }
    return out.sort((a, b) => a.label.localeCompare(b.label));
  },
  add(id) { const [block, port] = id.split(':'); if (!plotted(block, port)) togglePlot(block, port); },
  remove(id) { const [block, port] = id.split(':'); if (plotted(block, port)) togglePlot(block, port); },
  online: () => !!S.online,
  monitorMs: () => S.project?.settings?.monitor_ms,
  startCapture: (req) => api('POST', '/api/capture', req),
  cancelCapture: () => api('POST', '/api/capture/cancel'),
  status: (t) => setStatus(t),
});

function plotButton(inst, port) {
  const on = plotted(inst.id, port);
  return h('button', { class: `plot-btn${on ? ' on' : ''}`, title: on ? 'Remove from the scope' : 'Plot in the scope',
    onclick: () => togglePlot(inst.id, port) }, '📈');
}

// ===================================================================== properties panel
function selectedBlock() {
  if (editor.sel.size !== 1) return null;
  const [k] = editor.sel;
  return k.startsWith('b:') ? editor.block(k.slice(2)) : null;
}

function edit(fn, rerender = false) {
  editor.checkpoint();
  fn();
  editor.changed();
  if (rerender) renderProps();
}

function field(label, control, hint, hintClass) {
  return h('div', { class: 'field' }, h('label', { title: label }, label), control, hint ? h('div', { class: `hint ${hintClass || ''}` }, hint) : null);
}

function renderProps() {
  const el = $('#props');
  S.propsRefs = { inputs: new Map(), live: new Map() };
  el.replaceChildren();
  const keys = [...editor.sel];
  if (keys.length === 0) return renderProjectProps(el);
  if (keys.length > 1) {
    el.append(h('h2', {}, `${keys.length} items selected`),
      h('div', { class: 'actions' },
        h('button', { onclick: () => editor.duplicate() }, 'Duplicate'),
        h('button', { class: 'danger', onclick: () => editor.deleteSelection() }, 'Delete')));
    return;
  }
  const [kind, id] = [keys[0][0], keys[0].slice(2)];
  if (kind === 'b') return renderBlockProps(el, editor.block(id));
  if (kind === 'w') return renderWireProps(el, editor.wire(id));
  if (kind === 'n') return renderNoteProps(el, editor.note(id));
}

function renderProjectProps(el) {
  const p = S.project;
  const boardSel = h('select', { onchange: (e) => edit(() => { p.board = e.target.value; updateCount(); renderPalette(); }, true) },
    [...S.boards.values()].map((b) => h('option', { value: b.id, selected: b.id === p.board }, b.title)));
  const num = (key, min, max, title) => h('input', {
    type: 'number', min, max, value: p.settings[key], title,
    onchange: (e) => edit(() => { p.settings[key] = Math.max(min, Math.min(max, parseInt(e.target.value, 10) || 0)); }, true),
  });
  const board = S.boards.get(p.board);
  el.append(
    h('h2', {}, 'Project'),
    h('div', { class: 'desc' }, 'Nothing selected. These settings apply to the whole program.'),
    field('Description', h('textarea', { rows: 3, value: p.description || '', onchange: (e) => edit(() => { p.description = e.target.value; }) })),
    h('h3', {}, 'Target'),
    field('Board', boardSel, board?.description),
    field('Scan cycle', num('cycle_ms', 0, 10000, 'ms; 0 = as fast as possible'), 'ms per scan; 0 = free running'),
    h('h3', {}, 'Online monitoring'),
    field('Update every', num('monitor_ms', 0, 10000), 'ms; 0 = off (saves serial bandwidth)'),
    field('Baud rate', h('select', { onchange: (e) => edit(() => { p.settings.baud = parseInt(e.target.value, 10); }) },
      [9600, 57600, 115200, 230400, 460800, 921600].map((b) => h('option', { value: b, selected: b === p.settings.baud }, b)))),
    h('div', { class: 'tip' },
      h('b', {}, 'Getting started: '),
      'drag blocks from the library, connect outputs (right) to inputs (left), then press ', h('b', {}, '▶ Simulate'),
      '. Open the examples via ', h('b', {}, 'Open…'), '.'),
  );
}

function renderWireProps(el, w) {
  const s = editor.block(w.src.block), d = editor.block(w.dst.block);
  el.append(h('h2', {}, 'Wire'),
    h('div', { class: 'kv' }, h('span', {}, 'From'), h('span', {}, `${s?.name}.${w.src.port}`), h('span', {}, 'To'), h('span', {}, `${d?.name}.${w.dst.port}`)),
    editor.feedback.has(w.id) ? h('div', { class: 'tip' }, 'This wire closes a feedback loop: the receiving block gets the value from the previous scan cycle.') : '',
    h('div', { class: 'actions' }, h('button', { class: 'danger', onclick: () => editor.deleteSelection() }, 'Delete wire')));
}

function renderNoteProps(el, n) {
  el.append(h('h2', {}, 'Note'),
    h('textarea', { rows: 8, style: { width: '100%' }, value: n.text, onchange: (e) => edit(() => { n.text = e.target.value; }) }),
    h('div', { class: 'actions' }, h('button', { class: 'danger', onclick: () => editor.deleteSelection() }, 'Delete note')));
}

function symbolFor(blockId, port) {
  return S.online?.symbols.find((s) => s.block === blockId && s.port === port);
}

function renderBlockProps(el, inst) {
  const def = S.lib.get(inst.type);
  el.append(h('h2', {}, inst.type));
  if (!def) {
    el.append(h('div', { class: 'desc' }, `Unknown block type "${inst.type}". If it is a user block, put its YAML file in the blocks folder and press Reload blocks.`),
      h('div', { class: 'actions' }, h('button', { class: 'danger', onclick: () => editor.deleteSelection() }, 'Delete')));
    return;
  }
  const { inputs, outputs, typeArgs } = resolvePorts(inst, def);
  el.append(h('div', { class: 'desc' }, h('b', {}, def.title), ' — ', def.description));
  if (def.source !== 'builtin') el.append(h('div', { class: 'src' }, `User block: ${def.source}`));

  el.append(h('h3', {}, 'Instance'),
    field('Name', h('input', {
      value: inst.name, spellcheck: false,
      onchange: (e) => edit(() => { inst.name = e.target.value.trim(); }),
    })),
    field('Comment', h('input', { value: inst.comment || '', onchange: (e) => edit(() => { inst.comment = e.target.value; }) })));

  // type parameters
  const tps = Object.entries(def.type_params || {});
  if (tps.length) {
    el.append(h('h3', {}, 'Data type'));
    for (const [tp, spec] of tps) {
      const allowed = expandAllowed(spec.allowed, S.groups);
      el.append(field(tp, h('select', { onchange: (e) => edit(() => { inst.type_args[tp] = e.target.value; }, true) },
        allowed.map((t) => h('option', { value: t, selected: t === typeArgs[tp] }, t))),
        'Set automatically when you connect a wire'));
    }
  }

  // variadic inputs
  const vars = def.inputs.filter((p) => p.variadic);
  if (vars.length) {
    for (const p of vars) {
      const v = p.variadic;
      const n = inst.counts?.[p.name] ?? v.default;
      el.append(field(`${p.name} inputs`, h('input', {
        type: 'number', min: v.min, max: v.max, value: n,
        onchange: (e) => edit(() => {
          const k = Math.max(v.min, Math.min(v.max, parseInt(e.target.value, 10) || v.default));
          inst.counts[p.name] = k;
          const valid = new Set(Array.from({ length: k }, (_, i) => `${p.name}${i + v.start}`));
          S.project.wires = editor.project.wires = editor.project.wires.filter((w) =>
            !(w.dst.block === inst.id && w.dst.port.startsWith(p.name) && /\d+$/.test(w.dst.port.slice(p.name.length)) && !valid.has(w.dst.port)));
        }, true),
      }), `${v.min}..${v.max}`));
    }
  }

  // parameters
  if (def.params.length) {
    el.append(h('h3', {}, 'Parameters'));
    const board = S.boards.get(S.project.board);
    for (const p of def.params) {
      let cur = inst.params?.[p.name] ?? p.default;
      if (p.kind === 'pin' && typeof cur === 'string' && cur.startsWith('@')) cur = board?.pin_defaults?.[cur.slice(1)] ?? '';
      let ctl, hint, hintCls;
      if (p.kind === 'pin') {
        const pins = (board?.pins || []).filter((x) => x.caps.includes(p.capability));
        ctl = h('select', { onchange: (e) => edit(() => { inst.params[p.name] = e.target.value; }, true) },
          h('option', { value: '', selected: !cur }, '— select pin —'),
          pins.map((x) => h('option', { value: x.id, selected: x.id === cur }, `${x.id}${x.warn ? ' ⚠' : ''}`)));
        const pin = pins.find((x) => x.id === cur);
        hint = pin?.note || `Pins that support ${p.capability} on this board`;
        hintCls = pin?.warn ? 'warn' : '';
      } else if (p.kind === 'enum') {
        ctl = h('select', { onchange: (e) => edit(() => { inst.params[p.name] = e.target.value; }, true) },
          Object.keys(p.options).map((o) => h('option', { value: o, selected: o === cur }, o)));
      } else if (p.kind === 'bool') {
        ctl = h('input', { type: 'checkbox', checked: Boolean(cur), style: { width: 'auto', justifySelf: 'start' }, onchange: (e) => edit(() => { inst.params[p.name] = e.target.checked; }, true) });
      } else if (p.kind === 'string') {
        const inp = h('input', { type: p.secret ? 'password' : 'text', value: cur ?? '', maxlength: 200, autocomplete: 'off', spellcheck: false,
          onchange: (e) => edit(() => { inst.params[p.name] = e.target.value; }) });
        ctl = p.secret
          ? h('div', { class: 'secret' }, inp, h('button', { type: 'button', title: 'Show / hide',
            onclick: () => { inp.type = inp.type === 'password' ? 'text' : 'password'; } }, '👁'))
          : inp;
      } else if (p.kind === 'int' && p.hex) {
        const show = (v) => (typeof v === 'number' && v >= 0 ? `0x${v.toString(16).toUpperCase().padStart(2, '0')}` : (v ?? ''));
        ctl = h('input', { type: 'text', value: show(cur), spellcheck: false, placeholder: '0x00',
          onchange: (e) => edit(() => {
            const t = e.target.value.trim();
            const v = /^-?(0x[0-9a-f]+|\d+)$/i.test(t) ? Number(t.startsWith('-') ? -Number(t.slice(1)) : Number(t)) : t;
            inst.params[p.name] = t === '' ? null : v;
          }) });
        if (typeof cur === 'number' && cur >= 0) hint = `${p.description ? `${p.description} · ` : ''}= ${cur} decimal`;
      } else {
        ctl = h('input', { type: 'number', value: cur ?? '', min: p.min ?? undefined, max: p.max ?? undefined, step: p.kind === 'int' ? 1 : 'any',
          onchange: (e) => edit(() => { inst.params[p.name] = e.target.value === '' ? null : Number(e.target.value); }) });
      }
      el.append(field(p.name, ctl, hint || p.description, hintCls));
    }
  }

  // simulator controls
  if (S.online?.kind === 'sim') renderSimControls(el, inst, def);

  // inputs
  const wires = S.project.wires;
  if (inputs.length) {
    el.append(h('h3', {}, S.online ? 'Inputs (online)' : 'Inputs'));
    for (const p of inputs) {
      const w = wires.find((x) => x.dst.block === inst.id && x.dst.port === p.name);
      let right;
      if (w) {
        const src = editor.block(w.src.block);
        right = h('span', { class: 'wired', title: 'Connected' }, `← ${src?.name}.${w.src.port}`);
      } else if (S.online) {
        const sym = symbolFor(inst.id, p.name);
        const live = h('span', { class: 'live' }, formatValue(p.type, S.live?.get(`${inst.id}:${p.name}`)));
        S.propsRefs.live.set(p.name, { span: live, type: p.type });
        const inp = h('input', { placeholder: 'new value', title: 'Changes the value in the running program only. Edit offline to keep it in the project.' });
        const send = async () => {
          if (!sym) return setStatus('This input is not available online — restart the simulation / upload again.');
          try { await api('POST', '/api/link/write', { idx: sym.idx, value: inp.value }); inp.value = ''; inp.classList.remove('invalid'); }
          catch (err) { inp.classList.add('invalid'); setStatus(err.message); }
        };
        inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') send(); });
        right = h('div', { class: 'online-edit' }, live, inp, h('button', { class: 'small', onclick: send }, 'Set'));
      } else {
        const v = inst.values?.[p.name];
        const ph = formatValue(p.type, p.def.default ?? zeroValue(p.type));
        const inp = h('input', {
          value: v ?? '', placeholder: ph, spellcheck: false,
          title: p.def.description || (p.type === 'TIME' ? 'e.g. T#1s500ms, T#2m, or a number of milliseconds' : p.type === 'BOOL' ? 'TRUE or FALSE' : ''),
          onchange: (e) => edit(() => { const t = e.target.value.trim(); if (t === '') delete inst.values[p.name]; else inst.values[p.name] = t; }),
        });
        S.propsRefs.inputs.set(p.name, inp);
        right = inp;
      }
      el.append(h('div', { class: 'io-row plot' }, h('span', { class: 'name', title: p.def.description || p.name }, p.name),
        h('span', { class: 'ty', style: { background: TYPE_COLORS[p.type] } }, p.type), right, plotButton(inst, p.name)));
    }
  }
  if (outputs.length) {
    el.append(h('h3', {}, 'Outputs'));
    for (const p of outputs) {
      const live = h('span', { class: 'live' }, S.online ? formatValue(p.type, S.live?.get(`${inst.id}:${p.name}`)) : '');
      S.propsRefs.live.set(p.name, { span: live, type: p.type });
      el.append(h('div', { class: 'io-row plot' }, h('span', { class: 'name', title: p.def.description || p.name }, p.name),
        h('span', { class: 'ty', style: { background: TYPE_COLORS[p.type] } }, p.type), live, plotButton(inst, p.name)));
    }
  }
  el.append(h('div', { class: 'actions' },
    h('button', { onclick: () => editor.duplicate() }, 'Duplicate'),
    h('button', { class: 'danger', onclick: () => editor.deleteSelection() }, 'Delete')));
  markInvalidFields();
}

// ===================================================================== simulator I/O
function pinIndex(inst) {
  const board = S.boards.get(S.project.board);
  return board ? board.pins.findIndex((p) => p.id === inst.params?.pin) : -1;
}

function renderSimControls(el, inst, def) {
  const idx = pinIndex(inst);
  if (idx < 0) return;
  if (def.type === 'DI') {
    const level = S.pinLevels.get(idx) ?? 0;
    el.append(h('h3', {}, 'Simulator'),
      field('Pin level', h('button', { onclick: () => simToggle(inst) }, level ? 'HIGH (click for LOW)' : 'LOW (click for HIGH)'),
        'You can also click the switch on the block'));
  } else if (def.type === 'AI') {
    const max = S.boards.get(S.project.board)?.adc_max ?? 4095;
    const val = S.analog.get(idx) ?? 0;
    const out = h('span', {}, val);
    const slider = h('input', {
      type: 'range', min: 0, max, value: val,
      oninput: (e) => { const v = parseInt(e.target.value, 10); out.textContent = v; S.analog.set(idx, v); api('POST', '/api/sim/pin', { kind: 'A', pin: idx, value: v }).catch(() => {}); },
    });
    el.append(h('h3', {}, 'Simulator'), field('Analog value', h('div', { class: 'slider-row' }, slider, out), `Raw ADC value 0..${max}`));
  }
}

function simToggle(inst) {
  if (S.online?.kind !== 'sim') return;
  const idx = pinIndex(inst);
  if (idx < 0) return setStatus(`${inst.name}: select a pin first`);
  const level = (S.pinLevels.get(idx) ?? 0) ? 0 : 1;
  S.pinLevels.set(idx, level);
  api('POST', '/api/sim/pin', { kind: 'D', pin: idx, value: level }).catch((e) => setStatus(e.message));
  updateSimLevels();
  editor.render();
  if (selectedBlock()?.id === inst.id) renderProps();
}

function updateSimLevels() {
  if (S.online?.kind !== 'sim') { editor.sim = null; return; }
  const levels = new Map();
  for (const b of S.project.blocks) if (b.type === 'DI') levels.set(b.id, S.pinLevels.get(pinIndex(b)) ?? 0);
  editor.sim = { levels };
}

// ===================================================================== online (sim / serial)
function onlineStarted(kind, res, extra = '') {
  S.online = { kind, symbols: res.compile.symbols, hash: res.compile.hash, startedDoc: docKey() };
  S.live = new Map();
  if (kind === 'sim') {
    S.pinLevels = new Map();
    S.analog = new Map();
    for (const b of S.project.blocks) {
      if (b.type === 'DI') { const i = pinIndex(b); if (i >= 0) S.pinLevels.set(i, b.params?.mode === 'PULLUP' ? 1 : 0); }
    }
  }
  updateSimLevels();
  editor.setLive(S.live);
  $('#btn-stop').hidden = false;
  $('#btn-sim').hidden = true;
  const pill = $('#status-link');
  pill.textContent = kind === 'sim' ? 'Simulating' : `Online ${extra}`;
  pill.className = `pill ${kind === 'sim' ? 'sim' : 'online'}`;
  banner(kind === 'sim' ? 'SIMULATION — click DI switches to change inputs' : `ONLINE — ${extra}`);
  renderProps();
  scope.refresh();
}

function stopOnline(text) {
  if (!S.online) return;
  api('POST', '/api/link/stop').catch(() => {});
  S.online = null;
  S.live = null;
  editor.sim = null;
  editor.setLive(null);
  $('#btn-stop').hidden = true;
  $('#btn-sim').hidden = false;
  $('#status-link').textContent = 'Offline';
  $('#status-link').className = 'pill';
  $('#status-scan').textContent = '';
  banner(null);
  if (text) setStatus(text);
  renderProps();
  scope.onOffline();
}

function banner(text, warn = false) {
  const b = $('#online-banner');
  b.hidden = !text;
  b.textContent = text || '';
  b.classList.toggle('warn', warn);
}

const docKey = () => JSON.stringify({ b: S.project.blocks.map((b) => [b.id, b.type, b.type_args, b.counts, b.params, b.values]), w: S.project.wires, s: S.project.settings, board: S.project.board });

function checkOnlineDrift() {
  if (!S.online) return;
  if (docKey() !== S.online.startedDoc) banner('Diagram changed — restart the simulation / upload again to apply', true);
}

async function startSim() {
  setStatus('Compiling for the simulator…');
  $('#btn-sim').disabled = true;
  try {
    const res = await api('POST', '/api/sim/start', { project: S.project });
    applyCompile(res.compile);
    if (!res.started) {
      if (res.error) { log(res.error, 'err'); showTab('output'); } else showTab('problems');
      setStatus(res.error ? 'Simulator could not start' : 'Fix the problems first');
      return;
    }
    onlineStarted('sim', res);
    setStatus('Simulation running');
  } catch (e) { setStatus(e.message); }
  finally { $('#btn-sim').disabled = false; }
}

async function startMonitor() {
  const port = $('#port-select').value;
  if (!port) return setStatus('Select the serial port of your board first (⟳ refreshes the list)');
  try {
    const res = await api('POST', '/api/monitor/start', { project: S.project, port });
    applyCompile(res.compile);
    if (!res.started) { if (res.error) log(res.error, 'err'); setStatus(res.error || 'Fix the problems first'); return; }
    onlineStarted('serial', res, port);
    setStatus(`Connected to ${port}, waiting for the board…`);
  } catch (e) { setStatus(e.message); }
}

async function build(upload) {
  const port = $('#port-select').value;
  if (upload && !port && !confirm('No serial port selected. Let PlatformIO auto-detect the board?')) return;
  if (upload && S.online?.kind === 'serial') stopOnline();
  try {
    const res = await api('POST', '/api/build', { project: S.project, upload, port: port || null });
    applyCompile(res.compile);
    if (!res.started) {
      if (res.error) { log(res.error, 'err'); if (res.dir) log(`The PlatformIO project was generated in: ${res.dir}`, 'dim'); showTab('output'); }
      else { showTab('problems'); setStatus('Fix the problems first'); }
      return;
    }
    showTab('output');
    log(`${upload ? 'Uploading' : 'Building'} in ${res.dir} …`, 'dim');
    setStatus(upload ? 'Uploading…' : 'Building…');
  } catch (e) { setStatus(e.message); log(e.message, 'err'); }
}

async function refreshPorts() {
  const sel = $('#port-select');
  const prev = sel.value || storage('fbd.port');
  try {
    const { ports } = await api('GET', '/api/ports');
    // Remember the previous choice; otherwise pick the port if there is exactly one.
    const pick = ports.some((p) => p.device === prev) ? prev : (ports.length === 1 ? ports[0].device : '');
    sel.replaceChildren(h('option', { value: '' }, ports.length ? '— serial port —' : 'no serial ports found'),
      ...ports.map((p) => h('option', { value: p.device, title: p.hwid || p.description }, `${p.device}${p.description && p.description !== 'n/a' ? ` – ${p.description}` : ''}`)));
    sel.value = pick;
  } catch (e) { setStatus(e.message); }
}

// ----- websocket events
function connectWs() {
  const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
  ws.onmessage = (m) => onEvent(JSON.parse(m.data));
  ws.onclose = () => setTimeout(connectWs, 1500);
}

function onEvent(ev) {
  switch (ev.ev) {
    case 'values': {
      if (!S.online) return;
      const map = new Map();
      S.online.symbols.forEach((s, i) => map.set(`${s.block}:${s.port}`, ev.values[i]));
      S.live = map;
      editor.setLive(map);
      for (const [port, ref] of S.propsRefs.live) {
        const id = selectedBlock()?.id;
        if (id) ref.span.textContent = formatValue(ref.type, map.get(`${id}:${port}`));
      }
      $('#status-scan').textContent = `scan ${ev.scan_us} µs (max ${ev.scan_max_us} µs)`;
      scope.onValues(ev.t, map);
      break;
    }
    case 'capture_state': if (S.online) scope.onCaptureState(ev); break;
    case 'capture': if (S.online) scope.onCapture(ev); break;
    case 'hello':
      if (!S.online) return;
      if (ev.match) { checkOnlineDrift(); if (S.online.kind === 'serial') setStatus('Board is running this program'); }
      else banner('The board runs a different program than this diagram — Upload first', true);
      break;
    case 'mismatch':
      banner('The board runs a different program than this diagram — Upload first', true);
      break;
    case 'serial': log(ev.text, ''); break;
    case 'device_error': setStatus(`Device: ${ev.text}`); break;
    case 'link': if (ev.state === 'stopped') stopOnline(ev.text); break;
    case 'build': log(ev.line, /error|failed/i.test(ev.line) ? 'err' : ''); break;
    case 'build_done':
      log(ev.text, ev.ok ? 'ok' : 'err');
      setStatus(ev.text);
      if (ev.ok && ev.upload && $('#port-select').value) setTimeout(startMonitor, 1500);
      break;
    default: break;
  }
}

// ===================================================================== bottom panel
function showTab(name) {
  for (const b of document.querySelectorAll('.tabs button[data-tab]')) b.classList.toggle('active', b.dataset.tab === name);
  for (const t of document.querySelectorAll('.tab')) t.classList.toggle('active', t.id === `tab-${name}`);
  $('#btn-clear-output').hidden = name !== 'output';
  $('#btn-copy-code').hidden = name !== 'code';
}
for (const b of document.querySelectorAll('.tabs button[data-tab]')) b.addEventListener('click', () => showTab(b.dataset.tab));

function log(text, cls) {
  const out = $('#output');
  const atBottom = out.parentElement.scrollTop + out.parentElement.clientHeight >= out.parentElement.scrollHeight - 20;
  out.append(h('span', { class: cls || '' }, text + '\n'));
  while (out.childNodes.length > 4000) out.firstChild.remove();
  if (atBottom) out.parentElement.scrollTop = out.parentElement.scrollHeight;
}

function setStatus(text) { $('#status-msg').textContent = text; }

// ===================================================================== projects
async function save() {
  const name = S.project.name.trim();
  if (!name) return setStatus('Give the project a name first');
  try {
    await api('PUT', `/api/projects/${encodeURIComponent(name)}`, S.project);
    S.dirty = false;
    $('#dirty').hidden = true;
    saveDraft();
    setStatus(`Saved "${name}"`);
  } catch (e) { setStatus(`Save failed: ${e.message}`); }
}

const confirmDiscard = () => !S.dirty || confirm('Discard unsaved changes?');

async function openDialog() {
  const dlg = $('#dlg-open');
  const list = $('#open-list');
  list.replaceChildren('Loading…');
  dlg.showModal();
  const { projects, dir } = await api('GET', '/api/projects');
  list.replaceChildren();
  const groups = [['workspace', `My projects (${dir})`], ['example', 'Examples']];
  for (const [src, title] of groups) {
    const items = projects.filter((p) => p.source === src);
    list.append(h('h4', {}, title));
    if (!items.length) list.append(h('div', { class: 'open-item d' }, src === 'workspace' ? 'No saved projects yet.' : 'No examples.'));
    for (const p of items) {
      list.append(h('div', {
        class: 'open-item',
        onclick: async () => {
          if (!confirmDiscard()) return;
          const proj = await api('GET', `/api/projects/${src}/${encodeURIComponent(p.name)}`);
          dlg.close();
          setProject(proj, src === 'example');
          setStatus(src === 'example' ? `Opened example "${p.name}" — Save stores a copy in your workspace` : `Opened "${p.name}"`);
        },
      }, h('span', { class: 'n' }, p.name),
      p.modified ? h('span', { class: 'd' }, new Date(p.modified * 1000).toLocaleString()) : null,
      src === 'workspace' ? h('button', {
        class: 'small', title: 'Delete project',
        onclick: async (e) => {
          e.stopPropagation();
          if (!confirm(`Delete project "${p.name}"? This cannot be undone.`)) return;
          await api('DELETE', `/api/projects/${encodeURIComponent(p.name)}`);
          dlg.close();
          openDialog();
        },
      }, '✕') : null));
    }
  }
}

async function exportZip(kind) {
  $('#dlg-export').close();
  const modelica = kind === 'modelica';
  try {
    if (modelica) setStatus('Building the Modelica package…');
    const r = await fetch(modelica ? '/api/export/modelica' : '/api/export', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ project: S.project }),
    });
    if (!r.ok) {
      let msg = r.statusText;
      try { msg = (await r.json()).detail || msg; } catch { /* not json */ }
      throw new Error(msg);
    }
    const m = /filename="([^"]+)"/.exec(r.headers.get('content-disposition') || '');
    const name = m ? m[1] : `${S.project.name.replace(/[^A-Za-z0-9_\-]/g, '_')}.zip`;
    const a = h('a', { href: URL.createObjectURL(await r.blob()), download: name });
    document.body.append(a);
    a.click();
    a.remove();
    setStatus(modelica ? `Exported Modelica package ${name}: unzip it and open package.mo in OpenModelica` : 'Exported PlatformIO project');
  } catch (e) { setStatus(e.message); showTab('problems'); }
}

// ===================================================================== wiring up the UI
$('#project-name').addEventListener('change', (e) => { S.project.name = e.target.value.trim() || 'Untitled'; e.target.value = S.project.name; markDirty(); scheduleCompile(); });
$('#btn-new').onclick = () => { if (confirmDiscard()) setProject(newProject()); };
$('#btn-open').onclick = openDialog;
$('#btn-save').onclick = save;
$('#btn-undo').onclick = () => editor.undo();
$('#btn-redo').onclick = () => editor.redo();
$('#btn-note').onclick = () => { const c = editor.centerWorld(); editor.addNote(c.x, c.y); };
$('#btn-fit').onclick = () => editor.fit();
$('#btn-sim').onclick = startSim;
$('#btn-stop').onclick = () => stopOnline('Stopped');
$('#btn-build').onclick = () => build(false);
$('#btn-upload').onclick = () => build(true);
$('#btn-monitor').onclick = startMonitor;
$('#btn-export').onclick = () => $('#dlg-export').showModal();
$('#export-pio').onclick = () => exportZip('pio');
$('#export-modelica').onclick = () => exportZip('modelica');
$('#btn-ports').onclick = refreshPorts;
$('#port-select').onchange = (e) => storage('fbd.port', e.target.value);
$('#btn-help').onclick = () => $('#dlg-help').showModal();
$('#btn-reload-blocks').onclick = async () => { await loadLibrary(true); editor.render(); renderProps(); compileNow(); setStatus(`Reloaded ${S.lib.size} block types`); };
$('#palette-search').addEventListener('input', renderPalette);
$('#zoom-in').onclick = () => editor.zoomAt(1.2);
$('#zoom-out').onclick = () => editor.zoomAt(1 / 1.2);
$('#btn-clear-output').onclick = () => $('#output').replaceChildren();
$('#btn-copy-code').onclick = () => navigator.clipboard?.writeText(S.compile?.code || '').then(() => setStatus('Code copied'));

document.addEventListener('keydown', (e) => {
  const mod = e.ctrlKey || e.metaKey;
  if (mod && e.key.toLowerCase() === 's') { e.preventDefault(); save(); return; }
  const tag = e.target.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || e.target.isContentEditable) return;
  if (document.querySelector('dialog[open]')) return;
  const k = e.key.toLowerCase();
  if (k === 'delete' || k === 'backspace') { e.preventDefault(); editor.deleteSelection(); }
  else if (mod && k === 'z' && !e.shiftKey) { e.preventDefault(); editor.undo(); }
  else if (mod && (k === 'y' || (k === 'z' && e.shiftKey))) { e.preventDefault(); editor.redo(); }
  else if (mod && k === 'c') { editor.copy(); }
  else if (mod && k === 'v') { e.preventDefault(); editor.paste(); }
  else if (mod && k === 'd') { e.preventDefault(); editor.duplicate(); }
  else if (mod && k === 'a') { e.preventDefault(); editor.selectAll(); }
  else if (k === 'f' && !mod) { editor.fit(); }
  else if (k === 'escape') { editor.sel.clear(); editor.render(); renderProps(); }
});

window.addEventListener('beforeunload', (e) => { if (S.dirty) { saveDraft(); } });

// ===================================================================== resizable bottom panel
(function bottomResize() {
  const set = (px) => document.body.style.setProperty('--bottom-h', `${Math.round(px)}px`);
  const saved = storage('fbd.bottomH');
  if (saved) set(Math.max(120, Math.min(window.innerHeight * 0.8, saved)));
  const handle = $('#bottom-resize');
  handle.addEventListener('mousedown', (e) => {
    e.preventDefault();
    const startY = e.clientY, startH = $('.bottom').getBoundingClientRect().height;
    document.body.classList.add('resizing');
    const move = (ev) => set(Math.max(120, Math.min(window.innerHeight * 0.8, startH + startY - ev.clientY)));
    const up = () => {
      document.body.classList.remove('resizing');
      window.removeEventListener('mousemove', move);
      window.removeEventListener('mouseup', up);
      storage('fbd.bottomH', $('.bottom').getBoundingClientRect().height);
    };
    window.addEventListener('mousemove', move);
    window.addEventListener('mouseup', up);
  });
  handle.addEventListener('dblclick', () => { set(210); storage('fbd.bottomH', 210); });
})();

// ===================================================================== start
(async function start() {
  try {
    await loadLibrary();
  } catch (e) {
    document.body.textContent = `Could not reach the FBD Studio server: ${e.message}`;
    return;
  }
  const draft = storage('fbd.draft');
  if (draft?.project?.blocks) setProject(draft.project, draft.dirty);
  else setProject(newProject());
  refreshPorts();
  connectWs();
  showTab('problems');
  const tools = await api('GET', '/api/tools').catch(() => ({}));
  if (!tools.platformio) log('PlatformIO not found: Build/Upload need it ("pip install platformio"). Simulation works without it.', 'dim');
  if (!tools.cxx) log('No C++ compiler found: the simulator needs g++ or clang++.', 'err');
})();
