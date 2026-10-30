'use strict';
/* Fleet Charge Controller: yard UI. Renders the real simulator output from /api/data. */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const fmt = (n, d = 0) => Number(n).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
const dur = (h) => { const m = Math.max(0, Math.round(h * 60)); return Math.floor(m / 60) + 'h ' + String(m % 60).padStart(2, '0') + 'm'; };

const AWAY = 0, WAITING = 1, MOVING = 2, CHARGING = 3, UNPLUG = 4, READY = 5, GONE = 6;
const POLICIES = ['smart', 'fcfs', 'edf', 'ideal'];
const ZDEF = [
  { key: 'e_transit', title: 'Van bays', spec: 'Ford E-Transit · 89 kWh · 19.2 kW AC', min: 230, color: 'var(--c-van)' },
  { key: 'vnr_electric', title: 'Regional tractor bays', spec: 'Volvo VNR Electric · ~452 kWh usable · 150 kW DC', min: 300, color: 'var(--c-reg)' },
  { key: 'eactros_600', title: 'Long-haul tractor bays', spec: 'Mercedes-Benz eActros 600 · ~590 kWh usable · 180 kW DC', min: 320, color: 'var(--c-long)' }
];
const GEO = {
  e_transit:    { tw: 104, th: 58, cw: 38, ch: 42, gap: 0, winW: 17, winH: 15, wheels: [22, 104], m: { tw: 40, th: 20, cw: 14, ch: 15, gap: 0, wheels: [6, 38] } },
  vnr_electric: { tw: 168, th: 66, cw: 50, ch: 54, gap: 6, winW: 20, winH: 17, wheels: [12, 38, 132, 176], m: { tw: 58, th: 20, cw: 17, ch: 16, gap: 2, wheels: [4, 14, 50, 62] } },
  eactros_600:  { tw: 186, th: 70, cw: 54, ch: 58, gap: 6, winW: 21, winH: 18, wheels: [12, 38, 150, 200], m: { tw: 66, th: 20, cw: 18, ch: 17, gap: 2, wheels: [4, 14, 56, 72] } }
};
const S = { policy: 'smart', slot: 36, playing: false, speed: 1, tab: 'yard', filter: 'all', timer: null, loadedVersion: -1 };
let D = null, MILES = null;

/* ---------------------------------------------------------------- API */
async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const j = await r.json();
  if (!r.ok || j.error) throw new Error(j.error || r.statusText);
  return j;
}
function toast(msg, ms = 4200) { const t = $('#toast'); t.textContent = msg; t.hidden = false; clearTimeout(toast.h); toast.h = setTimeout(() => (t.hidden = true), ms); }

async function waitForRun(title) {
  $('#overlay').hidden = false; $('#ovTitle').textContent = title || 'Simulating the depot day';
  for (;;) {
    const s = await api('/api/status');
    const p = s.progress;
    $('#ovLabel').textContent = p.label === 'done' ? 'Done' : p.label;
    $('#ovBar').style.width = (p.done / p.total * 100) + '%';
    if (s.error) { $('#overlay').hidden = true; throw new Error(s.error); }
    if (!s.running && s.version !== S.loadedVersion) break;
    await new Promise((r) => setTimeout(r, 600));
  }
  D = await api('/api/data');
  S.loadedVersion = D.version;
  MILES = milestones();
  $('#overlay').hidden = true;
  syncInputs(); buildTimeControls(); renderAll();
}
async function runSim(title) {
  try {
    await api('/api/run', { seed: Number($('#seed').value), temp: Number($('#temp').value), fault: Number($('#fault').value) });
    S.loadedVersion = -2;
    await waitForRun(title);
  } catch (e) { $('#overlay').hidden = true; toast('Simulation failed: ' + e.message); }
}
function syncInputs() { $('#seed').value = D.seed; $('#temp').value = Math.round(D.temp_c); $('#fault').value = String(D.fault); }

/* ---------------------------------------------------------------- milestones on the scrubber */
function milestones() {
  const sol = D.solar, pr = D.price;
  const solarPeak = sol.indexOf(Math.max(...sol));
  const top = pr.indexOf(Math.max(...pr));
  let a = top, b = top;
  while (a > 0 && pr[a - 1] >= 0.24) a--;
  while (b < 95 && pr[b + 1] >= 0.24) b++;
  const wave = (key) => { const c = Array(96).fill(0); D.trucks.forEach((t) => (c[t[key]]++)); let best = 0, bi = 0; for (let i = 0; i < 95; i++) { const s = c.slice(Math.max(0, i - 2), i + 3).reduce((x, y) => x + y, 0); if (s > best) { best = s; bi = i; } } return bi; };
  return { solarPeak, tariff: [a, b], arrive: wave('a'), depart: wave('d') };
}
function buildTimeControls() {
  const pct = (s) => (s / 95 * 100).toFixed(2) + '%';
  const m = MILES;
  $('#miles').innerHTML =
    `<div class="band" style="left:${pct(m.tariff[0])};width:${((m.tariff[1] - m.tariff[0]) / 95 * 100).toFixed(2)}%"><span>Peak tariff</span></div>` +
    `<div class="pin sun ${m.solarPeak > 82 ? 'end' : ''}" style="left:${pct(m.solarPeak)}"><span>Solar peak</span></div>` +
    `<div class="pin ${m.arrive > 82 ? 'end' : ''}" style="left:${pct(m.arrive)}"><span>Arrival wave</span></div>` +
    `<div class="pin ${m.depart > 82 ? 'end' : ''}" style="left:${pct(m.depart)}"><span>Departure wave</span></div>`;
  $('#jumps').innerHTML = [['Now', 'now'], ['Peak tariff', 'tariff'], ['Arrival wave', 'arrive'], ['Departure wave', 'depart']]
    .map(([l, k]) => `<button class="jump" data-jump="${k}">${l}</button>`).join('');
}
function jumpTo(k) {
  if (k === 'now') { const d = new Date(); S.slot = Math.floor((((d.getHours() * 60 + d.getMinutes()) - 720 + 1440) % 1440) / 15); }
  else if (k === 'tariff') S.slot = MILES.tariff[0];
  else S.slot = MILES[k];
  renderAll();
}

