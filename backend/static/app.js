'use strict';
/* reclaim — page logic. Data: /api/model (titles carry per-user play rows so the
   "plays by" lens re-computes locally). Untrusted strings only ever reach the DOM
   through text nodes (h() below) or canvas fillText. */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const HDR = { 'Content-Type': 'application/json', 'X-Reclaim': '1' };
const YEAR = 365.25 * 86400;
const now = () => Date.now() / 1000;

async function get(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
  return r.json();
}
async function post(url, body) {
  const r = await fetch(url, { method: 'POST', headers: HDR, body: JSON.stringify(body || {}) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
  return j;
}

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  if (attrs) for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'style') el.setAttribute('style', v);
    else if (k === 'checked' || k === 'disabled' || k === 'value' || k === 'open') el[k] = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const c of kids.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

/* replaceChildren() stringifies null/false ("null" on screen) — always go through fill() */
function fill(el, ...kids) {
  el.replaceChildren(...kids.flat(Infinity).filter(k => k != null && k !== false));
}

/* ------------------------------------------------------------- formatting */
function fmtB(b) {
  if (!b) return '0';
  if (b >= 1e12) return (b / 1e12).toFixed(b >= 1e13 ? 1 : 2) + ' TB';
  if (b >= 1e9) return (b / 1e9).toFixed(b >= 1e11 ? 0 : 1) + ' GB';
  if (b >= 1e6) return Math.round(b / 1e6) + ' MB';
  return Math.round(b / 1e3) + ' KB';
}
function fmtAgo(ts) {
  if (!ts) return 'never';
  const d = (now() - ts) / 86400;
  if (d < 1) return 'today';
  if (d < 45) return Math.round(d) + 'd ago';
  if (d < 365) return Math.round(d / 30.44) + ' mo ago';
  return (d / 365.25).toFixed(1) + ' yr ago';
}
function fmtDate(ts) {
  return ts ? new Date(ts * 1000).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }) : '—';
}
const fmtN = n => (n || 0).toLocaleString();
const plural = (n, w, p) => `${fmtN(n)} ${n === 1 ? w : (p || w + 's')}`;
const pct = (a, b) => (b ? (100 * a / b).toFixed(1) : '0') + '%';
function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

/* ------------------------------------------------------------------ state */
const BUCKETS = [
  { label: 'Under 1 year', short: '< 1 yr' },
  { label: '1–3 years', short: '1–3 yrs' },
  { label: '3–5 years', short: '3–5 yrs' },
  { label: '5+ years', short: '5+ yrs' },
  { label: 'Never played', short: 'never' },
];
const VBUCKETS = [
  { label: 'no one', test: v => v === 0 },
  { label: '1 person', test: v => v === 1 },
  { label: '2–3', test: v => v >= 2 && v <= 3 },
  { label: '4–9', test: v => v >= 4 && v <= 9 },
  { label: '10+', test: v => v >= 10 },
];
const RES_RANK = { '4K': 6, '1080p': 5, '720p': 4, '576p': 3, '480p': 2, 'SD': 1 };

let M = null;            // model payload
let CFG = { display_paths: [], walk_roots: [] };   // /api/config: which sources/features are on
let T = [];              // titles
const byKey = new Map();
const userById = new Map();
let shortlist = [];
const dropFull = new Set();      // title keys listed whole
const dropPart = new Set();      // title keys with a season/version listed
let dropBytes = 0;
const sel = new Set();
let view = [];                   // titles passing every filter
let STATUS = {};

const DEFAULTS = { lib: 'all', q: '', last: 'any', viewers: 'any', added: 'any', size: '0', req: 'any',
  count: 'any', hideKept: true, users: null, bucket: null, vbucket: null, sort: 'z', dir: -1 };
let F = { ...DEFAULTS };
try { Object.assign(F, JSON.parse(localStorage.getItem('reclaim.filters') || '{}'), { bucket: null, vbucket: null }); } catch {}
function saveF() { try { localStorage.setItem('reclaim.filters', JSON.stringify(F)); } catch {} }

/* ------------------------------------------------------------- lens math */
function applyLens() {
  const us = F.users ? new Set(F.users) : null;
  const fin = F.count === 'fin';
  const t0 = now();
  for (const t of T) {
    let p = 0, v = 0, l = 0, s = 0;
    for (const row of t.u) {
      if (us && !us.has(row[0])) continue;
      const c = fin ? row[2] : row[1];
      if (!c) continue;
      p += c; v++; s += row[5];
      const x = fin ? row[4] : row[3];
      if (x > l) l = x;
    }
    t._p = p; t._v = v; t._l = l; t._h = s / 3600;
    const y = l ? (t0 - l) / YEAR : Infinity;
    t._y = y;
    t._b = !l ? 4 : y < 1 ? 0 : y < 3 ? 1 : y < 5 ? 2 : 3;
    t._vb = VBUCKETS.findIndex(b => b.test(v));
    // did the requester(s) ever play it? (requester-specific, ignores the people lens)
    t._rq = t.q.map(([uid]) => uid);
    t._rqPlayed = t.q.some(([uid]) => t.u.some(r => r[0] === uid && (fin ? r[2] : r[1]) > 0));
  }
}

function passes(t, skip) {
  if (F.lib === 'movie' && !t.m) return false;
  if (F.lib === 'show' && t.m) return false;
  if (F.hideKept && t.kp) return false;
  if (F.q && !t._n.includes(F.q)) return false;
  if (F.last !== 'any') {
    if (F.last === 'never' && t._b !== 4) return false;
    if (F.last === 'recent' && !(t._l && t._y < 1)) return false;
    if (/^\d$/.test(F.last) && t._y < +F.last) return false;
  }
  if (F.viewers !== 'any' && t._v > +F.viewers) return false;
  if (F.added !== 'any' && (now() - (t.a || 0)) / YEAR < +F.added) return false;
  if (+F.size && t.z < +F.size * 1e9) return false;
  if (F.req !== 'any') {
    if (F.req === 'none' ? t.q.length : !t._rq.includes(+F.req)) return false;
  }
  if (skip !== 'bucket' && F.bucket != null && t._b !== F.bucket) return false;
  if (skip !== 'vbucket' && F.vbucket != null && t._vb !== F.vbucket) return false;
  return true;
}

function sortKey(t) {
  switch (F.sort) {
    case 'n': return t._n;
    case 'l': return t._l || 0;
    case 'v': return t._v;
    case 'p': return t._p;
    case 'hr': return t._h;
    case 'a': return t.a || 0;
    case 'rq': return t.q.length ? (userById.get(t.q[0][0])?.n || '').toLowerCase() : '\uffff';
    case 'r': return RES_RANK[t.r] || 0;
    case 'ar': return t.ar ? (t.ar[0] ? 2 : 1) : 0;
    default: return t.z;
  }
}

function recompute() {
  view = T.filter(t => passes(t));
  const k = F.sort, d = F.dir;
  const keyed = view.map(t => [sortKey(t), t]);
  keyed.sort((a, b) => (a[0] < b[0] ? -d : a[0] > b[0] ? d : b[1].z - a[1].z));
  view = keyed.map(x => x[1]);
  saveF();
  renderSummary();
  renderSide();
  drawMap();
  drawScatter();
  renderTable();
  renderCapacity();
}

/* ---------------------------------------------------------------- loading */
function libEmpty() {
  const empty = $('#libEmpty');
  $('#libBody').hidden = !M;
  if (!$('#tab-settings').hidden) return;
  $('#cap').hidden = !M;
  empty.hidden = !!M;
  if (M) return;
  fill(empty, CFG.needs_setup
    ? [h('h3', null, 'Not connected yet'), h('p', { class: 'ink2' }, 'Connect your Plex server to see your library.'),
      h('button', { class: 'btn primary', onclick: () => showTab('settings') }, 'Open Settings')]
    : [h('h3', null, STATUS.last_error ? 'The last build failed' : 'Building your library…'),
      h('p', { class: 'ink2' }, STATUS.last_error || (STATUS.phase ? `Now: ${STATUS.phase}` : 'This takes about a minute the first time.')),
      STATUS.last_error ? h('button', { class: 'btn', onclick: () => showTab('settings') }, 'Check Settings') : null]);
}

async function loadModel() {
  M = await get('/api/model');
  T = M.titles;
  byKey.clear(); userById.clear();
  for (const t of T) { t._n = (t.n || '').toLowerCase(); byKey.set(t.k, t); }
  for (const u of M.users) userById.set(u.id, u);
  for (const k of [...sel]) if (!byKey.has(k)) sel.delete(k);
  $('#server').textContent = M.server.friendlyName || 'Plex';
  $('#byReq').closest('div').parentElement.hidden = !CFG.seerr;
  $('#fReq').closest('label').hidden = !CFG.seerr;
  const notes = $('#notes');
  if (notes) { notes.hidden = !M.notes.length; notes.textContent = M.notes.length ? `Some sources failed on the last refresh: ${M.notes.join(' · ')}` : ''; }
  fillReqSelect();
  applyLens();
  syncFilterUI();
  recompute();
  renderBuilt();
  libEmpty();
}

async function loadShortlist(rows) {
  shortlist = rows || await get('/api/shortlist');
  dropFull.clear(); dropPart.clear(); dropBytes = 0;
  for (const r of shortlist) {
    (r.kind === 'movie' || r.kind === 'show' ? dropFull : dropPart).add(r.title_key);
    dropBytes += r.bytes;
  }
  $('#dropCount').textContent = shortlist.length;
  $('#dropBytes').textContent = shortlist.length ? fmtB(dropBytes) : '';
  $('#dropToggle').classList.toggle('has', shortlist.length > 0);
  $('.drop-btn .dot').hidden = !shortlist.length;
  if ($('#drop').classList.contains('open')) renderDrop();
}

function renderBuilt() {
  const g = M?.generated;
  const s = STATUS;
  let txt = g ? `built ${fmtAgo(g) === 'today' ? new Date(g * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : fmtAgo(g)}` : '';
  if (CFG.needs_setup) txt = 'not connected';
  if (CFG.demo) txt = 'demo data';
  if (s.refreshing) txt = `refreshing… ${s.phase || ''}`;
  else if (s.last_error) txt += ` · last refresh failed: ${s.last_error}`;
  $('#built').textContent = txt;
  $('#refresh').disabled = !!s.refreshing || !!CFG.needs_setup || !!CFG.demo;
}

/* --------------------------------------------------------------- capacity */
function renderCapacity() {
  const a = M.array;
  const where = CFG.capacity === 'unraid' ? 'on the array' : 'on disk';
  if (!a) {
    // no free-space source: show the library itself as the whole bar
    const indexed = M.space.indexed + (M.space.unindexed || 0);
    $('#heroFree').textContent = fmtB(indexed) + ' in Plex';
    $('#heroSub').textContent = 'Add “Free space” in Settings to see how full your disks are.';
    return renderCapacityBar({ total: indexed, used: indexed, free: 0 }, where, false);
  }
  $('#heroFree').textContent = fmtB(a.free) + ' free';
  $('#heroSub').textContent = `of ${fmtB(a.total)} ${where} · ${pct(a.free, a.total)} free · ${fmtB(a.used)} used`;
  if (a.used < M.space.indexed * 0.95) {
    // the capacity source can't be the disk the media lives on: don't draw a bar that lies
    $('#heroSub').textContent += ` — less than the ${fmtB(M.space.indexed)} Plex indexes, so the free-space folders in Settings don't seem to be on your media disks`;
    const indexed = M.space.indexed + (M.space.unindexed || 0);
    return renderCapacityBar({ total: indexed, used: indexed, free: 0 }, where, false);
  }
  renderCapacityBar(a, where, true);
}

function renderCapacityBar(a, where, hasFree) {

  // whole library under the current people lens (not the filter row: it sits above it)
  const byB = [0, 0, 0, 0, 0];
  for (const t of T) byB[t._b] += t.z;
  const loose = M.space.unindexed;
  const indexed = M.space.indexed;
  const other = Math.max(0, a.used - indexed - (loose || 0));
  const segs = [
    ...[4, 3, 2, 1, 0].map(b => ({ label: BUCKETS[b].label, bytes: byB[b], color: `var(--b${b})` })),
    ...(loose != null ? [{ label: 'Not in Plex', bytes: loose, color: 'var(--loose)' }] : []),
    ...(hasFree ? [{ label: `Everything else ${where}`, bytes: other, color: 'var(--other)' }] : []),
  ];
  const bar = $('#capbar');
  fill(bar, ...segs.map(s => h('div', {
    style: `width:${(100 * s.bytes / a.total).toFixed(3)}%;background:${s.color}`,
    title: `${s.label}: ${fmtB(s.bytes)}`,
  })));
  bar.setAttribute('aria-label', segs.map(s => `${s.label} ${fmtB(s.bytes)}`).join(', ') + `, free ${fmtB(a.free)}`);
  const marks = $('#capmarks');
  fill(marks);
  if (dropBytes > 0 && hasFree) {
    const x = 100 * (a.used - dropBytes) / a.total;
    marks.append(h('div', { class: 'capmark', style: `left:${x.toFixed(3)}%` }, h('span', null, `after drop list: ${fmtB(a.free + dropBytes)} free`)));
  }
  fill($('#caplegend'), 
    ...segs.map(s => h('span', { class: 'k' }, h('span', { class: 'sw', style: `background:${s.color}` }), s.label, ' ', h('span', { class: 'v' }, fmtB(s.bytes)))),
    hasFree ? h('span', { class: 'k' }, h('span', { class: 'sw', style: 'background:var(--track);outline:1px solid var(--ring)' }), 'Free ', h('span', { class: 'v' }, fmtB(a.free))) : null,
  );

  const never = T.filter(t => t._b === 4);
  const stale3 = T.filter(t => t._b >= 2);
  const lensNote = lensLabel() === 'Everyone' ? '' : ` (${lensLabel()})`;
  const tiles = [
    { l: 'Never played' + lensNote, v: fmtB(byB[4]), s: `${fmtN(never.length)} titles · ${pct(byB[4], indexed)} of Plex` },
    { l: 'Not played in 3+ years', v: fmtB(byB[2] + byB[3] + byB[4]), s: `${fmtN(stale3.length)} titles incl. never` },
  ];
  if (loose != null) tiles.push({ l: 'On disk, not in Plex', v: fmtB(loose), s: `walked ${fmtAgo(M.walk_at)}` });
  tiles.push({ l: 'Drop list', v: dropBytes ? fmtB(dropBytes) : '—', crit: dropBytes > 0,
    s: !dropBytes ? 'nothing listed yet' : hasFree ? `→ ${fmtB(a.free + dropBytes)} free (${pct(a.free + dropBytes, a.total)})` : plural(shortlist.length, 'item') });
  fill($('#tiles'), ...tiles.map(t => h('div', { class: 'tile' },
    h('div', { class: 'l' }, t.l), h('div', { class: 'v' + (t.crit ? ' crit' : '') }, t.v), h('div', { class: 's' }, t.s))));
}

/* --------------------------------------------------------- side bar lists */
function barRow({ label, bytes, count, max, color, on, onclick, stack, title }) {
  const track = stack
    ? h('div', { class: 'stack', style: `width:${Math.max(0.5, 100 * bytes / max)}%` },
      ...stack.filter(s => s.bytes > 0).map(s => h('div', { style: `flex:${s.bytes} 0 0;background:${s.color}`, title: `${s.label}: ${fmtB(s.bytes)}` })))
    : h('div', { class: 'fill', style: `width:${Math.max(0.5, 100 * bytes / max)}%;background:${color}` });
  return h('button', { class: 'bar-row' + (on ? ' on' : ''), onclick, title },
    h('span', { class: 'lab' }, label),
    h('span', { class: 'track' }, track),
    h('span', { class: 'val' }, fmtB(bytes), h('small', null, count != null ? plural(count, 'title') : '')));
}

