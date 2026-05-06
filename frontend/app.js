'use strict';

// ── State ──────────────────────────────────────────────────────────────
let appConfig = null;
let staleness = { transit: null, traffic: null, bikeshare: null };
let refreshTimer = null;

// ── Line colors ────────────────────────────────────────────────────────
const WMATA_COLORS = {
  OR: '#F27B26', SV: '#A2AAAD', BL: '#00A1E0',
  RD: '#E51636', GR: '#00A859', YL: '#FFD700',
};

const MTA_COLORS = {
  '1':'#EE352E','2':'#EE352E','3':'#EE352E',
  '4':'#00933C','5':'#00933C','6':'#00933C',
  '7':'#B933AD',
  'A':'#0039A6','C':'#0039A6','E':'#0039A6',
  'B':'#FF6319','D':'#FF6319','F':'#FF6319','M':'#FF6319',
  'G':'#6CBE45',
  'J':'#996633','Z':'#996633',
  'L':'#A7A9AC',
  'N':'#FCCC0A','Q':'#FCCC0A','R':'#FCCC0A','W':'#FCCC0A',
  'S':'#808183',
};

function lineColor(line, provider) {
  if (provider === 'wmata') return WMATA_COLORS[line] || '#666';
  return MTA_COLORS[line] || '#666';
}

// ── Helpers ────────────────────────────────────────────────────────────
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function fmtMinutes(minutes, status) {
  if (status === 'ARR' || minutes === 0) return { text: 'BRD', boarding: true };
  return { text: `${minutes} min`, boarding: false };
}

function delayClass(delayMin) {
  if (delayMin <= 2) return 'none';
  if (delayMin <= 10) return 'light';
  return 'heavy';
}

function delayLabel(delayMin) {
  if (delayMin <= 2) return 'On time';
  return `+${delayMin} min`;
}

function updateClock() {
  const now = new Date();
  const h = now.getHours();
  const m = String(now.getMinutes()).padStart(2, '0');
  const ampm = h >= 12 ? 'PM' : 'AM';
  const h12 = h % 12 || 12;
  document.getElementById('clock').textContent = `${h12}:${m} ${ampm}`;
}

function updateHealthDot() {
  const dot = document.getElementById('health-dot');
  const values = Object.values(staleness).filter(v => v !== null);
  if (!values.length) return;
  const max = Math.max(...values);
  const warning = appConfig?.display?.staleness_warning_sec ?? 60;
  const critical = appConfig?.display?.staleness_critical_sec ?? 120;
  if (max >= critical) {
    dot.className = 'health-dot error';
  } else if (max >= warning) {
    dot.className = 'health-dot stale';
  } else {
    dot.className = 'health-dot healthy';
  }
}

// ── Transit rendering ──────────────────────────────────────────────────
function renderTransit(data) {
  const col = document.getElementById('transit-col');
  col.innerHTML = '';

  if (!data || !data.length) {
    col.appendChild(el('div', 'loading-msg', 'No transit data'));
    return;
  }

  const provider = appConfig?.transit?.provider || 'mta';

  data.forEach(station => {
    const card = el('div', 'station-card');

    // Header
    const header = el('div', 'station-header');
    const nameRow = el('div', 'station-name-row');

    // Line pills
    const pills = el('div', 'line-pills');
    (station.lines || []).forEach(line => {
      const pill = el('span', 'line-pill', line);
      pill.style.background = lineColor(line, provider);
      // MTA yellow lines need dark text
      if (['N','Q','R','W'].includes(line)) pill.style.color = '#1a1a1a';
      pills.appendChild(pill);
    });
    nameRow.appendChild(pills);
    nameRow.appendChild(el('span', 'station-name', station.station_name));
    header.appendChild(nameRow);

    if (station.walk_minutes != null) {
      header.appendChild(el('span', 'walk-time', `${station.walk_minutes} min walk`));
    }

    card.appendChild(header);

    // Direction blocks
    (station.directions || []).forEach(direction => {
      const block = el('div', 'direction-block');
      block.appendChild(el('div', 'direction-label', direction.label));

      const arrivals = direction.arrivals || [];
      if (!arrivals.length) {
        block.appendChild(el('div', 'no-arrivals', 'No trains scheduled'));
      } else {
        arrivals.slice(0, 4).forEach(arrival => {
          const row = el('div', 'arrival-row');

          const linePill = el('span', 'arrival-line-pill', arrival.line);
          linePill.style.background = arrival.line_color || lineColor(arrival.line, provider);
          if (['N','Q','R','W'].includes(arrival.line)) linePill.style.color = '#1a1a1a';
          row.appendChild(linePill);

          const dest = el('span', 'arrival-destination',
            arrival.destination || direction.label);
          row.appendChild(dest);

          const { text, boarding } = fmtMinutes(arrival.minutes, arrival.status);
          const minEl = el('span', 'arrival-minutes' + (boarding ? ' boarding' : ''), text);
          row.appendChild(minEl);

          block.appendChild(row);
        });
      }

      card.appendChild(block);
    });

    col.appendChild(card);
  });
}

