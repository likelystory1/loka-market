/* ── utils ───────────────────────────────────────────────────────────────── */

function formatName(raw) {
  return raw.split('_').map(w => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase()).join(' ');
}

function formatPrice(n) {
  if (n == null) return '—';
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + 'M';
  if (n >= 1_000)     return (n / 1_000).toFixed(1) + 'K';
  return Number(n).toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 2 });
}

const BLOCK_TEXTURE_ITEMS = new Set([
  'shulker_box','white_shulker_box','orange_shulker_box','magenta_shulker_box',
  'light_blue_shulker_box','yellow_shulker_box','lime_shulker_box','pink_shulker_box',
  'gray_shulker_box','light_gray_shulker_box','cyan_shulker_box','purple_shulker_box',
  'blue_shulker_box','brown_shulker_box','green_shulker_box','red_shulker_box',
  'black_shulker_box',
]);

function iconUrl(name) {
  const slug   = name.toLowerCase().replace(/ /g, '_');
  const folder = BLOCK_TEXTURE_ITEMS.has(slug) ? 'block' : 'item';
  return `https://raw.githubusercontent.com/InventivetalentDev/minecraft-assets/1.21.4/assets/minecraft/textures/${folder}/${slug}.png`;
}

function avatarUrl(uid) {
  return `https://mc-heads.net/avatar/${uid}/20`;
}

function clamp(v, lo = 0, hi = 100) { return Math.max(lo, Math.min(hi, v)); }

/* ── price meter builder ─────────────────────────────────────────────────── */
/**
 * Returns an HTML string for a compact color-coded price meter.
 * Red → Yellow → Green → Yellow → Red mapping:
 *   fence_lo … zone_low  = red→yellow (below fair)
 *   zone_low … zone_high = green      (fair zone)
 *   zone_high … fence_hi = yellow→red (above fair)
 * White marker shows last_price position.
 */
function buildMeter(fenceLo, fenceHi, zoneLow, zoneHigh, lastPrice, trackClass, markerClass) {
  const range = fenceHi - fenceLo;
  if (!range || range <= 0) return '';

  const pLow  = clamp((zoneLow  - fenceLo) / range * 100);
  const pHigh = clamp((zoneHigh - fenceLo) / range * 100);
  const pMark = clamp((lastPrice - fenceLo) / range * 100);

  const grad = [
    `#ef4444 0%`,
    `#f59e0b ${pLow.toFixed(1)}%`,
    `#10b981 ${pLow.toFixed(1)}%`,
    `#10b981 ${pHigh.toFixed(1)}%`,
    `#f59e0b ${pHigh.toFixed(1)}%`,
    `#ef4444 100%`,
  ].join(', ');

  return `
    <div class="${trackClass}" style="background: linear-gradient(to right, ${grad})">
      <div class="${markerClass}" style="left:${pMark.toFixed(1)}%"
           title="Last sold: ${formatPrice(lastPrice)}"></div>
    </div>`;
}

/* ── global stats strip ──────────────────────────────────────────────────── */
async function loadStats() {
  try {
    const d = await fetch('/api/stats').then(r => r.json());
    document.getElementById('statTrades').textContent  = d.total_trades.toLocaleString();
    document.getElementById('statItems').textContent   = d.unique_items.toLocaleString();
    document.getElementById('statTraders').textContent = d.active_traders.toLocaleString();
  } catch (_) {}
}

/* ══════════════════════════════════════════════════════════════════════════ */
/*  MARKET PAGE                                                                */
/* ══════════════════════════════════════════════════════════════════════════ */

let allItems     = [];
let currentSort  = 'volume';
let sortDir      = -1;
let recentLoaded  = false;
let playersLoaded = false;

/* ── render item grid ───────────────────────────────────────────────────── */
function renderGrid(items) {
  const grid = document.getElementById('itemGrid');
  if (!items.length) {
    grid.innerHTML = '<div class="empty-state">No items match your search.</div>';
    return;
  }
  grid.innerHTML = items.map(item => {
    const zoneLabel  = `Fair Value  ${formatPrice(item.zone_low)} – ${formatPrice(item.zone_high)}`;
    const outlierTip = item.outlier_count > 0
      ? ` title="${item.outlier_count} outlier${item.outlier_count > 1 ? 's' : ''} excluded"`
      : '';
    const meter = buildMeter(
      item.fence_lo, item.fence_hi,
      item.zone_low, item.zone_high,
      item.last_price,
      'card-meter-track', 'card-meter-marker'
    );
    const url = `/item?item=${encodeURIComponent(item.item)}`;
    return `
      <div class="item-card" onclick="location.href='${url}'">
        <div class="card-icon-wrap">
          <img class="card-icon" src="${iconUrl(item.item)}" alt="${formatName(item.item)}"
               onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">
          <div class="card-icon-fallback" style="display:none">${formatName(item.item).charAt(0)}</div>
        </div>
        <div class="card-body">
          <div class="card-name">${formatName(item.item)}</div>
          <div class="card-price">${formatPrice(item.last_price)}</div>
          <div class="card-meta">
            <span class="badge zone"${outlierTip}>${zoneLabel}</span>
            <span class="card-volume">${item.volume.toLocaleString()} trades</span>
          </div>
          <div class="card-meter">${meter}</div>
          <div class="card-range">
            <span class="range-low">↓ ${formatPrice(item.atl)}</span>
            <span class="range-high">↑ ${formatPrice(item.ath)}</span>
          </div>
        </div>
      </div>`;
  }).join('');
}

function applySort() {
  let items = [...allItems];
  const q = document.getElementById('searchInput').value.toLowerCase().trim();
  if (q) {
    items = items.filter(i =>
      i.item.toLowerCase().includes(q) ||
      formatName(i.item).toLowerCase().includes(q)
    );
  }
  items.sort((a, b) => {
    let va, vb;
    switch (currentSort) {
      case 'fmv':  va = a.fmv;    vb = b.fmv;    break;
      case 'name': va = a.item;   vb = b.item;   break;
      default:     va = a.volume; vb = b.volume;
    }
    if (typeof va === 'string') return sortDir * va.localeCompare(vb);
    return sortDir * ((va ?? 0) - (vb ?? 0));
  });
  renderGrid(items);
}

/* ── render recent transactions ─────────────────────────────────────────── */
async function loadRecent() {
  if (recentLoaded) return;
  recentLoaded = true;

  const wrap = document.getElementById('recentWrap');
  try {
    const trades = await fetch('/api/recent').then(r => r.json());
    if (!trades.length) {
      wrap.innerHTML = '<div class="empty-state">No recent trades found.</div>';
      return;
    }

    wrap.innerHTML = `
      <div class="table-wrap">
        <table class="recent-table">
          <thead>
            <tr>
              <th>Item</th>
              <th>Sold For</th>
              <th>Fair Value Zone</th>
              <th>Meter</th>
              <th>Buyer</th>
            </tr>
          </thead>
          <tbody id="recentBody"></tbody>
        </table>
      </div>`;

    const tbody = document.getElementById('recentBody');
    tbody.innerHTML = trades.map(t => {
      const zoneLabel = (t.zone_low != null && t.zone_high != null)
        ? `${formatPrice(t.zone_low)} – ${formatPrice(t.zone_high)}`
        : '—';
      const meter = (t.fence_lo != null && t.fence_hi != null && t.zone_low != null)
        ? buildMeter(t.fence_lo, t.fence_hi, t.zone_low, t.zone_high, t.price,
                     'recent-meter-track', 'recent-meter-marker')
        : '';
      const itemUrl = `/item?item=${encodeURIComponent(t.item)}`;
      return `
        <tr onclick="location.href='${itemUrl}'">
          <td>
            <div class="recent-item-cell">
              <img class="recent-item-icon" src="${iconUrl(t.item)}" alt=""
                   onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">
              <div class="recent-item-icon-fallback" style="display:none">${t.item.charAt(0)}</div>
              <span class="recent-item-name">${formatName(t.item)}</span>
            </div>
          </td>
          <td class="price-cell">${formatPrice(t.price)}</td>
          <td><span class="badge zone">${zoneLabel}</span></td>
          <td class="recent-meter-cell">${meter}</td>
          <td>
            <div class="player-cell">
              <img class="player-avatar" src="${avatarUrl(t.buyer_uuid)}"
                   onerror="this.style.display='none'" alt="">
              <span class="player-name">${t.buyer}</span>
            </div>
          </td>
        </tr>`;
    }).join('');
  } catch (_) {
    wrap.innerHTML = '<div class="empty-state">Failed to load recent trades.</div>';
  }
}

/* ── render player leaderboard ──────────────────────────────────────────── */
async function loadPlayers() {
  if (playersLoaded) return;
  playersLoaded = true;

  const wrap = document.getElementById('playersWrap');
  try {
    const players = await fetch('/api/players').then(r => r.json());
    if (!players.length) {
      wrap.innerHTML = '<div class="empty-state">No player data found.</div>';
      return;
    }

    const rankClass = r => r === 1 ? 'rank-gold' : r === 2 ? 'rank-silver' : r === 3 ? 'rank-bronze' : '';
    const rankLabel = r => r === 1 ? '1ST' : r === 2 ? '2ND' : r === 3 ? '3RD' : `#${r}`;

    wrap.innerHTML = `<div class="player-grid">${
      players.map(p => {
        const favName = p.fav_item ? formatName(p.fav_item) : '—';
        const favIcon = p.fav_item
          ? `<img class="player-fav-icon" src="${iconUrl(p.fav_item)}" onerror="this.style.display='none'" alt="">`
          : '';
        return `
          <div class="player-card ${rankClass(p.rank)}">
            <div class="player-rank">${rankLabel(p.rank)}</div>
            <img class="player-head"
                 src="https://mc-heads.net/avatar/${p.uuid}/56"
                 alt="${p.name}"
                 onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">
            <div class="player-head-fallback" style="display:none">${p.name.charAt(0)}</div>
            <div class="player-info">
              <div class="player-username">${p.name}</div>
              <div class="player-spent">
                ${formatPrice(p.total_spent)}
                <span class="player-spent-label">shards</span>
              </div>
              <div class="player-meta">
                <span class="player-trades">${p.trade_count.toLocaleString()} purchases</span>
                <span class="player-fav">${favIcon}${favName}</span>
              </div>
            </div>
          </div>`;
      }).join('')
    }</div>`;
  } catch (_) {
    wrap.innerHTML = '<div class="empty-state">Failed to load leaderboard.</div>';
  }
}

/* ── render sellers leaderboard ─────────────────────────────────────────── */
let sellersLoaded = false;

async function loadSellers() {
  if (sellersLoaded) return;
  sellersLoaded = true;

  const wrap = document.getElementById('sellersWrap');
  try {
    const sellers = await fetch('/api/sellers').then(r => r.json());
    if (!sellers.length) {
      wrap.innerHTML = '<div class="empty-state">No seller data found.</div>';
      return;
    }

    const rankClass = r => r === 1 ? 'rank-gold' : r === 2 ? 'rank-silver' : r === 3 ? 'rank-bronze' : '';
    const rankLabel = r => r === 1 ? '1ST' : r === 2 ? '2ND' : r === 3 ? '3RD' : `#${r}`;

    wrap.innerHTML = `<div class="player-grid">${
      sellers.map(s => {
        const favName = s.fav_item ? formatName(s.fav_item) : '—';
        const favIcon = s.fav_item
          ? `<img class="player-fav-icon" src="${iconUrl(s.fav_item)}" onerror="this.style.display='none'" alt="">`
          : '';
        return `
          <div class="player-card ${rankClass(s.rank)}">
            <div class="player-rank">${rankLabel(s.rank)}</div>
            <img class="player-head"
                 src="https://mc-heads.net/avatar/${s.uuid}/56"
                 alt="${s.name}"
                 onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">
            <div class="player-head-fallback" style="display:none">${s.name.charAt(0)}</div>
            <div class="player-info">
              <div class="player-username">${s.name}</div>
              <div class="player-spent">
                ${formatPrice(s.total_earned)}
                <span class="player-spent-label">shards earned</span>
              </div>
              <div class="player-meta">
                <span class="player-trades">${s.trade_count.toLocaleString()} sales</span>
                <span class="player-fav">${favIcon}${favName}</span>
              </div>
            </div>
          </div>`;
      }).join('')
    }</div>`;
  } catch (_) {
    wrap.innerHTML = '<div class="empty-state">Failed to load leaderboard.</div>';
  }
}