function renderSide() {
  // crossfilter: each list ignores its own bucket filter so its other bars stay visible
  const lastSet = T.filter(t => passes(t, 'bucket'));
  const b = BUCKETS.map(() => ({ bytes: 0, count: 0 }));
  for (const t of lastSet) { b[t._b].bytes += t.z; b[t._b].count++; }
  const maxB = Math.max(1, ...b.map(x => x.bytes));
  fill($('#byLast'), ...[4, 3, 2, 1, 0].map(i => barRow({
    label: BUCKETS[i].label, bytes: b[i].bytes, count: b[i].count, max: maxB, color: `var(--b${i})`,
    on: F.bucket === i, onclick: () => { F.bucket = F.bucket === i ? null : i; recompute(); },
  })));

  const vSet = T.filter(t => passes(t, 'vbucket'));
  const v = VBUCKETS.map(() => ({ bytes: 0, count: 0 }));
  for (const t of vSet) { v[t._vb].bytes += t.z; v[t._vb].count++; }
  const maxV = Math.max(1, ...v.map(x => x.bytes));
  fill($('#byViewers'), ...VBUCKETS.map((vb, i) => barRow({
    label: vb.label, bytes: v[i].bytes, count: v[i].count, max: maxV,
    // emphasis: "no one" is the same set as never played under this lens
    color: i === 0 ? 'var(--b4)' : 'var(--gray)',
    on: F.vbucket === i, onclick: () => { F.vbucket = F.vbucket === i ? null : i; recompute(); },
  })));

  const req = new Map();
  for (const t of view) {
    for (const uid of new Set(t._rq)) {
      const r = req.get(uid) || { bytes: 0, count: 0, unplayed: 0 };
      r.bytes += t.z; r.count++;
      if (!t.u.some(x => x[0] === uid && (F.count === 'fin' ? x[2] : x[1]) > 0)) r.unplayed += t.z;
      req.set(uid, r);
    }
  }
  const top = [...req.entries()].sort((a, b) => b[1].bytes - a[1].bytes).slice(0, 8);
  const maxR = Math.max(1, ...top.map(x => x[1].bytes));
  fill($('#byReq'), ...(top.length ? top.map(([uid, r]) => barRow({
    label: userById.get(uid)?.n || `user ${uid}`, bytes: r.bytes, count: r.count, max: maxR,
    stack: [{ label: 'requester never played it', bytes: r.unplayed, color: 'var(--b4)' },
            { label: 'requester played it', bytes: r.bytes - r.unplayed, color: 'var(--gray)' }],
    on: F.req === String(uid), title: `${fmtB(r.unplayed)} of it never played by the person who asked`,
    onclick: () => { F.req = F.req === String(uid) ? 'any' : String(uid); syncFilterUI(); recompute(); },
  })) : [h('div', { class: 'muted' }, 'No requested titles in this view.')]));
  fill($('#reqLegend'), 
    h('span', { class: 'k' }, h('span', { class: 'sw', style: 'background:var(--b4)' }), 'requester never played it'),
    h('span', { class: 'k' }, h('span', { class: 'sw', style: 'background:var(--gray)' }), 'requester played it'));
}

/* ---------------------------------------------------------------- summary */
function renderSummary() {
  const bytes = view.reduce((s, t) => s + t.z, 0);
  const keptHidden = F.hideKept ? T.filter(t => t.kp).length : 0;
  const chips = [];
  if (F.bucket != null) chips.push([`last played: ${BUCKETS[F.bucket].label}`, () => { F.bucket = null; recompute(); }]);
  if (F.vbucket != null) chips.push([`viewers: ${VBUCKETS[F.vbucket].label}`, () => { F.vbucket = null; recompute(); }]);
  fill($('#summary'), 
    h('span', null, h('b', null, fmtN(view.length)), ' titles · ', h('b', null, fmtB(bytes)),
      ` of ${fmtB(M.space.indexed)} in Plex`, keptHidden ? ` · ${keptHidden} kept hidden` : ''),
    ...chips.map(([txt, fn]) => h('button', { class: 'btn sm', onclick: fn, title: 'Clear this filter' }, txt, ' ✕')),
    view.length ? h('button', { class: 'btn sm ghost', onclick: () => { view.forEach(t => sel.add(t.k)); afterSel(); } }, `Select all ${fmtN(view.length)}`) : null,
  );
  $('#tableNote').textContent = `${fmtN(view.length)} titles, sorted by ${$(`#thead button[data-k="${F.sort}"]`)?.textContent || 'size'}`;
}

/* ---------------------------------------------------------------- treemap */
const MAP = { leaves: [], groups: [], hover: null };
function setupCanvas(cv) {
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth, hgt = cv.clientHeight;
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(hgt * dpr)) {
    cv.width = Math.round(w * dpr); cv.height = Math.round(hgt * dpr);
  }
  const ctx = cv.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w, h: hgt };
}
function inkOn(hex) {
  const c = d3.color(hex); if (!c) return '#fff';
  const { r, g, b } = c.rgb();
  const lin = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b) > 0.35 ? '#0b0b0b' : '#ffffff';
}
function ellipsize(ctx, s, w) {
  if (ctx.measureText(s).width <= w) return s;
  let lo = 0, hi = s.length;
  while (lo < hi) { const m = (lo + hi + 1) >> 1; if (ctx.measureText(s.slice(0, m) + '…').width <= w) lo = m; else hi = m - 1; }
  return lo > 1 ? s.slice(0, lo) + '…' : '';
}

function drawMap() {
  const cv = $('#treemap');
  const { ctx, w, h: H } = setupCanvas(cv);
  const colors = [0, 1, 2, 3, 4].map(i => css(`--b${i}`));
  const surface = css('--surface'), ink = css('--ink'), ink2 = css('--ink2'), crit = css('--crit');
  ctx.fillStyle = surface; ctx.fillRect(0, 0, w, H);
  const groups = [];
  const mv = view.filter(t => t.m && t.z > 0), sh = view.filter(t => !t.m && t.z > 0);
  if (mv.length) groups.push({ name: 'Movies', children: mv });
  if (sh.length) groups.push({ name: 'TV', children: sh });
  MAP.leaves = []; MAP.groups = [];
  fill($('#mapLegend'), ...[4, 3, 2, 1, 0].map(i => h('span', { class: 'k' }, h('span', { class: 'sw', style: `background:var(--b${i})` }), BUCKETS[i].short)),
    h('span', { class: 'k' }, h('span', { class: 'sw', style: `background:transparent;outline:2px solid var(--crit);outline-offset:-2px` }), 'drop list'));
  if (!groups.length) {
    ctx.fillStyle = ink2; ctx.font = '13px system-ui'; ctx.fillText('Nothing matches these filters.', 12, 24);
    return;
  }
  const root = d3.hierarchy({ children: groups }).sum(d => (d.children ? 0 : d.z)).sort((a, b) => b.value - a.value);
  d3.treemap().size([w, H]).tile(d3.treemapSquarify.ratio(1.2))
    // paddingOuter is a shorthand that also sets paddingTop, so it must come first
    .paddingOuter(n => (n.depth === 1 ? 1 : 0)).paddingTop(n => (n.depth === 1 ? 20 : 0))
    .paddingInner(n => (n.depth === 0 ? 6 : 1)).round(false)(root);
  ctx.textBaseline = 'top';
  for (const g of root.children) {
    MAP.groups.push(g);
    ctx.fillStyle = ink; ctx.font = '600 12px system-ui';
    ctx.fillText(`${g.data.name} · ${fmtB(g.value)} · ${plural(g.children.length, 'title')}`, g.x0 + 2, g.y0 + 4);
  }
  ctx.font = '11px system-ui';
  for (const leaf of root.leaves()) {
    const t = leaf.data;
    const x = leaf.x0, y = leaf.y0, lw = leaf.x1 - leaf.x0, lh = leaf.y1 - leaf.y0;
    MAP.leaves.push(leaf);
    const c = colors[t._b];
    ctx.fillStyle = c; ctx.fillRect(x, y, lw, lh);
    const listed = dropFull.has(t.k), part = dropPart.has(t.k);
    if ((listed || part) && lw > 3 && lh > 3) {
      ctx.strokeStyle = crit; ctx.lineWidth = listed ? 2 : 1;
      ctx.strokeRect(x + 1, y + 1, lw - 2, lh - 2);
    }
    if (sel.has(t.k) && lw > 3 && lh > 3) {
      ctx.strokeStyle = ink; ctx.lineWidth = 2; ctx.strokeRect(x + 1, y + 1, lw - 2, lh - 2);
    }
    if (lw > 54 && lh > 16) {
      ctx.fillStyle = inkOn(c);
      const name = ellipsize(ctx, t.n, lw - 8);
      if (name) ctx.fillText(name, x + 4, y + 3);
      if (lh > 30) {
        ctx.globalAlpha = 0.8;
        ctx.fillText(ellipsize(ctx, `${fmtB(t.z)} · ${t._l ? fmtAgo(t._l) : 'never'}`, lw - 8), x + 4, y + 17);
        ctx.globalAlpha = 1;
      }
    }
  }
  if (MAP.hover) {
    const l = MAP.hover;
    ctx.strokeStyle = ink; ctx.lineWidth = 2;
    ctx.strokeRect(l.x0 + 1, l.y0 + 1, Math.max(1, l.x1 - l.x0 - 2), Math.max(1, l.y1 - l.y0 - 2));
  }
}
function mapLeafAt(ev) {
  const r = $('#treemap').getBoundingClientRect();
  const x = ev.clientX - r.left, y = ev.clientY - r.top;
  for (const l of MAP.leaves) if (x >= l.x0 && x < l.x1 && y >= l.y0 && y < l.y1) return l;
  return null;
}

/* ---------------------------------------------------------------- tooltip */
function showTip(ev, t) {
  const tip = $('#tip');
  const req = t.q.length ? [...new Set(t.q.map(q => userById.get(q[0])?.n || '?'))].join(', ') : '—';
  const rows = [
    ['Last played', t._l ? `${fmtAgo(t._l)} (${fmtDate(t._l)})` : 'never'],
    ['Viewers', `${t._v}${lensLabel() === 'Everyone' ? '' : ' in lens'}`],
    ['Plays', fmtN(t._p)],
    ['Added', fmtDate(t.a)],
    ['Requested by', req],
  ];
  if (!t.m) rows.splice(3, 0, ['Episodes seen', `${fmtN(t.es)} of ${fmtN(t.e)}`]);
  if (t.x) rows.push(['Not in Plex, same folder', fmtB(t.x)]);
  fill(tip, 
    h('div', { class: 'tv' }, fmtB(t.z)),
    h('div', { class: 'tn' }, `${t.n}${t.y ? ` (${t.y})` : ''}${t.m ? '' : ' · TV'}`),
    ...rows.map(([k, v]) => h('div', { class: 'tr' }, h('span', null, k), h('b', null, v))),
    dropFull.has(t.k) ? h('div', { class: 'tr' }, h('span', null, 'On the drop list'), h('b', null, '✕')) : null,
  );
  tip.hidden = false;
  const pad = 14, tw = tip.offsetWidth, th = tip.offsetHeight;
  let x = ev.clientX + pad, y = ev.clientY + pad;
  if (x + tw > innerWidth - 8) x = ev.clientX - tw - pad;
  if (y + th > innerHeight - 8) y = ev.clientY - th - pad;
  tip.style.left = x + 'px'; tip.style.top = y + 'px';
}
function hideTip() { $('#tip').hidden = true; }

function wireMap() {
  const cv = $('#treemap');
  cv.addEventListener('pointermove', ev => {
    const l = mapLeafAt(ev);
    if (l !== MAP.hover) { MAP.hover = l; drawMap(); }
    if (l) showTip(ev, l.data); else hideTip();
  });
  cv.addEventListener('pointerleave', () => { MAP.hover = null; hideTip(); drawMap(); });
  cv.addEventListener('click', ev => {
    const l = mapLeafAt(ev);
    if (!l) return;
    if (ev.shiftKey || ev.ctrlKey || ev.metaKey) { toggleSel(l.data.k); return; }
    openDetail(l.data.k);
  });
}

/* ---------------------------------------------------------------- scatter */
const SC = {};
function hash(s) { let x = 2166136261; for (let i = 0; i < s.length; i++) { x ^= s.charCodeAt(i); x = Math.imul(x, 16777619); } return (x >>> 0) / 4294967295; }

function drawScatter() {
  const all = view.filter(t => t.z > 0);
  const maxY = Math.max(1, Math.ceil(d3.max(all, t => (t._l ? t._y : 0)) || 1));
  const zMin = Math.max(5e7, d3.min(all, t => t.z) || 5e7), zMax = Math.max(zMin * 10, d3.max(all, t => t.z) || 1e9);
  const panels = $('.scatter-panels');
  panels.children[0].hidden = F.lib === 'show';
  panels.children[1].hidden = F.lib === 'movie';
  panels.style.gridTemplateColumns = F.lib === 'all' ? '' : '1fr';
  for (const [id, isMovie] of [['scMovie', 1], ['scShow', 0]]) {
    if ((isMovie && F.lib === 'show') || (!isMovie && F.lib === 'movie')) continue;
    const pts = all.filter(t => !!t.m === !!isMovie);
    $(id === 'scMovie' ? '#scMovieT' : '#scShowT').textContent =
      `${isMovie ? 'Movies' : 'TV shows'} · ${fmtN(pts.length)} · ${fmtB(pts.reduce((s, t) => s + t.z, 0))}`;
    drawPanel(id, pts, maxY, zMin, zMax);
  }
}

