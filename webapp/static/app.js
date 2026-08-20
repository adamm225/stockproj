/* Screener UI.
 *
 * Everything rendered here comes from a single /api/latest read against SQLite,
 * so first paint does not wait on the ~3 minute pipeline. "Run now" starts a
 * background job and polls /api/job; the browser can go away entirely without
 * losing the run, because results are written to disk, not held in the session.
 */

'use strict';

const $ = (sel) => document.querySelector(sel);

/* Served from our own origin — the browser blocks finviz's cross-origin
   redirect chain outright. See webapp/charts.py. */
const CHART_URL = (t) => '/api/chart/' + encodeURIComponent(t);

const LS_KEY = 'screener.settings.v1';
const LS_TAB = 'screener.tab.v1';

const DEFAULTS = {
  max_tickers: 600,
  skip_polygon: false,
  large: false,
  sources: null,      // null = server defaults
  strategies: null,
  s1_price: null,       // [min, max] for Strategy 1; null = its own $0.50–$10 default
  other_price: null,    // [min, max] for Strategies 2–8; null = their own floors
  s1_custom: false,     // is that block in Custom mode? (drawer state only)
  other_custom: false,
};

/* Quick bands per block. `null` means "leave the strategies on their built-in
   gates" — that is what keeps Strategy 1 sub-$10 when you never touch this.
   Strategy 1 is already capped at $10, so its shortcuts move the cap somewhere
   else rather than repeating the default. */
const BAND_PRESETS = {
  s1_price: [
    { id: 'default', label: 'Default (<$10)', band: null },
    { id: 'sub5', label: 'Sub $5', band: [0.5, 5] },
    { id: 'sub20', label: 'Sub $20', band: [0.5, 20] },
  ],
  other_price: [
    { id: 'default', label: 'Default', band: null },
    { id: 'sub10', label: 'Sub $10', band: [0.5, 10] },
    { id: 'sub20', label: 'Sub $20', band: [0.5, 20] },
  ],
};

/* Where the min/max boxes start when you first switch a block to Custom. */
const BAND_SEED = { s1_price: [0.5, 20], other_price: [0.5, 20] };

const state = {
  meta: [],           // strategy registry from /api/strategies
  sources: [],
  defaultSources: [],
  run: null,          // run metadata
  rows: {},           // export key -> rows
  tab: null,          // export key, or '__rate__' / '__heat__'
  settings: { ...DEFAULTS },
  polling: null,
  sort: {},           // export key -> {col, dir}
  rating: null,
  quotes: {},         // ticker -> live Finnhub quote
  quotesAt: null,     // when they were fetched
  quotesBusy: false,
  jobRunning: false,  // header button is Stop while this is true
};

/* ── utils ───────────────────────────────── */

function loadSettings() {
  try {
    const raw = JSON.parse(localStorage.getItem(LS_KEY) || '{}');
    state.settings = { ...DEFAULTS, ...raw };
  } catch { state.settings = { ...DEFAULTS }; }
}

function saveSettings() {
  try { localStorage.setItem(LS_KEY, JSON.stringify(state.settings)); } catch {}
}

function ago(iso) {
  if (!iso) return 'never run';
  const secs = (Date.now() - new Date(iso).getTime()) / 1000;
  if (secs < 60) return 'just now';
  const m = Math.floor(secs / 60);
  if (m < 60) return m + 'm ago';
  const h = Math.floor(m / 60);
  if (h < 24) return h + 'h ' + (m % 60) + 'm ago';
  return Math.floor(h / 24) + 'd ago';
}

function num(v) {
  const n = parseFloat(String(v).replace(/[$,%+x]/g, ''));
  return Number.isFinite(n) ? n : null;
}

/* The same thresholds the rich tables and the old Streamlit grid used:
   Strategy 1 scores on 1-10, everything else on 0-100 confidence. */