/* ── tabs ───────────────────────────────────────────────────────────────── */
function initTabs() {
  document.querySelectorAll('.tab').forEach(btn => {
    btn.addEventListener('click', () => {
      const tab = btn.dataset.tab;
      document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      document.getElementById('tab-market').style.display  = tab === 'market'  ? '' : 'none';
      document.getElementById('tab-recent').style.display  = tab === 'recent'  ? '' : 'none';
      document.getElementById('tab-players').style.display = tab === 'players' ? '' : 'none';
      document.getElementById('tab-sellers').style.display = tab === 'sellers' ? '' : 'none';
      if (tab === 'recent')  loadRecent();
      if (tab === 'players') loadPlayers();
      if (tab === 'sellers') loadSellers();
    });
  });
}

/* ── init market ────────────────────────────────────────────────────────── */
async function initIndex() {
  loadStats();
  initTabs();

  const data = await fetch('/api/items').then(r => r.json());
  allItems = data;
  applySort();

  document.getElementById('searchInput').addEventListener('input', applySort);

  document.querySelectorAll('.sort-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const s = btn.dataset.sort;
      sortDir = (currentSort === s) ? sortDir * -1 : (s === 'name' ? 1 : -1);
      currentSort = s;
      document.querySelectorAll('.sort-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      applySort();
    });
  });
}

/* ══════════════════════════════════════════════════════════════════════════ */
/*  ITEM DETAIL PAGE                                                          */
/* ══════════════════════════════════════════════════════════════════════════ */

function renderZoneSection(detail) {
  const { atl, ath, zone_low, zone_high, fence_lo, fence_hi, fmv, last_price, outlier_count } = detail;

  // Tick positions as % of meter
  const range = fence_hi - fence_lo;
  const pLow  = clamp((zone_low  - fence_lo) / range * 100);
  const pHigh = clamp((zone_high - fence_lo) / range * 100);
  const pFmv  = clamp((fmv       - fence_lo) / range * 100);
  const pMark = clamp((last_price - fence_lo) / range * 100);

  const grad = [
    `#ef4444 0%`,
    `#f59e0b ${pLow.toFixed(1)}%`,
    `#10b981 ${pLow.toFixed(1)}%`,
    `#10b981 ${pHigh.toFixed(1)}%`,
    `#f59e0b ${pHigh.toFixed(1)}%`,
    `#ef4444 100%`,
  ].join(', ');

  if (outlier_count > 0) {
    document.getElementById('zoneOutliers').textContent =
      `${outlier_count} outlier${outlier_count > 1 ? 's' : ''} excluded from calculation`;
  }

  document.getElementById('zoneBody').innerHTML = `
    <div class="zone-meter-wrap">

      <!-- tick labels above meter -->
      <div class="zone-ticks">
        <div class="zone-tick zone-tick-fmv" style="left:${pFmv.toFixed(1)}%">
          <span class="zone-tick-label">FMV</span>
          <span class="zone-tick-line"></span>
        </div>
        <div class="zone-tick" style="left:${pLow.toFixed(1)}%">
          <span class="zone-tick-label">${formatPrice(zone_low)}</span>
          <span class="zone-tick-line"></span>
        </div>
        <div class="zone-tick" style="left:${pHigh.toFixed(1)}%">
          <span class="zone-tick-label">${formatPrice(zone_high)}</span>
          <span class="zone-tick-line"></span>
        </div>
      </div>

      <!-- meter bar -->
      <div class="zone-track" style="background: linear-gradient(to right, ${grad})">
        <div class="zone-marker" style="left:${pMark.toFixed(1)}%"
             title="Last sold: ${formatPrice(last_price)}"></div>
      </div>

      <!-- min / max labels -->
      <div class="zone-bound-labels">
        <span>${formatPrice(fence_lo)}</span>
        <span>${formatPrice(fence_hi)}</span>
      </div>
    </div>

    <!-- stats row -->
    <div class="zone-stats-row">
      <div class="zone-stat-item">
        <span class="zone-stat-label">All-Time Low *</span>
        <span class="zone-stat-val atl">${formatPrice(atl)}</span>
      </div>
      <div class="zone-stat-item">
        <span class="zone-stat-label">Zone Low (Q1)</span>
        <span class="zone-stat-val">${formatPrice(zone_low)}</span>
      </div>
      <div class="zone-stat-item highlight">
        <span class="zone-stat-label">Fair Value</span>
        <span class="zone-stat-val fmv">${formatPrice(fmv)}</span>
      </div>
      <div class="zone-stat-item">
        <span class="zone-stat-label">Zone High (Q3)</span>
        <span class="zone-stat-val">${formatPrice(zone_high)}</span>
      </div>
      <div class="zone-stat-item">
        <span class="zone-stat-label">All-Time High *</span>
        <span class="zone-stat-val ath">${formatPrice(ath)}</span>
      </div>
    </div>
    <p class="zone-footnote">* Includes all outlier transactions — shown as data points only, not used in zone calculation.</p>
  `;
}

async function initItem() {
  loadStats();

  const params   = new URLSearchParams(location.search);
  const itemName = params.get('item');
  if (!itemName) { location.href = '/'; return; }

  document.title = formatName(itemName) + ' — Loka Market';

  const detail = await fetch(`/api/item/${encodeURIComponent(itemName)}`).then(r => {
    if (!r.ok) { location.href = '/'; return null; }
    return r.json();
  });
  if (!detail) return;

  // hero
  const icon  = document.getElementById('itemIcon');
  icon.src    = iconUrl(itemName);
  icon.alt    = formatName(itemName);
  icon.onerror = () => { icon.style.display = 'none'; };

  document.getElementById('itemName').textContent  = formatName(itemName);
  document.getElementById('itemPrice').textContent = formatPrice(detail.last_price);

  const zoneBadge = document.getElementById('zoneBadge');
  zoneBadge.textContent = `Fair Value  ${formatPrice(detail.zone_low)} – ${formatPrice(detail.zone_high)}`;
  if (detail.outlier_count > 0)
    zoneBadge.title = `${detail.outlier_count} outlier${detail.outlier_count > 1 ? 's' : ''} excluded`;

  document.getElementById('statFmv').textContent      = formatPrice(detail.fmv);
  document.getElementById('statZoneLow').textContent  = formatPrice(detail.zone_low);
  document.getElementById('statZoneHigh').textContent = formatPrice(detail.zone_high);
  document.getElementById('statVol').textContent      = detail.volume.toLocaleString();

  // zone meter section
  renderZoneSection(detail);

  // trades table
  document.getElementById('tradesCount').textContent =
    `Showing ${detail.trades.length} most recent`;

  document.getElementById('tradesBody').innerHTML = detail.trades.map(t => `
    <tr>
      <td class="price-cell">${formatPrice(t.price)}</td>
      <td>
        <div class="player-cell">
          <img class="player-avatar" src="${avatarUrl(t.buyer_uuid)}"
               onerror="this.style.display='none'" alt="">
          <span class="player-name">${t.buyer}</span>
        </div>
      </td>
    </tr>`).join('');
}

/* ══════════════════════════════════════════════════════════════════════════ */
/*  LOKA API SHARED UTILS                                                     */
/* ══════════════════════════════════════════════════════════════════════════ */

const LOKA_API = '/api/lokamc';

const WORLD_NAMES  = { north: 'Kalros', west: 'Ascalon', south: 'Garama' };
const WORLD_COLORS = { north: '#10b981', west: '#6366f1', south: '#f59e0b' };

function worldName(w)  { return WORLD_NAMES[w]  ?? w ?? '—'; }
function worldColor(w) { return WORLD_COLORS[w] ?? '#8b949e'; }

function formatVulnWindow(w) {
  if (w == null) return '—';
  const start = ((w - 4) + 24) % 24;
  const end   = (w + 4)  % 24;
  const fmt   = h => {
    const ampm = h < 12 ? 'am' : 'pm';
    const h12  = h % 12 || 12;
    return `${h12}${ampm}`;
  };
  return `${fmt(start)} – ${fmt(end)} ST`;
}

async function fetchPaged(url, embeddedKey) {
  const out = [];
  let page = 0;
  while (true) {
    const sep = url.includes('?') ? '&' : '?';
    const res = await fetch(`${url}${sep}size=100&page=${page}`).then(r => r.json());
    const items = res._embedded?.[embeddedKey] ?? (Array.isArray(res) ? res : []);
    if (!items.length) break;
    out.push(...items);
    const pg = res.page;
    if (!pg || page >= pg.totalPages - 1) break;
    page++;
  }
  return out;
}

/* ══════════════════════════════════════════════════════════════════════════ */
/*  ALLIANCES PAGE                                                            */
/* ══════════════════════════════════════════════════════════════════════════ */

let allAlliances = [];
let allTownsMap  = {};   // id → town object

function pct(v, max) {
  if (!max || max <= 0) return 0;
  return Math.min(100, (v / max) * 100);
}

/* Rank color palette (index 0 = rank 1) */
const RANK_COLORS = [
  '#f59e0b', // 1 gold
  '#94a3b8', // 2 silver
  '#cd7f32', // 3 bronze
  '#6366f1', // 4
  '#10b981', // 5
  '#ef4444', // 6
  '#ec4899', // 7
  '#3b82f6', // 8
  '#f97316', // 9
  '#a855f7', // 10
  '#14b8a6', // 11
];

function rankColor(rank) {
  const idx = (rank - 1) % RANK_COLORS.length;
  return RANK_COLORS[idx];
}

/* Returns the alliance-strength-sorted rank list */
function allianceRanked() {
  return [...allAlliances].sort((a, b) => (b.strength ?? 0) - (a.strength ?? 0));
}

/* Returns the world ('north'|'west'|'south'|null) where the alliance has the most towns */
function alliancePrimaryWorld(a) {
  const counts = { north: 0, west: 0, south: 0 };
  for (const tid of a.townIds ?? []) {
    const t = allTownsMap[tid];
    if (t && !t.deleted && counts[t.world] !== undefined) counts[t.world]++;
  }
  const best = Object.entries(counts).sort((x, y) => y[1] - x[1])[0];
  return best && best[1] > 0 ? best[0] : null;
}

/* Compute champion for each continent + balak */
function computeChampions() {
  const worlds = ['north', 'west', 'south'];
  const champs = {};

  for (const world of worlds) {
    // Count towns per alliance in this world
    const counts = {};
    for (const a of allAlliances) {
      const townCount = (a.townIds ?? []).filter(tid => {
        const t = allTownsMap[tid];
        return t && t.world === world && !t.deleted;
      }).length;
      if (townCount > 0) counts[a.id] = townCount;
    }
    // Pick alliance with most towns
    let best = null;
    let bestCount = 0;
    for (const [id, count] of Object.entries(counts)) {
      if (count > bestCount) {
        bestCount = count;
        best = allAlliances.find(a => a.id === id) ?? null;
      }
    }
    champs[world] = best ? { alliance: best, stat: bestCount } : null;
  }

  // Balak: highest bbStrength
  const byBB = [...allAlliances].sort((a, b) => (b.bbStrength ?? 0) - (a.bbStrength ?? 0));
  champs.balak = byBB.length ? { alliance: byBB[0], stat: byBB[0].bbStrength ?? 0 } : null;

  return champs;
}

function renderChampions() {
  const el = document.getElementById('allianceChampions');
  const ranked = allianceRanked();
  const champs = computeChampions();

  const categories = [
    { key: 'north', label: 'Kalros',  icon: '🌿', color: '#10b981', statLabel: 'towns' },
    { key: 'west',  label: 'Ascalon', icon: '⚔️', color: '#6366f1', statLabel: 'towns' },
    { key: 'south', label: 'Garama',  icon: '🔥', color: '#f59e0b', statLabel: 'towns' },
    { key: 'balak', label: 'Balak',   icon: '💠', color: '#ec4899', statLabel: 'balak' },
  ];

  el.innerHTML = categories.map(cat => {
    const entry = champs[cat.key];
    if (!entry) {
      return `
        <div class="champion-card" style="--champ-color:${cat.color}">
          <div class="champion-crown">👑</div>
          <div class="champion-world-label" style="color:${cat.color}">${cat.icon} ${cat.label}</div>
          <div class="champion-name" style="color:var(--muted)">No data</div>
        </div>`;
    }
    const { alliance, stat } = entry;
    const rank = ranked.findIndex(x => x.id === alliance.id) + 1;
    const statText = cat.statLabel === 'balak'
      ? `${stat.toLocaleString()} Balak`
      : `${stat} town${stat !== 1 ? 's' : ''}`;
    return `
      <div class="champion-card" style="--champ-color:${cat.color}"
           onclick="openAllianceModal('${alliance.id}')">
        <div class="champion-crown">👑</div>
        <div class="champion-world-label" style="color:${cat.color}">${cat.icon} ${cat.label}</div>
        <div class="champion-name">${alliance.name}</div>
        <div class="champion-stat" style="color:${cat.color}">${statText}</div>
        <div class="champion-rank">Overall rank #${rank}</div>
      </div>`;
  }).join('');
}