function drawPanel(id, pts, maxY, zMin, zMax) {
  const cv = $('#' + id);
  const { ctx, w, h: H } = setupCanvas(cv);
  const m = { l: 52, r: 10, t: 8, b: 26 };
  const neverW = Math.min(90, Math.max(48, (w - m.l - m.r) * 0.14));
  const plotR = w - m.r - neverW - 12;
  const x = d3.scaleLinear().domain([0, maxY]).range([m.l, plotR]);
  const y = d3.scaleLog().domain([zMin, zMax]).range([H - m.b, m.t]).clamp(true);
  const st = SC[id] || (SC[id] = { brush: null, hover: null });
  st.x = x; st.y = y; st.m = m; st.neverX0 = plotR + 12; st.neverW = neverW; st.H = H; st.w = w;

  const surface = css('--surface'), grid = css('--grid'), axis = css('--axis'), muted = css('--muted'),
    ink = css('--ink'), crit = css('--crit');
  const colors = [0, 1, 2, 3, 4].map(i => css(`--b${i}`));
  ctx.fillStyle = surface; ctx.fillRect(0, 0, w, H);
  ctx.font = '11px system-ui'; ctx.fillStyle = muted; ctx.strokeStyle = grid; ctx.lineWidth = 1;
  // y grid at decades
  ctx.textBaseline = 'middle'; ctx.textAlign = 'right';
  for (const v of [1e8, 1e9, 1e10, 1e11, 1e12, 1e13]) {
    if (v < zMin * 0.999 || v > zMax * 1.001) continue;
    const yy = Math.round(y(v)) + 0.5;
    ctx.beginPath(); ctx.moveTo(m.l, yy); ctx.lineTo(w - m.r, yy); ctx.stroke();
    ctx.fillText(fmtB(v).replace('.00', ''), m.l - 6, yy);
  }
  // x ticks (years)
  ctx.textAlign = 'center'; ctx.textBaseline = 'top';
  const step = maxY > 12 ? 2 : 1;
  for (let yr = 0; yr <= maxY; yr += step) {
    const xx = Math.round(x(yr)) + 0.5;
    ctx.strokeStyle = grid; ctx.beginPath(); ctx.moveTo(xx, m.t); ctx.lineTo(xx, H - m.b); ctx.stroke();
    ctx.fillText(yr === 0 ? 'now' : `${yr}y`, xx, H - m.b + 6);
  }
  ctx.fillText('never', st.neverX0 + neverW / 2, H - m.b + 6);
  ctx.strokeStyle = axis;
  ctx.beginPath(); ctx.moveTo(m.l, H - m.b + 0.5); ctx.lineTo(plotR, H - m.b + 0.5); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(st.neverX0, H - m.b + 0.5); ctx.lineTo(st.neverX0 + neverW, H - m.b + 0.5); ctx.stroke();

  st.pts = pts.map(t => {
    const px = t._l ? x(t._y) : st.neverX0 + 4 + hash(t.k) * (neverW - 8);
    return [px, y(Math.max(t.z, zMin)), t];
  });
  st.qt = d3.quadtree().x(p => p[0]).y(p => p[1]).addAll(st.pts);
  const r = 3.5;
  for (const p of st.pts) {
    ctx.beginPath(); ctx.arc(p[0], p[1], r + 1, 0, 2 * Math.PI); ctx.fillStyle = surface; ctx.fill();
    ctx.beginPath(); ctx.arc(p[0], p[1], r, 0, 2 * Math.PI); ctx.fillStyle = colors[p[2]._b]; ctx.fill();
  }
  // listed + selected on top
  for (const p of st.pts) {
    const t = p[2];
    if (dropFull.has(t.k) || dropPart.has(t.k)) {
      ctx.beginPath(); ctx.arc(p[0], p[1], r + 2, 0, 2 * Math.PI); ctx.strokeStyle = crit; ctx.lineWidth = 2; ctx.stroke();
    }
    if (sel.has(t.k)) {
      ctx.beginPath(); ctx.arc(p[0], p[1], r + 2, 0, 2 * Math.PI); ctx.strokeStyle = ink; ctx.lineWidth = 2; ctx.stroke();
    }
  }
  if (st.hover) {
    const p = st.hover;
    ctx.beginPath(); ctx.arc(p[0], p[1], r + 3, 0, 2 * Math.PI); ctx.strokeStyle = ink; ctx.lineWidth = 2; ctx.stroke();
  }
  if (st.brush) {
    const b = st.brush;
    ctx.fillStyle = css('--wash'); ctx.strokeStyle = ink; ctx.lineWidth = 1;
    const bx = Math.min(b.x0, b.x1), by = Math.min(b.y0, b.y1), bw = Math.abs(b.x1 - b.x0), bh = Math.abs(b.y1 - b.y0);
    ctx.fillRect(bx, by, bw, bh); ctx.strokeRect(bx + 0.5, by + 0.5, bw, bh);
  }
}

function wireScatter(id) {
  const cv = $('#' + id);
  const pos = ev => { const r = cv.getBoundingClientRect(); return [ev.clientX - r.left, ev.clientY - r.top]; };
  let down = null;
  cv.addEventListener('pointerdown', ev => {
    const [x, y] = pos(ev); down = { x, y, shift: ev.shiftKey || ev.ctrlKey || ev.metaKey };
    cv.setPointerCapture(ev.pointerId);
  });
  cv.addEventListener('pointermove', ev => {
    const st = SC[id]; if (!st?.qt) return;
    const [x, y] = pos(ev);
    if (down && (Math.abs(x - down.x) > 4 || Math.abs(y - down.y) > 4)) {
      st.brush = { x0: down.x, y0: down.y, x1: x, y1: y }; st.hover = null; hideTip();
      drawPanel(id, st.pts.map(p => p[2]), st.x.domain()[1], st.y.domain()[0], st.y.domain()[1]);
      return;
    }
    const p = st.qt.find(x, y, 14);
    if (p !== st.hover) { st.hover = p; drawPanel(id, st.pts.map(q => q[2]), st.x.domain()[1], st.y.domain()[0], st.y.domain()[1]); }
    if (p) showTip(ev, p[2]); else hideTip();
  });
  cv.addEventListener('pointerup', ev => {
    const st = SC[id]; const was = down; down = null;
    if (!st) return;
    if (st.brush) {
      const b = st.brush; st.brush = null;
      const x0 = Math.min(b.x0, b.x1), x1 = Math.max(b.x0, b.x1), y0 = Math.min(b.y0, b.y1), y1 = Math.max(b.y0, b.y1);
      if (!was?.shift) sel.clear();
      for (const p of st.pts) if (p[0] >= x0 && p[0] <= x1 && p[1] >= y0 && p[1] <= y1) sel.add(p[2].k);
      afterSel();
      return;
    }
    const [x, y] = pos(ev);
    const p = st.qt?.find(x, y, 14);
    if (p) { if (was?.shift) toggleSel(p[2].k); else openDetail(p[2].k); }
  });
  cv.addEventListener('pointerleave', () => { const st = SC[id]; if (st && st.hover) { st.hover = null; drawScatter(); } hideTip(); });
}

/* ------------------------------------------------------------------ table */
const COLS = [
  { k: null, label: '' }, { k: null, label: '' },
  { k: 'n', label: 'Title' }, { k: 'z', label: 'Size', r: 1 }, { k: 'l', label: 'Last played' },
  { k: 'v', label: 'Viewers', r: 1 }, { k: 'p', label: 'Plays', r: 1 }, { k: 'hr', label: 'Hours', r: 1 },
  { k: 'a', label: 'Added' }, { k: 'rq', label: 'Requested by' }, { k: 'r', label: 'Res' }, { k: 'ar', label: 'Arr' },
];
const ROW_H = 34;
function renderHead() {
  fill($('#thead'), ...COLS.map((c, i) => {
    if (i === 0) return h('div', null, h('input', { type: 'checkbox', 'aria-label': 'Select all in view',
      checked: view.length > 0 && view.every(t => sel.has(t.k)),
      onchange: e => { if (e.target.checked) view.forEach(t => sel.add(t.k)); else view.forEach(t => sel.delete(t.k)); afterSel(); } }));
    if (!c.k) return h('div', { title: 'Keep' }, '★');
    const on = F.sort === c.k;
    return h('button', { class: (c.r ? 'r ' : '') + (on ? 'on' : ''), 'data-k': c.k,
      onclick: () => { if (F.sort === c.k) F.dir = -F.dir; else { F.sort = c.k; F.dir = c.k === 'n' || c.k === 'rq' ? 1 : -1; } recompute(); } },
      c.label + (on ? (F.dir < 0 ? ' ↓' : ' ↑') : ''));
  }));
}
function renderTable() {
  renderHead();
  $('#tspacer').style.height = view.length * ROW_H + 'px';
  paintRows();
}
function paintRows() {
  const body = $('#tbody');
  const top = body.scrollTop, hgt = body.clientHeight;
  const i0 = Math.max(0, Math.floor(top / ROW_H) - 6), i1 = Math.min(view.length, Math.ceil((top + hgt) / ROW_H) + 6);
  for (const el of $$('.trow', body)) el.remove();
  const frag = document.createDocumentFragment();
  for (let i = i0; i < i1; i++) frag.append(rowEl(view[i], i));
  body.append(frag);
}
function rowEl(t, i) {
  const req = t.q.length ? [...new Set(t.q.map(q => userById.get(q[0])?.n || '?'))].join(', ') : '';
  const meta = [t.m ? (t.v > 1 ? `${t.v} versions` : '') : `${fmtN(t.e)} eps · ${t.e ? Math.round(100 * t.es / t.e) : 0}% seen`,
    DG.active.has(t.k) ? `↓${DG.active.get(t.k).target}p downloading` : ''].filter(Boolean).join(' · ');
  return h('div', { class: `trow${sel.has(t.k) ? ' sel' : ''}${dropFull.has(t.k) ? ' drop' : ''}`, style: `top:${i * ROW_H}px`,
    onclick: e => { if (e.target.closest('input,button')) return; if (e.shiftKey || e.ctrlKey || e.metaKey) toggleSel(t.k); else openDetail(t.k); } },
    h('div', null, h('input', { type: 'checkbox', checked: sel.has(t.k), 'aria-label': `Select ${t.n}`, onchange: () => toggleSel(t.k) })),
    h('div', null, h('button', { class: 'star' + (t.kp ? ' on' : ''), title: t.kp ? 'Kept — click to un-keep' : 'Keep (never suggest)',
      onclick: () => setKeep([t.k], !t.kp) }, t.kp ? '★' : '☆')),
    h('div', { class: 't-name', title: t.n }, t.m ? null : h('span', { class: 'kind' }, 'TV'), t.n,
      h('span', { class: 'meta' }, [t.y, meta].filter(Boolean).join(' · '))),
    h('div', { class: 'n' }, fmtB(t.z), t.x ? h('span', { class: 'loose', title: 'Not in Plex, same folder' }, ` +${fmtB(t.x)}`) : null),
    h('div', null, h('span', { class: 'lp' }, h('i', { style: `background:var(--b${t._b})` }), t._l ? fmtAgo(t._l) : 'never')),
    h('div', { class: 'n' }, fmtN(t._v)), h('div', { class: 'n' }, fmtN(t._p)), h('div', { class: 'n' }, t._h ? t._h.toFixed(t._h < 10 ? 1 : 0) : '0'),
    h('div', { class: 'tnum' }, t.a ? new Date(t.a * 1000).toLocaleDateString(undefined, { year: 'numeric', month: 'short' }) : '—'),
    h('div', { title: req }, req || h('span', { class: 'muted' }, '—')),
    h('div', null, t.r || '—'),
    h('div', null, t.ar ? h('span', { class: 'mon' + (t.ar[0] ? ' on' : ''), title: `${t.m ? 'Radarr' : 'Sonarr'}: ${t.ar[0] ? 'monitored' : 'unmonitored'} · ${t.ar[1] || ''}` }, t.ar[0] ? '● mon' : '○ off') : h('span', { class: 'muted', title: 'Not found in Radarr/Sonarr' }, '—')),
  );
}

/* -------------------------------------------------------------- selection */
function toggleSel(k) { if (sel.has(k)) sel.delete(k); else sel.add(k); afterSel(); }
function afterSel() {
  const bar = $('#selbar');
  if (!sel.size) bar.hidden = true;
  else {
    let b = 0; for (const k of sel) b += byKey.get(k)?.z || 0;
    $('#selText').textContent = `${fmtN(sel.size)} selected · ${fmtB(b)}`;
    bar.hidden = false;
  }
  drawMap(); drawScatter(); renderTable();
}

async function addToDrop(items) {
  try {
    const r = await post('/api/shortlist', { items });
    await loadShortlist(r.shortlist);
    const kept = r.skipped.filter(s => s.why === 'kept').length;
    if (kept) toast(`${kept} kept title${kept > 1 ? 's' : ''} skipped`);
    redrawMarks();
  } catch (e) { toast('Could not add: ' + e.message); }
}
function redrawMarks() { drawMap(); drawScatter(); renderTable(); renderCapacity(); }

async function setKeep(keys, on) {
  let res;
  for (const k of keys) { res = await post('/api/keep', { key: k, on }); const t = byKey.get(k); if (t) t.kp = on ? 1 : 0; }
  if (res) await loadShortlist(res.shortlist);
  if (on) for (const k of keys) sel.delete(k);
  recompute(); afterSel();
  if ($('#detail').classList.contains('open') && keys.includes(DETAIL.key)) openDetail(DETAIL.key);
}

function toast(msg) {
  const el = h('div', { class: 'tip', style: 'left:50%;top:70px;transform:translateX(-50%);pointer-events:auto' }, msg);
  document.body.append(el);
  setTimeout(() => el.remove(), 3500);
}

/* ------------------------------------------------------------------ lens */
function lensLabel() {
  if (!F.users) return 'Everyone';
  const admin = M.users.find(u => u.ad);
  const all = M.users.filter(u => u.pl > 0);
  if (F.users.length === 1) return userById.get(F.users[0])?.n || '1 person';
  if (admin && F.users.length === all.length - 1 && !F.users.includes(admin.id)) return `Everyone but ${admin.n}`;
  return `${F.users.length} people`;
}
function openLens() {
  const pop = $('#lensPop');
  const btn = $('#lensBtn').getBoundingClientRect();
  const admin = M.users.find(u => u.ad);
  const people = M.users.filter(u => u.pl > 0);
  const chosen = new Set(F.users || people.map(u => u.id));
  const apply = () => {
    F.users = chosen.size === people.length ? null : [...chosen];
    $('#lensBtn').textContent = lensLabel();
    applyLens(); recompute();
  };
  const list = h('div', { class: 'ulist' }, ...people.map(u => h('label', null,
    h('input', { type: 'checkbox', checked: chosen.has(u.id), onchange: e => { e.target.checked ? chosen.add(u.id) : chosen.delete(u.id); apply(); } }),
    h('span', null, u.n, u.ad ? ' (admin)' : ''), h('small', null, `${fmtN(u.pl)} plays`))));
  const preset = (label, ids) => h('button', { class: 'btn sm', onclick: () => {
    chosen.clear(); ids.forEach(i => chosen.add(i)); apply();
    $$('input', list).forEach((c, i) => { c.checked = chosen.has(people[i].id); });
  } }, label);
  fill(pop, 
    h('div', { class: 'presets' }, preset('Everyone', people.map(u => u.id)),
      admin ? preset(`Only ${admin.n}`, [admin.id]) : null,
      admin ? preset(`All but ${admin.n}`, people.filter(u => u.id !== admin.id).map(u => u.id)) : null),
    h('div', { class: 'muted', style: 'font-size:12px;margin-bottom:6px' }, 'Count plays from these people. Viewers, plays and last played all follow this.'),
    list);
  pop.style.left = Math.min(btn.left, innerWidth - 316) + scrollX + 'px';
  pop.style.top = btn.bottom + 6 + scrollY + 'px';
  pop.hidden = false;
}

/* ---------------------------------------------------------------- filters */
function fillReqSelect() {
  const s = $('#fReq');
  const by = new Map();
  for (const t of T) for (const uid of new Set(t.q.map(q => q[0]))) by.set(uid, (by.get(uid) || 0) + t.z);
  fill(s, h('option', { value: 'any' }, 'anyone or no one'), h('option', { value: 'none' }, 'not requested'),
    ...[...by.entries()].sort((a, b) => b[1] - a[1]).map(([uid, b]) => h('option', { value: String(uid) }, `${userById.get(uid)?.n || uid} · ${fmtB(b)}`)));
  s.value = F.req;
  if (s.value !== F.req) F.req = 'any';
}
function syncFilterUI() {
  $$('#fLib button').forEach(b => b.classList.toggle('on', b.dataset.v === F.lib));
  $$('#fCount button').forEach(b => b.classList.toggle('on', b.dataset.v === F.count));
  $('#fSearch').value = F.q; $('#fLast').value = F.last; $('#fViewers').value = F.viewers;
  $('#fAdded').value = F.added; $('#fSize').value = F.size; $('#fReq').value = F.req;
  $('#fHideKept').checked = F.hideKept;
  if (M) $('#lensBtn').textContent = lensLabel();
}
function wireFilters() {
  $$('#fLib button').forEach(b => b.onclick = () => { F.lib = b.dataset.v; syncFilterUI(); recompute(); });
  $$('#fCount button').forEach(b => b.onclick = () => { F.count = b.dataset.v; syncFilterUI(); applyLens(); recompute(); });
  let qt; $('#fSearch').oninput = e => { clearTimeout(qt); qt = setTimeout(() => { F.q = e.target.value.trim().toLowerCase(); recompute(); }, 120); };
  $('#fLast').onchange = e => { F.last = e.target.value; F.bucket = null; recompute(); };
  $('#fViewers').onchange = e => { F.viewers = e.target.value; F.vbucket = null; recompute(); };
  $('#fAdded').onchange = e => { F.added = e.target.value; recompute(); };
  $('#fSize').onchange = e => { F.size = e.target.value; recompute(); };
  $('#fReq').onchange = e => { F.req = e.target.value; recompute(); };
  $('#fHideKept').onchange = e => { F.hideKept = e.target.checked; recompute(); };
  $('#fReset').onclick = () => { F = { ...DEFAULTS, sort: F.sort, dir: F.dir }; syncFilterUI(); applyLens(); recompute(); };
  $('#lensBtn').onclick = e => { e.stopPropagation(); const p = $('#lensPop'); if (p.hidden) openLens(); else p.hidden = true; };
  document.addEventListener('click', e => { const p = $('#lensPop'); if (!p.hidden && !p.contains(e.target) && e.target !== $('#lensBtn')) p.hidden = true; });
}