/* ---------------------------------------------------------------- derived frame */
function truckState(i, slot, pol) {
  const t = D.trucks[i];
  const code = pol.st[i][slot];
  const socR = pol.soc[i][slot];
  const soc = socR < 0 ? 0 : socR / 1000;
  const energy = soc * t.captrue;
  const needH = Math.max(0, t.need - energy) / (0.93 * t.maxkw) * 1.12;
  const slack = (t.d - slot) * 0.25 - needH - 0.5;
  return { t, i, code, soc, energy, needH, slack, fill: Math.min(1, soc) * 100, mark: Math.min(100, t.need / t.captrue * 100), kw: pol.pw[i][slot], bay: pol.bay[i][slot], q: pol.q[i][slot] };
}
const atRisk = (ts) => (ts.code === WAITING && ts.slack < 0) || (ts.code === CHARGING && ts.late);
const matches = (ts) => S.filter === 'all' || (S.filter === 'risk' && atRisk(ts)) || (S.filter === 'high' && ts.soc > 0.8);
function frame(slot, pol) {
  const tot = { onsite: 0, gone: 0, waiting: 0, bays: 0, ready: 0, risk: 0, high: 0 };
  const zones = ZDEF.map((z) => {
    const nb = D.bays[z.key];
    const bays = Array.from({ length: nb }, (_, b) => ({ n: b + 1, ts: null }));
    const queue = [], ready = []; let gone = 0, short = 0, oldest = 0;
    D.trucks.forEach((t, i) => {
      if (t.key !== z.key) return;
      const ts = truckState(i, slot, pol);
      if (ts.code === GONE) { gone++; const last = pol.soc[i][t.d - 1] / 1000 * t.captrue; if (last < t.need - 0.5) short++; return; }
      if (ts.code === AWAY) return;
      tot.onsite++;
      if (ts.soc > 0.8) tot.high++;
      if (ts.code === READY) { tot.ready++; ready.push(ts); }
      else if (ts.code === WAITING) { tot.waiting++; queue.push(ts); if (ts.slack < 0) tot.risk++; oldest = Math.max(oldest, (slot - t.a) * 0.25); }
      else {
        tot.bays++;
        ts.etaIdx = slot + Math.ceil(ts.needH / 0.25);
        ts.late = ts.code === CHARGING && ts.etaIdx >= t.d - 1; if (ts.late) tot.risk++;
        if (ts.bay >= 0 && bays[ts.bay]) bays[ts.bay].ts = ts;
      }
    });
    queue.sort((a, b) => a.q - b.q); ready.sort((a, b) => a.t.d - b.t.d);
    tot.gone += gone;
    return { z, bays, queue, ready, gone, short, oldest, inUse: bays.filter((b) => b.ts).length };
  });
  return { zones, tot };
}

/* ---------------------------------------------------------------- truck drawings */
function truckHTML(g, fill, mark, fcls) {
  const w = g.tw + g.gap + g.cw, h = g.th + 10;
  let s = `<div class="truck" style="width:${w}px;height:${h}px"><div class="trailer" style="width:${g.tw}px;height:${g.th}px"><div class="fillbar ${fcls}" style="height:${fill.toFixed(1)}%"></div><div class="mark" style="bottom:${mark.toFixed(1)}%"></div></div>`;
  s += `<div class="cab" style="left:${g.tw + g.gap}px;top:${g.th - g.ch}px;width:${g.cw}px;height:${g.ch}px"><div class="glass" style="width:${g.winW}px;height:${g.winH}px"></div></div>`;
  g.wheels.forEach((x) => (s += `<div class="wheel" style="left:${x}px;top:${g.th - 8}px"></div>`));
  return s + '</div>';
}
function miniHTML(m, fill, mark) {
  let s = `<div class="mini" style="height:${m.th + 4}px"><div class="trailer" style="width:${m.tw}px;height:${m.th}px"><div class="fillbar" style="height:${fill.toFixed(1)}%"></div><div class="mark" style="bottom:${mark.toFixed(1)}%"></div></div>`;
  s += `<div class="cab" style="left:${m.tw + m.gap}px;top:${m.th - m.ch}px;width:${m.cw}px;height:${m.ch}px"></div>`;
  m.wheels.forEach((x) => (s += `<div class="wheel" style="left:${x}px;top:${m.th - 4}px"></div>`));
  return s + '</div>';
}
const BOLT = '<svg width="9" height="9" viewBox="0 0 24 24" fill="#fff"><path d="M13 2 4 14h7l-1 8 9-12h-7z"/></svg>';