function renderAlliances() {
  const grid = document.getElementById('allianceGrid');
  const q    = (document.getElementById('allianceSearch')?.value ?? '').toLowerCase().trim();

  const ranked = allianceRanked();
  const list   = ranked.filter(a => !q || a.name.toLowerCase().includes(q));

  if (!list.length) {
    grid.innerHTML = '<div class="empty-state">No alliances found.</div>';
    return;
  }

  // Group by primary continent, sorted by strength within each group
  const CONTINENT_ORDER = ['north', 'west', 'south', null];
  const groups = {};
  for (const w of CONTINENT_ORDER) groups[w] = [];
  for (const a of list) {
    const w = alliancePrimaryWorld(a);
    groups[w in groups ? w : null].push(a);
  }

  const cardHtml = (a) => {
    const rank      = ranked.findIndex(x => x.id === a.id) + 1;
    const color     = rankColor(rank);
    const townCount = a.townIds?.length ?? 0;
    const vuln      = formatVulnWindow(a.vulnerabilityWindow);
    const world     = alliancePrimaryWorld(a);
    const wName     = worldName(world);
    const wColor    = worldColor(world);
    return `
      <div class="alliance-card" style="--a-color:${color}" onclick="openAllianceModal('${a.id}')">
        <div class="alliance-card-left">
          <div class="alliance-rank-badge" style="color:${color}">#${rank}</div>
          <div class="alliance-color-bar" style="background:${color}"></div>
          <div class="alliance-main">
            <div class="alliance-name">${a.name}</div>
            <div class="alliance-meta-row">
              <div class="ameta-item">
                <span class="ameta-label">Strength</span>
                <span class="ameta-val" style="color:${color}">${(a.strength ?? 0).toLocaleString()}</span>
              </div>
              <div class="ameta-item">
                <span class="ameta-label">Balak</span>
                <span class="ameta-val ameta-balak">${(a.bbStrength ?? 0).toLocaleString()}</span>
              </div>
              <div class="ameta-item">
                <span class="ameta-label">Towns</span>
                <span class="ameta-val">${townCount}</span>
              </div>
              <div class="ameta-item">
                <span class="ameta-label">Vuln Window</span>
                <span class="ameta-val ameta-vuln">${vuln}</span>
              </div>
            </div>
          </div>
        </div>
        <div class="alliance-chevron">›</div>
      </div>`;
  };

  const sections = CONTINENT_ORDER
    .filter(w => groups[w].length)
    .map(w => {
      const label = w ? `${worldName(w)}` : 'Other';
      const icon  = w === 'north' ? '🌿' : w === 'west' ? '⚔️' : w === 'south' ? '🔥' : '🌐';
      const color = worldColor(w);
      return `
        <div class="continent-rankings-group">
          <div class="continent-rankings-header" style="color:${color}">${icon} ${label}</div>
          ${groups[w].map(cardHtml).join('')}
        </div>`;
    });

  grid.innerHTML = sections.join('');
}

function openAllianceModal(id) {
  const a = allAlliances.find(x => x.id === id);
  if (!a) return;

  document.getElementById('modalAllianceName').textContent = a.name;

  const ranked    = allianceRanked();
  const rank      = ranked.findIndex(x => x.id === a.id) + 1;
  const towns     = (a.townIds ?? []).map(tid => allTownsMap[tid]);
  const vuln      = formatVulnWindow(a.vulnerabilityWindow);
  const maxStr    = Math.max(1, ...allAlliances.map(x => x.strength   ?? 0));
  const maxBB     = Math.max(1, ...allAlliances.map(x => x.bbStrength ?? 0));
  const strPct    = pct(a.strength   ?? 0, maxStr);
  const bbPct     = pct(a.bbStrength ?? 0, maxBB);

  const townRows = towns.length
    ? towns.map(t => {
        if (!t) return `<div class="modal-town-item"><span class="modal-town-name" style="color:var(--muted)">Unknown town</span></div>`;
        const wc = worldColor(t.world);
        const wn = worldName(t.world);
        return `
          <div class="modal-town-item">
            <span class="modal-town-name">${t.name}</span>
            <span class="modal-town-meta">
              <span style="color:${wc}">${wn}</span>
              · Lv.${Math.round(t.townLevel ?? 0)}
              · ${Object.keys(t.members ?? {}).length} members
            </span>
          </div>`;
      }).join('')
    : '<div style="color:var(--muted);font-size:13px;padding:8px 0">No towns in this alliance.</div>';

  document.getElementById('modalBody').innerHTML = `
    <div class="modal-stats-grid">
      <div class="modal-stat">
        <div class="modal-stat-label">Power Rank</div>
        <div class="modal-stat-val" style="color:${rankColor(rank)}">#${rank}</div>
      </div>
      <div class="modal-stat">
        <div class="modal-stat-label">Continent</div>
        <div class="modal-stat-val astat-cc">${(a.strength ?? 0).toLocaleString()}</div>
      </div>
      <div class="modal-stat">
        <div class="modal-stat-label">Balak</div>
        <div class="modal-stat-val astat-bb">${(a.bbStrength ?? 0).toLocaleString()}</div>
      </div>
      <div class="modal-stat">
        <div class="modal-stat-label">Vulnerability Window</div>
        <div class="modal-stat-val">${vuln}</div>
      </div>
      <div class="modal-stat">
        <div class="modal-stat-label">Member Towns</div>
        <div class="modal-stat-val">${towns.length}</div>
      </div>
    </div>
    <div class="strength-bars">
      <div class="strength-bar-wrap">
        <span class="strength-bar-label">CC</span>
        <div class="strength-bar-track">
          <div class="strength-bar-fill strength-bar-cc" style="width:${strPct.toFixed(1)}%"></div>
        </div>
      </div>
      <div class="strength-bar-wrap">
        <span class="strength-bar-label">BB</span>
        <div class="strength-bar-track">
          <div class="strength-bar-fill strength-bar-bb" style="width:${bbPct.toFixed(1)}%"></div>
        </div>
      </div>
    </div>
    ${towns.length
      ? `<div class="modal-section">
           <div class="modal-section-title">Member Towns (${towns.length})</div>
           <div class="modal-town-list">${townRows}</div>
         </div>`
      : `<div class="modal-section"><div class="modal-section-title">Member Towns</div>${townRows}</div>`}
  `;

  document.getElementById('allianceModal').style.display = 'flex';
}

function closeModal() {
  document.getElementById('allianceModal').style.display = 'none';
}

async function initAlliances() {
  try {
    const [allianceData, towns] = await Promise.all([
      fetchPaged(`${LOKA_API}/alliances`, 'alliances'),
      fetchPaged(`${LOKA_API}/towns/search/findAll`, 'towns'),
    ]);
    allAlliances = allianceData;
    towns.forEach(t => { allTownsMap[t.id] = t; });

    renderChampions();
    renderAlliances();

    let searchTimer;
    document.getElementById('allianceSearch').addEventListener('input', () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(renderAlliances, 150);
    });

    document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });
  } catch (e) {
    document.getElementById('allianceGrid').innerHTML =
      '<div class="empty-state">Failed to load alliance data. Check console for details.</div>';
    document.getElementById('allianceChampions').innerHTML =
      '<div class="empty-state">Failed to load champion data.</div>';
    console.error(e);
  }
}

/* ══════════════════════════════════════════════════════════════════════════ */
/*  TOWNS PAGE                                                                */
/* ══════════════════════════════════════════════════════════════════════════ */

let allTowns        = [];
let townAllianceMap = {};  // townId → { id, name }
let currentContinent = null; // null=hub, 'north'|'west'|'south'
const _playerCache  = {};

async function fetchPlayer(lokaId) {
  if (lokaId in _playerCache) return _playerCache[lokaId];
  try {
    const p = await fetch(`${LOKA_API}/players/${lokaId}`).then(r => r.ok ? r.json() : null);
    _playerCache[lokaId] = p;
    return p;
  } catch (_) {
    _playerCache[lokaId] = null;
    return null;
  }
}

function buildTownAllianceMap(alliances) {
  townAllianceMap = {};
  for (const a of alliances) {
    for (const tid of (a.townIds ?? [])) {
      townAllianceMap[tid] = { id: a.id, name: a.name };
    }
  }
}

function memberCount(t) {
  return t.members ? Object.keys(t.members).length : 0;
}

function selectContinent(world) {
  currentContinent = world;
  const searchBar = document.getElementById('townSearchBar');
  if (!world) {
    searchBar.style.display = 'none';
    renderTownHub();
  } else {
    searchBar.style.display = '';
    const inp = document.getElementById('townSearch');
    if (inp) inp.value = '';
    renderContinent(world);
  }
}

function renderTownHub() {
  const content = document.getElementById('townContent');
  const liveTowns = allTowns.filter(t => !t.deleted);

  const northCount = liveTowns.filter(t => t.world === 'north').length;
  const westCount  = liveTowns.filter(t => t.world === 'west').length;
  const southCount = liveTowns.filter(t => t.world === 'south').length;

  // Top 10 by player count
  const top10 = [...liveTowns]
    .sort((a, b) => memberCount(b) - memberCount(a))
    .slice(0, 10);

  const topRows = top10.map((t, i) => {
    const rank = i + 1;
    const rankClass = rank === 1 ? 'rank-1' : rank === 2 ? 'rank-2' : rank === 3 ? 'rank-3' : '';
    const rankLabel = rank === 1 ? '1ST' : rank === 2 ? '2ND' : rank === 3 ? '3RD' : `#${rank}`;
    const ally = townAllianceMap[t.id];
    const wColor = worldColor(t.world);
    const wName  = worldName(t.world);
    const level  = Math.round(t.townLevel ?? 0);
    return `
      <div class="top-town-row" onclick="openTownModal('${t.id}')">
        <div class="top-town-rank ${rankClass}">${rankLabel}</div>
        <div class="top-town-info">
          <div class="top-town-name">${t.name}</div>
          ${ally ? `<div class="top-town-alliance" style="color:var(--muted)">${ally.name}</div>` : ''}
        </div>
        <div class="top-town-stats">
          <span class="top-town-world" style="color:${wColor}">${wName}</span>
          <span class="top-town-level">${memberCount(t)} players</span>
        </div>
      </div>`;
  }).join('');

  content.innerHTML = `
    <div class="continent-hub">
      <div class="continent-hub-title">Choose a Continent</div>
      <div class="continent-cards">
        <div class="continent-card continent-card--north" onclick="selectContinent('north')">
          <div class="continent-icon">🌿</div>
          <div class="continent-name">Kalros</div>
          <div class="continent-count">${northCount} town${northCount !== 1 ? 's' : ''}</div>
          <div class="continent-arrow">Explore →</div>
        </div>
        <div class="continent-card continent-card--west" onclick="selectContinent('west')">
          <div class="continent-icon">⚔️</div>
          <div class="continent-name">Ascalon</div>
          <div class="continent-count">${westCount} town${westCount !== 1 ? 's' : ''}</div>
          <div class="continent-arrow">Explore →</div>
        </div>
        <div class="continent-card continent-card--south" onclick="selectContinent('south')">
          <div class="continent-icon">🔥</div>
          <div class="continent-name">Garama</div>
          <div class="continent-count">${southCount} town${southCount !== 1 ? 's' : ''}</div>
          <div class="continent-arrow">Explore →</div>
        </div>
      </div>
    </div>
    <div class="top-towns-section">
      <div class="top-towns-header">
        <div class="top-towns-title">🏆 Top Towns on Loka</div>
        <div class="top-towns-sub">by player count</div>
      </div>
      <div class="top-towns-list">${topRows || '<div class="empty-state">No town data.</div>'}</div>
    </div>`;
}