/* ----------------------------------------------------------------- detail */
const DETAIL = { key: null };
async function openDetail(key) {
  DETAIL.key = key;
  const dr = $('#detail');
  $('#drop').classList.remove('open');
  dr.classList.add('open');
  fill(dr, h('div', { class: 'muted' }, 'Loading…'));
  let d;
  try { d = await get(`/api/title/${encodeURIComponent(key)}`); }
  catch (e) { fill(dr, closeBtn(dr), h('div', { class: 'callout' }, 'Not in the library any more.')); return; }
  if (DETAIL.key !== key) return;
  const t = byKey.get(key);
  const listed = dropFull.has(key);
  const listedRows = shortlist.filter(r => r.title_key === key);
  const arrName = d.kind === 'movie' ? 'Radarr' : 'Sonarr';
  const facts = [
    d.year, d.kind === 'movie' ? 'Movie' : `TV · ${d.seasons.length} seasons · ${fmtN(d.eps)} episodes`,
    d.res, d.content, d.rating ? `★ ${d.rating}` : null, d.duration ? `${Math.round(d.duration / 60000)} min` : null,
  ].filter(Boolean);
  const kids = [
    closeBtn(dr),
    h('div', { class: 'd-head' },
      h('img', { src: t?.th ? `/api/thumb?path=${encodeURIComponent(t.th)}` : '', alt: '', loading: 'lazy' }),
      h('div', null,
        h('h2', null, d.title),
        h('div', { class: 'facts' }, facts.join(' · ')),
        h('div', { class: 'd-size' }, fmtB(d.size), d.xbytes ? h('span', { class: 'loose', style: 'font-size:13px;font-weight:400' }, `  +${fmtB(d.xbytes)} not in Plex`) : null),
        h('div', { class: 'facts' },
          h('span', null, 'Last played ', h('b', null, t?._l ? fmtAgo(t._l) : 'never')),
          h('span', null, 'Viewers ', h('b', null, fmtN(t?._v))),
          h('span', null, 'Plays ', h('b', null, fmtN(t?._p))),
          h('span', null, 'Added ', h('b', null, fmtDate(d.added)))),
        d.genres?.length ? h('div', { class: 'facts' }, d.genres.join(', ')) : null)),
    h('div', { class: 'd-actions' },
      listed
        ? h('button', { class: 'btn', onclick: async () => { await removeDrop(listedRows.map(r => r.id)); openDetail(key); } }, '✓ On the drop list — remove')
        : h('button', { class: 'btn danger', disabled: d.kept, onclick: async () => { await addToDrop([{ kind: d.kind, key }]); openDetail(key); } },
          `Add whole ${d.kind === 'movie' ? 'movie' : 'show'} to drop list · ${fmtB(d.size)}`),
      h('button', { class: 'btn', onclick: () => setKeep([key], !d.kept) }, d.kept ? '★ Kept — un-keep' : '☆ Keep'),
    ),
    h('div', { class: 'path', style: 'margin-top:8px' }, d.folder || ''),
  ];
  if (d.arr) {
    kids.push(h('div', { class: 'callout info' },
      `${arrName}: ${d.arr.monitored ? 'monitored' : 'unmonitored'}${d.arr.profile ? ` · profile ${d.arr.profile}` : ''}`,
      d.arr.monitored ? ' — deleting here also unmonitors it, so it stays listed as missing and is never re-grabbed.' : ''));
  } else kids.push(h('div', { class: 'callout' }, (M.arr.safety || {})[d.kind === 'movie' ? 'radarr' : 'sonarr'] === undefined
    ? `${arrName} isn't connected. Deleting removes the files; the Deleted tab keeps the record.`
    : `Not found in ${arrName}. Deleting removes it with no record outside the Deleted log.`));
  if (d.kind === 'movie') kids.push(dgMovieSection(d));

  kids.push(h('h4', null, 'Who played it'));
  kids.push(d.users.length ? h('table', { class: 'mini' },
    h('tr', null, h('th', null, 'Person'), h('th', { class: 'n' }, 'Plays'), h('th', { class: 'n' }, 'Finished'),
      d.kind === 'show' ? h('th', { class: 'n' }, 'Eps') : null, h('th', { class: 'n' }, 'Hours'), h('th', null, 'Last')),
    ...d.users.map(u => h('tr', null, h('td', null, u.name), h('td', { class: 'n' }, fmtN(u.plays)), h('td', { class: 'n' }, fmtN(u.fin)),
      d.kind === 'show' ? h('td', { class: 'n' }, fmtN(u.eps)) : null, h('td', { class: 'n' }, u.hours),
      h('td', { title: fmtDate(u.last) }, fmtAgo(u.last))))) : h('div', { class: 'muted' }, `No one has played this since ${fmtDate(M.history.plex_since)}.`));

  if (d.kind === 'show') {
    const listedSeason = new Set(listedRows.filter(r => r.kind === 'season').map(r => r.key));
    kids.push(h('h4', null, 'Seasons'));
    kids.push(h('table', { class: 'mini' },
      h('tr', null, h('th', null, ''), h('th', { class: 'n' }, 'Size'), h('th', { class: 'n' }, 'Eps'), h('th', { class: 'n' }, 'Seen'),
        h('th', { class: 'n' }, 'Viewers'), h('th', null, 'Last'), h('th', null, 'Arr'), h('th', null, ''), h('th', null, '')),
      ...d.seasons.map(s => h('tr', { class: listedSeason.has(s.key) || listed ? 'drop' : '' },
        h('td', null, s.title), h('td', { class: 'n' }, fmtB(s.size)), h('td', { class: 'n' }, fmtN(s.eps)),
        h('td', { class: 'n' }, fmtN(s.eps_seen)), h('td', { class: 'n', title: s.viewer_names.join(', ') }, fmtN(s.viewers)),
        h('td', { title: fmtDate(s.last) }, s.last ? fmtAgo(s.last) : 'never'),
        h('td', null, s.monitored == null ? '—' : s.monitored ? 'mon' : 'off'),
        h('td', null, listed || listedSeason.has(s.key) || !s.size ? null : h('button', { class: 'btn sm', title: 'Add this season to the drop list',
          onclick: async () => { await addToDrop([{ kind: 'season', key: s.key }]); openDetail(key); } }, '+ drop')), h('td', null, dgSeasonBtn(d, s))))));
    kids.push(dgShowControls(d));
    if (d.res_mix && Object.keys(d.res_mix).length > 1) {
      kids.push(h('div', { class: 'facts' }, 'Resolution mix: ', Object.entries(d.res_mix).sort((a, b) => b[1] - a[1]).map(([r, b]) => `${r} ${fmtB(b)}`).join(' · ')));
    }
  }
  if (d.kind === 'movie' && d.versions.length > 1) {
    const listedV = new Set(listedRows.filter(r => r.kind === 'version').map(r => r.key));
    const tracked = d.arr?.file;
    kids.push(h('h4', null, `${d.versions.length} versions`));
    kids.push(h('table', { class: 'mini' },
      ...d.versions.map(v => {
        const isTracked = tracked && v.files.some(f => f.endsWith('/' + tracked));
        return h('tr', { class: listedV.has(`${key}:${v.id}`) ? 'drop' : '' },
          h('td', null, [v.res, v.vcodec, v.acodec].filter(Boolean).join(' · '), isTracked ? h('span', { class: 'badge', style: 'margin-left:6px' }, 'Radarr tracks this') : null,
            h('div', { class: 'path' }, v.files.join('\n'))),
          h('td', { class: 'n' }, fmtB(v.bytes)),
          h('td', null, listed || listedV.has(`${key}:${v.id}`) ? null : h('button', { class: 'btn sm', title: isTracked ? 'Radarr tracks this file — it would show the movie as missing' : 'Drop just this version',
            onclick: async () => { await addToDrop([{ kind: 'version', title_key: key, media_id: v.id }]); openDetail(key); } }, '+ drop')));
      })));
  }
  if (d.extras?.length) {
    kids.push(h('h4', null, `Plex extras · ${fmtB(d.extras.reduce((s, f) => s + f[1], 0))}`));
    kids.push(h('div', { class: 'muted', style: 'font-size:12px' }, 'Indexed by Plex as extras of this title; not counted in its size above.'));
    kids.push(fileList(d.extras));
  }
  if (d.unindexed) {
    kids.push(h('h4', null, `Not in Plex, same folder · ${fmtB(d.unindexed.bytes)}`));
    kids.push(h('div', { class: 'muted', style: 'font-size:12px' }, 'Plex delete leaves these behind. Remove them on the server.'));
    kids.push(fileList(d.unindexed.files));
  }
  if (d.requests.length) {
    kids.push(h('h4', null, 'Requested'));
    kids.push(h('div', null, ...d.requests.map(r => h('div', { class: 'facts' },
      h('b', null, r.name || r.by || '?'), ` on ${fmtDate(Date.parse(r.at) / 1000)}`, r.seasons?.length ? ` · seasons ${r.seasons.join(', ')}` : ''))));
  }
  if (d.recent.length) {
    kids.push(h('h4', null, 'Recent plays'));
    kids.push(h('table', { class: 'mini' }, ...d.recent.map(p => h('tr', null,
      h('td', { title: fmtDate(p.at) }, fmtAgo(p.at)), h('td', null, p.user), h('td', null, p.ep || ''),
      h('td', { class: 'n' }, `${p.pct}%`), h('td', { class: 'n' }, `${p.mins} min`)))));
  }
  fill(dr, ...kids);
  dgResume(key);
}
function closeBtn(dr) { return h('button', { class: 'btn sm ghost d-close', onclick: () => dr.classList.remove('open'), 'aria-label': 'Close' }, '✕'); }
// DISPLAY_PATHS maps Plex's paths to the ones you'd use on the server
const showPath = p => { for (const [from, to] of CFG.display_paths || []) if (p === from || p.startsWith(from + '/')) return to + p.slice(from.length); return p; };
function fileList(files) {
  const real = files.filter(f => f[2] !== 'sidecar');
  return h('div', null,
    h('table', { class: 'mini' }, ...files.slice(0, 60).map(([p, s, c]) => h('tr', null,
      h('td', null, h('div', { class: 'path' }, showPath(p))), h('td', { class: 'n' }, fmtB(s)), h('td', null, h('span', { class: 'badge' + (c === 'temp' ? ' crit' : '') }, c))))),
    files.length > 60 ? h('div', { class: 'muted' }, `…and ${files.length - 60} more`) : null,
    real.length ? h('button', { class: 'btn sm', style: 'margin-top:6px', onclick: () => copy(real.map(f => showPath(f[0])).join('\n')) }, `Copy ${real.length} path${real.length > 1 ? 's' : ''}`) : null);
}
async function copy(text) {
  try { await navigator.clipboard.writeText(text); toast('Copied'); }
  catch { prompt('Copy:', text); }
}

/* -------------------------------------------------------------- downgrade */
const DG = { jobs: [], active: new Map(), search: new Map(), sel: new Map() };
const RES_N = { '4K': 2160, '2160p': 2160, '1080p': 1080, '720p': 720 };
const tiersBelow = res => [1080, 720].filter(x => x < (RES_N[res] || 0));
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function loadDowngrades() {
  try { DG.jobs = await get('/api/downgrades'); } catch { return; }
  DG.active = new Map();
  for (const j of DG.jobs) if (j.state === 'grabbed') DG.active.set(j.title_key, j);
}

function dgJobsFor(key) {
  const jobs = DG.jobs.filter(j => j.title_key === key);
  if (!jobs.length) return null;
  return h('table', { class: 'mini', style: 'margin-bottom:8px' }, ...jobs.map(j => h('tr', null,
    h('td', null, j.season != null ? `Season ${j.season}` : 'Movie', h('div', { class: 'muted' }, `${j.old_quality || '?'} → ${j.quality}`)),
    h('td', { class: 'n' }, `${fmtB(j.old_size)} → ${fmtB(j.final_size || j.new_size)}`),
    h('td', null, h('span', { class: 'badge' + (j.state === 'failed' ? ' crit' : '') }, j.state), h('div', { class: 'muted' }, j.note || '')),
    h('td', null, j.state === 'grabbed' ? h('button', { class: 'btn sm', onclick: () => dgCancel(j) }, 'Cancel') : null))));
}

function dgMovieSection(d) {
  if (CFG.read_only) return null;
  const tiers = tiersBelow(d.res);
  const app = 'Radarr';
  return h('div', null,
    h('h4', null, 'Downgrade'),
    dgJobsFor(d.key),
    !d.arr ? h('div', { class: 'muted' }, `Not in ${app}, so it can't be swapped for a smaller release.`)
      : !tiers.length ? h('div', { class: 'muted' }, `Already ${d.res || 'low resolution'} — nothing smaller to aim for.`)
        : h('div', { class: 'd-actions', style: 'margin-top:0' },
          ...tiers.map(x => h('button', { class: 'btn', onclick: () => dgStart(d.key, x, null) }, `Find ${x}p releases`)),
          d.versions?.length > 1 ? h('span', { class: 'muted', style: 'font-size:12px' }, `Radarr swaps only the file it tracks; the other ${d.versions.length - 1} version(s) stay.`) : null),
    h('div', { id: 'dgbox' }));
}

function dgShowControls(d) {
  if (CFG.read_only) return null;
  const dom = Object.entries(d.res_mix || {}).sort((a, b) => b[1] - a[1])[0]?.[0];
  const tiers = tiersBelow(dom);
  return h('div', null,
    h('h4', null, 'Downgrade'),
    dgJobsFor(d.key),
    !d.arr ? h('div', { class: 'muted' }, "Not in Sonarr, so it can't be swapped for smaller releases.")
      : !tiers.length ? h('div', { class: 'muted' }, `Mostly ${dom || 'low resolution'} already — use the ↓ on a season if one is bigger.`)
        : h('div', { class: 'd-actions', style: 'margin-top:0' },
          ...tiers.map(x => h('button', { class: 'btn', onclick: () => dgStart(d.key, x, null) }, `Plan whole show ↓${x}p`)),
          h('span', { class: 'muted', style: 'font-size:12px' }, `Sonarr searches each season (~2 min each, ${d.seasons.filter(s => s.size && s.index > 0).length} seasons).`)),
    h('div', { id: 'dgbox' }));
}

function dgSeasonBtn(d, s) {
  if (CFG.read_only) return null;
  const tier = tiersBelow(s.res)[0];
  if (!d.arr || !tier || !s.size) return null;
  return h('button', { class: 'btn sm', title: `Find ${tier}p season packs (${s.res} now)`, onclick: () => dgStart(d.key, tier, [s.key]) }, `↓${tier}`);
}

