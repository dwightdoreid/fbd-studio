// Type helpers shared by the editor and the UI (mirrors fbdstudio/types.py).

export const TYPE_COLORS = {
  BOOL: 'var(--t-bool)', INT: 'var(--t-int)', DINT: 'var(--t-dint)', REAL: 'var(--t-real)', TIME: 'var(--t-time)',
};

const CATEGORY_COLORS = {
  'Bit logic': '#4a6fa5', 'Bistables': '#5b5fa8', 'Edge detection': '#6d5aa6',
  'Timers': '#b0662a', 'Counters': '#a8523f', 'Generators': '#9a6b1f',
  'Comparison': '#2b7f8a', 'Arithmetic': '#2e7d6b', 'Selection': '#3d7a46', 'Conversion': '#5f6b7a',
  'I/O': '#2f7d4f', 'Control': '#8a3f6b', 'Filters': '#7a4f8f', 'Signal processing': '#2f6f8f',
};

export function categoryColor(def) {
  if (def.color) return def.color;
  return CATEGORY_COLORS[def.category] || '#6b5b8a';
}

export function expandAllowed(allowed, groups) {
  const items = Array.isArray(allowed) ? allowed : [allowed];
  const out = [];
  for (const it of items) for (const t of groups[it] || [it]) if (!out.includes(t)) out.push(t);
  return out;
}

export function compatible(src, dst, widening) {
  return src === dst || widening.some(([a, b]) => a === src && b === dst);
}

/** Expand variadic inputs and substitute type arguments for one block instance. */
export function resolvePorts(inst, def) {
  const typeArgs = {};
  for (const [tp, spec] of Object.entries(def.type_params || {})) {
    typeArgs[tp] = (inst.type_args && inst.type_args[tp]) || spec.default;
  }
  const concrete = (t) => typeArgs[t] || t;
  const inputs = [];
  for (const p of def.inputs) {
    if (p.variadic) {
      const n = clampInt((inst.counts || {})[p.name] ?? p.variadic.default, p.variadic.min, p.variadic.max);
      for (let i = 0; i < n; i++) {
        inputs.push({ name: `${p.name}${i + p.variadic.start}`, base: p.name, type: concrete(p.type), generic: p.type in typeArgs ? p.type : null, def: p });
      }
    } else {
      inputs.push({ name: p.name, base: p.name, type: concrete(p.type), generic: p.type in typeArgs ? p.type : null, def: p });
    }
  }
  const outputs = def.outputs.map((p) => ({ name: p.name, base: p.name, type: concrete(p.type), generic: p.type in typeArgs ? p.type : null, def: p }));
  return { inputs, outputs, typeArgs };
}

function clampInt(v, lo, hi) {
  v = parseInt(v, 10);
  if (Number.isNaN(v)) v = lo;
  return Math.max(lo, Math.min(hi, v));
}

export function formatTime(ms) {
  if (ms === 0) return 'T#0ms';
  const sign = ms < 0 ? '-' : '';
  ms = Math.abs(ms);
  let out = '';
  for (const [u, size] of [['d', 86400000], ['h', 3600000], ['m', 60000], ['s', 1000], ['ms', 1]]) {
    if (ms >= size) { out += Math.floor(ms / size) + u; ms %= size; }
  }
  return `T#${sign}${out}`;
}

/** Text for a value as shown on the diagram. */
export function formatValue(type, v) {
  if (v === null || v === undefined || v === '') return '';
  if (type === 'BOOL') {
    if (typeof v === 'string') return v.toUpperCase() === 'TRUE' || v === '1' ? 'TRUE' : 'FALSE';
    return v ? 'TRUE' : 'FALSE';
  }
  if (type === 'TIME') {
    if (typeof v === 'number') return formatTime(v);
    return String(v);
  }
  if (type === 'REAL' && typeof v === 'number') {
    return Number.isInteger(v) ? v.toFixed(1) : String(Math.round(v * 1000) / 1000);
  }
  return String(v);
}

export function zeroValue(type) {
  return type === 'BOOL' ? 'FALSE' : type === 'TIME' ? 'T#0ms' : '0';
}