function renderTownCard(t) {
  const members = memberCount(t);
  const vuln    = formatVulnWindow(t.vulnerabilityWindow);
  const wColor  = worldColor(t.world);
  const level   = Math.round(t.townLevel ?? 0);

  return `
    <div class="town-card" onclick="openTownModal('${t.id}')">
      <div class="town-card-header">
        <div class="town-name">${t.name}</div>
        <div class="town-badges">
          ${t.recruiting ? '<span class="badge pos">Recruiting</span>' : ''}
        </div>
      </div>
      <div class="town-level-row">
        <span class="town-level-label">Level</span>
        <span class="town-level-num">${level}</span>
        <div class="town-level-bar-track">
          <div class="town-level-bar-fill" style="width:${Math.min(100, level)}%;background:${wColor}"></div>
        </div>
      </div>
      <div class="town-stats-grid">
        <div class="tstat">
          <span class="tstat-label">Continent</span>
          <span class="tstat-val tstat-cc">${(t.strength ?? 0).toLocaleString()}</span>
        </div>
        <div class="tstat">
          <span class="tstat-label">Balak</span>
          <span class="tstat-val tstat-bb">${(t.bbStrength ?? 0).toLocaleString()}</span>
        </div>
        <div class="tstat">
          <span class="tstat-label">Members</span>
          <span class="tstat-val">${members}</span>
        </div>
        <div class="tstat">
          <span class="tstat-label">Vuln Window</span>
          <span class="tstat-val tstat-vuln">${vuln}</span>
        </div>
      </div>
    </div>`;
}

function renderContinent(world) {
  const content  = document.getElementById('townContent');
  const q        = (document.getElementById('townSearch')?.value ?? '').toLowerCase().trim();
  const wColor   = worldColor(world);
  const wName    = worldName(world);

  let towns = allTowns.filter(t => !t.deleted && t.world === world);
  if (q) towns = towns.filter(t => t.name.toLowerCase().includes(q));

  // Group by alliance
  const groups = {}; // allianceId → { name, towns[] }
  const independent = [];

  for (const t of towns) {
    const ally = townAllianceMap[t.id];
    if (ally) {
      if (!groups[ally.id]) groups[ally.id] = { name: ally.name, towns: [] };
      groups[ally.id].towns.push(t);
    } else {
      independent.push(t);
    }
  }

  // Sort groups by town count desc, then towns by level desc
  const sortedGroups = Object.values(groups).sort((a, b) => b.towns.length - a.towns.length);
  for (const g of sortedGroups) {
    g.towns.sort((a, b) => (b.townLevel ?? 0) - (a.townLevel ?? 0));
  }
  independent.sort((a, b) => (b.townLevel ?? 0) - (a.townLevel ?? 0));

  const totalCount = towns.length;

  const renderGroup = (name, groupTowns) => `
    <div class="alliance-section">
      <div class="alliance-section-header">
        <div class="alliance-section-name">${name}</div>
        <div class="alliance-section-count">${groupTowns.length} town${groupTowns.length !== 1 ? 's' : ''}</div>
      </div>
      <div class="town-card-grid">${groupTowns.map(renderTownCard).join('')}</div>
    </div>`;

  const groupsHtml = sortedGroups.map(g => renderGroup(g.name, g.towns)).join('');
  const indepHtml  = independent.length ? renderGroup('Independent', independent) : '';

  const emptyHtml = !towns.length
    ? '<div class="empty-state">No towns match your search.</div>'
    : '';

  content.innerHTML = `
    <div class="continent-view-header">
      <button class="back-btn" onclick="selectContinent(null)">← Back</button>
      <div class="continent-view-title" style="color:${wColor}">${wName}</div>
      <div class="continent-view-count">${totalCount} town${totalCount !== 1 ? 's' : ''}</div>
    </div>
    ${emptyHtml || (groupsHtml + indepHtml)}`;
}

async function openTownModal(id) {
  const t = allTowns.find(x => x.id === id);
  if (!t) return;

  const members = memberCount(t);
  const vuln    = formatVulnWindow(t.vulnerabilityWindow);
  const wName   = worldName(t.world);
  const wColor  = worldColor(t.world);
  const ally    = townAllianceMap[t.id];

  document.getElementById('modalTownName').textContent = t.name;
  document.getElementById('modalTownWorld').innerHTML  =
    `<span style="color:${wColor}">${wName}</span>${t.deleted ? ' · <span style="color:#ef4444">Deleted</span>' : ''}`;

  // Build member list with placeholders
  const memberEntries  = Object.entries(t.members ?? {});
  const ownerId        = t.owner ?? null;

  // Order: owner first, then subowners, then regular members
  const ownerEntry    = memberEntries.find(([lid]) => lid === ownerId);
  const subownerEntries = memberEntries.filter(([lid, v]) => lid !== ownerId && v.subowner);
  const regularEntries  = memberEntries.filter(([lid, v]) => lid !== ownerId && !v.subowner);
  const ordered = [
    ...(ownerEntry ? [ownerEntry] : []),
    ...subownerEntries,
    ...regularEntries,
  ];

  const memberRowsHtml = ordered.map(([lokaId, v]) => {
    const isOwner    = lokaId === ownerId;
    const isSubowner = !isOwner && v.subowner;
    const badge = isOwner
      ? '<span class="badge pos" style="font-size:10px">Owner</span>'
      : isSubowner
        ? '<span class="badge zone" style="font-size:10px">Sub-Owner</span>'
        : '';
    return `
      <div class="modal-member-item" id="mp-${lokaId}">
        <div class="modal-member-avatar-wrap">
          <div class="modal-member-placeholder"></div>
        </div>
        <span class="modal-member-name" style="color:var(--muted)">${lokaId}</span>
        ${badge}
      </div>`;
  }).join('');

  const memberSection = ordered.length ? `
    <div class="modal-section">
      <div class="modal-section-title">Members (${members})</div>
      <div class="modal-member-list">${memberRowsHtml}</div>
    </div>` : '';

  document.getElementById('townModalBody').innerHTML = `
    <div class="modal-stats-grid">
      <div class="modal-stat">
        <div class="modal-stat-label">Town Level</div>
        <div class="modal-stat-val" style="color:var(--accent)">${Math.round(t.townLevel ?? 0)}</div>
      </div>
      <div class="modal-stat">
        <div class="modal-stat-label">Continent</div>
        <div class="modal-stat-val tstat-cc">${(t.strength ?? 0).toLocaleString()}</div>
      </div>
      <div class="modal-stat">
        <div class="modal-stat-label">Balak</div>
        <div class="modal-stat-val tstat-bb">${(t.bbStrength ?? 0).toLocaleString()}</div>
      </div>
      <div class="modal-stat">
        <div class="modal-stat-label">Members</div>
        <div class="modal-stat-val">${members}</div>
      </div>
      <div class="modal-stat">
        <div class="modal-stat-label">Vulnerability Window</div>
        <div class="modal-stat-val">${vuln}</div>
      </div>
      <div class="modal-stat">
        <div class="modal-stat-label">Recruiting</div>
        <div class="modal-stat-val">${t.recruiting
          ? '<span style="color:var(--green)">Yes</span>'
          : '<span style="color:var(--muted)">No</span>'}</div>
      </div>
      ${ally ? `
      <div class="modal-stat">
        <div class="modal-stat-label">Alliance</div>
        <div class="modal-stat-val" style="font-size:15px">${ally.name}</div>
      </div>` : ''}
    </div>
    ${memberSection}
  `;

  document.getElementById('townModal').style.display = 'flex';

  // Async: fetch player data for each member and update DOM rows
  if (ordered.length) {
    const ids = ordered.map(([lid]) => lid);

    async function limitedFetch(fetchIds, fn, limit = 8) {
      let i = 0;
      async function worker() {
        while (i < fetchIds.length) {
          const idx = i++;
          const lokaId = fetchIds[idx];
          const player = await fn(lokaId).catch(() => null);
          // Update that specific row's avatar and name
          const row = document.getElementById(`mp-${lokaId}`);
          if (!row) return;
          const wrap = row.querySelector('.modal-member-avatar-wrap');
          const nameEl = row.querySelector('.modal-member-name');
          if (player && player.uuid) {
            if (wrap) wrap.innerHTML = `
              <img class="modal-member-avatar"
                   src="https://mc-heads.net/avatar/${player.uuid}/28"
                   onerror="this.src='https://mc-heads.net/avatar/${player.name ?? lokaId}/28'"
                   alt="">`;
            if (nameEl) {
              nameEl.textContent = player.name ?? lokaId;
              nameEl.style.color = '';
            }
          } else {
            if (wrap) wrap.innerHTML = `<div class="modal-member-placeholder"></div>`;
            if (nameEl) nameEl.textContent = lokaId;
          }
        }
      }
      await Promise.all(Array.from({ length: Math.min(limit, fetchIds.length) }, worker));
    }

    limitedFetch(ids, fetchPlayer, 8).catch(() => {});
  }
}

function closeTownModal() {
  document.getElementById('townModal').style.display = 'none';
}

async function initTowns() {
  try {
    const [towns, allianceData] = await Promise.all([
      fetchPaged(`${LOKA_API}/towns/search/findAll`, 'towns'),
      fetchPaged(`${LOKA_API}/alliances`, 'alliances'),
    ]);

    allTowns = towns;
    buildTownAllianceMap(allianceData);

    selectContinent(null);

    let searchTimer;
    document.getElementById('townSearch').addEventListener('input', () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => {
        if (currentContinent) renderContinent(currentContinent);
      }, 150);
    });

    document.addEventListener('keydown', e => { if (e.key === 'Escape') closeTownModal(); });
  } catch (e) {
    document.getElementById('townContent').innerHTML =
      '<div class="empty-state">Failed to load town data. Check console for details.</div>';
    console.error(e);
  }
}

/* ══════════════════════════════════════════════════════════════════════════ */
/*  INDEX: ANNOUNCEMENT BAR + BATTLE REEL + HERO IMAGES                      */
/* ══════════════════════════════════════════════════════════════════════════ */

async function initAnnouncement() {
  try {
    const cfg = await fetch('/api/site_config').then(r => r.json());
    const ann = cfg.announcement;
    const messages = ann && ann.enabled
      ? (ann.messages || (ann.text ? [{ text: ann.text, type: ann.type || 'warning' }] : []))
      : [];
    if (messages.length) {
      const bar = document.getElementById('announcementBar');
      if (!bar) return;
      const el = document.getElementById('annText');
      const spacer = '\u00a0\u00a0\u00a0\u00a0\u2605\u00a0\u00a0\u00a0\u00a0';

      let idx = 0;
      function showMsg(i) {
        const m = messages[i % messages.length];
        bar.className = `type-${m.type || 'warning'}`;
        // Triple the text for a seamless scroll loop
        el.textContent = m.text + spacer + m.text + spacer + m.text;
        // Reset animation
        el.style.animation = 'none';
        el.offsetHeight; // reflow
        el.style.animation = '';
        bar.style.display = 'flex';
      }
      showMsg(0);
      // Rotate message every 18 seconds (matches scroll duration)
      setInterval(() => { idx++; showMsg(idx); }, 18000);
    }

    // Hero images
    const heroImages = cfg.hero_images || [];
    const heroEl = document.getElementById('heroImages');
    if (heroEl) {
      const slots = heroImages.map(img => {
        if (img.url) {
          return `
            <div class="hero-img-slot">
              <img src="${img.url}" alt="${img.caption || ''}" onerror="this.parentElement.className='hero-img-slot empty';this.parentElement.innerHTML='No image'">
              ${img.caption ? `<div class="hero-img-caption">${img.caption}</div>` : ''}
            </div>`;
        }
        return '';
      }).filter(Boolean).join('');
      if (slots) heroEl.innerHTML = slots;
    }
  } catch (_) {}
}