async function dgStart(key, target, seasons) {
  const box = $('#dgbox');
  if (box) fill(box, h('div', { class: 'muted' }, 'Starting search…'));
  try {
    const job = await post('/api/downgrade/search', { key, target, seasons });
    DG.search.set(key, job.id);
    dgWatch(job.id, key);
  } catch (e) { if (box) fill(box, h('div', { class: 'callout' }, e.message)); }
}
function dgResume(key) { const id = DG.search.get(key); if (id) dgWatch(id, key); }

async function dgWatch(id, key, alive = () => DETAIL.key === key) {
  for (;;) {
    const box = $('#dgbox');
    if (!box || !alive()) return;                      // drawer/modal moved on; the search keeps running server-side
    let job;
    try { job = await get(`/api/downgrade/search/${id}`); }
    catch (e) { DG.search.delete(key); fill(box, h('div', { class: 'callout' }, e.message)); return; }
    if (!box.isConnected) return;
    renderSearch(box, job);
    if (job.status === 'done') return;
    await sleep(job.kind === 'movie' ? 1500 : 4000);
  }
}

function renderSearch(box, job) {
  const app = job.app === 'radarr' ? 'Radarr' : 'Sonarr';
  const keyOf = i => `${job.id}:${i}`;
  job.parts.forEach((p, i) => {
    if (p.status === 'done' && !DG.sel.has(keyOf(i))) DG.sel.set(keyOf(i), p.candidates.find(c => c.recommended)?.guid || null);
  });
  const picks = job.parts.map((p, i) => ({ p, i, c: (p.candidates || []).find(c => c.guid === DG.sel.get(keyOf(i))) })).filter(x => x.c);
  const saves = picks.reduce((s, x) => s + x.c.saves, 0);
  const searching = job.parts.findIndex(p => p.status === 'searching');
  const head = h('div', { class: 'dg-head' },
    h('b', null, job.mode === 'replace' ? `${job.target}p` : `↓${job.target}p`), ' · ',
    job.status === 'done' ? `${app} searched ${job.parts.length > 1 ? `${job.parts.length} seasons` : ''}`.trim()
      : `${app} is searching${job.parts.length > 1 ? ` season ${searching + 1} of ${job.parts.length}` : ''}… (${job.kind === 'movie' ? '~10 s' : '~2 min per season'})`);
  const parts = job.parts.map((p, i) => renderPart(job, p, i, keyOf(i)));
  const foot = h('div', { class: 'modal-actions', style: 'justify-content:space-between' },
    h('span', { class: 'muted' }, !picks.length ? 'Nothing picked' : job.mode === 'replace' ? `${fmtB(picks[0].c.size)} ${picks[0].c.quality}`
      : `${picks.length} pick${picks.length > 1 ? 's' : ''} · saves ${fmtB(saves)}`),
    h('button', { class: 'btn primary', disabled: !picks.length, onclick: () => dgConfirm(job, picks) },
      picks.length > 1 ? `Grab ${picks.length} season packs…` : 'Replace with this…'));
  fill(box, h('div', { class: 'dg' }, head, ...parts, job.parts.some(p => p.status === 'done') ? foot : null));
}

function renderPart(job, p, i, selKey) {
  const multi = job.parts.length > 1;
  const title = multi ? h('div', { class: 'dg-part-t' }, h('b', null, p.label),
    p.ctx ? h('span', { class: 'muted' }, ` · now ${p.ctx.file_quality || '?'} ${fmtB(p.ctx.file_size)}${p.ctx.files != null ? ` · ${plural(p.ctx.files, 'file')}` : ''}`) : null) : null;
  if (p.status === 'pending') return h('div', { class: 'dg-part' }, title, h('div', { class: 'muted' }, 'queued'));
  if (p.status === 'searching') return h('div', { class: 'dg-part' }, title, h('div', { class: 'muted' }, 'searching indexers…'));
  if (p.status === 'error') return h('div', { class: 'dg-part' }, title, h('div', { class: 'callout' }, p.error));
  const cands = p.candidates || [];
  const ok = cands.filter(c => c.ok), blocked = cands.filter(c => !c.ok);
  const sel = DG.sel.get(selKey);
  const radio = c => h('input', { type: 'radio', name: `pick-${selKey}`, checked: c.guid === sel, disabled: !c.ok,
    onchange: () => { DG.sel.set(selKey, c.guid); renderSearch($('#dgbox'), job); } });
  const row = c => h('tr', { class: c.recommended ? 'rec' : '' },
    h('td', null, radio(c)),
    h('td', { class: 'dg-rel' },
      h('div', null, h('b', null, c.quality), c.recommended ? h('span', { class: 'badge', style: 'margin-left:6px' }, 'pick') : null,
        c.edition ? h('span', { class: 'badge', style: 'margin-left:6px', title: 'Edition — compare with what you have' }, c.edition) : null,
        c.multi ? h('span', { class: 'badge', style: 'margin-left:6px', title: 'Several audio languages — check which track plays by default' }, c.languages.length > 1 ? c.languages.join('+') : 'multi-audio') : null),
      h('div', { class: 'path', title: c.title }, c.title),
      h('div', { class: 'muted', style: 'font-size:11px' }, [c.indexer, c.age_days != null ? `${c.age_days} days old` : null].filter(Boolean).join(' · ')),
      ...c.warnings.map(w => h('div', { class: 'muted', style: 'font-size:11px' }, '⚠ ' + w)),
      ...c.blockers.map(b => h('div', { class: 'res-bad', style: 'font-size:11px' }, '✕ ' + b))),
    h('td', { class: 'n' }, fmtB(c.size)),
    h('td', { class: 'n' }, c.saves > 0 ? `−${fmtB(c.saves)}` : '—', h('div', { class: 'muted' }, c.saves > 0 ? `${Math.round(100 * c.saves_pct)}%` : '')),
    h('td', { class: 'n', title: 'MB per minute of runtime' }, c.mbpm ?? '—'));
  const showAll = DG.sel.get(selKey + ':all');
  const visible = showAll ? cands : ok.slice(0, 12);
  return h('div', { class: 'dg-part' },
    title,
    !multi && p.ctx ? h('div', { class: 'facts' }, job.mode === 'replace' && !p.ctx.file_id ? 'Radarr has no file for it'
      : `Now ${p.ctx.file_quality || '?'} · ${fmtB(p.ctx.file_size)}`, p.runtime_min ? ` · ${p.runtime_min} min` : '',
      ` · ${fmtN(p.total)} results, ${fmtN(cands.length)} at ${job.target}p, ${fmtN(ok.length)} usable`) :
      h('div', { class: 'muted', style: 'font-size:12px' }, `${fmtN(cands.length)} ${job.target}p season packs · ${fmtN(ok.length)} usable`),
    cands.length ? h('table', { class: 'mini dg-table' },
      h('tr', null, h('th', null, ''), h('th', null, 'Release'), h('th', { class: 'n' }, 'Size'), h('th', { class: 'n' }, 'Saves'),
        h('th', { class: 'n' }, 'MB/min')),
      ...visible.map(row),
      multi ? h('tr', null, h('td', null, h('input', { type: 'radio', name: `pick-${selKey}`, checked: !sel,
        onchange: () => { DG.sel.set(selKey, null); renderSearch($('#dgbox'), job); } })), h('td', { colspan: 4, class: 'muted' }, 'skip this season')) : null)
      : h('div', { class: 'muted' }, `No ${job.target}p ${multi ? 'season packs' : 'releases'} found.`),
    cands.length > visible.length || showAll ? h('button', { class: 'btn sm ghost', onclick: () => { DG.sel.set(selKey + ':all', !showAll); renderSearch($('#dgbox'), job); } },
      showAll ? 'Show usable only' : `Show all ${cands.length} (${blocked.length} blocked)`) : null);
}

function dgConfirm(job, picks) {
  const modal = $('#modal');
  const app = job.app === 'radarr' ? 'Radarr' : 'Sonarr';
  const total = picks.reduce((s, x) => s + x.c.saves, 0);
  const go = h('button', { class: 'btn primary' }, `Grab ${picks.length > 1 ? `${picks.length} packs` : 'it'}`);
  go.onclick = async () => {
    go.disabled = true;
    try {
      const r = await post('/api/downgrade/grab', { search_id: job.id, picks: picks.map(x => ({ part: x.i, guid: x.c.guid })) });
      await loadDowngrades();
      fill(modal, h('div', { class: 'modal-box' },
        h('h2', null, r.results.some(x => x.ok) ? 'Sent to the download client' : 'Nothing grabbed'),
        h('div', { class: 'list' }, ...r.results.map(x => h('div', null,
          h('span', null, h('span', { class: x.ok ? 'res-ok' : 'res-bad' }, x.ok ? '✓ ' : '✗ '), x.label, x.error ? h('div', { class: 'muted' }, x.error) : null),
          h('span', { class: 'num' }, x.ok ? `−${fmtB(x.saves)}` : '')))),
        h('div', { class: 'ink2' }, `${app} profile is now "${r.profile}". Progress shows on the Downgrades tab; reclaim checks every 2 minutes and rescans Plex when the new file lands.`),
        h('div', { class: 'modal-actions' }, h('button', { class: 'btn primary', onclick: () => {
          modal.hidden = true; DG.search.delete(job.key);
          job.mode === 'replace' ? renderLoose() : openDetail(job.key);
        } }, 'Close'))));
    } catch (e) { toast('Grab failed: ' + e.message); go.disabled = false; }
  };
  const whole = job.kind === 'show';
  if (job.mode === 'replace') return replaceConfirm(job, picks, go);
  fill(modal, h('div', { class: 'modal-box', role: 'dialog', 'aria-modal': 'true' },
    h('h2', null, `Downgrade ${job.title} → ${job.target}p · saves ${fmtB(total)}`),
    h('div', { class: 'list' }, ...picks.map(x => h('div', null,
      h('span', null, x.p.season != null ? `${x.p.label}: ` : '', x.c.title), h('span', { class: 'num' }, `${fmtB(x.c.size)} (−${fmtB(x.c.saves)})`)))),
    h('ul', { class: 'ink2', style: 'font-size:13px;padding-left:18px' },
      h('li', null, `${app} downloads ${picks.length > 1 ? 'these' : 'this'}. Nothing changes until it finishes.`),
      h('li', null, `On import ${app} replaces the current ${whole ? 'episode files' : 'file'} and deletes ${whole ? 'them' : 'it'} (no recycle bin).`),
      h('li', null, `The ${whole ? 'series' : 'movie'} moves to the "Reclaim ↓${job.target}p" profile with upgrades off, so it can't drift back up${whole ? ' — new episodes also come in at ' + job.target + 'p' : ''}. If the download fails or you cancel, the original profile comes back.`)),
    h('div', { class: 'modal-actions' }, h('button', { class: 'btn', onclick: () => { modal.hidden = true; } }, 'Cancel'), go)));
  modal.hidden = false;
}

function replaceConfirm(job, picks, go) {
  const modal = $('#modal');
  const c = picks[0].c, ctx = picks[0].p.ctx || {};
  go.textContent = 'Grab it';
  fill(modal, h('div', { class: 'modal-box', role: 'dialog', 'aria-modal': 'true' },
    h('h2', null, `Replace ${job.title} with ${c.quality} · ${fmtB(c.size)}`),
    h('div', { class: 'list' }, h('div', null, h('span', null, c.title), h('span', { class: 'num' }, fmtB(c.size)))),
    h('ul', { class: 'ink2', style: 'font-size:13px;padding-left:18px' },
      h('li', null, 'Radarr downloads it. Nothing changes until it finishes.'),
      ctx.file_id
        ? h('li', null, `On import Radarr deletes ${ctx.file || 'the file it tracks'} (${fmtB(ctx.file_size)}, no recycle bin) and puts the new file in its place. Reclaim then has Plex rescan the folder, so the movie shows up.`)
        : h('li', null, 'Radarr tracks no file here, so the new one lands next to the old files. Delete those afterwards with a cleanup script.'),
      h('li', null, `The movie moves to the "Reclaim ↓${job.target}p" profile with upgrades off, which is what lets the release win over a disc rip and keeps Radarr from grabbing anything else. If the download fails or you cancel, the original profile comes back.`)),
    h('div', { class: 'modal-actions' }, h('button', { class: 'btn', onclick: () => { modal.hidden = true; } }, 'Cancel'), go)));
  modal.hidden = false;
}

async function dgCancel(j) {
  if (!confirm(`Cancel the ${j.label} downgrade? The download is removed and the original profile restored.`)) return;
  try { await post('/api/downgrade/cancel', { id: j.id }); } catch (e) { toast(e.message); }
  await loadDowngrades();
  if ($('#detail').classList.contains('open')) openDetail(DETAIL.key);
  if (!$('#tab-downgrades').hidden) renderDowngrades();
}

async function renderDowngrades() {
  const el = $('#tab-downgrades');
  await loadDowngrades();
  const done = DG.jobs.filter(j => j.state === 'imported');
  const saved = done.reduce((s, j) => s + Math.max(0, (j.old_size || 0) - (j.final_size || 0)), 0);
  fill(el, h('div', { class: 'card' },
    h('div', { class: 'card-h' }, h('h3', null, `Downgrades · ${fmtN(done.length)} done · ${fmtB(saved)} saved`),
      h('span', { class: 'note' }, `${DG.active.size} running · checked every 2 min · start one from a title's detail drawer`)),
    DG.jobs.length ? h('table', { class: 'mini' },
      h('tr', null, h('th', null, 'Started'), h('th', null, 'What'), h('th', null, 'From → to'), h('th', { class: 'n' }, 'Size'),
        h('th', null, 'State'), h('th', null, '')),
      ...DG.jobs.map(j => h('tr', null,
        h('td', { title: new Date(j.created * 1000).toLocaleString() }, fmtAgo(j.created)),
        h('td', null, h('a', { href: '#', onclick: e => { e.preventDefault(); j.title_key.startsWith('loose:') ? showTab('loose') : openDetail(j.title_key); } }, j.label),
          h('div', { class: 'path', title: j.release }, j.release)),
        h('td', null, `${j.old_quality || '?'} → ${j.quality}`),
        h('td', { class: 'n' }, `${fmtB(j.old_size)} → ${fmtB(j.final_size || j.new_size)}`,
          j.state === 'imported' && j.final_size ? h('div', { class: 'res-ok' }, `−${fmtB(j.old_size - j.final_size)}`) : null),
        h('td', null, h('span', { class: 'badge' + (j.state === 'failed' ? ' crit' : '') }, j.state), h('div', { class: 'muted' }, j.note || '')),
        h('td', null, j.state === 'grabbed' ? h('button', { class: 'btn sm', onclick: () => dgCancel(j) }, 'Cancel') : null))))
      : h('div', { class: 'empty' }, 'No downgrades yet. Open a 4K or 1080p title and use "Find 1080p/720p releases", or the ↓ on a TV season.')));
}