function bayHTML(z, b) {
  const g = GEO[z.key];
  if (!b.ts) {
    return `<div class="bay empty"><div class="bay-top"><div><div class="baylabel">BAY ${b.n}</div><div class="vid" style="color:var(--muted)">Available</div><div class="model">Next truck from the queue</div></div></div>
      <div class="stage" style="min-height:${g.th + 16}px"><div class="post">${BOLT}</div><div class="cable"></div></div></div>`;
  }
  const ts = b.ts, t = ts.t, dim = matches(ts) ? '' : ' dim', lit = S.filter !== 'all' && matches(ts) ? ' lit' : '';
  let cls = 'charging', pill = 'CHARGING', fcls = 'charging', eta = '', etaCls = '', remain = '';
  if (ts.code === MOVING) { cls = 'moving'; pill = 'PLUGGING IN'; fcls = ''; eta = 'Connecting'; remain = 'Plug-in under way'; }
  else if (ts.code === UNPLUG) { cls = 'unplug'; pill = 'UNPLUGGING'; fcls = 'ready'; eta = 'Done, heading to gate'; remain = 'Target reached'; }
  else {
    const at = ts.etaIdx > 95 ? 'after 12:00' : D.clocks[ts.etaIdx];
    eta = ts.late ? 'Will miss: ETA ' + at : 'Ready ~' + at; etaCls = ts.late ? 'eta-bad' : '';
    remain = ts.needH < 0.02 ? 'Target reached' : `${dur(ts.needH)} to ${Math.round(ts.mark)}%`;
  }
  return `<button class="bay ${cls}${dim}${lit}" data-vid="${t.vid}" aria-label="Open ${t.vid}">
    <div class="bay-top"><div style="min-width:0"><div class="baylabel">BAY ${b.n}</div><div class="vid mono">${t.vid}</div><div class="model">${esc(t.model)}</div></div><span class="pill ${cls}">${pill}</span></div>
    <div class="stage" style="min-height:${g.th + 16}px"><div class="post">${BOLT}</div><div class="cable"></div>${truckHTML(g, ts.fill, ts.mark, fcls)}</div>
    <div class="bay-bot"><div><div class="pct mono">${Math.round(ts.fill)}%</div><div class="mono small">needs ${Math.round(ts.mark)}% · ${ts.code === CHARGING ? fmt(ts.kw) + ' kW' : '0 kW'}</div></div>
      <div class="small" style="text-align:right"><div class="mono bay-time" style="color:var(--fg)">${remain}</div><div class="mono ${etaCls}">${eta}</div><div class="mono">departs ${D.clocks[t.d]}</div></div></div></button>`;
}
function queueRowHTML(z, ts) {
  const t = ts.t, m = GEO[z.key].m;
  const risk = ts.slack < 0 ? 'bad' : ts.slack < 1 ? 'tight' : '';
  const chip = ts.slack < 0 ? 'AT RISK' : (ts.slack < 1 ? 'TIGHT ' : 'SLACK ') + dur(ts.slack);
  const waited = (S.slot - t.a) * 0.25, dim = matches(ts) ? '' : ' dim', lit = S.filter !== 'all' && matches(ts) ? ' lit' : '';
  return `<button class="qrow ${risk}${dim}${lit}" data-vid="${t.vid}"><div class="qline1"><div class="qpos mono">#${ts.q}</div>
    <div style="width:92px;flex:none">${miniHTML(m, ts.fill, ts.mark)}</div>
    <div style="min-width:0;flex:1"><div class="mono b" style="font-size:13px">${t.vid}</div><div class="model" style="font-size:11px">${esc(t.model)}</div></div>
    <span class="chip">${chip}</span></div>
    <div class="qline2 mono">${Math.round(ts.fill)}% now · needs ${Math.round(ts.mark)}% · charge takes ~${dur(ts.needH)} · waited ${dur(waited)} · departs ${D.clocks[t.d]}</div></button>`;
}

/* ---------------------------------------------------------------- site power chart (dual axis) */
function siteChart(svgEl, pol, slot) {
  const L = 54, R = 58, T = 8, H = 120, W = 1400 - L - R, YM = 1600, PM = 0.8;
  const X = (i) => L + i / 95 * W, Y = (v) => T + H - Math.min(v, YM) / YM * H, YP = (p) => T + H - Math.min(p, PM) / PM * H;
  const lim = D.limit;
  let sa = `M${L},${T + H}`, la = `M${L},${T + H}`, lp = '', gp = '', pp = '', danger = '';
  for (let i = 0; i < 96; i++) {
    const x = X(i).toFixed(1);
    sa += ` L${x},${Y(D.solar[i]).toFixed(1)}`; la += ` L${x},${Y(pol.load[i]).toFixed(1)}`;
    lp += `${i ? ' L' : 'M'}${x},${Y(pol.load[i]).toFixed(1)}`; gp += `${i ? ' L' : 'M'}${x},${Y(pol.grid[i]).toFixed(1)}`;
    pp += `${i ? ' L' : 'M'}${x},${YP(D.price[i]).toFixed(1)}`;
  }
  sa += ` L${X(95)},${T + H} Z`; la += ` L${X(95)},${T + H} Z`;
  // shade the headroom between grid import and the limit wherever import is within 30% of the limit
  let run = [];
  const flush = () => { if (run.length > 1) { danger += 'M' + run.map((i) => `${X(i).toFixed(1)},${Y(pol.grid[i]).toFixed(1)}`).join(' L') + ' L' + [...run].reverse().map((i) => `${X(i).toFixed(1)},${Y(lim).toFixed(1)}`).join(' L') + ' Z '; } run = []; };
  for (let i = 0; i < 96; i++) { if (pol.grid[i] >= 0.7 * lim) run.push(i); else flush(); }
  flush();
  const peak = Math.max(...pol.grid), pk = pol.grid.indexOf(peak);
  let ax = '';
  for (let k = 0; k <= 4; k++) {
    const y = T + H - k / 4 * H;
    ax += `<line x1="${L}" x2="${L + W}" y1="${y}" y2="${y}" stroke="var(--line)"/><text x="${L - 8}" y="${y + 4}" text-anchor="end" class="m mono">${fmt(k * 400)}</text><text x="${L + W + 8}" y="${y + 4}" class="mono" style="fill:var(--bad)">$${(k * 0.2).toFixed(2)}</text>`;
  }
  ax += `<text x="${L - 8}" y="${T - 1}" text-anchor="end" class="m mono">kW</text><text x="${L + W + 8}" y="${T - 1}" class="mono" style="fill:var(--bad)">$/kWh</text>`;
  const tick = (i, a = 'middle') => `<text x="${X(i)}" y="${T + H + 20}" class="m mono" text-anchor="${a}">${D.clocks[i]}</text>`;
  const cx = X(slot).toFixed(1), cy = Y(pol.load[slot]).toFixed(1), ly = Y(lim).toFixed(1);
  svgEl.innerHTML = `${ax}<path d="${sa}" fill="var(--solar)" opacity=".75"/><path d="${la}" fill="var(--accent)" opacity=".16"/><path d="${danger}" fill="var(--bad)" opacity=".35"/>
    <path d="${lp}" fill="none" stroke="var(--accent)" stroke-width="2"/><path d="${gp}" fill="none" stroke="var(--ink)" stroke-width="1.4"/><path d="${pp}" fill="none" stroke="var(--bad)" stroke-width="1.6"/>
    <line x1="${L}" x2="${L + W}" y1="${ly}" y2="${ly}" stroke="var(--muted)" stroke-dasharray="5 4"/><text x="${L + W - 4}" y="${Number(ly) - 5}" text-anchor="end" class="m mono">grid limit ${fmt(lim)} kW</text>
    <line x1="${L}" x2="${L + W}" y1="${Y(peak)}" y2="${Y(peak)}" stroke="var(--ink)" stroke-dasharray="2 3" opacity=".6"/><text x="${X(pk)}" y="${Y(peak) - 5}" text-anchor="middle" class="mono b">peak ${fmt(peak)} kW sets the demand charge</text>
    <line x1="${cx}" x2="${cx}" y1="${T}" y2="${T + H}" stroke="var(--ink)" stroke-width="1.5"/><circle cx="${cx}" cy="${cy}" r="4" fill="var(--accent)" stroke="var(--surface)" stroke-width="1.5"/>
    ${tick(0, 'start')}${tick(24)}${tick(48)}${tick(72)}${tick(95, 'end')}`;
}