async function initBattleReel() {
  const wrap = document.getElementById('battleReelWrap');
  const reel = document.getElementById('battleReel');
  if (!wrap || !reel) return;

  function reelClass(b) {
    if (b.territory_won_by === 'attacker') return 'won-attacker';
    if (b.territory_won_by === 'defender') return 'won-defender';
    if (b.mutator === 'rivina' || b.world === 'rivina') return 'won-rivina';
    if (b.territory_won_by === 'activity') return 'won-unknown';
    return 'won-unknown';
  }
  function reelLabel(b) {
    if (b.territory_won_by === 'attacker') return 'CAPTURED';
    if (b.territory_won_by === 'defender') return 'DEFENDED';
    if (b.territory_won_by === 'activity') return 'BATTLE';
    if (b.mutator === 'rivina' || b.world === 'rivina') return 'RIVINA';
    return 'STANDOFF';
  }
  function reelItem(b) {
    const cls  = reelClass(b);
    const lbl  = reelLabel(b);
    const area = b.area_name || `T-${b.territory_num}`;
    const world = (b.world_display || b.world) ? `[${(b.world_display || b.world).toUpperCase()}]` : '';
    let detail, ts;
    if (b.territory_won_by === 'activity') {
      const owner = b.alliance_name || b.town_name || '?';
      const agoSec = b.last_battle_ts ? Math.round((Date.now()/1000) - b.last_battle_ts) : null;
      const agoStr = agoSec != null ? (agoSec < 3600 ? `${Math.round(agoSec/60)}m ago` : `${Math.round(agoSec/3600)}h ago`) : '';
      detail = `${area} — ${owner}${agoStr ? ' · ' + agoStr : ''}`;
      ts = '';
    } else {
      const attacker = b.new_town_name || b.new_alliance_name || '?';
      const defender = b.old_town_name || b.old_alliance_name || '?';
      detail = b.territory_won_by === 'attacker'
        ? `${attacker} seized ${area} from ${defender}`
        : b.territory_won_by === 'defender'
        ? `${defender} held ${area}`
        : `Standoff at ${area}`;
      ts = b.detected_ts ? new Date(b.detected_ts * 1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}) : '';
    }
    return `<div class="reel-item ${cls}">
      <span class="reel-dot"></span>
      <div class="reel-body">
        <span class="reel-label">${lbl}${world ? ' ' + world : ''}</span>
        <span class="reel-detail">${detail}</span>
        ${ts ? `<span class="reel-ts">${ts}</span>` : ''}
      </div>
    </div>`;
  }

  const WORLD_NAMES = {north:'Kalros',south:'Garama',west:'Ascalon',lilboi:'Rivina',bigboi:'Balak'};

  function fightReelItem(f) {
    const cls = f.is_live ? 'won-unknown' : f.winner === 'attacker' ? 'won-attacker' : f.winner === 'defender' ? 'won-defender' : 'won-unknown';
    const lbl = f.is_live ? 'LIVE' : f.winner === 'attacker' ? 'CAPTURED' : f.winner === 'defender' ? 'DEFENDED' : 'STANDOFF';
    const worldName = WORLD_NAMES[f.world] || f.world || '';
    const worldTag  = worldName ? `[${worldName.toUpperCase()}]` : '';
    const attacker  = f.attacker_town || 'Attackers';
    const defender  = f.defender_town || 'Defenders';
    const area      = f.location ? `${f.location} ${f.territory_num}` : `T-${f.territory_num}`;
    const detail    = f.is_live
      ? `${attacker} vs ${defender} at ${area}`
      : f.winner === 'attacker'
      ? `${attacker} seized ${area} from ${defender}`
      : f.winner === 'defender'
      ? `${defender} held ${area}`
      : `Standoff at ${area}`;
    const ts = f.time_display ? `${f.date_display || ''} ${f.time_display}`.trim() : '';
    return `<div class="reel-item ${cls}">
      <span class="reel-dot"></span>
      <div class="reel-body">
        <span class="reel-label">${lbl}${worldTag ? ' ' + worldTag : ''}</span>
        <span class="reel-detail">${detail}</span>
        ${ts ? `<span class="reel-ts">${ts}</span>` : ''}
      </div>
    </div>`;
  }

  function fightSortTs(f) {
    try { return new Date(`${f.date_display} ${f.time_display}`).getTime(); } catch { return 0; }
  }

  async function load() {
    wrap.style.display = 'flex'; // always show; content updates in place
    try {
      const [data, fights] = await Promise.all([
        fetch('/api/battles').then(r => { if (!r.ok) throw new Error(r.status); return r.json(); }),
        fetch('/api/fights').then(r => r.json()).catch(() => []),
      ]);

      // Territory battle events (deduped)
      const battleEvents = [...(data.recent_battles||[]), ...(data.top_alliance_battles||[]), ...(data.rivina_battles||[])];
      const seen = new Set();
      const deduped = battleEvents.filter(b => { if(seen.has(b.id)) return false; seen.add(b.id); return true; });

      // Build combined list: territory battles + fight logs, sorted newest first
      const terrItems = deduped.map(b => ({ ts: (b.detected_ts || b.last_battle_ts || 0) * 1000, html: reelItem(b) }));
      const fightItems = (fights || []).filter(f => f.winner || f.is_live).map(f => ({ ts: fightSortTs(f), html: fightReelItem(f) }));
      const combined = [...terrItems, ...fightItems].sort((a, b) => b.ts - a.ts);

      if (combined.length) {
        reel.innerHTML = combined.map(i => i.html).join('');
      } else {
        // Last resort: snapshot activity
        const activity = data.recent_activity || [];
        reel.innerHTML = activity.length
          ? activity.map(reelItem).join('')
          : `<div class="reel-item won-unknown"><span class="reel-dot"></span><div class="reel-body"><span class="reel-detail">No recent battle data</span></div></div>`;
      }
    } catch (_) {
      // Keep existing content on failure; only show placeholder if reel is empty
      if (!reel.innerHTML) {
        reel.innerHTML = `<div class="reel-item won-unknown"><span class="reel-dot"></span><div class="reel-body"><span class="reel-detail">Battle feed loading…</span></div></div>`;
      }
    }
  }

  await load();
  setInterval(load, 5 * 60 * 1000);
}

/* ══════════════════════════════════════════════════════════════════════════ */
/*  MAP PAGE                                                                  */
/* ══════════════════════════════════════════════════════════════════════════ */

function selectMap(btn) {
  document.querySelectorAll('.map-pill').forEach(p => p.classList.remove('active'));
  btn.classList.add('active');
  const frame = document.getElementById('mapFrame');
  if (frame) frame.src = btn.dataset.map;
}

function initMap() {
  // Map page is purely HTML-driven via onclick; nothing async needed.
  // Active pill is already set in HTML.
}

/* ══════════════════════════════════════════════════════════════════════════ */
/*  FOUNDERS PAGE                                                             */
/* ══════════════════════════════════════════════════════════════════════════ */

async function initFounders() {
  const grid = document.getElementById('foundersGrid');
  if (!grid) return;
  try {
    const all = await fetch('/api/founders').then(r => r.json());
    if (!all.length) {
      grid.innerHTML = '<div class="founders-empty">No founders listed yet.</div>';
      return;
    }

    const founders = all.filter(f => f.type !== 'traitor');
    const traitors = all.filter(f => f.type === 'traitor');

    const renderCard = (f, isTraitor = false) => {
      const isGeneral = f.rank === 'GENERAL';
      const headUrl = f.uuid
        ? (isGeneral
            ? `https://mc-heads.net/body/${f.uuid}/100`
            : `https://mc-heads.net/avatar/${f.uuid}/80`)
        : '';
      const imgEl = headUrl
        ? `<div class="founder-avatar-wrap ${isGeneral ? 'founder-general' : ''} ${isTraitor ? 'founder-traitor-wrap' : ''}">
             <img class="founder-avatar" src="${headUrl}" alt="${f.name || ''}" onerror="this.parentElement.style.display='none'">
             ${isTraitor ? `<div class="founder-rank-badge founder-rank-badge--traitor">✕ EXCOMMUNICATED</div>` : isGeneral ? `<div class="founder-rank-badge">⚔ GENERAL</div>` : ''}
           </div>`
        : '';
      return `
        <div class="founder-card ${isGeneral ? 'founder-card--general' : ''} ${isTraitor ? 'founder-card--traitor' : ''}">
          ${imgEl}
          <div class="founder-info">
            <div class="founder-name ${isTraitor ? 'founder-name--traitor' : ''}">${isTraitor ? '<span class="traitor-x">✕</span> ' : ''}${f.name || 'Unknown'}</div>
            ${f.title ? `<div class="founder-title">${f.title}</div>` : ''}
            ${f.note  ? `<div class="founder-note">${f.note}</div>`  : ''}
          </div>
        </div>`;
    };

    grid.innerHTML = `
      <div class="founders-section-header">
        <div class="founders-section-title">Pieces of the Stromgarde War Machine</div>
      </div>
      <div class="founders-grid-inner">
        ${founders.map(f => renderCard(f, false)).join('')}
      </div>
      ${traitors.length ? `
      <div class="founders-section-header founders-section-header--traitor">
        <div class="founders-section-title founders-section-title--traitor">Enemies of Stromgarde</div>
        <div class="founders-section-sub">These individuals betrayed the realm. Their names are remembered so they are never forgotten.</div>
      </div>
      <div class="founders-grid-inner">
        ${traitors.map(f => renderCard(f, true)).join('')}
      </div>` : ''}`;
  } catch (_) {
    grid.innerHTML = '<div class="founders-empty">Failed to load founders.</div>';
  }
}

/* ══════════════════════════════════════════════════════════════════════════ */
/*  TERRITORIES / WAR ROOM PAGE                                               */
/* ══════════════════════════════════════════════════════════════════════════ */

let _battleData = null;

function terrTab(btn) {
  document.querySelectorAll('.terr-tab').forEach(t => t.classList.remove('active'));
  btn.classList.add('active');
  if (_battleData) renderTerrFeed(_battleData, btn.dataset.tab);
}

function terrClass(b) {
  if (b.territory_won_by === 'attacker') return 'won-attacker';
  if (b.territory_won_by === 'defender') return 'won-defender';
  if (b.mutator === 'rivina' || b.world === 'rivina') return 'won-rivina';
  return 'won-unknown';
}
function terrLabel(b) {
  if (b.territory_won_by === 'attacker') return 'CAPTURED';
  if (b.territory_won_by === 'defender') return 'DEFENDED';
  if (b.territory_won_by === 'activity') return 'BATTLE';
  if (b.mutator === 'rivina' || b.world === 'rivina') return 'RIVINA';
  return 'STANDOFF';
}