/* -------------------------------------------------------------- drop list */
async function removeDrop(ids) {
  await loadShortlist(await post('/api/shortlist/remove', { ids }));
  redrawMarks();
}
function renderDrop() {
  const dr = $('#drop');
  const a = M.array;
  const checked = new Set(shortlist.map(r => r.id));
  const sumChecked = () => shortlist.filter(r => checked.has(r.id)).reduce((s, r) => s + r.bytes, 0);
  const delBtn = h('button', { class: 'btn danger', disabled: !shortlist.length, onclick: () => confirmDelete(shortlist.filter(r => checked.has(r.id))) });
  const setDel = () => { delBtn.textContent = `Delete ${checked.size} · ${fmtB(sumChecked())}…`; delBtn.disabled = !checked.size; };
  setDel();
  fill(dr, 
    closeBtn(dr),
    h('h2', null, 'Drop list'),
    h('div', { class: 'drop-sum' }, h('b', null, `${plural(shortlist.length, 'item')} · ${fmtB(dropBytes)}`),
      a ? ` → array free goes from ${fmtB(a.free)} to ${fmtB(a.free + dropBytes)} (${pct(a.free + dropBytes, a.total)})` : ''),
    h('div', { class: 'callout info' }, 'Deleting goes through Plex (files removed from disk). If Radarr/Sonarr are connected, the matching item is unmonitored so it stays as a missing record and is never re-grabbed. Every delete is written to the Deleted tab.'),
    shortlist.length ? h('div', { class: 'drop-list' }, ...shortlist.map(r => h('div', { class: 'drop-item' },
      h('input', { type: 'checkbox', checked: true, 'aria-label': `Include ${r.label}`, onchange: e => { e.target.checked ? checked.add(r.id) : checked.delete(r.id); setDel(); } }),
      h('span', { class: 'lab', title: r.label, onclick: () => openDetail(r.title_key) }, r.kind === 'season' || r.kind === 'version' ? h('span', { class: 'badge', style: 'margin-right:6px' }, r.kind) : null, r.label),
      h('span', { class: 'n' }, fmtB(r.bytes)),
      h('button', { class: 'btn sm ghost', title: 'Remove from list', onclick: () => removeDrop([r.id]) }, '✕'))))
      : h('div', { class: 'empty' }, 'Nothing listed. Select titles (checkboxes, shift-click, or drag on the scatter) and add them.'),
    h('div', { class: 'modal-actions' },
      h('button', { class: 'btn ghost', disabled: !shortlist.length, onclick: () => exportCsv() }, 'Export CSV'),
      h('button', { class: 'btn', disabled: !shortlist.length, onclick: async () => { if (confirm('Empty the drop list? (Nothing is deleted.)')) { await loadShortlist(await post('/api/shortlist/remove', { all: true })); redrawMarks(); } } }, 'Clear list'),
      CFG.read_only ? h('span', { class: 'muted' }, 'Read-only mode: export the list and delete elsewhere.') : delBtn),
  );
}
function exportCsv() {
  const q = s => `"${String(s ?? '').replace(/"/g, '""')}"`;
  const rows = [['kind', 'label', 'bytes', 'GB', 'last played', 'viewers', 'plays', 'requested by'].join(',')];
  for (const r of shortlist) {
    const t = byKey.get(r.title_key);
    rows.push([r.kind, q(r.label), r.bytes, (r.bytes / 1e9).toFixed(2), t?._l ? fmtDate(t._l) : 'never', t?._v ?? '', t?._p ?? '',
      q(t ? [...new Set(t.q.map(x => userById.get(x[0])?.n))].join('; ') : '')].join(','));
  }
  const a = h('a', { href: URL.createObjectURL(new Blob([rows.join('\n')], { type: 'text/csv' })), download: `reclaim-drop-list-${new Date().toISOString().slice(0, 10)}.csv` });
  document.body.append(a); a.click(); a.remove();
}

function confirmDelete(items) {
  if (!items.length) return;
  const modal = $('#modal');
  const total = items.reduce((s, r) => s + r.bytes, 0);
  const safety = M.arr.safety || {};
  const off = Object.entries(safety).filter(([, v]) => !v).map(([k]) => k === 'radarr' ? 'Radarr' : 'Sonarr');
  const unmon = h('input', { type: 'checkbox', checked: true });
  const typed = h('input', { type: 'text', placeholder: 'DELETE', autocomplete: 'off', 'aria-label': 'Type DELETE to confirm' });
  const go = h('button', { class: 'btn danger', disabled: true }, `Delete ${fmtB(total)}`);
  typed.oninput = () => { go.disabled = typed.value !== 'DELETE'; };
  go.onclick = async () => {
    go.disabled = true;
    try {
      await post('/api/delete', { ids: items.map(r => r.id), unmonitor: unmon.checked, confirm: 'DELETE' });
      watchJob();
    } catch (e) { toast('Delete refused: ' + e.message); go.disabled = false; }
  };
  fill(modal, h('div', { class: 'modal-box', role: 'dialog', 'aria-modal': 'true' },
    h('h2', null, `Delete ${items.length} item${items.length > 1 ? 's' : ''} · ${fmtB(total)}`),
    h('div', { class: 'ink2' }, 'Plex deletes these files from the array. Nothing on this path keeps a copy, so this cannot be undone.'),
    h('div', { class: 'list' }, ...items.map(r => h('div', null, h('span', null, r.label), h('span', { class: 'num' }, fmtB(r.bytes))))),
    h('label', { class: 'chk' }, unmon, h('span', null, h('b', null, 'Unmonitor in Radarr/Sonarr'),
      h('div', { class: 'muted', style: 'font-size:12px' }, off.length
        ? `${off.join(' and ')} ${off.length > 1 ? 'have' : 'has'} "unmonitor deleted" switched off, so a monitored item that goes missing gets re-grabbed by RSS sync. Unmonitoring keeps the record (shows as missing) without re-downloading.`
        : 'Keeps the record as missing without re-downloading.'))),
    h('div', { class: 'modal-actions' }, h('span', { class: 'muted' }, 'Type DELETE'), typed,
      h('button', { class: 'btn', onclick: () => { modal.hidden = true; } }, 'Cancel'), go)));
  modal.hidden = false;
  typed.focus();
}

let jobTimer = null;
function watchJob() {
  const modal = $('#modal');
  const box = h('div', { class: 'modal-box' }, h('h2', null, 'Deleting…'));
  fill(modal, box); modal.hidden = false;
  clearInterval(jobTimer);
  jobTimer = setInterval(async () => {
    const s = await get('/api/status');
    const j = s.job; if (!j) return;
    fill(box, 
      h('h2', null, j.running ? `Deleting ${j.done} / ${j.total}` : `Done · freed ${fmtB(j.freed)}`),
      j.phase ? h('div', { class: 'muted' }, j.phase) : null,
      h('div', { class: 'list' }, ...j.results.map(r => h('div', null,
        h('span', null, h('span', { class: r.ok ? 'res-ok' : 'res-bad' }, r.ok ? '✓ ' : '✗ '), r.label,
          h('div', { class: 'muted' }, [r.plex, r.arr, r.error].filter(Boolean).join(' · '))),
        h('span', { class: 'num' }, fmtB(r.bytes))))),
      j.error ? h('div', { class: 'callout' }, j.error) : null,
      !j.running ? h('div', { class: 'modal-actions' }, h('button', { class: 'btn primary', onclick: () => { modal.hidden = true; } }, 'Close')) : null);
    if (!j.running) {
      clearInterval(jobTimer);
      await loadModel(); await loadShortlist();
      if ($('#drop').classList.contains('open')) renderDrop();
    }
  }, 1000);
}

/* --------------------------------------------------------------- settings */
const SET = { data: null, edits: {}, clear: new Set(), results: {}, busy: {}, errors: null, plex: null, browse: null };
const SUMMARY_KEY = { tautulli: 'tautulli', radarr: 'radarr', sonarr: 'sonarr', seerr: 'seerr', capacity: 'capacity', walk: 'walk' };

async function renderSettings(reload) {
  const el = $('#tab-settings');
  if (!SET.data || reload) {
    if (!SET.data) fill(el, h('div', { class: 'card muted' }, 'Loading…'));
    SET.data = await get('/api/settings');
  }
  const d = SET.data;
  const first = d.summary.needs_setup;
  fill(el,
    first ? h('div', { class: 'card welcome' }, h('h2', null, 'Welcome to reclaim'),
      h('p', null, 'Connect your Plex server to get started — “Sign in with Plex” is the quickest way. Everything else is optional and can be added any time.')) : null,
    ...d.groups.map(groupCard),
    h('div', { class: 'set-save' },
      h('span', { class: 'muted', id: 'setMsg' }, dirtyText()),
      h('span', { class: 'spacer' }),
      h('button', { class: 'btn', onclick: () => { SET.edits = {}; SET.clear.clear(); SET.errors = null; renderSettings(); } }, 'Discard changes'),
      h('button', { class: 'btn primary', onclick: saveSettings }, first ? 'Save and build' : 'Save')));
}

function dirtyText() {
  const n = Object.keys(SET.edits).length + SET.clear.size;
  return n ? `${n} unsaved change${n > 1 ? 's' : ''}` : `Settings live in ${SET.data?.data_dir || 'the data folder'}; environment variables override them.`;
}
function markDirty() { const m = $('#setMsg'); if (m) m.textContent = dirtyText(); }

function statusPill(g) {
  const r = SET.results[g.id];
  if (r) return h('span', { class: 'pill ' + (r.ok ? (r.lines.some(l => l[0] === 'warn') ? 'warn' : 'ok') : 'error') }, r.ok ? 'test passed' : 'test failed');
  const s = SET.data.summary;
  if (g.id === 'plex') return h('span', { class: 'pill ' + (s.plex ? 'ok' : 'error') }, s.plex ? 'set' : 'not set');
  const k = SUMMARY_KEY[g.id];
  if (!k) return null;
  return h('span', { class: 'pill' + (s[k] ? ' ok' : '') }, s[k] ? 'set' : 'off');
}

function groupCard(g) {
  const res = SET.results[g.id];
  return h('section', { class: 'card set-card', id: `set-${g.id}` },
    h('div', { class: 'card-h' }, h('h3', null, g.title), g.required ? h('span', { class: 'badge' }, 'required') : null, statusPill(g),
      h('span', { class: 'spacer' }),
      g.id === 'plex' ? h('button', { class: 'btn sm primary', onclick: plexSignIn }, 'Sign in with Plex') : null,
      g.test ? h('button', { class: 'btn sm', disabled: !!SET.busy[g.id], onclick: () => runTest(g.id) }, SET.busy[g.id] ? 'Testing…' : 'Test') : null),
    h('p', { class: 'set-blurb' }, g.blurb),
    g.id === 'plex' && SET.plex ? plexPicker() : null,
    h('div', { class: 'set-fields' }, ...g.fields.map(fieldEl)),
    res ? h('div', { class: 'set-results' }, ...res.lines.map(([lvl, txt]) =>
      h('div', { class: 'set-line ' + lvl }, h('span', { class: 'ic' }, lvl === 'ok' ? '✓' : lvl === 'warn' ? '⚠' : '✕'), h('span', null, txt)))) : null);
}

function fieldEl(f) {
  const has = f.key in SET.edits;
  const cur = has ? SET.edits[f.key] : f.value;
  const locked = !!f.locked;
  const set = v => { SET.edits[f.key] = v; markDirty(); };
  let input;
  if (f.kind === 'bool') {
    input = h('input', { type: 'checkbox', checked: !!cur, disabled: locked, onchange: e => set(e.target.checked) });
  } else if (f.kind === 'secret' || f.kind === 'password') {
    const cleared = SET.clear.has(f.key);
    input = h('div', { class: 'set-row' },
      h('input', { type: 'password', autocomplete: 'new-password', disabled: locked, value: SET.edits[f.key] || '',
        placeholder: locked ? 'set in the environment' : f.set && !cleared ? '•••••••• saved — type to replace' : (cleared ? 'will be cleared on save' : f.placeholder || ''),
        oninput: e => { set(e.target.value); SET.clear.delete(f.key); } }),
      f.set && !locked && !cleared ? h('button', { class: 'btn sm ghost', title: 'Remove the saved value',
        onclick: () => { SET.clear.add(f.key); delete SET.edits[f.key]; markDirty(); renderSettings(); } }, 'Clear') : null);
  } else if (f.kind === 'pairs') {
    input = pairsEditor(f, (cur || []).map(p => [...p]), locked, set);
  } else if (f.kind === 'list') {
    const box = h('input', { type: 'text', disabled: locked, value: (cur || []).join(', '), placeholder: f.placeholder,
      oninput: e => set(e.target.value.split(',').map(x => x.trim()).filter(Boolean)) });
    input = h('div', { class: 'set-row' }, box, locked ? null : browseBtn(box, v => {
      const list = box.value.split(',').map(x => x.trim()).filter(Boolean);
      if (!list.includes(v)) list.push(v);
      box.value = list.join(', '); set(list);
    }));
  } else {
    input = h('input', { type: f.kind === 'int' ? 'number' : 'text', disabled: locked, value: cur ?? '', placeholder: f.placeholder,
      min: f.kind === 'int' ? 0 : null, max: f.kind === 'int' ? 23 : null, oninput: e => set(e.target.value) });
  }
  return h('div', { class: 'set-field' + (f.kind === 'bool' ? ' inline' : '') },
    h('div', { class: 'set-label' }, f.label,
      locked ? h('span', { class: 'badge', style: 'margin-left:6px', title: `Set by the ${f.locked} environment variable — change it there.` }, `from ${f.locked}`) : null),
    input,
    f.help ? h('div', { class: 'set-help' }, f.help) : null,
    SET.errors?.[f.key] ? h('div', { class: 'set-line error' }, h('span', { class: 'ic' }, '✕'), h('span', null, SET.errors[f.key])) : null);
}

function pairsEditor(f, rows, locked, set) {
  const wrap = h('div', { class: 'pairs' });
  const emit = () => set(rows.filter(r => r[0] || r[1]));
  const roots = SET.data.plex_roots || [];
  const paint = () => {
    fill(wrap,
      ...rows.map((r, i) => {
        const right = h('input', { type: 'text', value: r[1], disabled: locked, placeholder: f.key === 'WALK_PATHS' ? '/media/movies (inside reclaim)' : '/mnt/user/media',
          oninput: e => { r[1] = e.target.value.trim(); emit(); } });
        return h('div', { class: 'set-row pair' },
          h('input', { type: 'text', value: r[0], disabled: locked, placeholder: f.key === 'WALK_PATHS' ? '/data/movies (as Plex sees it)' : '/data',
            oninput: e => { r[0] = e.target.value.trim(); emit(); } }),
          h('span', { class: 'muted' }, '→'), right,
          f.key === 'WALK_PATHS' && !locked ? browseBtn(right, v => { right.value = v; r[1] = v; emit(); }) : null,
          locked ? null : h('button', { class: 'btn sm ghost', title: 'Remove', onclick: () => { rows.splice(i, 1); emit(); paint(); } }, '✕'));
      }),
      locked ? null : h('div', { class: 'set-row' },
        h('button', { class: 'btn sm', onclick: () => { rows.push(['', '']); paint(); } }, '+ Add folder'),
        f.key === 'WALK_PATHS' && roots.length ? h('span', { class: 'muted', style: 'font-size:12px' }, 'Plex library folders:') : null,
        ...(f.key === 'WALK_PATHS' ? roots.filter(rt => !rows.some(r => r[0] === rt)).map(rt =>
          h('button', { class: 'btn sm ghost', title: 'Add a row for this Plex folder', onclick: () => { rows.push([rt, '']); emit(); paint(); } }, rt)) : [])));
  };
  paint();
  return wrap;
}