/* ---------------------------------------------------------------- yard view */
function renderYard() {
  const pol = D.pol[S.policy], slot = S.slot, f = frame(slot, pol);
  const tot = f.tot, nBays = Object.values(D.bays).reduce((a, b) => a + b, 0);
  const kp = [
    ['Trucks on site', `${tot.onsite}<small> of 50 · ${tot.gone} left</small>`, ''],
    ['Waiting for a bay', tot.waiting, 'color:var(--slate-text)'],
    ['Bays in use', `${tot.bays}<small> / ${nBays} bays</small>`, 'color:var(--warn)'],
    ['Ready to dispatch', tot.ready, 'color:var(--good)'],
    ['At risk of missing departure', tot.risk, tot.risk ? 'color:var(--bad-text)' : ''],
    ['Site load · price', `${fmt(pol.load[slot])}<small> kW · $${D.price[slot].toFixed(3)}</small>`, '']
  ];
  $('#kpis').innerHTML = kp.map(([l, n, st]) => `<div class="card kpi"><div class="l">${l}</div><div class="n mono" style="${st}">${n}</div></div>`).join('');
  $('#riskCount').textContent = tot.risk; $('#highCount').textContent = tot.high;
  $$('.chipbtn').forEach((b) => b.classList.toggle('on', b.dataset.filter === S.filter));
  $('#pools').innerHTML = f.zones.map((zz) => {
    const z = zz.z;
    const q = zz.queue.length ? zz.queue.map((ts) => queueRowHTML(z, ts)).join('') : '<div class="nothing">No trucks waiting. Every arrival has a bay or has already finished.</div>';
    const r = zz.ready.length ? zz.ready.map((ts) => `<button class="rchip${matches(ts) ? '' : ' dim'}" data-vid="${ts.t.vid}"><i></i><b class="mono">${ts.t.vid}</b><span class="mono">${Math.round(ts.soc * 100)}% · leaves ${D.clocks[ts.t.d]}</span></button>`).join('') : '<div class="nothing">Nothing ready yet.</div>';
    return `<section class="pool"><div class="pool-head"><div><h2>${z.title}</h2><span class="small">${z.spec}</span></div>
      <span class="mono small stats">${zz.inUse} of ${D.bays[z.key]} bays in use · ${zz.queue.length} waiting · ${zz.ready.length} ready · ${zz.gone} dispatched${zz.short ? ' (' + zz.short + ' left short)' : ''}</span></div>
      <div class="pool-body"><div class="bays" style="--bay-min:${z.min}px">${zz.bays.map((b) => bayHTML(z, b)).join('')}</div>
      <div class="right-col"><div class="card panel"><div class="panel-head"><h3>Waiting for a bay</h3><span class="mono small">${zz.queue.length} in queue${zz.queue.length ? ' · oldest ' + dur(zz.oldest) : ''}</span></div><div class="qlist">${q}</div></div>
      <div class="card panel"><div class="panel-head"><h3>Ready lane</h3><span class="mono small">${zz.ready.length} ready to dispatch</span></div><div class="rlist">${r}</div></div></div></div></section>`;
  }).join('');
  siteChart($('#siteChart'), pol, slot);
}
function renderRecStrip() {
  const recs = (D.pol[S.policy].recs || []);
  $('#recBadge').hidden = !recs.length; $('#recBadge').textContent = recs.length;
  $('#recStrip').innerHTML = recs.length ? `<div class="recstrip"><span class="t">${recs.length} recommended action${recs.length > 1 ? 's' : ''}</span><span class="small" style="color:var(--fg)">${esc(recs[0].title)}${recs.length > 1 ? ` and ${recs.length - 1} more` : ''}</span><button class="btn ghost" id="recOpen">Review</button></div>` : '';
  const b = $('#recOpen'); if (b) b.onclick = openNotes;
}