function terrCardHTML(b) {
  const cls   = terrClass(b);
  const lbl   = terrLabel(b);
  const area  = b.area_name || `T-${b.territory_num}`;
  const world = (b.world_display || b.world || '').toUpperCase();
  let title = '', sub = '', ts = '', delta = '';

  if (b.territory_won_by === 'activity') {
    const owner  = b.alliance_name || b.town_name || 'Unknown';
    const agoSec = b.last_battle_ts ? Math.round(Date.now()/1000 - b.last_battle_ts) : null;
    const agoStr = agoSec != null
      ? (agoSec < 3600 ? `${Math.round(agoSec/60)}m ago` : agoSec < 86400 ? `${Math.round(agoSec/3600)}h ago` : `${Math.round(agoSec/86400)}d ago`)
      : '';
    title = `Battle at ${area}`;
    sub   = `Held by ${owner}${agoStr ? ' · ' + agoStr : ''}`;
    ts    = b.last_battle_ts ? new Date(b.last_battle_ts * 1000).toLocaleString([], {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'}) : '';
  } else {
    const attacker = b.new_town_name || b.new_alliance_name || b.new_town_id || '?';
    const defender = b.old_town_name || b.old_alliance_name || b.old_town_id || '?';
    if (b.territory_won_by === 'attacker') {
      title = `${attacker} seized ${area}`;
      sub   = `Taken from ${defender}`;
    } else if (b.territory_won_by === 'defender') {
      title = `${defender} held ${area}`;
      sub   = `Repelled attackers`;
    } else {
      title = `Standoff at ${area}`;
      sub   = `${attacker} vs ${defender}`;
    }
    ts = b.detected_ts ? new Date(b.detected_ts * 1000).toLocaleString([], {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'}) : '';
    delta = b.strength_delta != null
      ? `<span class="terr-card-delta ${b.strength_delta >= 0 ? 'pos' : 'neg'}">${b.strength_delta >= 0 ? '+' : ''}${b.strength_delta.toLocaleString()} CS</span>`
      : '';
  }

  return `<div class="terr-card ${cls}">
    <div class="terr-card-badge">${lbl}</div>
    <div class="terr-card-body">
      <div class="terr-card-title">${title}</div>
      <div class="terr-card-sub">${sub}</div>
    </div>
    <div class="terr-card-meta">
      ${world ? `<span class="terr-card-world">${world}</span>` : ''}
      ${delta}
      ${ts ? `<span class="terr-card-ts">${ts}</span>` : ''}
    </div>
  </div>`;
}

function renderTerrFeed(data, tab) {
  const feed = document.getElementById('terrFeed');
  if (!feed) return;
  let battles;
  if (tab === 'recent') {
    // Prefer real battle events; fall back to snapshot activity
    battles = (data.recent_battles||[]).length ? data.recent_battles : (data.recent_activity||[]);
  } else if (tab === 'top') {
    battles = (data.top_alliance_battles||[]).length ? data.top_alliance_battles : (data.recent_activity||[]);
  } else if (tab === 'rivina') {
    battles = data.rivina_battles || [];
  } else {
    battles = data.no_transfer_battles || [];
  }

  if (!battles.length) {
    feed.innerHTML = '<div class="terr-empty">No battles found in this category.</div>';
    return;
  }
  feed.innerHTML = battles.map(terrCardHTML).join('');
}

async function initTerritories() {
  const feed = document.getElementById('terrFeed');
  if (!feed) return;
  try {
    const data = await fetch('/api/battles').then(r => r.json());
    _battleData = data;

    // Stats
    const recent = data.recent_battles || [];
    const captures = recent.filter(b => b.territory_won_by === 'attacker').length;
    const defenses = recent.filter(b => b.territory_won_by === 'defender').length;
    const rivina = (data.rivina_battles || []).length;
    const el = id => document.getElementById(id);
    if (el('statTotalBattles')) el('statTotalBattles').textContent = recent.length;
    if (el('statCaptures'))    el('statCaptures').textContent = captures;
    if (el('statDefenses'))    el('statDefenses').textContent = defenses;
    if (el('statRivina'))      el('statRivina').textContent = rivina;

    // Render default tab
    renderTerrFeed(data, 'recent');
  } catch(e) {
    feed.innerHTML = '<div class="terr-empty">Failed to load battle data.</div>';
  }
}

/* ── router ──────────────────────────────────────────────────────────────── */
if      (document.getElementById('announcementBar')) { initAnnouncement(); initBattleReel(); }
else if (document.getElementById('itemGrid'))      { initIndex(); }
else if (document.getElementById('zoneBody'))      initItem();
else if (document.getElementById('allianceGrid'))  initAlliances();
else if (document.getElementById('townContent'))   initTowns();
else if (document.getElementById('mapFrame'))      initMap();
else if (document.getElementById('foundersGrid'))  initFounders();
else if (document.getElementById('terrFeed'))      initTerritories();
else if (document.getElementById('fightList'))     initFightsPage();
else if (document.getElementById('playerLb'))      initPlayersPage();

// ── fights page (EldritchBot) ─────────────────────────────────────────────

const _ebPageSize  = 50;
let _ebPage        = 1;
let _ebTotal       = 0;
let _ebFights      = [];
let _ebLoading     = false;
let _ebSearch      = '';
let _ebPlayerSort  = 'kills';
let currentEbFight = null;

async function initFightsPage() {
  if (!document.getElementById('fightList')) return;
  fetch('/api/eb_fights/stats').then(r => r.json()).then(s => {
    const hdr = document.querySelector('.page-subtitle');
    if (hdr && s.total_fights) hdr.textContent = `${s.total_fights.toLocaleString()} fights · ${s.unique_players.toLocaleString()} players · powered by EldritchBot`;
  }).catch(() => {});
  await _ebFetchPage(1);
}

async function _ebFetchPage(page) {
  if (_ebLoading) return;
  _ebLoading = true;
  _ebPage = page;
  const list = document.getElementById('fightList');
  list.innerHTML = '<div class="loading-state"><div class="spinner spinner--purple"></div><span>Loading fights…</span></div>';
  try {
    const params = new URLSearchParams({ limit: _ebPageSize, offset: (_ebPage - 1) * _ebPageSize });
    if (_ebSearch) params.set('town', _ebSearch);
    const data = await fetch(`/api/eb_fights?${params}`).then(r => r.json());
    _ebFights = data.fights || data;
    _ebTotal  = data.total  || _ebFights.length;
    _renderFightList();
  } catch {
    list.innerHTML = '<div class="empty-state">Failed to load fights.</div>';
  }
  _ebLoading = false;
}

function _ebSearchReset() {
  _ebSearch = document.getElementById('fightSearchInput')?.value.trim() || '';
  _ebFetchPage(1);
}

function _renderFightList() {
  const el = document.getElementById('fightList');
  if (!_ebFights.length) {
    el.innerHTML = '<div class="empty-state">No fights found.</div>';
    return;
  }

  const totalPages = Math.max(1, Math.ceil(_ebTotal / _ebPageSize));

  const rows = _ebFights.map(f => {
    const won    = f.attackers_won;
    const winCls = won ? 'fight-winner--att' : 'fight-winner--def';
    const attCls = won ? 'fr-town--att fr-town--winner' : 'fr-town--att';
    const defCls = won ? 'fr-town--def' : 'fr-town--def fr-town--winner';
    const dur    = f.duration_mins ? `${Math.round(f.duration_mins)}m` : '—';
    const attK   = `${f.attacker_pkills ?? 0}K ${f.attacker_gkills ?? 0}G`;
    const defK   = `${f.defender_pkills ?? 0}K ${f.defender_gkills ?? 0}G`;
    const world  = f.world ? `<span class="fr-world">${f.world}</span>` : '<span></span>';
    const date   = f.fight_date || '';
    const time   = f.fight_time || '';
    return `
      <div class="fight-row" onclick="openEbFight('${f.id}')">
        <div class="fr-date">
          <span class="fr-date-d">${date}</span>
          <span class="fr-date-t">${time}</span>
        </div>
        ${world}
        <div class="fr-matchup">
          <span class="fr-town ${attCls}">⚔ ${f.attacker_town || 'Attackers'}</span>
          <span class="fr-kills fr-kills--att">${attK}</span>
          <span class="fr-vs">vs</span>
          <span class="fr-kills fr-kills--def">${defK}</span>
          <span class="fr-town ${defCls}">🛡 ${f.defender_town || 'Defenders'}</span>
        </div>
        <div class="fr-meta">
          <span class="fr-players">${f.total_players ?? '?'}p</span>
          <span class="fr-dur">${dur}</span>
        </div>
        <div class="fight-winner-badge ${winCls}">${won ? (f.attacker_town||'ATK') : (f.defender_town||'DEF')} Win</div>
      </div>`;
  }).join('');

  const pagBtn = (label, page, disabled, active = false) =>
    `<button class="fight-page-btn${active?' active':''}" ${disabled?'disabled':''} onclick="_ebFetchPage(${page})">${label}</button>`;

  const pageStart = Math.max(1, _ebPage - 2);
  const pageEnd   = Math.min(totalPages, pageStart + 4);
  const pagNums   = Array.from({length: pageEnd - pageStart + 1}, (_,i) => pageStart+i)
    .map(p => pagBtn(p, p, false, p === _ebPage)).join('');

  const pagination = `
    <div class="fight-pagination">
      ${pagBtn('«', 1, _ebPage === 1)}
      ${pagBtn('‹', _ebPage - 1, _ebPage === 1)}
      ${pagNums}
      ${pagBtn('›', _ebPage + 1, _ebPage === totalPages)}
      ${pagBtn('»', totalPages, _ebPage === totalPages)}
      <span class="fight-page-info">Page ${_ebPage} of ${totalPages} · ${_ebTotal.toLocaleString()} fights</span>
    </div>`;

  el.innerHTML = `
    <div class="fight-list-controls">
      <input id="fightSearchInput" class="search-input" placeholder="Filter by town…"
             value="${_ebSearch}" style="max-width:260px"
             oninput="clearTimeout(this._t);this._t=setTimeout(_ebSearchReset,350)">
    </div>
    <div class="fight-table">
      <div class="fight-table-head">
        <span>Date</span><span>World</span>
        <span style="text-align:center">Matchup</span>
        <span>Size</span><span>Result</span>
      </div>
      ${rows}
    </div>
    ${pagination}`;
}

async function openEbFight(fightId) {
  document.getElementById('fightList').style.display = 'none';
  const detail = document.getElementById('fightDetail');
  detail.style.display = '';
  detail.innerHTML = '<div class="loading-state"><div class="spinner spinner--purple"></div><span>Loading fight…</span></div>';
  try {
    const fight = await fetch(`/api/eb_fights/${encodeURIComponent(fightId)}`).then(r => r.json());
    currentEbFight = fight;
    _renderEbFightDetail(fight);
  } catch {
    detail.innerHTML = '<div class="empty-state">Failed to load fight data.</div>';
  }
}

function backToFightList() {
  document.getElementById('fightDetail').style.display = 'none';
  document.getElementById('fightList').style.display = '';
  currentEbFight = null;
}

function setEbPlayerSort(sort) {
  _ebPlayerSort = sort;
  if (currentEbFight) _renderEbFightDetail(currentEbFight);
}

function _ebSortPlayers(players) {
  const p = [...players];
  if (_ebPlayerSort === 'golems') p.sort((a, b) => (b.gkills||0) - (a.gkills||0) || (b.pkills||0) - (a.pkills||0));
  else if (_ebPlayerSort === 'lamps') p.sort((a, b) => (b.lamps||0) - (a.lamps||0));
  else p.sort((a, b) => (b.pkills||0) - (a.pkills||0) || (b.assists||0) - (a.assists||0));
  return p;
}

/* Convert an EB fight row (from /api/eb_fights/<id>) into the same schema
   as fights_parser.py so we can use one unified renderer. */
function _ebToLokaFmt(row) {
  const fd    = row.fight_data || {};
  const fdAtt = fd.attackers?.players || [];
  const fdDef = fd.defenders?.players || [];

  const mapPlayer = (p) => ({
    name:           p.name,
    town:           p.town || '',
    kills:          p.pkills || 0,
    deaths:         p.deaths || 0,
    assists:        Math.round(p.assists || 0),
    pearls:         p.pearls || 0,
    damage_dealt:   0,                          // not tracked by EB
    damage_taken:   0,
    total_hits:     0,
    crits:          0,
    crit_ratio:     0,
    shulkers_broken:0,
    shulkers_placed:0,
    blocks_broken:  0,
    items_dropped:  0,
    golem_kills:    p.gkills || 0,
    charges_taken:  p.lamps  || 0,
    charge_part:    (p.charges || []).length,
    ancient_ingots: p.ancientIngots || 0,
    potions:        p.potions ? {'(total)': p.potions} : {},
    food:           p.food    ? {'(total)': p.food}    : {},
    // EB-specific extras passed through for richer modal
    dps_avg:        p.dps?.avg  || null,
    dps_max:        p.dps?.max  || null,
    close_calls:    p.closeCalls || 0,
    deathInfo:      p.deathInfo || [],
    charges_arr:    p.charges   || [],
  });

  const sortFn = (a,b) =>
    b.kills - a.kills || b.assists - a.assists || b.golem_kills - a.golem_kills;

  const att = fdAtt.map(mapPlayer).sort(sortFn);
  const def = fdDef.map(mapPlayer).sort(sortFn);

  const totals = (players) => ({
    kills:         players.reduce((s,p) => s + p.kills, 0),
    damage:        0,
    pearls:        players.reduce((s,p) => s + p.pearls, 0),
    food:          players.reduce((s,p) => s + (p.food['(total)'] || 0), 0),
    golem_kills:   players.reduce((s,p) => s + p.golem_kills, 0),
    charges:       players.reduce((s,p) => s + p.charges_taken, 0),
    charge_part:   players.reduce((s,p) => s + p.charge_part, 0),
    total_potions: players.reduce((s,p) => s + (p.potions['(total)'] || 0), 0),
  });

  return {
    filename:        row.id,
    location:        row.location || '',
    winner:          row.attackers_won ? row.attacker_town : row.defender_town,
    duration:        row.duration_mins ? `${Math.round(row.duration_mins)} minutes` : '',
    mutator:         '',
    attacker_town:   row.attacker_town || '',
    defender_town:   row.defender_town || '',
    world:           row.world || '',
    date_display:    row.fight_date || '',
    time_display:    row.fight_time || '',
    attackers:       att,
    defenders:       def,
    attacker_kills:  att.reduce((s,p) => s + p.kills, 0),
    defender_kills:  def.reduce((s,p) => s + p.kills, 0),
    attacker_totals: totals(att),
    defender_totals: totals(def),
  };
}

function _ebTotals(players) {
  return players.reduce((t, p) => ({
    kills:   t.kills   + (p.pkills  || 0),
    golems:  t.golems  + (p.gkills  || 0),
    charges: t.charges + (p.lamps   || 0),
    potions: t.potions + (p.potions || 0),
    pearls:  t.pearls  + (p.pearls  || 0),
    food:    t.food    + (p.food    || 0),
  }), { kills:0, golems:0, charges:0, potions:0, pearls:0, food:0 });
}

function _renderEbFightDetail(row) {
  // Convert EB JSON → Loka format, then use shared renderer
  const loka = _ebToLokaFmt(row);
  _renderFightDetail(loka, row);
}

/* Shared fight detail renderer — accepts Loka-format fight object.
   ebRaw is the original EB row (for modal access), null for native Loka fights. */
function _renderFightDetail(fight, ebRaw) {
  // Store for modal use
  currentEbFight = ebRaw || fight;

  const won     = fight.winner === fight.attacker_town;
  const winCls  = won ? 'fight-winner--att' : 'fight-winner--def';
  const winLbl  = fight.winner ? `${fight.winner} Win` : 'Draw';
  const dur     = fight.duration || '';
  const total   = (fight.attackers?.length||0) + (fight.defenders?.length||0);
  const metaParts = [fight.world, fight.time_display, fight.date_display,
                     total + ' players', dur].filter(Boolean);

  const renderTotals = (t, cls) => {
    const stats = [
      [t.kills,                         'Kills'],
      [t.total_potions?.toLocaleString() || 0, 'Potions'],
      [t.pearls,                        'Pearls'],
      [t.food,                          'Food'],
      [t.golem_kills,                   'Golems'],
      [t.charges,                       'Charges'],
    ];
    return `<div class="fight-team-totals fight-team-totals--${cls}">
      ${stats.map(([v,l]) => `
        <div class="ftt-item">
          <span class="ftt-val">${v}</span>
          <span class="ftt-label">${l}</span>
        </div>`).join('')}
    </div>`;
  };

  const renderPlayers = (players, cls, side) => {
    if (!players?.length) return '<div class="empty-state">No data.</div>';
    return players.map((p, i) => {
      const rLabel = i === 0 ? '1ST' : i === 1 ? '2ND' : i === 2 ? '3RD' : `#${i+1}`;
      const rClass = i < 3 ? `rank-${i+1}` : '';
      const dpsVal = p.dps_avg ? `${parseFloat(p.dps_avg).toFixed(1)} DPS`
                               : p.damage_dealt ? p.damage_dealt.toLocaleString()
                               : '—';
      const rightTitle = p.dps_avg ? 'Avg DPS' : 'Damage Dealt';
      return `
        <div class="fight-player-row ${cls}-row"
             onclick="openEbPlayerModal('${p.name.replace(/'/g,"\\'")}','${side}')">
          <div class="fight-player-rank ${rClass}">${rLabel}</div>
          <img class="fight-player-head"
               src="https://mc-heads.net/avatar/${encodeURIComponent(p.name)}/24"
               alt="" loading="lazy">
          <div class="fight-player-info">
            <div class="fight-player-name">${p.name}</div>
            <div class="fight-player-town">${p.town || ''}</div>
          </div>
          <div class="fight-player-kda">
            <span class="kda-k">${p.kills}</span>
            <span class="kda-sep">/</span>
            <span class="kda-d">${p.deaths}</span>
            <span class="kda-sep">/</span>
            <span class="kda-a">${p.assists}</span>
          </div>
          <div class="fight-player-dmg" title="${rightTitle}">${dpsVal}</div>
        </div>`;
    }).join('');
  };

  const teamPanel = (town, icon, players, totals, cls, side) => `
    <div class="fight-team-panel fight-team-panel--${cls}">
      <div class="fight-team-header fight-team-header--${cls}">
        <div class="fight-team-header-main">${icon} ${town || (cls==='att'?'Attackers':'Defenders')}</div>
        <span class="fight-team-count">${players?.length||0} players</span>
      </div>
      ${renderTotals(totals, cls)}
      <div class="fight-leaderboard-header">
        <span class="lbh-rank">Rank</span><span></span>
        <span class="lbh-name">Player</span>
        <span class="lbh-kda">K/D/A</span>
        <span class="lbh-dmg">${fight.attackers?.[0]?.dps_avg != null ? 'Avg DPS' : 'Dmg'}</span>
      </div>
      <div class="fight-leaderboard">${renderPlayers(players, cls, side)}</div>
    </div>`;

  document.getElementById('fightDetail').innerHTML = `
    <div class="fight-detail-header">
      <button class="back-btn" onclick="backToFightList()">← Back</button>
      <div>
        <div class="fight-detail-title">${fight.attacker_town} vs ${fight.defender_town}</div>
        <div class="fight-detail-meta">
          ${metaParts.join(' &nbsp;·&nbsp; ')}
          &nbsp;·&nbsp; <span class="fight-winner-inline ${winCls}">${winLbl}</span>
        </div>
      </div>
    </div>
    <div class="fight-teams-grid">
      ${teamPanel(fight.attacker_town, '⚔', fight.attackers, fight.attacker_totals, 'att', 'attacker')}
      ${teamPanel(fight.defender_town, '🛡', fight.defenders, fight.defender_totals, 'def', 'defender')}
    </div>`;
}

function openEbPlayerModal(playerName, side) {
  const fight = currentEbFight;
  if (!fight) return;
  const fd  = fight.fight_data || {};
  const all = [...(fd.attackers?.players||[]), ...(fd.defenders?.players||[])];
  const p   = all.find(x => x.name === playerName);
  if (!p) return;

  document.getElementById('fightModalName').textContent = p.name;
  document.getElementById('fightModalSubtitle').textContent =
    `${p.town || ''} · ${side === 'attacker' ? fight.attacker_town : fight.defender_town}`;

  const kd    = p.deaths ? (p.pkills / p.deaths).toFixed(2) : '∞';
  const lampsArr = p.charges || [];  // charge events array
  const deaths   = (p.deathInfo || []).map(d =>
    `<div class="detail-stat-row"><span>Killed by ${d.killedBy || '?'}</span><span class="detail-stat-val">${d.time ? Math.round(d.time) + 'm' : ''}</span></div>`
  ).join('') || '';

  document.getElementById('fightModalBody').innerHTML = `
    <div class="fight-modal-kda">
      <div class="fight-modal-stat"><div class="fight-modal-stat-val kda-k">${p.pkills||0}</div><div class="fight-modal-stat-label">Kills</div></div>
      <div class="fight-modal-stat"><div class="fight-modal-stat-val kda-d">${p.deaths||0}</div><div class="fight-modal-stat-label">Deaths</div></div>
      <div class="fight-modal-stat"><div class="fight-modal-stat-val kda-a">${Math.round(p.assists||0)}</div><div class="fight-modal-stat-label">Assists</div></div>
      <div class="fight-modal-stat"><div class="fight-modal-stat-val">${p.pearls||0}</div><div class="fight-modal-stat-label">Pearls</div></div>
    </div>
    <div class="fight-modal-dmg-row">
      <span>${kd} K/D</span>
      <span>${p.lamps||0} lamps · ${p.gkills||0} golems</span>
      ${p.closeCalls ? `<span>${p.closeCalls} close calls</span>` : ''}
    </div>
    <div class="fight-modal-dmg-row" style="margin-top:4px">
      <span>${p.potions||0} potions</span>
      <span>${p.food||0} food</span>
      ${p.ancientIngots ? `<span>${p.ancientIngots} ingots</span>` : ''}
      ${p.dps ? `<span>DPS avg ${p.dps.avg} · max ${p.dps.max}</span>` : ''}
    </div>
    ${lampsArr.length ? `
    <div class="detail-section">
      <div class="detail-section-title">Charge Times</div>
      ${lampsArr.map(c => `<div class="detail-stat-row"><span>Charge</span><span class="detail-stat-val">${c.time != null ? c.time.toFixed(1) + 'm' : '—'}</span></div>`).join('')}
    </div>` : ''}
    ${deaths ? `
    <div class="detail-section">
      <div class="detail-section-title">Deaths</div>
      ${deaths}
    </div>` : ''}
  `;
  document.getElementById('fightPlayerModal').style.display = 'flex';
}

function closeFightPlayerModal() {
  document.getElementById('fightPlayerModal').style.display = 'none';
}

/* ══════════════════════════════════════════════════════════════════════════ */
/*  PLAYERS PAGE                                                              */
/* ══════════════════════════════════════════════════════════════════════════ */

let _lbSort = 'kills';
let _lbDebounce = null;
const _lbCache = {};       // sort → rendered HTML, persists for the session
const _lbFetching = {};    // sort → true while in-flight (prevent duplicate requests)

function setLbSort(sort, btn) {
  _lbSort = sort;
  document.querySelectorAll('.ps-tab').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  const lb = document.getElementById('playerLb');
  // Cached — instant render, no network needed
  if (_lbCache[sort]) {
    lb.innerHTML = _lbCache[sort];
    return;
  }
  // Show skeleton immediately so the UI feels responsive
  lb.innerHTML = _lbSkeleton(12);
  // Debounce in case of rapid tab-flicking, but keep skeleton visible
  clearTimeout(_lbDebounce);
  _lbDebounce = setTimeout(() => loadLeaderboard(), 100);
}

function _lbSkeleton(n = 10) {
  return Array.from({length: n}, () => `
    <div class="plb-skeleton">
      <div class="plb-skel-block sm" style="width:28px"></div>
      <div class="plb-skel-block circle"></div>
      <div class="plb-skel-block" style="width:55%;max-width:130px"></div>
      <div class="plb-skel-block" style="width:60%;justify-self:end"></div>
      <div class="plb-skel-block sm" style="width:38px;justify-self:end"></div>
    </div>`).join('');
}

function _kdClass(kd) {
  const v = parseFloat(kd);
  if (isNaN(v)) return 'kd-good';
  if (v > 10)   return 'kd-legendary';
  if (v >= 1)   return 'kd-good';
  return 'kd-bad';
}

async function loadLeaderboard() {
  const lb   = document.getElementById('playerLb');
  const sort = _lbSort;
  if (!lb) return;
  if (_lbFetching[sort]) return;   // already in-flight for this sort
  _lbFetching[sort] = true;
  try {
    const rows = await fetch(`/api/eldritch/leaderboard?sort=${sort}&limit=50`).then(r => r.json());
    if (!rows.length) {
      lb.innerHTML = '<div class="players-lb-loading">No data yet — search a player to add them.</div>';
      return;
    }
    const isKda      = _lbSort === 'kda';
    const isKdaWorst = _lbSort === 'kda_worst';
    const isCharges  = _lbSort === 'charges';
    const header = isKda
      ? '<div class="plb-filter-note">Minimum 50 kills required · sorted by highest K/D ratio</div>'
      : isKdaWorst
      ? '<div class="plb-filter-note">Sorted by lowest K/D ratio</div>'
      : isCharges
      ? '<div class="plb-filter-note">Charges = lamps lit · Completion = lamps ÷ golems</div>'
      : '';
    lb.innerHTML = header + rows.map((p, i) => {
      const kdRaw = p.kd_ratio ?? (p.deaths ? (p.kills / p.deaths) : p.kills);
      const kd = Number(kdRaw).toFixed(2);
      const kdCls = _kdClass(kd);
      // Charges: main stat = "lamps / golems", right col = completion %
      let sortVal, sortLabel, rightCol;
      if (isCharges) {
        const rate   = p.charge_rate != null ? (p.charge_rate * 100).toFixed(1) : '—';
        sortVal      = `${(p.lamps ?? 0).toLocaleString()} / ${(p.golems ?? 0).toLocaleString()}`;
        sortLabel    = 'Charges / Golems';
        rightCol     = `<span class="plb-kd" title="Charge completion rate">${rate}%</span>`;
      } else {
        sortVal   = (isKda || isKdaWorst) ? kd : (p[_lbSort] ?? p.kills);
        sortLabel = { kills:'Kills', assists:'Assists', kda:'K/D', kda_worst:'K/D', conquest_wins:'Wins' }[_lbSort] || 'Kills';
        rightCol  = `<span class="plb-kd ${kdCls}" title="K/D">${kd}</span>`;
      }

      // Worst K/D gets no glory effects — plain rows only
      const rankCls = isKdaWorst ? '' : (i === 0 ? 'plb-row--gold' : i === 1 ? 'plb-row--silver' : i === 2 ? 'plb-row--bronze' : i < 10 ? 'plb-row--elite' : '');

      return `<div class="plb-row ${rankCls}" onclick="showPlayerCard('${p.uuid}')">
        <span class="plb-rank">#${i + 1}</span>
        <img class="plb-head" src="https://mc-heads.net/avatar/${p.name}/36" alt="">
        <div class="plb-name-col">
          <div class="plb-name">${p.name}</div>
          ${p.alliance ? `<div class="plb-sub">${p.alliance}</div>` : ''}
        </div>
        <span class="plb-stat"><span class="plb-stat-val">${isCharges || isKda || isKdaWorst ? sortVal : (sortVal?.toLocaleString?.() ?? sortVal)}</span><span class="plb-stat-label">${sortLabel}</span></span>
        ${rightCol}
      </div>`;
    }).join('');
    // Only cache if user hasn't switched away while loading
    if (_lbSort === sort) {
      _lbCache[sort] = lb.innerHTML;
    }
  } catch {
    lb.innerHTML = '<div class="players-lb-loading">Click a category to load leaderboard…</div>';
  } finally {
    _lbFetching[sort] = false;
  }
}

function _statRow(label, val) {
  if (!val && val !== 0) return '';
  return `<div class="pc-stat-row"><span class="pc-stat-label">${label}</span><span class="pc-stat-val">${Number(val).toLocaleString()}</span></div>`;
}

function renderPlayerCard(p) {
  const card  = document.getElementById('playerCard');
  const empty = document.getElementById('playerCardEmpty');
  if (!card) return;

  const kdRaw = p.deaths ? (p.kills / p.deaths) : p.kills;
  const kd    = p.deaths ? kdRaw.toFixed(2) : '∞';
  const kdCls = _kdClass(kd);
  const ratio = (p.conquest_wins + p.conquest_losses)
    ? ((p.conquest_wins / (p.conquest_wins + p.conquest_losses)) * 100).toFixed(0) + '%'
    : '—';
  const avatarUrl = `https://mc-heads.net/head/${p.name}/80`;

  card.innerHTML = `
    <div class="pc-hero" style="--pc-bg: url('${avatarUrl}')">
      <img class="pc-avatar" src="${avatarUrl}" alt="${p.name}">
      <div class="pc-info">
        <div class="pc-name">${p.name}</div>
        ${p.alliance ? `<div class="pc-alliance">${p.alliance}</div>` : ''}
        ${p.last_fight ? `<div class="pc-last-fight">Last fight: ${p.last_fight}</div>` : ''}
      </div>
      <div class="pc-kda-block">
        <div class="pc-kda-nums">
          <span class="pc-kda-k" title="Kills">${p.kills.toLocaleString()}</span>
          <span class="pc-kda-sep">/</span>
          <span class="pc-kda-d" title="Deaths">${p.deaths.toLocaleString()}</span>
          <span class="pc-kda-sep">/</span>
          <span class="pc-kda-a" title="Assists">${p.assists.toLocaleString()}</span>
        </div>
        <div class="pc-kd-label-row">Kills / Deaths / Assists</div>
        <div class="pc-kd-ratio ${kdCls}">${kd} K/D</div>
      </div>
    </div>
    <div class="pc-sections">
      <div class="pc-section">
        <div class="pc-section-title">⚔ Combat</div>
        ${_statRow('Kills', p.kills)}
        ${_statRow('Deaths', p.deaths)}
        ${_statRow('Assists', p.assists)}
      </div>
      <div class="pc-section">
        <div class="pc-section-title">🏰 Conquest</div>
        ${_statRow('Wins', p.conquest_wins)}
        ${_statRow('Losses', p.conquest_losses)}
        <div class="pc-stat-row"><span class="pc-stat-label">Win Rate</span><span class="pc-stat-val">${ratio}</span></div>
        ${_statRow('Golems', p.golems)}
        ${_statRow('Lamps', p.lamps)}
        ${_statRow('First Bloods', p.first_bloods)}
        ${_statRow('Close Calls', p.close_calls)}
      </div>
      <div class="pc-section">
        <div class="pc-section-title">🧪 Consumables</div>
        ${_statRow('Potions', p.potions)}
        ${_statRow('Pearls', p.pearls)}
        ${_statRow('Food', p.food)}
        ${_statRow('Ancient Ingots', p.ancient_ingots)}
      </div>
      ${p.nemesis ? `<div class="pc-section pc-section--nemesis" onclick="${p.nemesis_uuid ? `showPlayerCard('${p.nemesis_uuid}')` : `searchByName('${p.nemesis}')`}" title="View ${p.nemesis}'s profile">
        <div class="pc-section-title">💀 Nemesis</div>
        <div class="pc-nemesis-name">
          <img src="https://mc-heads.net/avatar/${p.nemesis}/24" class="pc-nemesis-head" alt="">
          ${p.nemesis}
          <span class="pc-nemesis-arrow">→</span>
        </div>
        ${p.nemesis_deaths ? `<div class="pc-nemesis-deaths">${p.nemesis_deaths} deaths to them</div>` : ''}
      </div>` : ''}
      ${p.best_kda_score ? `<div class="pc-section">
        <div class="pc-section-title">🏆 Best KDA</div>
        <div class="pc-best-kda">${p.best_kda_score}</div>
        ${p.best_kda_fight ? `<div class="pc-best-kda-fight">${p.best_kda_fight}</div>` : ''}
      </div>` : ''}
    </div>
    <div class="pc-source">Data from <a href="https://eldritchbot.com/player/${p.uuid}" target="_blank">EldritchBot</a></div>
  `;

  card.style.display = 'block';
  if (empty) empty.style.display = 'none';
}

async function searchByName(name) {
  const input = document.getElementById('playerSearchInput');
  if (input) input.value = name;
  // close autocomplete if open (hideSuggestions lives in the DOMContentLoaded closure)
  const sugBox = document.getElementById('playerSuggestions');
  if (sugBox) sugBox.classList.remove('open');
  try {
    const results = await fetch(`/api/eldritch/search?q=${encodeURIComponent(name)}`).then(r => r.json());
    const exact = results.find(r => r.name.toLowerCase() === name.toLowerCase());
    if (exact) { showPlayerCard(exact.uuid); return; }
  } catch {}
  searchPlayer();
}

async function showPlayerCard(uuid) {
  const status = document.getElementById('playerSearchStatus');
  const card   = document.getElementById('playerCard');
  const empty  = document.getElementById('playerCardEmpty');
  card.style.display = 'none';
  if (empty) empty.style.display = 'none';
  if (status) { status.textContent = 'Loading…'; status.className = 'players-search-status'; }
  try {
    const p = await fetch(`/api/eldritch/player/${uuid}`).then(r => r.json());
    if (p.error) throw new Error(p.error);
    if (status) status.textContent = '';
    renderPlayerCard(p);
  } catch (e) {
    if (empty) empty.style.display = 'flex';
    if (status) { status.textContent = e.message || 'Not found'; status.className = 'players-search-status error'; }
  }
}

async function searchPlayer() {
  const input  = document.getElementById('playerSearchInput');
  const status = document.getElementById('playerSearchStatus');
  const card   = document.getElementById('playerCard');
  const q = (input?.value || '').trim();
  if (!q) return;

  card.style.display = 'none';
  if (status) { status.textContent = 'Searching…'; status.className = 'players-search-status'; }

  // Check local leaderboard first
  try {
    const local = await fetch(`/api/eldritch/search?q=${encodeURIComponent(q)}`).then(r => r.json());
    if (local.length === 1) {
      if (status) status.textContent = '';
      renderPlayerCard(local[0]);
      return;
    }
    if (local.length > 1) {
      // Show picker
      if (status) status.textContent = '';
      const lb = document.getElementById('playerLb');
      if (lb) lb.innerHTML = local.map((p, i) => `
        <div class="plb-row" onclick="showPlayerCard('${p.uuid}')">
          <span class="plb-rank">#${i+1}</span>
          <img class="plb-head" src="https://mc-heads.net/avatar/${p.name}/36" alt="">
          <div class="plb-name-col">
            <div class="plb-name">${p.name}</div>
            ${p.alliance ? `<div class="plb-sub">${p.alliance}</div>` : ''}
          </div>
          <span class="plb-stat"></span>
          <span class="plb-kd"></span>
        </div>`).join('');
      return;
    }
  } catch { /* fall through to direct fetch */ }

  // Not in DB — resolve via Mojang + fetch from EldritchBot
  try {
    const p = await fetch(`/api/eldritch/player/${encodeURIComponent(q)}`).then(r => r.json());
    if (p.error) throw new Error(p.error);
    if (status) status.textContent = '';
    renderPlayerCard(p);
  } catch (e) {
    if (status) {
      status.textContent = e.message?.includes('Mojang') ? 'Player not found' : (e.message || 'Failed to fetch stats');
      status.className = 'players-search-status error';
    }
  }
}

document.addEventListener('DOMContentLoaded', () => {
  const input = document.getElementById('playerSearchInput');
  if (!input) return;

  input.addEventListener('keydown', e => { if (e.key === 'Enter') { hideSuggestions(); searchPlayer(); } });

  // Autocomplete
  const sugBox = document.getElementById('playerSuggestions');
  let _sugDebounce = null;
  let _sugActive = -1;
  let _sugItems = [];

  function hideSuggestions() {
    sugBox.classList.remove('open');
    _sugActive = -1;
  }

  function pickSuggestion(p) {
    input.value = p.name;
    hideSuggestions();
    showPlayerCard(p.uuid);
  }

  function renderSuggestions(players) {
    _sugItems = players;
    _sugActive = -1;
    if (!players.length) { hideSuggestions(); return; }
    sugBox.innerHTML = players.map((p, i) => `
      <div class="ps-suggestion" data-i="${i}">
        <img src="https://mc-heads.net/avatar/${p.name}/24" width="24" height="24" alt="">
        <span class="ps-suggestion-name">${p.name}</span>
        ${p.alliance ? `<span class="ps-suggestion-alliance">${p.alliance}</span>` : ''}
      </div>`).join('');
    sugBox.querySelectorAll('.ps-suggestion').forEach((el, i) => {
      el.addEventListener('mousedown', e => { e.preventDefault(); pickSuggestion(_sugItems[i]); });
    });
    sugBox.classList.add('open');
  }

  input.addEventListener('input', () => {
    clearTimeout(_sugDebounce);
    const q = input.value.trim();
    if (!q) { hideSuggestions(); return; }
    _sugDebounce = setTimeout(async () => {
      try {
        const results = await fetch(`/api/eldritch/search?q=${encodeURIComponent(q)}`).then(r => r.json());
        if (input.value.trim() === q) renderSuggestions(results.slice(0, 8));
      } catch { hideSuggestions(); }
    }, 150);
  });

  input.addEventListener('keydown', e => {
    if (!sugBox.classList.contains('open')) return;
    const items = sugBox.querySelectorAll('.ps-suggestion');
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      _sugActive = Math.min(_sugActive + 1, items.length - 1);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      _sugActive = Math.max(_sugActive - 1, -1);
    } else if (e.key === 'Escape') {
      hideSuggestions(); return;
    } else { return; }
    items.forEach((el, i) => el.classList.toggle('active', i === _sugActive));
    if (_sugActive >= 0) input.value = _sugItems[_sugActive].name;
  });

  document.addEventListener('click', e => {
    if (!sugBox.contains(e.target) && e.target !== input) hideSuggestions();
  });
});

async function updateScrapeProgress() {
  try {
    const s = await fetch('/api/eldritch/status').then(r => r.json());
    const wrap  = document.getElementById('progressWrap');
    const label = document.getElementById('progressLabel');
    const bar   = document.getElementById('progressBar');
    if (!wrap) return;
    const total = s.loka_players || 0;
    const done  = (s.done || 0) + (s.not_found || 0);
    if (!total || done >= total) { wrap.style.display = 'none'; return; }
    const pct = Math.round((done / total) * 100);
    const eta = s.eta_minutes > 60
      ? `~${Math.round(s.eta_minutes / 60)}h remaining`
      : `~${s.eta_minutes}m remaining`;
    label.textContent = `Building player database: ${done.toLocaleString()} / ${total.toLocaleString()} (${pct}%) — ${eta}`;
    bar.style.width = pct + '%';
    wrap.style.display = 'block';
  } catch { /* silent */ }
}

async function initPlayersPage() {
  // Leaderboard loads on tab click only — no auto-refresh
}