function browseBtn(target, use) {
  const panel = h('div', { class: 'browse', hidden: true });
  const go = async path => {
    const d = await get('/api/settings/browse?path=' + encodeURIComponent(path || '/'));
    fill(panel,
      h('div', { class: 'set-row' }, h('b', { class: 'path' }, d.path),
        h('span', { class: 'spacer' }),
        h('button', { class: 'btn sm primary', onclick: () => { use(d.path); panel.hidden = true; } }, 'Use this folder'),
        h('button', { class: 'btn sm ghost', onclick: () => { panel.hidden = true; } }, '✕')),
      d.error ? h('div', { class: 'set-line error' }, d.error) : null,
      h('div', { class: 'browse-list' },
        d.parent != null ? h('button', { class: 'btn sm ghost', onclick: () => go(d.parent) }, '↑ ..') : null,
        ...d.dirs.map(n => h('button', { class: 'btn sm ghost', onclick: () => go((d.path === '/' ? '' : d.path) + '/' + n) }, '📁 ' + n)),
        d.more ? h('span', { class: 'muted' }, `+${d.more} more`) : null,
        !d.dirs.length && !d.error ? h('span', { class: 'muted' }, 'no folders here') : null));
  };
  const btn = h('button', { class: 'btn sm', title: "Browse folders inside reclaim's container",
    onclick: () => { panel.hidden = !panel.hidden; if (!panel.hidden) go(target.value || '/'); } }, 'Browse');
  return h('span', { class: 'browse-wrap' }, btn, panel);
}

async function runTest(id) {
  SET.busy[id] = true; renderSettings();
  try {
    SET.results[id] = await post('/api/settings/test', { group: id, values: SET.edits });
    if (id === 'plex' && SET.results[id].data?.sections) {
      SET.data.plex_roots = [...new Set(SET.results[id].data.sections.flatMap(s => s.roots))];
    }
  } catch (e) { SET.results[id] = { ok: false, lines: [['error', e.message]] }; }
  SET.busy[id] = false;
  renderSettings();
}

async function plexSignIn() {
  const win = window.open('', 'reclaim-plex', 'width=600,height=720');   // opened in the click, so it isn't blocked
  try {
    const pin = await post('/api/settings/plex/pin');
    if (win) win.location.href = pin.auth_url; else window.open(pin.auth_url, '_blank');
    SET.plex = { waiting: true }; renderSettings();
    const until = Date.now() + 10 * 60 * 1000;
    while (Date.now() < until) {
      await sleep(2000);
      const r = await post(`/api/settings/plex/pin/${pin.id}`);
      if (r.done) {
        SET.plex = { servers: r.servers };
        try { win && win.close(); } catch {}
        renderSettings();
        return;
      }
      if (win && win.closed && Date.now() > until - 9.5 * 60 * 1000) {
        // window closed without approving (give it one more look first)
        const again = await post(`/api/settings/plex/pin/${pin.id}`);
        if (again.done) { SET.plex = { servers: again.servers }; renderSettings(); return; }
        SET.plex = { error: 'The Plex window was closed before reclaim was approved.' }; renderSettings(); return;
      }
    }
    SET.plex = { error: 'Sign-in timed out — try again.' }; renderSettings();
  } catch (e) {
    try { win && win.close(); } catch {}
    SET.plex = { error: e.message }; renderSettings();
  }
}

function plexPicker() {
  if (SET.plex.waiting) return h('div', { class: 'callout info' }, 'Waiting for you to approve reclaim in the Plex window…');
  if (SET.plex.error) return h('div', { class: 'callout' }, SET.plex.error);
  const servers = SET.plex.servers || [];
  if (!servers.length) return h('div', { class: 'callout' }, 'No Plex Media Servers on this account.');
  let si = 0;
  const conSel = h('select');
  const fillCon = () => fill(conSel, ...servers[si].connections.map((c, i) =>
    h('option', { value: i }, `${c.uri}${c.local ? ' · local' : ' · remote'}${c.relay ? ' · relay' : ''}`)));
  const srvSel = h('select', { onchange: e => { si = +e.target.value; fillCon(); } },
    ...servers.map((s, i) => h('option', { value: i }, s.owned ? s.name : `${s.name} (shared with you — history and deleting need the owner)`)));
  fillCon();
  return h('div', { class: 'callout info' },
    h('div', null, 'Signed in. Pick the server and the address reclaim should use (a local address is best):'),
    h('div', { class: 'set-row', style: 'margin-top:8px' }, srvSel, conSel,
      h('button', { class: 'btn sm primary', onclick: () => {
        const s = servers[si], c = s.connections[+conSel.value];
        SET.edits.PLEX_URL = c.uri; SET.edits.PLEX_TOKEN = s.token; SET.clear.delete('PLEX_TOKEN');
        SET.plex = null; renderSettings(); runTest('plex');
      } }, 'Use')));
}