/* ---------------------------------------------------------------- header, schedule, compare */
function renderHeader() {
  const pol = D.pol[S.policy], m = pol.m, base = D.pol.fcfs.m;
  const nB = Object.values(D.bays).reduce((a, b) => a + b, 0);
  $('#subtitle').textContent = `Charging yard · 50 trucks · ${nB} bays in three model pools (${D.bays.e_transit} van, ${D.bays.vnr_electric} regional, ${D.bays.eactros_600} long-haul) · ${D.temp_c.toFixed(0)} °C · grid limit ${fmt(D.limit)} kW`;
  $('#policySeg').innerHTML = POLICIES.map((p) => `<button class="seg ${p === S.policy ? 'on' : ''}" data-policy="${p}" aria-pressed="${p === S.policy}">${esc(D.labels[p])}</button>`).join('');
  const dcv = (m.total_cost / base.total_cost - 1) * 100;
  const dc = S.policy === 'fcfs' ? '' : `<div class="d">${dcv >= 0 ? '+' : ''}${fmt(dcv)}% vs first come</div>`;
  $('#summary').innerHTML = `<div><div class="l">Cost / day</div><div class="v mono">$${fmt(m.total_cost)}</div>${dc}</div>
    <div><div class="l">Peak grid</div><div class="v mono">${fmt(m.peak_kw)} kW</div></div>
    <div><div class="l">Missed departures</div><div class="v mono">${m.missed} / 50</div></div>
    <div><div class="l">Mean wait</div><div class="v mono">${m.mean_wait_h.toFixed(1)} h</div></div>`;
}
let GANTT = null;
function renderSchedule() {
  const pol = D.pol[S.policy], rows = [];
  ZDEF.forEach((z) => { for (let b = 0; b < D.bays[z.key]; b++) rows.push([z.key, b]); });
  const x0 = 110, W = 1260, rh = 30, X = (t) => x0 + t / 96 * W;
  let s = '';
  [0, 12, 24, 36, 48, 60, 72, 84, 96].forEach((t) => { s += `<line x1="${X(t)}" x2="${X(t)}" y1="8" y2="${8 + rows.length * rh}" stroke="var(--line)"/><text x="${X(t)}" y="${8 + rows.length * rh + 18}" text-anchor="middle" class="m mono">${D.clocks[Math.min(t, 95)]}</text>`; });
  const nm = { e_transit: 'Van', vnr_electric: 'Regional', eactros_600: 'Long-haul' };
  rows.forEach(([k, b], i) => { s += `<text x="${x0 - 10}" y="${8 + i * rh + 19}" text-anchor="end" class="m mono">${nm[k]} bay ${b + 1}</text><line x1="${x0}" x2="${x0 + W}" y1="${8 + (i + 1) * rh}" y2="${8 + (i + 1) * rh}" stroke="var(--line)"/>`; });
  const col = Object.fromEntries(ZDEF.map((z) => [z.key, z.color]));
  D.trucks.forEach((t, i) => {
    const row = pol.bay[i]; let a = 0;
    while (a < 96) {
      if (row[a] >= 0) {
        let e = a; while (e + 1 < 96 && row[e + 1] === row[a]) e++;
        const ri = rows.findIndex((r) => r[0] === t.key && r[1] === row[a]); const w = X(e + 1) - X(a);
        if (ri >= 0) s += `<g class="gb" data-vid="${t.vid}" data-a="${a}" data-e="${e + 1}" style="cursor:pointer"><rect x="${X(a) + 1}" y="${8 + ri * rh + 3}" width="${Math.max(w - 2, 2)}" height="${rh - 6}" rx="3" fill="${col[t.key]}"/>${w > 30 ? `<text x="${X(a) + w / 2}" y="${8 + ri * rh + 20}" text-anchor="middle" class="mono" style="fill:#0F1B2D;pointer-events:none">${t.vid}</text>` : ''}</g>`;
        a = e + 1;
      } else a++;
    }
  });
  const h = rows.length * rh;
  s += `<line id="gcur" x1="${X(S.slot)}" x2="${X(S.slot)}" y1="2" y2="${h + 10}" stroke="var(--ink)" stroke-width="1.6"/>`;
  GANTT = { X };
  const g = $('#gantt'); g.setAttribute('viewBox', `0 0 1400 ${h + 34}`); g.innerHTML = s;
  $('#queues').innerHTML = ZDEF.map((z) => {
    const idx = D.trucks.map((t, i) => (t.key === z.key ? i : -1)).filter((i) => i >= 0);
    const cnt = (p) => Array.from({ length: 96 }, (_, t) => idx.filter((i) => D.pol[p].st[i][t] === WAITING).length);
    const a = cnt('fcfs'), b = cnt(S.policy), mx = Math.max(2, ...a, ...b), x1 = 34, w = 380, hh = 110, y0 = 130;
    const X2 = (i) => x1 + i / 95 * w, Y2 = (v) => y0 - v / mx * hh;
    const step = (arr) => arr.map((v, i) => `${i ? 'L' : 'M'}${X2(i).toFixed(1)},${Y2(v).toFixed(1)} L${X2(Math.min(i + 1, 95)).toFixed(1)},${Y2(v).toFixed(1)}`).join(' ');
    let sv = `<text x="${x1}" y="12" class="b" style="font-size:13px">${z.title.replace(' bays', ' pool')}</text><line x1="${x1}" x2="${x1 + w}" y1="${y0}" y2="${y0}" stroke="var(--line)"/>`;
    for (let v = 0; v <= mx; v += Math.max(1, Math.ceil(mx / 4))) sv += `<line x1="${x1}" x2="${x1 + w}" y1="${Y2(v)}" y2="${Y2(v)}" stroke="var(--line)"/><text x="${x1 - 6}" y="${Y2(v) + 4}" text-anchor="end" class="m mono">${v}</text>`;
    [0, 24, 48, 72, 95].forEach((i) => (sv += `<text x="${X2(i)}" y="${y0 + 16}" text-anchor="middle" class="m mono">${D.clocks[i]}</text>`));
    sv += `<path d="${step(a)}" fill="none" stroke="var(--c-fcfs)" stroke-width="2"/><path d="${step(b)}" fill="none" stroke="var(--c-smart)" stroke-width="2.2"/><line x1="${X2(S.slot)}" x2="${X2(S.slot)}" y1="14" y2="${y0}" stroke="var(--ink)" stroke-width="1.3"/>`;
    return `<svg viewBox="0 0 430 150">${sv}</svg>`;
  }).join('');
  siteChart($('#siteChart2'), pol, S.slot);
}
function moveCursors() {   // cheap update while scrubbing or playing in the schedule tab
  if (S.tab !== 'schedule' || !GANTT) return;
  const g = $('#gcur'); if (g) { const x = GANTT.X(S.slot); g.setAttribute('x1', x); g.setAttribute('x2', x); }
  siteChart($('#siteChart2'), D.pol[S.policy], S.slot);
  $$('#queues svg').forEach((svg) => { const ln = svg.lastElementChild; if (ln && ln.tagName === 'line') { const x = 34 + S.slot / 95 * 380; ln.setAttribute('x1', x); ln.setAttribute('x2', x); } });
}
function renderCompare() {
  const rows = [['total_cost', 'Total cost per day', (v) => '$' + fmt(v)], ['energy_cost', 'Energy', (v) => '$' + fmt(v)], ['demand_cost', 'Demand charge', (v) => '$' + fmt(v)], ['health_cost', 'Battery wear', (v) => '$' + fmt(v)], ['peak_kw', 'Peak grid import (kW)', fmt], ['missed', 'Missed departures', fmt], ['mean_wait_h', 'Mean wait for a bay (h)', (v) => fmt(v, 1)], ['max_wait_h', 'Longest wait (h)', (v) => fmt(v, 1)], ['bay_utilisation', 'Bay utilisation', (v) => fmt(v * 100) + '%'], ['solar_self_consumption', 'Solar used on site', (v) => fmt(v * 100) + '%'], ['overcharge_kwh', 'Energy beyond requirement (kWh)', (v) => fmt(v, 1)], ['plan_time_s', 'Solver time (s)', (v) => fmt(v, 1)]];
  $('#cmpTable').innerHTML = `<thead><tr><th>Metric</th>${POLICIES.map((p) => `<th class="n">${esc(D.labels[p])}</th>`).join('')}</tr></thead><tbody>` +
    rows.map(([k, n, f]) => `<tr><td>${n}</td>${POLICIES.map((p) => `<td class="n">${f(D.pol[p].m[k])}</td>`).join('')}</tr>`).join('') + '</tbody>';
  const x0 = 220, W = 1100, mx = Math.max(...POLICIES.map((p) => D.pol[p].m.total_cost)) * 1.1, sc = W / mx;
  let s = '';
  POLICIES.forEach((p, i) => {
    const m = D.pol[p].m, y = 14 + i * 56, e = m.energy_cost, d = m.demand_cost, h = m.health_cost;
    s += `<text x="${x0 - 12}" y="${y + 24}" text-anchor="end" class="b" style="font-size:13px">${esc(D.labels[p])}</text>
      <rect x="${x0}" y="${y}" width="${e * sc}" height="34" fill="var(--accent)"/><rect x="${x0 + e * sc}" y="${y}" width="${d * sc}" height="34" fill="var(--warn-fill)"/><rect x="${x0 + (e + d) * sc}" y="${y}" width="${h * sc}" height="34" fill="var(--good-fill)"/>
      <text x="${x0 + (e + d + h) * sc + 10}" y="${y + 23}" class="mono b" style="font-size:13px">$${fmt(m.total_cost)}</text>`;
  });
  $('#costChart').innerHTML = s;
  const cards = [
    ['Average charge rate', 'Energy-weighted C-rate. High rates heat the pack and age it faster.', 'avg_c_rate', (v) => v.toFixed(2) + ' C'],
    ['Time sitting above 80% charge', 'Truck-hours spent nearly full. Full batteries age fastest when they idle.', 'hi_soc_dwell_h', (v) => fmt(v) + ' h'],
    ['Energy charged above 0.15 C', 'Share of kWh delivered in the faster, costlier wear tiers.', 'fast_charge_share', (v) => fmt(v * 100) + '%'],
    ['Battery wear cost', 'The proxy used by the planner (rate tiers plus dwell).', 'health_cost', (v) => '$' + fmt(v)]
  ];
  $('#stress').innerHTML = '<div class="stress-grid">' + cards.map(([t, h, k, f]) => {
    const mx = Math.max(...POLICIES.map((p) => D.pol[p].m[k])) || 1;
    return `<div class="sm"><h3>${t}</h3><div class="hint">${h}</div>` + POLICIES.map((p) => `<div class="mrow"><span>${esc(D.labels[p].replace(' (a charger per truck)', ''))}</span><span class="tr"><i style="width:${(D.pol[p].m[k] / mx * 100).toFixed(1)}%;background:${p === 'smart' ? 'var(--c-smart)' : p === 'fcfs' ? 'var(--c-fcfs)' : p === 'edf' ? '#7A6CC0' : 'var(--slate)'}"></i></span><span class="v">${f(D.pol[p].m[k])}</span></div>`).join('') + '</div>';
  }).join('') + '</div>';
}
function renderAll() {
  if (!D) return;
  renderHeader(); renderRecStrip();
  $$('.view').forEach((v) => (v.hidden = v.id !== 'view-' + S.tab));
  $$('.tab').forEach((t) => t.classList.toggle('on', t.dataset.tab === S.tab));
  $('#timebar').hidden = S.tab === 'compare';
  $('#clock').textContent = D.clocks[S.slot]; $('#slider').value = S.slot;
  $('#daylabel').textContent = S.slot < 48 ? 'Day 1 (afternoon and evening)' : 'Day 2 (overnight and morning)';
  if (S.tab === 'yard') renderYard(); else if (S.tab === 'schedule') renderSchedule(); else renderCompare();
  const ul = $('#logList'); if (ul) ul.innerHTML = (D.log || []).map((l) => `<li>${esc(l)}</li>`).join('') || '<li>None yet</li>';
  renderRecList();
}
function tick() { // time changed: update only what depends on time
  $('#clock').textContent = D.clocks[S.slot]; $('#slider').value = S.slot;
  $('#daylabel').textContent = S.slot < 48 ? 'Day 1 (afternoon and evening)' : 'Day 2 (overnight and morning)';
  if (S.tab === 'yard') renderYard(); else moveCursors();
}