function tone(col, value) {
  const n = num(value);
  if (n === null) return '';
  if (col === 'Score' || col === 'Strength') {
    return n >= 8 ? 'good' : n >= 5 ? 'warn' : 'dim';
  }
  if (col === 'Confidence') {
    return n >= 75 ? 'good' : n >= 55 ? 'warn' : 'dim';
  }
  return '';
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

async function getJSON(url) {
  const r = await fetch(url, { headers: { Accept: 'application/json' } });
  if (!r.ok) throw new Error(url + ' → ' + r.status);
  return r.json();
}

/* ── boot ────────────────────────────────── */

async function boot() {
  loadSettings();
  try {
    const [meta, latest] = await Promise.all([
      getJSON('/api/strategies'),
      getJSON('/api/latest'),
    ]);
    state.meta = meta.strategies;
    state.sources = meta.sources;
    state.defaultSources = meta.default_sources;
    applyLatest(latest);
    buildDrawer();
  } catch (e) {
    $('#main').innerHTML = '<div class="err">Could not reach the server.<br>' + esc(e.message) + '</div>';
    return;
  }
  state.tab = localStorage.getItem(LS_TAB) || (state.meta[0] && state.meta[0].export);
  renderTabs();
  render();
  wire();
  // If a scan was already running when the page opened (a cron scan, or one
  // this phone started before it slept), pick the progress back up.
  const job = await getJSON('/api/job').catch(() => null);
  if (job && job.status === 'running') startPolling();
}

function applyLatest(payload) {
  state.run = payload.run;
  state.rows = payload.strategies || {};
  const stamp = state.run && (state.run.finished_at || state.run.started_at);
  $('#updated').textContent = state.run ? 'Updated ' + ago(stamp) : 'No scans yet — press Run now';
}

/* ── live quotes (Finnhub, manual refresh only) ──────────────────
   The scan's Price is the setup anchor that Target/Stop derive from, so live
   prices are shown *alongside* it rather than replacing it. Nothing here runs
   on a timer — one tap costs one Finnhub call per visible ticker. */

function visibleTickers() {
  const rows = state.rows[state.tab] || [];
  return [...new Set(rows.map((r) => r.Ticker).filter(Boolean))];
}

async function refreshQuotes() {
  const tickers = visibleTickers();
  if (!tickers.length || state.quotesBusy) return;
  state.quotesBusy = true;
  const btn = $('#btn-quotes');
  if (btn) { btn.disabled = true; btn.textContent = '…'; }
  try {
    const d = await getJSON('/api/quotes?tickers=' + encodeURIComponent(tickers.join(',')));
    state.quotes = { ...state.quotes, ...(d.quotes || {}) };
    state.quotesAt = d.fetched_at;
    if (d.error) alert(d.error);
  } catch (e) {
    alert('Could not fetch live prices: ' + e.message);
  } finally {
    state.quotesBusy = false;
    render();
  }
}

/* Live price vs the scan's anchor price. Returns null when we have no quote. */
function liveFor(row) {
  const q = state.quotes[row.Ticker];
  if (!q) return null;
  const anchor = num(row.Price);
  const drift = anchor ? ((q.price - anchor) / anchor) * 100 : null;
  return {
    price: q.price,
    drift,
    text: '$' + Number(q.price).toFixed(2),
    driftText: drift === null ? '' : (drift >= 0 ? '+' : '') + drift.toFixed(1) + '%',
    cls: drift === null ? '' : drift > 0.05 ? 'up' : drift < -0.05 ? 'down' : '',
  };
}

/* ── tabs ────────────────────────────────── */

function tabList() {
  // Short names keep 3-4 tabs on screen at phone width; the full label still
  // heads the list below.
  const tabs = state.meta.map((m) => ({
    id: m.export,
    label: m.short || m.label,
    short: 'S' + m.key,
    count: (state.rows[m.export] || []).length,
  }));
  if ((state.rows.heat_gainers || []).length || (state.rows.s6_sectors || []).length) {
    tabs.push({ id: '__heat__', label: 'Heat', short: '', count: null });
  }
  tabs.push({ id: '__rate__', label: 'Rate', short: '', count: null });
  return tabs;
}

function renderTabs() {
  const tabs = tabList();
  if (!tabs.some((t) => t.id === state.tab)) state.tab = tabs[0].id;
  $('#tabs').innerHTML = tabs.map((t) => `
    <button class="tab" role="tab" data-tab="${esc(t.id)}"
            aria-selected="${t.id === state.tab}">
      ${t.short ? esc(t.short) + ' · ' : ''}${esc(t.label)}
      ${t.count !== null ? `<span class="count">${t.count}</span>` : ''}
    </button>`).join('');

  $('#tabs').querySelectorAll('.tab').forEach((el) => {
    el.onclick = () => {
      state.tab = el.dataset.tab;
      try { localStorage.setItem(LS_TAB, state.tab); } catch {}
      renderTabs();
      render();
      window.scrollTo({ top: 0 });
    };
  });
}

/* ── render ──────────────────────────────── */

function render() {
  const main = $('#main');
  if (state.tab === '__rate__') return renderRate(main);
  if (state.tab === '__heat__') return renderHeat(main);

  const meta = state.meta.find((m) => m.export === state.tab);
  const rows = state.rows[state.tab] || [];

  let html = `<div class="strat-head">
      <h2>${esc(meta ? meta.label : state.tab)}</h2>
      <p>${esc(meta ? meta.subtitle : '')}</p>
      ${runBandNote(meta)}
    </div>
    <div class="livebar">
      <button id="btn-quotes" class="ghost-btn live-btn">↻ Live prices</button>
      <span class="live-note">${state.quotesAt
        ? 'Finnhub · ' + esc(ago(state.quotesAt))
        : 'Prices below are from the scan'}</span>
    </div>`;

  if (!rows.length) {
    html += state.run
      ? '<div class="empty">No qualifying setups for this strategy in the last scan.</div>'
      : '<div class="empty">No scans yet. Press <b>Run now</b> to build the first one.</div>';
    main.innerHTML = html;
    return;
  }

  html += renderCards(rows, meta) + renderTable(rows, state.tab);
  main.innerHTML = html;
  wireRows(main);
  const qb = $('#btn-quotes');
  if (qb) qb.onclick = refreshQuotes;
}

/* Phone view: the 4-6 fields that matter, then tap to expand the rest. */
function renderCards(rows, meta) {
  const fields = (meta && meta.card) || [];
  const cards = rows.map((row, i) => {
    const tkr = row.Ticker || '—';
    const badgeCol = (meta && meta.badge) || ('Score' in row ? 'Score' : 'Confidence');
    const badgeVal = row[badgeCol];
    const summary = fields
      .filter((f) => f in row && String(row[f]).trim() !== '')
      .slice(0, 3)
      .map((f) => `<span>${esc(f)} <b>${esc(row[f])}</b></span>`)
      .join('');

    const kv = Object.entries(row)
      .filter(([k]) => k !== 'Ticker')
      .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`)
      .join('');

    const live = liveFor(row);
    return `<article class="card" data-idx="${i}" data-ticker="${esc(tkr)}">
      <div class="card-top">
        <span class="tkr">${esc(tkr)}</span>
        ${badgeVal !== undefined
          ? `<span class="badge ${tone(badgeCol, badgeVal)}">${esc(badgeVal)}</span>` : ''}
        <div class="card-summary">${summary}</div>
        ${live ? `<span class="live ${live.cls}">${esc(live.text)}
            <em>${esc(live.driftText)}</em></span>` : ''}
        <span class="chev">▶</span>
      </div>
      <div class="card-body">
        <dl class="kv">${kv}</dl>
        <div class="chart" data-chart></div>
      </div>
    </article>`;
  }).join('');
  return `<div class="cards">${cards}</div>`;
}

/* Desktop view: sortable table, sticky header, pinned ticker column. */
function renderTable(rows, key) {
  const cols = Object.keys(rows[0]);
  const sort = state.sort[key];
  let view = rows;
  if (sort) {
    const dir = sort.dir === 'asc' ? 1 : -1;
    view = rows.slice().sort((a, b) => {
      const x = num(a[sort.col]), y = num(b[sort.col]);
      if (x !== null && y !== null) return (x - y) * dir;
      return String(a[sort.col]).localeCompare(String(b[sort.col])) * dir;
    });
  }
  // A Live column is spliced in right after Price when quotes have been pulled.
  const hasLive = view.some((r) => state.quotes[r.Ticker]);
  const priceAt = cols.indexOf('Price');
  const outCols = cols.slice();
  if (hasLive && priceAt !== -1) outCols.splice(priceAt + 1, 0, 'Live');

  const head = outCols.map((c) =>
    `<th data-col="${esc(c)}">${esc(c)}${sort && sort.col === c
      ? `<span class="sort">${sort.dir === 'asc' ? '▲' : '▼'}</span>` : ''}</th>`).join('');
  const body = view.map((row) => {
    const live = liveFor(row);
    const tds = outCols.map((c) => {
      if (c === 'Live') {
        return live
          ? `<td class="live-cell ${live.cls}">${esc(live.text)}
               <em>${esc(live.driftText)}</em></td>`
          : '<td class="dim">—</td>';
      }
      const t = tone(c, row[c]);
      return `<td${t ? ` class="${t}"` : ''}>${esc(row[c])}</td>`;
    }).join('');
    return `<tr data-ticker="${esc(row.Ticker || '')}">${tds}</tr>`;
  }).join('');
  return `<div class="table-wrap"><table data-sortkey="${esc(key)}">
      <thead><tr>${head}</tr></thead>
      <tbody>${body}</tbody>
    </table></div>`;
}

function chartHTML(tkr) {
  return `<img src="${CHART_URL(tkr)}" alt="${esc(tkr)} daily chart" loading="lazy"
     onerror="this.style.display='none';this.nextElementSibling.textContent='Chart unavailable.'">
   <p class="cap">${esc(tkr)} — daily</p>`;
}

function wireRows(root) {
  // Cards: tap anywhere on the header to expand; the chart loads on first open
  // so a 20-row list doesn't pull 20 images over wifi up front.
  root.querySelectorAll('.card').forEach((card) => {
    card.querySelector('.card-top').onclick = () => {
      card.classList.toggle('open');
      const slot = card.querySelector('[data-chart]');
      if (card.classList.contains('open') && !slot.dataset.loaded) {
        slot.dataset.loaded = '1';
        slot.innerHTML = chartHTML(card.dataset.ticker);
      }
    };
  });

  // Table: click a header to sort, click a ticker cell to drop the chart inline.
  root.querySelectorAll('thead th').forEach((th) => {
    th.onclick = () => {
      const col = th.dataset.col;
      const key = th.closest('table').dataset.sortkey;
      const cur = state.sort[key];
      state.sort[key] =
        cur && cur.col === col && cur.dir === 'desc' ? { col, dir: 'asc' } : { col, dir: 'desc' };
      render();
    };
  });

  root.querySelectorAll('tbody tr').forEach((tr) => {
    const first = tr.firstElementChild;
    if (!first) return;
    first.onclick = () => {
      const next = tr.nextElementSibling;
      if (next && next.classList.contains('chart-row')) { next.remove(); return; }
      root.querySelectorAll('tr.chart-row').forEach((r) => r.remove());
      const span = tr.children.length;
      const row = document.createElement('tr');
      row.className = 'chart-row';
      row.innerHTML = `<td colspan="${span}"><div class="chart">${chartHTML(tr.dataset.ticker)}</div></td>`;
      tr.after(row);
    };
  });
}

/* ── sector heat ─────────────────────────── */

function renderHeat(main) {
  const blocks = [
    ['Sector leaderboard', state.rows.s6_sectors],
    ['Top gaining industries', state.rows.heat_gainers],
    ['Top losing industries', state.rows.heat_losers],
  ].filter(([, rows]) => (rows || []).length);

  if (!blocks.length) {
    main.innerHTML = '<div class="empty">No sector data in the last scan. Run Strategy 1 or 6 to populate it.</div>';
    return;
  }
  main.innerHTML = blocks.map(([title, rows]) => `
    <div class="strat-head"><h2>${esc(title)}</h2></div>
    <div class="cards">${rows.map((r) => `
      <article class="card"><div class="card-top" style="cursor:default">
        <span class="tkr">${esc(r.Sector || r.Industry || r.Ticker || '—')}</span>
        <div class="card-summary">${Object.entries(r)
          .filter(([k]) => !['Sector', 'Industry', 'Ticker'].includes(k))
          .map(([k, v]) => `<span>${esc(k)} <b>${esc(v)}</b></span>`).join('')}</div>
      </div></article>`).join('')}</div>
    ${renderTable(rows, 'heat:' + title)}`).join('');
  wireRows(main);
}

/* ── rate a ticker ───────────────────────── */

function renderRate(main) {
  const r = state.rating;
  let html = `<div class="rate-form">
      <input id="in-ticker" type="text" placeholder="e.g. NVDA" autocapitalize="characters"
             autocomplete="off" spellcheck="false" enterkeyhint="go">
      <button id="btn-rate" class="run-btn">Rate</button>
    </div>
    <p style="color:var(--fg-dim);font-size:13px;margin:0 0 18px">
      Scores the ticker against all 8 strategies with entry gates relaxed, so every
      strategy returns a number, then reports the best fit. Takes about 15 seconds.
    </p>`;

  if (r) {
    const scored = r.ratings.filter((x) => x.confidence !== null);
    html += `<div class="rate-hd">
        <h2>${esc(r.ticker)} — ${esc(r.company || '')}</h2>
        <p>${esc(r.sector || '—')}${r.industry ? ' · ' + esc(r.industry) : ''}
           · rated ${esc(ago(r.rated_at))}</p>
      </div>`;
    if (!scored.length) {
      html += '<div class="empty">Could not score this ticker on any strategy (insufficient price history).</div>';
    } else {
      html += '<div class="cards">' + r.ratings.map((x) => {
        const kv = x.row
          ? Object.entries(x.row).filter(([k]) => k !== 'Ticker')
              .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')
          : '<dt>—</dt><dd>This strategy could not score the ticker.</dd>';
        return `<article class="card" data-ticker="${esc(r.ticker)}">
          <div class="card-top">
            <span class="tkr" style="font-size:14px">S${x.key}</span>
            <span class="badge ${tone('Confidence', x.confidence)}">${
              x.confidence === null ? '—' : esc(x.confidence)}</span>
            <div class="card-summary"><span>${esc(x.label)}</span></div>
            <span class="chev">▶</span>
          </div>
          <div class="card-body"><dl class="kv">${kv}</dl></div>
        </article>`;
      }).join('') + '</div>';
    }
  }

  main.innerHTML = html;
  main.querySelectorAll('.card .card-top').forEach((top) => {
    top.onclick = () => top.parentElement.classList.toggle('open');
  });

  const input = $('#in-ticker');
  const go = async () => {
    const t = (input.value || '').trim().toUpperCase();
    if (!t) return;
    await startRating(t);
  };
  $('#btn-rate').onclick = go;
  input.onkeydown = (e) => { if (e.key === 'Enter') go(); };
}

async function startRating(ticker) {
  const res = await fetch('/api/rate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ticker }),
  });
  if (res.status === 409) { alert('A scan is already running — try again when it finishes.'); return; }
  if (!res.ok) { alert('Could not rate ' + ticker); return; }
  startPolling(ticker);
}

/* ── run / poll ──────────────────────────── */

/* The header button is Run now when idle and Stop while a job runs — one
   control, so there is never a dead "Run" sitting next to a live scan. */
function setRunButton(job) {
  const btn = $('#btn-run');
  const running = !!job && job.status === 'running';
  btn.classList.toggle('stopping', running);
  btn.disabled = running && !!job.stopping;
  btn.textContent = running ? (job.stopping ? 'Stopping…' : 'Stop') : 'Run now';
  state.jobRunning = running;
}

function setProgress(job) {
  const box = $('#progress');
  setRunButton(job);
  if (!job || job.status !== 'running') { box.classList.add('hidden'); return; }
  box.classList.remove('hidden');
  $('#bar-fill').style.width = Math.round((job.pct || 0) * 100) + '%';
  $('#prog-detail').textContent = job.stopping
    ? 'Stopping…'
    : (job.detail || job.phase || 'working…');
}

async function stopScan() {
  const btn = $('#btn-run');
  btn.disabled = true;
  btn.textContent = 'Stopping…';
  const res = await fetch('/api/job/stop', { method: 'POST' }).catch(() => null);
  // 409 means it already finished on its own — the poller will catch up.
  if (!res || (!res.ok && res.status !== 409)) {
    btn.disabled = false;
    btn.textContent = 'Stop';
    alert('Could not stop the job.');
  }
}

function startPolling(ratingTicker) {
  if (state.polling) clearInterval(state.polling);
  const tick = async () => {
    let job;
    try { job = await getJSON('/api/job'); } catch { return; }
    setProgress(job);
    if (job.status === 'running') return;

    clearInterval(state.polling);
    state.polling = null;
    setProgress(null);

    if (job.status === 'error') {
      $('#prog-detail').textContent = '';
      alert('Job failed: ' + (job.error || 'unknown error'));
      return;
    }
    if (job.status === 'cancelled') {
      // Nothing was written for this run, so leave the last good scan on screen.
      $('#updated').textContent = 'Stopped — showing the previous run';
      setTimeout(() => {
        if (state.run) $('#updated').textContent = 'Updated ' + ago(state.run.finished_at || state.run.started_at);
      }, 4000);
      return;
    }
    const wantTicker = ratingTicker || (job.kind === 'rate' ? job.ticker : null);
    if (wantTicker) {
      state.rating = await getJSON('/api/rating/' + encodeURIComponent(wantTicker)).catch(() => null);
      state.tab = '__rate__';
      renderTabs();
      render();
    } else {
      applyLatest(await getJSON('/api/latest'));
      renderTabs();
      render();
    }
  };
  tick();
  state.polling = setInterval(tick, 2000);
}

async function runScan() {
  const s = state.settings;
  setRunButton({ status: 'running', kind: 'scan' });   // before the round-trip
  // A dropped connection here must hand the button back — otherwise it sits on
  // "Stop" for a scan that never started.
  const res = await fetch('/api/scan', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      sources: s.sources,
      strategies: s.strategies,
      max_tickers: s.max_tickers,
      skip_polygon: s.skip_polygon,
      large: s.large,
      s1_price: s.s1_price,
      other_price: s.other_price,
    }),
  }).catch(() => null);
  if (res && res.status === 409) { startPolling(); return; }
  if (!res || !res.ok) { setRunButton(null); alert('Could not start the scan.'); return; }
  startPolling();
}

/* Price band the *displayed* run actually used — so the rows on screen are read
   against the right band, not whatever is currently set in the drawer. */
function runBandNote(meta) {
  const p = (state.run && state.run.params) || {};
  const band = meta && meta.key === 1 ? p.s1_price : p.other_price;
  if (!band) return '';
  return `<p class="band-note">Scanned ${esc(bandLabel(band, ''))}</p>`;
}


/* ── price bands ─────────────────────────── */

const sameBand = (a, b) => JSON.stringify(a || null) === JSON.stringify(b || null);

function bandLabel(band, fallback) {
  if (!band) return fallback;
  const money = (v) => (v == null ? 'any' : '$' + (Number.isInteger(v) ? v : v.toFixed(2)));
  return money(band[0]) + ' – ' + money(band[1]);
}

/* Wires one price-band block (Strategy 1, or Strategies 2–8) to a settings key.
   Both blocks are identical apart from which key they write and what "Default"
   means for them, so they share this.

   Custom is its own stored flag, never inferred from the band: a hand-typed
   $0.50–$10 is numerically identical to the Sub $10 preset, so inferring the
   mode would silently snap the block back out of Custom and hide the boxes. */
function buildBand(prefix, key, fallbackNote) {
  const presets = BAND_PRESETS[key];
  const customKey = prefix + '_custom';
  const band = state.settings[key];
  const custom = !!state.settings[customKey];

  $('#' + prefix + '-presets').innerHTML = presets.map((p) =>
    `<button class="chip" data-preset="${p.id}"
       aria-pressed="${!custom && sameBand(p.band, band)}">${esc(p.label)}</button>`).join('')
    + `<button class="chip" data-preset="custom" aria-pressed="${custom}">Custom</button>`;

  const box = $('#' + prefix + '-custom');
  box.classList.toggle('hidden', !custom);
  const minEl = $('#in-' + prefix + '-min');
  const maxEl = $('#in-' + prefix + '-max');
  minEl.value = band && band[0] != null ? band[0] : '';
  maxEl.value = band && band[1] != null ? band[1] : '';
  $('#' + prefix + '-band-note').textContent = 'Using ' + bandLabel(band, fallbackNote);

  $('#' + prefix + '-presets').querySelectorAll('.chip').forEach((el) => {
    el.onclick = () => {
      const id = el.dataset.preset;
      if (id === 'custom') {
        state.settings[customKey] = true;
        // Start from whatever band is active, so Custom opens where the last
        // preset left off rather than empty.
        state.settings[key] = band ? [...band] : [...BAND_SEED[key]];
      } else {
        state.settings[customKey] = false;
        const p = presets.find((x) => x.id === id);
        state.settings[key] = p.band ? [...p.band] : null;
      }
      saveSettings();
      buildBand(prefix, key, fallbackNote);
      if (state.settings[customKey]) minEl.focus();
    };
  });

  const readCustom = () => {
    // A blank box means "no bound"; anything unparseable is treated the same
    // way rather than sending NaN to the server.
    const num = (el) => {
      const v = Number(el.value);
      return el.value.trim() === '' || !Number.isFinite(v) ? null : Math.max(0, v);
    };
    const lo = num(minEl);
    const hi = num(maxEl);
    state.settings[key] = lo == null && hi == null ? null : [lo, hi];
    saveSettings();
    $('#' + prefix + '-band-note').textContent =
      'Using ' + bandLabel(state.settings[key], fallbackNote);
  };
  minEl.oninput = readCustom;
  maxEl.oninput = readCustom;
}

/* ── settings drawer ─────────────────────── */

function buildDrawer() {
  const s = state.settings;
  $('#in-max').value = s.max_tickers;
  $('#max-val').textContent = s.max_tickers;
  $('#in-skip-polygon').checked = s.skip_polygon;
  $('#in-large').checked = s.large;

  buildBand('s1', 's1_price', 'Strategy 1 default ($0.50 – $10)');
  buildBand('other', 'other_price', 'each strategy\u2019s own default floor');

  const activeSources = s.sources || state.defaultSources;
  $('#src-list').innerHTML = state.sources.map((src) =>
    `<button class="chip" data-src="${esc(src)}"
       aria-pressed="${activeSources.includes(src)}">${esc(src)}</button>`).join('');

  const activeStrats = s.strategies || state.meta.map((m) => m.key);
  $('#strat-list').innerHTML = state.meta.map((m) =>
    `<button class="chip" data-strat="${m.key}"
       aria-pressed="${activeStrats.includes(m.key)}">S${m.key} ${esc(m.label.replace(/ *\(.*\)/, ''))}</button>`).join('');

  $('#src-list').querySelectorAll('.chip').forEach((el) => {
    el.onclick = () => {
      const on = el.getAttribute('aria-pressed') !== 'true';
      el.setAttribute('aria-pressed', on);
      const cur = new Set(state.settings.sources || state.defaultSources);
      on ? cur.add(el.dataset.src) : cur.delete(el.dataset.src);
      state.settings.sources = [...cur];
      saveSettings();
    };
  });
  $('#strat-list').querySelectorAll('.chip').forEach((el) => {
    el.onclick = () => {
      const on = el.getAttribute('aria-pressed') !== 'true';
      el.setAttribute('aria-pressed', on);
      const cur = new Set(state.settings.strategies || state.meta.map((m) => m.key));
      const k = Number(el.dataset.strat);
      on ? cur.add(k) : cur.delete(k);
      state.settings.strategies = [...cur].sort((a, b) => a - b);
      saveSettings();
    };
  });
}

function wire() {
  $('#btn-run').onclick = () => (state.jobRunning ? stopScan() : runScan());

  const openDrawer = (open) => {
    $('#drawer').classList.toggle('hidden', !open);
    $('#drawer-scrim').classList.toggle('hidden', !open);
  };
  $('#btn-settings').onclick = () => openDrawer(true);
  $('#btn-close-settings').onclick = () => openDrawer(false);
  $('#drawer-scrim').onclick = () => openDrawer(false);

  $('#in-max').oninput = (e) => {
    state.settings.max_tickers = Number(e.target.value);
    $('#max-val').textContent = e.target.value;
    saveSettings();
  };
  $('#in-skip-polygon').onchange = (e) => {
    state.settings.skip_polygon = e.target.checked; saveSettings();
  };
  $('#in-large').onchange = (e) => {
    state.settings.large = e.target.checked; saveSettings();
  };
  $('#btn-reset').onclick = () => {
    state.settings = { ...DEFAULTS };
    saveSettings();
    buildDrawer();
  };

  // Keep the "Updated 14m ago" label honest without re-fetching anything.
  setInterval(() => {
    if (!state.run) return;
    $('#updated').textContent = 'Updated ' + ago(state.run.finished_at || state.run.started_at);
  }, 30000);
}

boot();