async function saveSettings() {
  const r = await fetch('/api/settings', { method: 'POST', headers: HDR, body: JSON.stringify({ values: SET.edits, clear: [...SET.clear] }) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) {
    SET.errors = j.errors || null;
    toast(j.errors ? 'Fix the highlighted settings' : 'Save failed: ' + (j.detail || r.status));
    renderSettings();
    return;
  }
  const wasSetup = SET.data.summary.needs_setup;
  SET.data = j; SET.edits = {}; SET.clear.clear(); SET.errors = null;
  try { CFG = await get('/api/config'); } catch {}
  toast(j.rebuilding ? 'Saved — rebuilding the library (about a minute for a big one)' : 'Saved');
  renderSettings();
  if (j.rebuilding) { STATUS.refreshing = true; renderBuilt(); setTimeout(pollStatus, 1500); }
  if (wasSetup && !j.summary.needs_setup) setTimeout(() => showTab('library'), 600);
}

/* ------------------------------------------------------------ other tabs */
const LOOSE_PAGE = 200;
const dispPath = showPath;
function walkBtn() {
  return h('button', { class: 'btn sm', onclick: async e => { e.target.disabled = true; await post('/api/walk/run'); toast('Walking — the tab updates when it finishes'); } }, 'Walk now');
}
// Not-in-Plex selection: path -> {size, cat, folder, root, titled}. Outlives repaints and tab switches.
const LOOSE = { sel: new Map() };
const rootOf = folder => folder.slice(0, folder.lastIndexOf('/'));
// What ticking a whole folder selects. Never 'alias' (indexed files under a name SMB can't show).
// Next to a title Plex plays, sidecars stay (they're its subtitles and artwork) unless you're
// looking at the sidecar category on purpose.
const loosePick = (g, cat) => g.files.filter(f => f[2] !== 'alias' && (cat ? f[2] === cat : !g.title || f[2] !== 'sidecar'));
const looseAdd = (g, files) => { for (const [p, s, c] of files) LOOSE.sel.set(p, { size: s, cat: c, folder: g.folder, root: rootOf(g.folder), titled: !!g.title }); };
const looseDrop = g => { for (const f of g.files) LOOSE.sel.delete(f[0]); };

async function renderLoose() {
  const el = $('#tab-loose');
  fill(el, h('div', { class: 'card muted' }, 'Loading…'));
  const d = await get('/api/unindexed');
  if (!d.walk_at) {
    fill(el, h('div', { class: 'card' }, h('h3', null, 'No disk walk yet'),
      CFG.walk === 'local'
        ? h('p', { class: 'ink2' }, `Walks ${CFG.walk_roots.join(', ')} after each refresh. `, walkBtn())
        : h('div', { class: 'ink2' },
          h('p', null, 'Mount your media read-only into reclaim’s container, then map the folders in Settings → Disk walk.'),
          h('button', { class: 'btn', onclick: () => showTab('settings') }, 'Open Settings'),
          h('p', null, 'If the files are only reachable from another machine, run tools/remote_walk.py there; it posts the list here.'))));
    return;
  }
  const listed = new Set(d.groups.flatMap(g => g.files.map(f => f[0])));
  for (const p of [...LOOSE.sel.keys()]) if (!listed.has(p)) LOOSE.sel.delete(p);   // gone since the last walk
  const cats = Object.entries(d.categories).sort((a, b) => b[1].bytes - a[1].bytes);
  const st = { q: '', cat: '', sort: 'size', side: false, shown: LOOSE_PAGE, open: new Set() };
  for (const g of d.groups) {
    g._all = g.files.reduce((s, f) => s + f[1], 0);
    g._hay = [g.folder, g.name || '', ...g.files.map(f => f[0].slice(g.folder.length))].join('\n').toLowerCase();
  }
  // the size a row shows and sorts by: its bytes in the chosen category, else what it costs
  // (sidecar-only folders cost 0, so they show and sort by their sidecars instead)
  const sizeOf = g => (st.cat ? g.cats[st.cat] || 0 : g.bytes || g._all);
  const SORTS = {
    size: (a, b) => sizeOf(b) - sizeOf(a),
    files: (a, b) => b.files.length - a.files.length || sizeOf(b) - sizeOf(a),
    name: (a, b) => a.folder.localeCompare(b.folder),
  };
  const head = h('div', { class: 'muted', style: 'margin:10px 0 2px' });
  const body = h('div');
  const bar = h('div', { class: 'selbar', hidden: true });
  const refs = new Map();             // folder -> { box, cbs: Map(path -> checkbox) } for rows on screen
  let matching = [];                  // every group the filters match, not just the page on screen

  const allBox = h('input', { type: 'checkbox', onchange: e => {
    for (const g of matching) e.target.checked ? looseAdd(g, loosePick(g, st.cat)) : looseDrop(g);
    syncAll();
  } });
  const allText = h('span');
  const syncGroup = g => {
    const r = refs.get(g.folder);
    if (!r) return;
    const picks = loosePick(g, st.cat);
    r.box.checked = picks.length > 0 && picks.every(f => LOOSE.sel.has(f[0]));
    r.box.indeterminate = !r.box.checked && g.files.some(f => LOOSE.sel.has(f[0]));
    for (const [p, cb] of r.cbs) cb.checked = LOOSE.sel.has(p);
  };
  const syncBar = () => {
    let bytes = 0;
    const folders = new Set();
    for (const v of LOOSE.sel.values()) { bytes += v.size; folders.add(v.folder); }
    bar.hidden = !LOOSE.sel.size;
    if (LOOSE.sel.size) {
      fill(bar, h('span', null, `${plural(LOOSE.sel.size, 'file')} · ${fmtB(bytes)} in ${plural(folders.size, 'folder')}`),
        h('button', { class: 'btn sm', onclick: () => { LOOSE.sel.clear(); syncAll(); } }, 'Clear'),
        h('button', { class: 'btn sm add', onclick: () => cleanupBuilder(d) }, 'Cleanup script…'));
    }
    const picks = matching.flatMap(g => loosePick(g, st.cat));
    allBox.checked = picks.length > 0 && picks.every(f => LOOSE.sel.has(f[0]));
    allBox.indeterminate = !allBox.checked && picks.some(f => LOOSE.sel.has(f[0]));
    allBox.disabled = !picks.length;
    allText.textContent = ` select all ${plural(matching.length, 'matching folder')}`;
  };
  const syncAll = () => { for (const g of matching) syncGroup(g); syncBar(); };

  const row = g => {
    const name = g.folder.split('/').pop();
    const picks = loosePick(g, st.cat);
    const why = picks.length ? null : g.files.every(f => f[2] === 'alias')
      ? 'Plex indexes these under their real names; they only look loose over SMB'
      : 'Only sidecars here, next to a title Plex plays. Pick Sidecar under Category to select them, or tick files one by one.';
    const box = h('input', { type: 'checkbox', 'aria-label': `Select ${name}`, disabled: !picks.length, title: why,
      onchange: e => { e.target.checked ? looseAdd(g, loosePick(g, st.cat)) : looseDrop(g); syncGroup(g); syncBar(); } });
    const cbs = new Map();
    refs.set(g.folder, { box, cbs });
    const files = h('div', { class: 'files' });
    const showFiles = () => { if (!files.firstChild) fill(files, looseFiles(g, cbs, () => { syncGroup(g); syncBar(); })); };
    const det = h('details', { class: 'ugroup', open: st.open.has(g.folder),
      ontoggle: e => { if (e.target.open) { st.open.add(g.folder); showFiles(); } else st.open.delete(g.folder); } },
      h('summary', null,
        h('div', null, box, ' ', h('b', null, name),
          g.title ? h('span', { class: 'muted' }, ' · Plex: ', h('a', { href: '#', onclick: e => { e.preventDefault(); openDetail(g.title); } }, g.name))
            : h('span', { class: 'badge crit', style: 'margin-left:6px' }, 'no Plex title in this folder'),
          h('div', { class: 'chips' }, ...Object.entries(g.cats).filter(([, b]) => b >= 1e6).map(([c, b]) => h('span', { class: 'badge' + (c === 'temp' ? ' crit' : '') }, `${c} ${fmtB(b)}`))),
          replaceBits(g)),
        h('div', { class: 'num', style: 'text-align:right' }, fmtB(sizeOf(g)))),
      files);
    if (st.open.has(g.folder)) showFiles();
    return det;
  };

  const paint = () => {
    const q = st.q.trim().toLowerCase();
    matching = d.groups.filter(g => (st.cat ? st.cat in g.cats : st.side || g.bytes > 0) && (!q || g._hay.includes(q)))
      .sort(SORTS[st.sort]);
    const rest = matching.length - st.shown;
    head.textContent = matching.length === d.groups.length ? plural(matching.length, 'folder') : `${fmtN(matching.length)} of ${plural(d.groups.length, 'folder')}`;
    refs.clear();
    if (!matching.length) fill(body, h('div', { class: 'empty' }, 'No folders match.'));
    else fill(body, ...matching.slice(0, st.shown).map(row), rest > 0 ? h('div', { class: 'more-row' },
      h('span', { class: 'muted' }, `Showing ${fmtN(st.shown)} of ${fmtN(matching.length)}`),
      h('button', { class: 'btn sm', onclick: () => { st.shown += LOOSE_PAGE; paint(); } }, `Show ${fmtN(Math.min(LOOSE_PAGE, rest))} more`),
      rest > LOOSE_PAGE ? h('button', { class: 'btn sm ghost', onclick: () => { st.shown = matching.length; paint(); } }, `Show all ${fmtN(matching.length)}`) : null) : null);
    syncAll();
  };
  const set = (k, v) => { st[k] = v; st.shown = LOOSE_PAGE; paint(); };
  const sideBox = h('input', { type: 'checkbox', onchange: e => set('side', e.target.checked) });
  const sideLabel = h('label', { class: 'f' }, sideBox, ' show sidecar-only folders (subs, nfo, art)');
  const toolbar = h('div', { class: 'filters loose-filters' },
    h('span', { class: 'f' }, h('input', { type: 'search', placeholder: 'Folder or file name', 'aria-label': 'Search folders and files',
      oninput: e => set('q', e.target.value) })),
    h('label', { class: 'f' }, 'Category ', h('select', { onchange: e => { sideLabel.hidden = !!e.target.value; set('cat', e.target.value); } },
      h('option', { value: '' }, 'any'),
      ...cats.map(([c, v]) => h('option', { value: c }, `${v.label} (${fmtN(v.files)})`)))),
    h('label', { class: 'f' }, 'Sort ', h('select', { onchange: e => set('sort', e.target.value) },
      h('option', { value: 'size' }, 'largest first'), h('option', { value: 'files' }, 'most files'), h('option', { value: 'name' }, 'folder name'))),
    sideLabel,
    h('label', { class: 'f' }, allBox, allText));
  paint();
  const total = cats.filter(([c]) => c !== 'alias').reduce((s, [, v]) => s + v.bytes, 0);
  fill(el, h('div', { class: 'card' },
    h('div', { class: 'card-h' }, h('h3', null, `On disk but not in Plex · ${fmtB(total)}`),
      h('span', { class: 'note' }, `walked ${fmtAgo(d.walk_at)} (${fmtDate(d.walk_at)})`), CFG.walk === 'local' ? walkBtn() : null),
    h('div', { class: 'cat-tiles' }, ...cats.map(([c, v]) => h('div', { class: 'tile' },
      h('div', { class: 'l' }, v.label), h('div', { class: 'v' }, fmtB(v.bytes)), h('div', { class: 's' }, plural(v.files, 'file'))))),
    d.missing_on_disk ? h('div', { class: 'callout info' }, `${d.missing_on_disk} indexed files weren't seen by the walk — files deleted since the walk, a Disk walk folder mapping that doesn't cover them, or (over SMB) names Windows can't represent, which show up as 8.3 aliases like DR0ON7~D.`) : null,
    d.extras?.files ? h('div', { class: 'callout info' }, `Plex has indexed ${plural(d.extras.files, 'file')} (${fmtB(d.extras.bytes)}) as extras: featurettes, trailers, deleted scenes and the like. They're in Plex, so they aren't listed here; each title's details show its own.`) : null,
    toolbar, head, body, bar));
}

function looseFiles(g, cbs, changed) {
  const LIMIT = 300;
  const real = g.files.filter(f => f[2] !== 'sidecar' && f[2] !== 'alias');
  return h('div', null,
    h('table', { class: 'mini' }, ...g.files.slice(0, LIMIT).map(f => {
      const [p, s, c] = f;
      const cb = h('input', { type: 'checkbox', 'aria-label': `Select ${p.split('/').pop()}`, checked: LOOSE.sel.has(p), disabled: c === 'alias',
        title: c === 'alias' ? 'Plex indexes this file under its real name; it only looks loose over SMB' : null,
        onchange: e => { e.target.checked ? looseAdd(g, [f]) : LOOSE.sel.delete(p); changed(); } });
      cbs.set(p, cb);
      return h('tr', null, h('td', null, cb), h('td', null, h('div', { class: 'path' }, dispPath(p))), h('td', { class: 'n' }, fmtB(s)),
        h('td', null, h('span', { class: 'badge' + (c === 'temp' ? ' crit' : '') }, c)));
    })),
    g.files.length > LIMIT ? h('div', { class: 'muted' }, `…and ${fmtN(g.files.length - LIMIT)} more (ticking the folder selects those too)`) : null,
    real.length ? h('button', { class: 'btn sm', style: 'margin-top:6px', onclick: () => copy(real.map(f => dispPath(f[0])).join('\n')) }, `Copy ${real.length} path${real.length > 1 ? 's' : ''}`) : null);
}

/* ------------------------------------------------- not in plex: replacement */
// A movie folder Plex has no title for, that Radarr knows: usually a disc rip Radarr tracks as the
// movie's file. The downgrade machinery swaps it for a normal release (its profiles rank disc rips lowest).
function replaceBits(g) {
  if (CFG.read_only || g.title || g.arr?.app !== 'radarr') return null;
  const j = g.replace;
  const note = (...k) => h('div', { class: 'muted', style: 'font-size:12px;margin-top:3px' }, ...k);
  if (j?.state === 'grabbed') return note(`Replacement on its way: ${j.quality} · ${j.note || ''}`);
  if (j?.state === 'imported') return note(`Replaced with ${j.quality} (${fmtB(j.final_size || j.new_size)}); Plex was told to rescan the folder.`);
  return h('div', { class: 'd-actions', style: 'margin-top:4px' },
    h('button', { class: 'btn sm', onclick: e => { e.preventDefault(); replaceStart(g); } }, 'Find a replacement…'),
    h('span', { class: 'muted', style: 'font-size:12px' },
      j?.state === 'failed' || j?.state === 'cancelled' ? `Last try ${j.state}: ${j.note || ''}` : g.arr.has_file ? `Radarr tracks ${g.arr.file}` : 'Radarr has no file for it'));
}

async function replaceStart(g, target = 1080) {
  const modal = $('#modal');
  const key = 'loose:' + g.folder;
  const box = h('div', { id: 'dgbox', 'data-key': key }, h('div', { class: 'muted' }, 'Starting search…'));
  const a = g.arr;
  fill(modal, h('div', { class: 'modal-box wide', role: 'dialog', 'aria-modal': 'true' },
    h('h2', null, `Find a replacement for ${a.title}${a.year ? ` (${a.year})` : ''}`),
    h('div', { class: 'ink2', style: 'font-size:13px' }, a.has_file
      ? `Plex can't play what Radarr tracks here (${a.file}). Radarr swaps it for the release you pick and deletes ${a.file} once the new file imports.`
      : 'Radarr has no file for this movie, so the release you pick lands next to what is in the folder now. Delete the old files afterwards with a cleanup script.'),
    h('div', { class: 'd-actions' }, h('span', { class: 'muted', style: 'font-size:12px' }, 'Resolution'),
      h('div', { class: 'seg' }, ...D_TIERS.map(x => h('button', { class: x === target ? 'on' : '', onclick: () => replaceStart(g, x) }, `${x}p`)))),
    box,
    h('div', { class: 'modal-actions' }, h('button', { class: 'btn', onclick: () => { modal.hidden = true; } }, 'Close'))));
  modal.hidden = false;
  try {
    const job = await post('/api/downgrade/search', { folder: g.folder, target });
    dgWatch(job.id, key, () => !modal.hidden && $('#dgbox')?.dataset.key === key);
  } catch (e) { fill(box, h('div', { class: 'callout' }, e.message)); }
}
const D_TIERS = [1080, 720];

/* ------------------------------------------------ not in plex: cleanup script */
const CLEAN_PREF = 'reclaim.cleanup';
function cleanupBuilder(d) {
  const modal = $('#modal');
  let pref = {};
  try { pref = JSON.parse(localStorage.getItem(CLEAN_PREF) || '{}') || {}; } catch { pref = {}; }
  const st = { shell: pref.shell === 'powershell' ? 'powershell' : 'bash', action: pref.action === 'move' ? 'move' : 'delete',
    maps: pref.maps || {}, holding: pref.holding || {} };
  const save = () => { try { localStorage.setItem(CLEAN_PREF, JSON.stringify(st)); } catch { /* private window: fine */ } };
  const files = [...LOOSE.sel].map(([path, v]) => ({ path, size: v.size, cat: v.cat, folder: v.folder, root: v.root }));
  const nearTitle = [...LOOSE.sel.values()].filter(v => v.titled && v.cat === 'sidecar').length;
  const roots = [...new Set(files.map(f => f.root))];
  const covered = (r, m) => Object.keys(m).some(k => { const f = k.replace(/\/+$/, ''); return r === f || r.startsWith(f + '/'); });
  const mapFor = shell => {
    const m = { ...(d.script_paths?.[shell] || {}), ...(st.maps[shell] || {}) };
    for (const r of roots) if (!covered(r, m)) m[r] = '';
    return m;
  };
  const defaultHolding = (shell, map) => {
    const sep = Cleanup.SHELLS[shell].sep;
    const first = roots.map(r => Cleanup.mapPath(r, map, sep)).find(Boolean);
    return first ? first.slice(0, first.lastIndexOf(sep)) + sep + '_reclaim-holding' : '';
  };
  const seg = (opts, cur, pick) => h('div', { class: 'seg' }, ...opts.map(([v, label]) =>
    h('button', { class: v === cur ? 'on' : '', onclick: () => { pick(v); save(); paint(); } }, label)));
  const controls = h('div', { class: 'd-actions' });
  const mapBox = h('div');
  const notes = h('div');
  const out = h('pre', { class: 'script', tabindex: '0', 'aria-label': 'Generated script' });
  let built = null;
  const cp = h('button', { class: 'btn', onclick: () => copy(built.text) }, 'Copy');
  const dl = h('button', { class: 'btn primary', onclick: () => {
    // Windows PowerShell 5.1 reads a script without a BOM as ANSI and mangles non-ASCII names
    const blob = new Blob([(st.shell === 'powershell' ? '\ufeff' : '') + built.text], { type: 'text/plain;charset=utf-8' });
    const a = h('a', { href: URL.createObjectURL(blob), download: built.filename });
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  } }, 'Download');
  const paint = () => {
    const map = mapFor(st.shell);
    const holding = st.holding[st.shell] ?? defaultHolding(st.shell, map);
    fill(controls,
      seg([['bash', 'bash (Unraid, Linux)'], ['powershell', 'PowerShell (Windows)']], st.shell, v => { st.shell = v; }),
      seg([['delete', 'Delete'], ['move', 'Move to a holding folder']], st.action, v => { st.action = v; }));
    fill(mapBox,
      h('div', { class: 'muted', style: 'font-size:12px;margin-top:10px' }, 'Plex path → where the script runs'),
      ...Object.entries(map).map(([from, to]) => h('label', { class: 'map-row' },
        h('code', null, from), h('span', { class: 'muted' }, '→'),
        h('input', { type: 'text', value: to, spellcheck: 'false', 'aria-label': `Path for ${from}`,
          placeholder: st.shell === 'bash' ? '/mnt/user/share' : '\\\\server\\share',
          onchange: e => { st.maps[st.shell] = { ...(st.maps[st.shell] || {}), [from]: e.target.value.trim() }; save(); paint(); } }))),
      st.action === 'move' ? h('label', { class: 'map-row' }, h('span', null, 'Holding folder'),
        h('input', { type: 'text', value: holding, spellcheck: 'false', 'aria-label': 'Holding folder',
          onchange: e => { st.holding[st.shell] = e.target.value.trim(); save(); paint(); } })) : null);
    built = Cleanup.build({ shell: st.shell, action: st.action, files, map, holding, walkAt: d.walk_at });
    out.textContent = built.text;
    fill(notes,
      nearTitle ? h('div', { class: 'callout' }, `${plural(nearTitle, 'sidecar')} in this list sit next to a title Plex plays. Those are usually its subtitles and artwork, which Plex uses. Untick them unless that's what you mean.`) : null,
      built.unmapped.length ? h('div', { class: 'callout' }, `${plural(built.unmapped.length, 'file')} left out: set where ${built.unmapped.length === 1 ? 'its' : 'their'} library lives above.`) : null,
      ...built.problems.map(p => h('div', { class: 'callout' }, p)));
    cp.disabled = dl.disabled = !built.count || built.problems.length > 0;
  };
  fill(modal, h('div', { class: 'modal-box wide', role: 'dialog', 'aria-modal': 'true' },
    h('h2', null, 'Cleanup script'),
    h('div', { class: 'ink2', style: 'font-size:13px' }, 'Reclaim only deletes through Plex, and Plex doesn\'t know these files. Run this where they live: a shell on the server, or PowerShell on a PC that reaches the share. It prints the list and asks before touching anything, removes folders it leaves empty, and skips anything already gone.'),
    controls, mapBox, notes, out,
    h('div', { class: 'modal-actions' }, h('button', { class: 'btn', onclick: () => { modal.hidden = true; } }, 'Close'), cp, dl)));
  modal.hidden = false;
  paint();
}

async function renderLog() {
  const el = $('#tab-log');
  const rows = await get('/api/log');
  const freed = rows.filter(r => r.ok).reduce((s, r) => s + r.bytes, 0);
  fill(el, h('div', { class: 'card' },
    h('div', { class: 'card-h' }, h('h3', null, `Deleted · ${plural(rows.filter(r => r.ok).length, 'item')} · ${fmtB(freed)} freed`),
      h('span', { class: 'note' }, 'The record of what you had: title, size, files, who watched and who asked, at the moment it went.')),
    rows.length ? h('table', { class: 'mini' },
      h('tr', null, h('th', null, 'When'), h('th', null, 'What'), h('th', { class: 'n' }, 'Size'), h('th', null, 'Viewers then'),
        h('th', null, 'Requested by'), h('th', null, 'Result')),
      ...rows.map(r => h('tr', null,
        h('td', { title: new Date(r.at * 1000).toLocaleString() }, fmtDate(r.at)),
        h('td', null, r.label, h('div', { class: 'path' }, r.meta.folder || '')),
        h('td', { class: 'n' }, fmtB(r.bytes)),
        h('td', null, (r.meta.viewers || []).map(v => `${v.name} (${v.plays})`).join(', ') || 'no one'),
        h('td', null, (r.meta.requested || []).map(v => v.name).join(', ') || '—'),
        h('td', null, h('span', { class: r.ok ? 'res-ok' : 'res-bad' }, r.ok ? '✓ ' : '✗ '), [r.plex, r.arr].filter(Boolean).join(' · ')))))
      : h('div', { class: 'empty' }, 'Nothing deleted through reclaim yet.')));
}

function showTab(name) {
  $$('.tabs button').forEach(b => b.classList.toggle('on', b.dataset.tab === name));
  $('#tab-library').hidden = name !== 'library';
  $('#tab-loose').hidden = name !== 'loose';
  $('#tab-log').hidden = name !== 'log';
  $('#tab-downgrades').hidden = name !== 'downgrades';
  $('#selbar').hidden = name !== 'library' || !sel.size;
  $('#tab-settings').hidden = name !== 'settings';
  $('#cap').hidden = name === 'settings' || !M;
  if (name === 'settings') renderSettings(true);
  if (name === 'downgrades') renderDowngrades();
  if (name === 'loose') renderLoose();
  if (name === 'log') renderLog();
  if (name === 'library' && M) { drawMap(); drawScatter(); paintRows(); }
  if (name === 'library') libEmpty();
}

/* ------------------------------------------------------------------- boot */
async function pollStatus() {
  try {
    const s = await get('/api/status');
    const was = STATUS;
    STATUS = s;
    renderBuilt();
    if (M && s.generated && s.generated !== M.generated && !s.refreshing) await loadModel();
    if (!M && s.generated) await loadModel();
    if (!M) libEmpty();
    if (was.refreshing !== s.refreshing) renderBuilt();
    const before = DG.active.size;
    await loadDowngrades();
    if (before !== DG.active.size && M) paintRows();
  } catch {}
  setTimeout(pollStatus, STATUS.refreshing ? 1500 : 10000);
}

async function boot() {
  try { CFG = await get('/api/config'); } catch {}
  if (CFG.read_only) document.title = CFG.demo ? 'Reclaim (demo)' : 'Reclaim (read-only)';
  if (CFG.needs_setup) showTab('settings'); else libEmpty();
  wireFilters();
  wireMap();
  wireScatter('scMovie'); wireScatter('scShow');
  $('#tbody').addEventListener('scroll', () => requestAnimationFrame(paintRows), { passive: true });
  $$('.tabs button').forEach(b => b.onclick = () => showTab(b.dataset.tab));
  $('#refresh').onclick = async () => { await post('/api/refresh'); STATUS.refreshing = true; renderBuilt(); setTimeout(pollStatus, 800); };
  $('#dropToggle').onclick = () => { const d = $('#drop'); $('#detail').classList.remove('open'); d.classList.toggle('open'); if (d.classList.contains('open')) renderDrop(); };
  $('#selAdd').onclick = () => { addToDrop([...sel].map(k => ({ kind: byKey.get(k)?.m ? 'movie' : 'show', key: k }))); sel.clear(); afterSel(); };
  $('#selKeep').onclick = () => setKeep([...sel], true);
  $('#selClear').onclick = () => { sel.clear(); afterSel(); };
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
      if (!$('#modal').hidden && !$('#modal .modal-box h2')?.textContent.startsWith('Deleting')) { $('#modal').hidden = true; return; }
      $('#detail').classList.remove('open'); $('#drop').classList.remove('open'); $('#lensPop').hidden = true;
    }
  });
  let rt; new ResizeObserver(() => { clearTimeout(rt); rt = setTimeout(() => { if (M) { drawMap(); drawScatter(); paintRows(); } }, 80); }).observe($('main'));
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => { if (M) redrawMarks(); });
  await loadShortlist();
  await loadDowngrades();
  pollStatus();
}
boot();