// ── Traffic rendering ──────────────────────────────────────────────────
function renderTraffic(data) {
  const section = document.getElementById('traffic-section');
  if (!data || !data.length) {
    section.style.display = 'none';
    return;
  }
  section.style.display = '';
  section.innerHTML = '';
  section.appendChild(el('div', 'section-header', 'Traffic'));

  data.forEach(route => {
    const card = el('div', 'route-card');
    card.appendChild(el('div', 'route-label', route.label));

    if (route.error) {
      card.appendChild(el('div', 'route-error', route.error));
    } else {
      const timeRow = el('div', 'route-time-row');
      timeRow.appendChild(el('span', 'route-duration', route.duration_minutes));
      timeRow.appendChild(el('span', 'route-unit', 'min'));

      const delay = route.delay_minutes ?? 0;
      const badge = el('span', `route-delay ${delayClass(delay)}`, delayLabel(delay));
      timeRow.appendChild(badge);
      card.appendChild(timeRow);

      if (route.summary) {
        card.appendChild(el('div', 'route-via', `via ${route.summary}`));
      }
    }

    section.appendChild(card);
  });
}

// ── Bikeshare rendering ────────────────────────────────────────────────
function renderBikeshare(data) {
  const section = document.getElementById('bikeshare-section');
  if (!data || !data.length) {
    section.style.display = 'none';
    return;
  }
  section.style.display = '';
  section.innerHTML = '';
  section.appendChild(el('div', 'section-header', 'Bikeshare'));

  data.forEach(station => {
    const card = el('div', 'dock-card');
    card.appendChild(el('div', 'dock-name', station.name));

    const counts = el('div', 'dock-counts');

    const bikeItem = el('div', 'dock-count-item');
    const bikeNum = el('div', `dock-count-num${station.bikes_available === 0 ? ' zero' : ''}`,
      station.bikes_available);
    bikeItem.appendChild(bikeNum);
    bikeItem.appendChild(el('div', 'dock-count-label', 'Bikes'));
    counts.appendChild(bikeItem);

    if (station.e_bikes_available > 0) {
      const eItem = el('div', 'dock-count-item');
      eItem.appendChild(el('div', 'dock-count-num', station.e_bikes_available));
      eItem.appendChild(el('div', 'dock-count-label', 'E-bikes'));
      counts.appendChild(eItem);
    }

    const dockItem = el('div', 'dock-count-item');
    const dockNum = el('div', `dock-count-num${station.docks_available === 0 ? ' zero' : ''}`,
      station.docks_available);
    dockItem.appendChild(dockNum);
    dockItem.appendChild(el('div', 'dock-count-label', 'Docks'));
    counts.appendChild(dockItem);

    card.appendChild(counts);

    if (!station.is_renting) {
      card.appendChild(el('div', 'dock-closed', 'Station closed'));
    }

    section.appendChild(card);
  });
}

// ── Data fetching ──────────────────────────────────────────────────────
async function fetchJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function refreshTransit() {
  try {
    const resp = await fetchJSON('/api/transit');
    if (resp.success) {
      renderTransit(resp.data);
      staleness.transit = resp.staleness_seconds;
      updateHealthDot();
    }
  } catch (err) {
    console.error('Transit fetch error:', err);
  }
}

async function refreshTraffic() {
  if (!appConfig?.traffic) return;
  try {
    const resp = await fetchJSON('/api/traffic');
    if (resp.success) {
      renderTraffic(resp.data);
      staleness.traffic = resp.staleness_seconds;
      updateHealthDot();
    }
  } catch (err) {
    console.error('Traffic fetch error:', err);
  }
}

async function refreshBikeshare() {
  if (!appConfig?.bikeshare) return;
  try {
    const resp = await fetchJSON('/api/bikeshare');
    if (resp.success) {
      renderBikeshare(resp.data);
      staleness.bikeshare = resp.staleness_seconds;
      updateHealthDot();
    }
  } catch (err) {
    console.error('Bikeshare fetch error:', err);
  }
}

async function refreshAll() {
  await Promise.all([refreshTransit(), refreshTraffic(), refreshBikeshare()]);
}

// ── Init ───────────────────────────────────────────────────────────────
async function init() {
  // Clock
  updateClock();
  setInterval(updateClock, 10000);

  // Load config
  try {
    const resp = await fetchJSON('/api/config');
    if (!resp.success) throw new Error('Config request failed');
    appConfig = resp.data;
  } catch (err) {
    console.error('Failed to load config:', err);
    document.getElementById('transit-col').innerHTML =
      '<div class="loading-msg">Failed to load config. Check server logs.</div>';
    return;
  }

  // Apply location name
  if (appConfig.location?.name) {
    document.getElementById('location-name').textContent = appConfig.location.name;
    document.title = `PeopleMover — ${appConfig.location.name}`;
  }

  // Apply orientation
  if (appConfig.display?.orientation === 'portrait') {
    document.getElementById('dashboard').classList.add('portrait');
  }

  // Hide sidebar if no traffic or bikeshare
  if (!appConfig.traffic && !appConfig.bikeshare) {
    document.getElementById('sidebar').style.display = 'none';
    document.querySelector('.main-grid').style.gridTemplateColumns = '1fr';
  }

  // Initial fetch
  await refreshAll();

  // Schedule polling
  const interval = appConfig.display?.refresh_interval_ms ?? 15000;
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = setInterval(refreshAll, interval);
}

document.addEventListener('DOMContentLoaded', init);