/* ---------------------------------------------------------------- dispatcher drawer */
function renderRecList() {
  const recs = D.pol[S.policy].recs || [];
  $('#recList').innerHTML = recs.length ? recs.map((r, ri) => `<div class="rec ${r.severity}"><h4>${esc(r.title)}</h4><p>${esc(r.detail)}</p>
    <div class="opts">${r.options.map((o, oi) => `<button class="opt" data-rec="${ri}:${oi}">${esc(o.label)}</button>`).join('') || '<span class="small">Operator action, not modelled.</span>'}</div></div>`).join('')
    : '<div class="empty-note">Nothing needs attention. Every truck is on track to leave on time with the energy it needs.</div>';
}
function openNotes() { $('#notesDrawer').hidden = false; $('#scrim').hidden = false; renderRecList(); }

/* ---------------------------------------------------------------- truck drawer */
async function openTruck(vid) {
  const i = D.trucks.findIndex((t) => t.vid === vid), t = D.trucks[i], pol = D.pol[S.policy];
  const dr = $('#truckDrawer'); dr.hidden = false; $('#scrim').hidden = false;
  dr.innerHTML = `<div class="dh"><div><h2>${t.vid} · ${esc(t.model)}</h2><div class="small">Loading energy requirement…</div></div><button class="x" data-close>Close</button></div>`;
  let info; try { info = await api('/api/truck?vid=' + vid); } catch (e) { dr.innerHTML += `<p class="small">${esc(e.message)}</p>`; return; }
  const q = info.requirement;
  const parts = [['Rolling', q.rolling, 'var(--accent)'], ['Aero', q.aero, '#7A6CC0'], ['Stop-go', q.stop_go, 'var(--slate)'], ['Climb', q.climb, 'var(--c-van)'], ['Aux and HVAC', q.aux, 'var(--solar)'], ['Margin', q.margin, 'var(--warn-fill)'], ['Reserve', q.reserve, 'var(--good-fill)']];
  const bw = 560; let x = 0, bar = '', lg = '';
  parts.forEach(([n, v, c]) => { const w = v / q.required * bw; bar += `<rect x="${x}" y="8" width="${w}" height="34" fill="${c}"/>${w > 36 ? `<text x="${x + w / 2}" y="30" text-anchor="middle" class="mono b" style="fill:#0F1B2D">${fmt(v)}</text>` : ''}`; x += w; lg += `<span><i style="background:${c}"></i>${n} ${fmt(v)}</span>`; });
  const wait = (pol.events.find((e) => e.topic === 'docking' && e.vid === vid) || {}).waited_h;
  const ev = pol.events.filter((e) => e.vid === vid && e.topic !== 'plan_shortfall');
  const W = 576, H = 120, X = (k) => k / 95 * W, Y = (v) => H - Math.min(v, 1) * H;
  const line = (p, col, w) => { let d = '', pen = false; D.pol[p].soc[i].forEach((v, k) => { if (v < 0) { pen = false; return; } d += `${pen ? 'L' : 'M'}${X(k).toFixed(1)},${Y(v / 1000).toFixed(1)} `; pen = true; }); return `<path d="${d}" fill="none" stroke="${col}" stroke-width="${w}"/>`; };
  const bands = pol.st[i].map((c, k) => (c >= 1 && c <= 5 ? `<rect x="${X(k)}" y="${H + 8}" width="${W / 95 + .5}" height="9" fill="${{ 1: 'var(--slate)', 2: 'var(--warn-fill)', 3: 'var(--warn-fill)', 4: 'var(--good-fill)', 5: 'var(--good-fill)' }[c]}"/>` : '')).join('');
  let grid = ''; for (let v = 0; v <= 4; v++) grid += `<line x1="0" x2="${W}" y1="${Y(v / 4)}" y2="${Y(v / 4)}" stroke="var(--line)"/><text x="-6" y="${Y(v / 4) + 4}" text-anchor="end" class="m mono">${v * 25}%</text>`;
  const ticks = [0, 24, 48, 72, 95].map((k) => `<text x="${X(k)}" y="${H + 32}" text-anchor="middle" class="m mono">${D.clocks[k]}</text>`).join('');
  const mk = t.need / t.captrue;
  dr.innerHTML = `<div class="dh"><div><h2>${t.vid} · ${esc(t.model)}</h2><div class="small">${esc(D.labels[S.policy])}</div></div><button class="x" data-close>Close</button></div>
    <div class="facts">
      <div class="fact"><div class="l">Round trip</div><div class="v mono">${t.route} km</div></div><div class="fact"><div class="l">Cargo</div><div class="v mono">${fmt(t.payload)} kg</div></div><div class="fact"><div class="l">Terrain · weather</div><div class="v mono">${t.elev} m/km · ${info.temp_c.toFixed(0)} °C</div></div>
      <div class="fact"><div class="l">Battery health</div><div class="v mono">${(t.soh * 100).toFixed(0)}% <span class="small">(reports ${(t.soh_rep * 100).toFixed(0)}%)</span></div></div><div class="fact"><div class="l">Arrives → departs</div><div class="v mono">${D.clocks[t.a]} → ${D.clocks[t.d]}</div></div><div class="fact"><div class="l">Waited for a bay</div><div class="v mono">${wait !== undefined ? dur(wait) : 'n/a'}</div></div></div>
    <h3>Energy the trip needs: ${fmt(q.required)} kWh (${Math.round(mk * 100)}% of the battery)</h3>
    <svg viewBox="0 0 ${bw} 50" style="max-width:${bw}px">${bar}</svg><div class="legend">${lg}</div>
    <div class="small">Trip alone ${fmt(q.total)} kWh (${q.kwh_per_km.toFixed(2)} kWh/km). Margin is 15% of the trip, reserve is 10% of usable capacity. Reported usable capacity ${fmt(info.cap_plan)} kWh.</div>
    <h3>Charge level through the day</h3>
    <svg viewBox="-30 -4 ${W + 40} ${H + 44}">${grid}<line x1="0" x2="${W}" y1="${Y(mk)}" y2="${Y(mk)}" stroke="var(--ink)" stroke-dasharray="5 4"/><text x="${W}" y="${Y(mk) - 5}" text-anchor="end" class="mono">needs ${Math.round(mk * 100)}%</text>
      ${S.policy !== 'fcfs' ? line('fcfs', 'var(--c-fcfs)', 1.8) : ''}${line(S.policy, 'var(--c-smart)', 2.6)}${bands}${ticks}</svg>
    <div class="legend"><span><i style="background:var(--c-smart)"></i>${esc(D.labels[S.policy])}</span>${S.policy !== 'fcfs' ? '<span><i style="background:var(--c-fcfs)"></i>First come, first served</span>' : ''}<span><i style="background:var(--slate)"></i>waiting</span><span><i style="background:var(--warn-fill)"></i>in bay</span><span><i style="background:var(--good-fill)"></i>done</span></div>
    <h3>Timeline</h3><div class="tl">${ev.map((e) => `<div class="t">${e.time}</div><div>${({ docking: `Docking, bay ${e.bay}, waited ${dur(e.waited_h)}`, plugged_in: `Plugged in, bay ${e.bay}`, ready: 'Ready at the gate', departure: e.on_time ? 'Dispatched on time' : 'Left short, state ' + e.state, charger_fault: 'Charger fault', charger_restored: 'Charger restored' }[e.topic] || e.topic)}</div>`).join('') || '<div class="t">-</div><div>No events</div>'}</div>
    <h3>Ask the controller</h3><input type="text" id="askQ" value="Why did ${t.vid} wait and charge when it did?"><div class="row"><button class="btn" id="askBtn">Explain</button><span class="small">${info.llm ? 'Nemotron on Token Factory' : 'Offline template (no NEBIUS_API_KEY)'}</span></div><div id="askOut"></div>`;
  $('#askBtn').onclick = async () => {
    $('#askOut').innerHTML = '<div class="small">Asking Nemotron…</div>';
    try { const r = await api('/api/explain', { vid, question: $('#askQ').value, policy: S.policy }); $('#askOut').innerHTML = `<div class="answer">${esc(r.answer)}</div><div class="small">source: ${esc(r.source)}</div>`; }
    catch (e) { $('#askOut').innerHTML = `<div class="small">${esc(e.message)}</div>`; }
  };
}
function closeDrawers() { $$('.drawer').forEach((d) => (d.hidden = true)); $('#scrim').hidden = true; }

