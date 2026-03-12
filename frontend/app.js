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

function iconUrl(name) {
  return `https://mc-heads.net/item/${name.toLowerCase()}`;
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
/*  INDEX PAGE                                                                */
/* ══════════════════════════════════════════════════════════════════════════ */

let allItems    = [];
let currentSort = 'volume';
let sortDir     = -1;
let recentLoaded = false;

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

/* ── tabs ───────────────────────────────────────────────────────────────── */
function initTabs() {
  document.querySelectorAll('.tab').forEach(btn => {
    btn.addEventListener('click', () => {
      const tab = btn.dataset.tab;
      document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      document.getElementById('tab-market').style.display = tab === 'market' ? '' : 'none';
      document.getElementById('tab-recent').style.display = tab === 'recent' ? '' : 'none';
      if (tab === 'recent') loadRecent();
    });
  });
}

/* ── init index ─────────────────────────────────────────────────────────── */
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

/* ── router ──────────────────────────────────────────────────────────────── */
if (document.getElementById('itemGrid')) {
  initIndex();
} else if (document.getElementById('zoneBody')) {
  initItem();
}