/* ---------------------------------------------------------------- gantt tooltip */
function showTip(e, el) {
  const i = D.trucks.findIndex((t) => t.vid === el.dataset.vid), t = D.trucks[i], pol = D.pol[S.policy];
  const a = Number(el.dataset.a), b = Number(el.dataset.e);
  const arriveSoc = pol.soc[i][t.a] >= 0 ? pol.soc[i][t.a] / 1000 * t.captrue / t.captrue : 0;
  const endSoc = pol.soc[i][Math.min(95, b)] / 1000;
  const slice = pol.pw[i].slice(a, b).filter((v) => v > 0);
  const avg = slice.length ? slice.reduce((x, y) => x + y, 0) / slice.length : 0, peak = slice.length ? Math.max(...slice) : 0;
  const tip = $('#tip');
  tip.innerHTML = `<b>${t.vid} · ${esc(t.model)}</b><div class="g"><span>Bay visit</span><span>${D.clocks[a]} to ${D.clocks[Math.min(95, b)]}</span><span>Arrived with</span><span>${Math.round(arriveSoc * 100)}% charge</span><span>Target</span><span>${Math.round(t.need / t.captrue * 100)}% (${fmt(t.need)} kWh)</span><span>Charged to</span><span>${Math.round(endSoc * 100)}%</span><span>Power</span><span>avg ${fmt(avg)} kW, peak ${fmt(peak)} kW</span><span>Departure deadline</span><span>${D.clocks[t.d]}</span></div>`;
  tip.hidden = false;
  const pad = 14, w = tip.offsetWidth, h = tip.offsetHeight;
  tip.style.left = Math.min(e.clientX + pad, innerWidth - w - 8) + 'px';
  tip.style.top = Math.min(e.clientY + pad, innerHeight - h - 8) + 'px';
}

/* ---------------------------------------------------------------- events */
document.addEventListener('click', async (e) => {
  const el = e.target.closest('[data-vid],[data-policy],[data-tab],[data-speed],[data-close],[data-filter],[data-jump],[data-preset],[data-rec]');
  if (!el) return;
  if (el.dataset.close !== undefined) return closeDrawers();
  if (el.dataset.vid) return openTruck(el.dataset.vid);
  if (el.dataset.policy) { S.policy = el.dataset.policy; return renderAll(); }
  if (el.dataset.tab) { S.tab = el.dataset.tab; return renderAll(); }
  if (el.dataset.filter) { S.filter = S.filter === el.dataset.filter && S.filter !== 'all' ? 'all' : el.dataset.filter; return renderAll(); }
  if (el.dataset.jump) return jumpTo(el.dataset.jump);
  if (el.dataset.speed) { S.speed = Number(el.dataset.speed); $$('.spd').forEach((b) => b.classList.toggle('on', b === el)); if (S.playing) startPlay(); return; }
  if (el.dataset.preset) {
    try { const r = await api('/api/preset', { name: el.dataset.preset }); toast(r.lines[0], 5200); closeDrawers(); await runSim('Re-planning the day'); }
    catch (err) { toast('Could not apply: ' + err.message); }
    return;
  }
  if (el.dataset.rec) {
    const [ri, oi] = el.dataset.rec.split(':').map(Number), opt = D.pol[S.policy].recs[ri].options[oi];
    try { const r = await api('/api/apply', { actions: opt.actions }); toast(r.lines.join(' · ') || 'Applied', 5200); closeDrawers(); await runSim('Re-planning with your change'); }
    catch (err) { toast('Could not apply: ' + err.message); }
  }
});
$('#scrim').onclick = closeDrawers;
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDrawers(); });
$('#slider').oninput = (e) => { S.slot = Number(e.target.value); tick(); };
function startPlay() { clearInterval(S.timer); S.timer = setInterval(() => { S.slot = S.slot >= 95 ? 0 : S.slot + 1; tick(); }, 520 / S.speed); }
$('#playBtn').onclick = () => { S.playing = !S.playing; $('#playBtn').textContent = S.playing ? 'Pause' : 'Play day'; if (S.playing) startPlay(); else clearInterval(S.timer); };
$('#runBtn').onclick = () => runSim();
$('#notesBtn').onclick = openNotes;
$('#gantt').addEventListener('mousemove', (e) => { const g = e.target.closest('.gb'); if (g) showTip(e, g); else $('#tip').hidden = true; });
$('#gantt').addEventListener('mouseleave', () => ($('#tip').hidden = true));
$('#notesApply').onclick = async () => {
  try {
    const r = await api('/api/notes', { text: $('#notesText').value });
    $('#notesSrc').textContent = 'parsed by ' + r.source;
    $('#notesOut').innerHTML = r.lines.length ? '<ul>' + r.lines.map((l) => `<li>${esc(l)}</li>`).join('') + '</ul>' : '<div class="small">No actions recognised.</div>';
    if (r.lines.length) { await new Promise((ok) => setTimeout(ok, 4000)); closeDrawers(); await runSim('Re-planning with your notes'); }
  } catch (e) { toast('Could not apply notes: ' + e.message); }
};
$('#weatherBtn').onclick = async () => {
  try {
    toast('Looking up live weather for Chicago…');
    const w = await api('/api/weather', { city: 'Chicago' });
    toast(`Chicago: ${w.temp_c.toFixed(1)} °C (${w.source}). Re-planning with the new energy requirement.`, 5200);
    await runSim('Re-planning for the new temperature');
  } catch (e) { toast('Weather lookup failed: ' + e.message); }
};

/* ---------------------------------------------------------------- boot */
(async () => { try { await waitForRun('Simulating the depot day'); } catch (e) { toast('Could not start: ' + e.message); } })();
