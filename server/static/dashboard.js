// ── DTF Floor Monitor — Dashboard JS ──

// Global fetch wrapper: redirect to login on 401
const _origFetch = window.fetch;
window.fetch = async function(...args) {
  const resp = await _origFetch(...args);
  if (resp.status === 401 && !String(args[0]).includes('/api/customer/')) {
    window.location.href = '/login';
  }
  return resp;
};

let ws = null;
let reconnectTimer = null;
let lastState = [];       // ALL machines from WS (unfiltered)
let filteredState = [];   // Machines filtered by current warehouse
let activePcTab = null;
let currentFilter = 'all';
let currentWarehouse = null;  // null = all warehouses
let warehouseList = [];       // distinct warehouse names

// ── Clock ──
function updateClock() {
  const now = new Date();
  document.getElementById('clock').textContent =
    now.toLocaleTimeString('en-US', { hour:'2-digit', minute:'2-digit', second:'2-digit', hour12: false });
}
setInterval(updateClock, 1000);
updateClock();

// ── WebSocket ──
function connectWS() {
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${protocol}//${location.host}/ws/dashboard`);

  ws.onopen = () => {
    const badge = document.getElementById('conn-badge');
    badge.className = 'live-badge connected';
    badge.innerHTML = '<div class="live-dot"></div> LIVE';
    if (reconnectTimer) { clearInterval(reconnectTimer); reconnectTimer = null; }
  };

  ws.onmessage = async (event) => {
    const data = JSON.parse(event.data);
    if (data.type === 'state_update') {
      lastState = data.machines;
      await loadWarehouses();
      applyWarehouseFilter();
      renderAll();
      loadHistory();
      renderCustomerFiles();
    }
    if (data.type === 'customer_file_uploaded') {
      loadCustomers();
      renderCustomerFiles();
    }
  };

  ws.onclose = () => {
    const badge = document.getElementById('conn-badge');
    badge.className = 'live-badge disconnected';
    badge.innerHTML = '<div class="live-dot"></div> OFFLINE';
    if (!reconnectTimer) {
      reconnectTimer = setInterval(connectWS, 3000);
    }
  };

  ws.onerror = () => ws.close();
}

// ── Warehouse filtering ──

async function loadWarehouses() {
  try {
    const resp = await fetch('/api/warehouses');
    warehouseList = await resp.json();
  } catch (err) {
    console.error('Failed to load warehouses:', err);
  }
}

function applyWarehouseFilter() {
  if (currentWarehouse === '__unassigned__') {
    filteredState = lastState.filter(m => !m.machine.warehouse);
  } else if (currentWarehouse) {
    filteredState = lastState.filter(m => m.machine.warehouse === currentWarehouse);
  } else {
    filteredState = lastState;
  }
  // Reset PC tab if it's no longer in filtered list
  if (activePcTab && !filteredState.find(m => m.machine.id === activePcTab)) {
    activePcTab = filteredState.length > 0 ? filteredState[0].machine.id : null;
  }
  if (!activePcTab && filteredState.length > 0) {
    activePcTab = filteredState[0].machine.id;
  }
  renderSidebar();
}

function renderSidebar() {
  const list = document.getElementById('warehouse-list');
  // "All" item
  let html = `
    <div class="sidebar-item ${!currentWarehouse ? 'active' : ''}" data-warehouse="">
      <div class="sidebar-icon">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>
      </div>
      All Warehouses
      <span class="sidebar-count">${lastState.length}</span>
    </div>`;

  warehouseList.forEach(w => {
    const count = lastState.filter(m => m.machine.warehouse === w).length;
    html += `
    <div class="sidebar-item ${currentWarehouse === w ? 'active' : ''}" data-warehouse="${esc(w)}">
      <div class="sidebar-icon">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 8.35V20a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V8.35A2 2 0 0 1 3.26 6.5l8-3.2a2 2 0 0 1 1.48 0l8 3.2A2 2 0 0 1 22 8.35z"/><path d="M6 18h12"/><path d="M6 14h12"/></svg>
      </div>
      ${esc(w)}
      <span class="sidebar-count">${count}</span>
      <button class="sidebar-delete-btn" data-warehouse-name="${esc(w)}" title="Delete warehouse">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18"/><path d="M6 6l12 12"/></svg>
      </button>
    </div>`;
  });

  // Unassigned machines
  const unassigned = lastState.filter(m => !m.machine.warehouse).length;
  if (unassigned > 0 && warehouseList.length > 0) {
    html += `
    <div class="sidebar-item ${currentWarehouse === '__unassigned__' ? 'active' : ''}" data-warehouse="__unassigned__" style="margin-top:8px;border-top:1px solid var(--border);padding-top:18px;">
      <div class="sidebar-icon" style="background:rgba(255,77,77,0.1);">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--red)" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 8v4"/><path d="M12 16h.01"/></svg>
      </div>
      Unassigned
      <span class="sidebar-count">${unassigned}</span>
    </div>`;
  }

  list.innerHTML = html;
}

// Sidebar click handler
document.getElementById('warehouse-list').addEventListener('click', async (e) => {
  // Delete warehouse button
  const delBtn = e.target.closest('.sidebar-delete-btn');
  if (delBtn) {
    e.stopPropagation();
    const name = delBtn.dataset.warehouseName;
    const count = lastState.filter(m => m.machine.warehouse === name).length;
    let msg = `Delete warehouse "${name}"?`;
    if (count > 0) msg += `\n\n${count} machine(s) will be unassigned.`;
    if (!confirm(msg)) return;
    try {
      const resp = await fetch(`/api/warehouses/${encodeURIComponent(name)}`, { method: 'DELETE' });
      const data = await resp.json();
      warehouseList = data.warehouses;
      if (currentWarehouse === name) currentWarehouse = null;
      applyWarehouseFilter();
      renderAll();
      loadHistory();
    } catch (err) {
      alert('Failed to delete warehouse: ' + err.message);
    }
    return;
  }

  // Select warehouse
  const item = e.target.closest('.sidebar-item');
  if (!item) return;
  const w = item.dataset.warehouse;
  if (w === '') {
    currentWarehouse = null;
  } else if (w === '__unassigned__') {
    currentWarehouse = '__unassigned__';
  } else {
    currentWarehouse = w;
  }
  applyWarehouseFilter();
  renderAll();
  loadHistory();
  if (document.getElementById('view-reports').style.display !== 'none') loadReport();
});

// Add new warehouse — saves to DB
document.getElementById('add-warehouse-btn').addEventListener('click', async () => {
  const input = document.getElementById('new-warehouse-input');
  const name = input.value.trim();
  if (!name) return;
  input.value = '';

  try {
    const resp = await fetch('/api/warehouses', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ warehouse: name }),
    });
    const data = await resp.json();
    warehouseList = data.warehouses;
    currentWarehouse = name;
    applyWarehouseFilter();
    renderAll();
  } catch (err) {
    alert('Failed to create warehouse: ' + err.message);
  }
});

document.getElementById('new-warehouse-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') document.getElementById('add-warehouse-btn').click();
});

// ── Render Everything ──
function renderAll() {
  renderKPIs();
  renderMachines();
  renderQueue();
  renderInchGrid();
  renderPcTabs();
  renderPcFiles();
}

// ── KPIs ──
function renderKPIs() {
  let activeCount = 0, queueCount = 0, totalInch = 0, todayInch = 0, todayJobs = 0, online = 0;

  filteredState.forEach(m => {
    if (m.printing) activeCount++;
    queueCount += m.queued.length;
    totalInch += m.total_queued_inches || 0;
    todayInch += m.today_inches || 0;
    todayJobs += m.today_jobs || 0;
    if (m.machine.is_online) online++;
  });

  document.getElementById('kpi-active').innerHTML = `${activeCount}<span class="stat-unit"> jobs</span>`;
  document.getElementById('kpi-today-jobs').textContent = todayJobs;
  document.getElementById('kpi-queue').innerHTML = `${queueCount}<span class="stat-unit"> files</span>`;
  document.getElementById('kpi-inch').innerHTML = `${totalInch.toFixed(1)}<span class="stat-unit"> in</span>`;
  document.getElementById('kpi-output').innerHTML = `${todayInch.toFixed(1)}<span class="stat-unit"> in</span>`;
  document.getElementById('kpi-online').textContent = online;
}

// ── Machines Grid ──
function renderMachines() {
  const grid = document.getElementById('machines-grid');
  let filtered = filteredState;

  if (currentFilter === 'printing') {
    filtered = filteredState.filter(m => m.printing);
  } else if (currentFilter === 'idle') {
    filtered = filteredState.filter(m => !m.printing && m.machine.is_online);
  }

  if (filtered.length === 0) {
    grid.innerHTML = '<div style="padding:40px;text-align:center;font-family:var(--mono);font-size:12px;color:var(--text3);grid-column:1/-1;">No machines found for this filter.</div>';
    return;
  }

  grid.innerHTML = filtered.map(m => {
    const online = m.machine.is_online;
    const isPrinting = !!m.printing;

    let statusHtml, statusClass;
    if (!online) {
      statusHtml = 'OFFLINE';
      statusClass = 'status-offline';
    } else if (isPrinting) {
      statusHtml = '<span class="pulse-dot" style="width:5px;height:5px;border-radius:50%;background:var(--green);animation:pulse 1.5s ease infinite;"></span> PRINTING';
      statusClass = 'status-printing';
    } else {
      statusHtml = 'IDLE';
      statusClass = 'status-idle';
    }

    let printingHtml = '';
    if (isPrinting) {
      const pAll = m.printing_all || [m.printing];
      const isNest = m.printing_nest && pAll.length > 1;

      if (isNest) {
        const totalIn = pAll.reduce((a, p) => a + p.print_inches * (p.copies || 1), 0);
        printingHtml = `
          <div class="mc-printing">
            <div class="mc-printing-label"><span class="pulse-dot"></span> Printing NEST (${pAll.length} files)</div>
            <div class="mc-printing-file">${totalIn.toFixed(1)} in total</div>
            <div class="mc-printing-meta" style="margin-top:4px;">
              ${pAll.map(p => {
                const c = p.copies || 1;
                const inTxt = c > 1 ? `${p.print_inches.toFixed(1)} in x${c}` : `${p.print_inches.toFixed(1)} in`;
                return `<div style="color:var(--text2);font-size:11px;">· ${esc(p.filename)} (${inTxt})</div>`;
              }).join('')}
            </div>
          </div>`;
      } else {
        const p = m.printing;
        const c = p.copies || 1;
        const inTxt = c > 1 ? `${p.print_inches.toFixed(1)} in x${c} = ${(p.print_inches * c).toFixed(1)} in` : `${p.print_inches.toFixed(1)} in`;
        printingHtml = `
          <div class="mc-printing">
            <div class="mc-printing-label"><span class="pulse-dot"></span> Currently printing</div>
            <div class="mc-printing-file">${esc(p.filename)}</div>
            <div class="mc-printing-meta">${p.width_px}x${p.height_px} px | ${inTxt}</div>
          </div>`;
      }
    } else if (online) {
      printingHtml = '<div class="mc-idle-msg" style="padding:8px 0;">— waiting for new job —</div>';
    } else {
      printingHtml = '<div class="mc-idle-msg" style="padding:8px 0;">— no connection —</div>';
    }

    return `
    <div class="machine-card">
      <div class="mc-header">
        <div>
          <div class="mc-id">${esc(m.machine.id).substring(0, 8)}</div>
          <div class="mc-name">${esc(m.machine.name)}</div>
        </div>
        <span class="status-badge ${statusClass}">${statusHtml}</span>
      </div>
      ${printingHtml}
      <div class="mc-meta">
        <span>Remaining: <strong>${m.total_queued_inches.toFixed(1)} in</strong></span>
        <span>Queue: <strong>${m.queued.length} jobs</strong></span>
        <span>Today: <strong>${m.today_inches.toFixed(1)} in</strong></span>
      </div>
    </div>`;
  }).join('');
}

// Filter buttons
document.getElementById('filter-bar').addEventListener('click', (e) => {
  const btn = e.target.closest('.filter-btn');
  if (!btn) return;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  currentFilter = btn.dataset.filter;
  renderMachines();
});

// ── Queue Panel ──
function renderQueue() {
  const list = document.getElementById('queue-list');
  const allQueued = [];

  filteredState.forEach(m => {
    m.queued.forEach(q => {
      allQueued.push({ ...q, machine_name: m.machine.name });
    });
  });

  document.getElementById('queue-count').textContent = `${allQueued.length} files`;

  if (allQueued.length === 0) {
    list.innerHTML = '<div class="queue-empty">No files in queue</div>';
    return;
  }

  const icons = ['🎨','👕','🧢','🎽','👔','🧥','👖','🧤','📄','🖼️'];

  // Group by nest_group
  const rendered = [];
  const seenNests = new Set();
  let num = 1;

  allQueued.forEach((q, i) => {
    const ng = q.nest_group;
    if (ng) {
      if (seenNests.has(ng)) return; // Already rendered this nest
      seenNests.add(ng);
      const nestFiles = allQueued.filter(x => x.nest_group === ng);
      const totalIn = nestFiles.reduce((a, f) => a + f.print_inches * (f.copies || 1), 0);
      rendered.push(`
        <div class="queue-item" style="background:rgba(167,139,250,0.04);border-left:3px solid var(--purple);">
          <div class="queue-num">${String(num).padStart(2, '0')}</div>
          <div class="queue-icon" style="background:rgba(167,139,250,0.15);color:var(--purple);font-size:12px;font-weight:700;">N</div>
          <div class="queue-info">
            <div class="queue-file" style="color:var(--purple);">NEST (${nestFiles.length} files · ${totalIn.toFixed(1)} in)</div>
            <div class="queue-meta-line">${nestFiles.map(f => esc(f.filename)).join(', ')}</div>
          </div>
          <span class="queue-machine-badge">${esc(q.machine_name)}</span>
          <span class="queue-inches">${totalIn.toFixed(1)}"</span>
        </div>`);
      num += nestFiles.length;
    } else {
      rendered.push(`
        <div class="queue-item">
          <div class="queue-num">${String(num).padStart(2, '0')}</div>
          <div class="queue-icon">${icons[num % icons.length]}</div>
          <div class="queue-info">
            <div class="queue-file">${esc(q.filename)}</div>
            <div class="queue-meta-line">${q.width_px}x${q.height_px} px | ${q.print_inches.toFixed(1)} in${(q.copies || 1) > 1 ? ` x${q.copies}` : ''}</div>
          </div>
          <span class="queue-machine-badge">${esc(q.machine_name)}</span>
          <span class="queue-inches">${(q.print_inches * (q.copies || 1)).toFixed(1)}"</span>
        </div>`);
      num++;
    }
  });

  list.innerHTML = rendered.join('');
}

// ── Inch Tracker ──
function renderInchGrid() {
  const grid = document.getElementById('inch-grid');
  if (filteredState.length === 0) {
    grid.innerHTML = '<div style="padding:30px;text-align:center;color:var(--text3);font-family:var(--mono);width:100%;">Waiting for machine data...</div>';
    return;
  }

  const maxInch = Math.max(...filteredState.map(m => m.total_queued_inches), 1);

  grid.innerHTML = filteredState.map(m => {
    const pct = Math.round((m.total_queued_inches / maxInch) * 100);
    const isPrinting = !!m.printing;
    const barColor = isPrinting ? 'var(--accent)' : m.machine.is_online ? 'var(--blue)' : 'var(--surface3)';
    const valueColor = m.machine.is_online ? 'var(--text)' : 'var(--text3)';

    return `
    <div class="inch-item">
      <div class="inch-machine">${esc(m.machine.name)}</div>
      <div class="inch-value" style="color:${valueColor}">${m.total_queued_inches.toFixed(1)}</div>
      <div class="inch-label">inches remaining</div>
      <div class="inch-bar"><div class="inch-bar-fill" style="width:${pct}%;background:${barColor}"></div></div>
      <div class="inch-sub">${m.queued.length} jobs queued | Today: ${m.today_inches.toFixed(1)} in</div>
    </div>`;
  }).join('');
}

// ── PC File Explorer ──
function renderPcTabs() {
  const bar = document.getElementById('pc-tab-bar');
  bar.innerHTML = filteredState.map(m => {
    const isActive = m.machine.id === activePcTab;
    const fileCount = m.queued.length + (m.printing ? 1 : 0);
    const dotColor = m.printing ? 'var(--green)' : m.machine.is_online ? 'var(--blue)' : 'var(--surface3)';

    return `
    <button class="pc-tab ${isActive ? 'active' : ''}" data-machine-id="${m.machine.id}">
      <span class="tab-dot" style="background:${dotColor}"></span>
      ${esc(m.machine.name)}
      <span class="tab-count">${fileCount}</span>
    </button>`;
  }).join('');

  // Event delegation
  bar.onclick = (e) => {
    const tab = e.target.closest('.pc-tab');
    if (!tab) return;
    activePcTab = tab.dataset.machineId;
    renderPcTabs();
    renderPcFiles();
  };
}

function renderPcFiles() {
  const detailBar = document.getElementById('pc-detail-bar');
  const fileTable = document.getElementById('pc-file-table');

  const m = filteredState.find(x => x.machine.id === activePcTab);
  if (!m) {
    detailBar.innerHTML = '';
    fileTable.innerHTML = '<div style="padding:30px;text-align:center;color:var(--text3);font-family:var(--mono);">Select a machine</div>';
    return;
  }

  const allFiles = [];
  const pAll = m.printing_all || (m.printing ? [m.printing] : []);
  pAll.forEach(p => allFiles.push({ ...p, _status: 'printing' }));
  m.queued.forEach(q => allFiles.push({ ...q, _status: 'queued' }));

  const totalInch = allFiles.reduce((a, f) => a + (f.print_inches || 0) * (f.copies || 1), 0);
  const isPrinting = !!m.printing;
  const [statusText, statusClass] = isPrinting
    ? ['PRINTING', 'status-printing']
    : m.machine.is_online
      ? ['IDLE', 'status-idle']
      : ['OFFLINE', 'status-offline'];

  detailBar.innerHTML = `
    <div class="pc-detail-bar">
      <div class="pc-detail-item" style="border-right:1px solid var(--border);">
        <div class="pc-detail-label">Computer</div>
        <div class="pc-detail-value">${esc(m.machine.name)}</div>
      </div>
      <div class="pc-detail-item" style="border-right:1px solid var(--border);">
        <div class="pc-detail-label">File Count</div>
        <div class="pc-detail-value">${allFiles.length} <span style="font-size:12px;color:var(--text3);font-weight:400;">files</span></div>
      </div>
      <div class="pc-detail-item" style="border-right:1px solid var(--border);">
        <div class="pc-detail-label">Total Inches</div>
        <div class="pc-detail-value">${totalInch.toFixed(1)} <span style="font-size:12px;color:var(--text3);font-weight:400;">in</span></div>
      </div>
      <div class="pc-detail-item">
        <div class="pc-detail-label">Machine Status</div>
        <div class="pc-detail-value"><span class="status-badge ${statusClass}">${statusText}</span></div>
      </div>
    </div>`;

  if (allFiles.length === 0) {
    fileTable.innerHTML = '<div style="padding:30px;text-align:center;color:var(--text3);font-family:var(--mono);">No files on this PC</div>';
    return;
  }

  const icons = ['🎨','👕','🧢','🎽','👔','🧥','👖','🧤','📄','🖼️'];

  fileTable.innerHTML = `
    <table class="pc-file-table">
      <thead>
        <tr>
          <th>Filename</th>
          <th>Size (px)</th>
          <th>DPI</th>
          <th>Inches</th>
          <th>Status</th>
        </tr>
      </thead>
      <tbody>
        ${allFiles.map((f, i) => {
          const rowClass = f._status === 'printing' ? 'pc-file-row-printing' : '';
          const nestBadge = f.nest_group
            ? `<span style="font-size:9px;font-weight:700;font-family:var(--mono);padding:2px 6px;border-radius:3px;background:rgba(167,139,250,0.15);color:#A78BFA;margin-left:6px;">NEST</span>`
            : '';
          const statusEl = f._status === 'printing'
            ? '<span class="file-status-printing"><span class="pulse-dot" style="width:5px;height:5px;border-radius:50%;background:var(--green);animation:pulse 1.5s ease infinite;"></span> PRINTING</span>'
            : '<span class="file-status-queued">QUEUED</span>';

          return `
          <tr class="${rowClass}">
            <td>
              <div style="display:flex;align-items:center;gap:9px;">
                <div style="width:28px;height:28px;border-radius:6px;background:var(--surface2);display:flex;align-items:center;justify-content:center;font-size:13px;flex-shrink:0;">
                  ${icons[i % icons.length]}
                </div>
                <span>${esc(f.filename)}${nestBadge}</span>
              </div>
            </td>
            <td>${f.width_px} x ${f.height_px}</td>
            <td>${f.dpi_x || '?'} x ${f.dpi_y || '?'}</td>
            <td style="font-weight:600;color:var(--text);">${(f.copies || 1) > 1 ? `${f.print_inches.toFixed(1)} x${f.copies} = ${(f.print_inches * f.copies).toFixed(1)}` : f.print_inches.toFixed(1)}</td>
            <td>${statusEl}</td>
          </tr>`;
        }).join('')}
      </tbody>
    </table>`;
}

// ── Search ──
let searchTimeout = null;

document.getElementById('search-input').addEventListener('input', (e) => {
  clearTimeout(searchTimeout);
  const query = e.target.value.trim();
  const resultsDiv = document.getElementById('search-results');

  if (query.length < 2) {
    resultsDiv.classList.remove('active');
    resultsDiv.innerHTML = '';
    return;
  }

  searchTimeout = setTimeout(async () => {
    try {
      let url = `/api/search?q=${encodeURIComponent(query)}`;
      if (currentWarehouse && currentWarehouse !== '__unassigned__') url += `&warehouse=${encodeURIComponent(currentWarehouse)}`;
      const resp = await fetch(url);
      const data = await resp.json();
      renderSearchResults(data.results);
    } catch (err) {
      console.error('Search error:', err);
    }
  }, 250);
});

// Close search on click outside
document.addEventListener('click', (e) => {
  if (!e.target.closest('.search-wrap') && !e.target.closest('.search-results')) {
    document.getElementById('search-results').classList.remove('active');
  }
});

function renderSearchResults(results) {
  const div = document.getElementById('search-results');
  if (results.length === 0) {
    div.innerHTML = '<div class="sr-none">No files matching your search</div>';
    div.classList.add('active');
    return;
  }

  div.innerHTML = results.map(r => `
    <div class="search-result-item">
      <span class="sr-filename">${esc(r.filename)}</span>
      <span class="sr-status ${r.status}">${r.status === 'printing' ? 'PRINTING' : r.status === 'completed' ? 'COMPLETED' : 'QUEUED'}</span>
      <span class="sr-machine">${esc(r.machine_name)}</span>
    </div>
  `).join('');
  div.classList.add('active');
}

// ── History ──
async function loadHistory() {
  try {
    let url = '/api/history?limit=20';
    if (currentWarehouse && currentWarehouse !== '__unassigned__') url += `&warehouse=${encodeURIComponent(currentWarehouse)}`;
    const resp = await fetch(url);
    const data = await resp.json();
    renderHistory(data);
  } catch (err) {
    console.error('History error:', err);
  }
}

function renderHistory(jobs) {
  const tbody = document.getElementById('history-body');
  if (jobs.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--text3);padding:30px;font-family:var(--mono);">No completed jobs yet</td></tr>';
    return;
  }

  tbody.innerHTML = jobs.map(j => {
    let duration = '—';
    if (j.started_at && j.completed_at) {
      const start = new Date(j.started_at + 'Z');
      const end = new Date(j.completed_at + 'Z');
      const secs = Math.round((end - start) / 1000);
      if (secs < 60) duration = `${secs}s`;
      else if (secs < 3600) duration = `${Math.floor(secs/60)}m ${secs%60}s`;
      else duration = `${Math.floor(secs/3600)}h ${Math.floor((secs%3600)/60)}m`;
    }

    let completedAt = '—';
    if (j.completed_at) {
      const d = new Date(j.completed_at + 'Z');
      completedAt = d.toLocaleTimeString('en-US', { hour:'2-digit', minute:'2-digit', hour12: false });
    }

    return `<tr>
      <td>${esc(j.filename)}</td>
      <td>${esc(j.machine_name)}</td>
      <td>${((j.copies || 1) > 1) ? `${j.print_inches.toFixed(1)} x${j.copies}` : j.print_inches.toFixed(1)}"</td>
      <td>${completedAt}</td>
      <td>${duration}</td>
      <td><span class="done-badge">DONE</span></td>
    </tr>`;
  }).join('');
}

// ── Utility ──
function esc(text) {
  const d = document.createElement('div');
  d.textContent = text || '';
  return d.innerHTML;
}

// ── Tab Navigation ──
document.querySelectorAll('.nav-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');

    const view = tab.dataset.tab;
    document.getElementById('view-dashboard').style.display = view === 'dashboard' ? '' : 'none';
    document.getElementById('view-reports').style.display = view === 'reports' ? '' : 'none';
    document.getElementById('view-stores').style.display = view === 'stores' ? '' : 'none';
    document.getElementById('view-machines').style.display = view === 'machines' ? '' : 'none';
    document.getElementById('view-customers').style.display = view === 'customers' ? '' : 'none';

    if (view === 'reports') loadReport();
    if (view === 'stores') loadStoreReport();
    if (view === 'machines') renderMachinesTab();
    if (view === 'customers') { loadCustomers(); renderCustomersTab(); }
  });
});


// ── Reports ──
let reportCache = null;

function initReportDates() {
  const end = new Date();
  const start = new Date();
  start.setDate(end.getDate() - 6); // Last 7 days

  document.getElementById('report-start').value = formatDate(start);
  document.getElementById('report-end').value = formatDate(end);
}

function formatDate(d) {
  return d.toISOString().split('T')[0];
}

function getReportRange() {
  return {
    start: document.getElementById('report-start').value,
    end: document.getElementById('report-end').value,
  };
}

// Quick range buttons
document.querySelectorAll('.report-quick-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.report-quick-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');

    const range = btn.dataset.range;
    const end = new Date();
    const start = new Date();

    if (range === 'today') {
      // start = today
    } else if (range === '7d') {
      start.setDate(end.getDate() - 6);
    } else if (range === '30d') {
      start.setDate(end.getDate() - 29);
    } else if (range === 'month') {
      start.setDate(1);
    } else if (range === 'lastmonth') {
      // First day of previous month → last day of previous month
      start.setMonth(start.getMonth() - 1, 1);
      end.setDate(0); // sets to last day of previous month
    }

    document.getElementById('report-start').value = formatDate(start);
    document.getElementById('report-end').value = formatDate(end);
    loadReport();
  });
});

// Date input change
document.getElementById('report-start').addEventListener('change', () => {
  document.querySelectorAll('.report-quick-btn').forEach(b => b.classList.remove('active'));
  loadReport();
});
document.getElementById('report-end').addEventListener('change', () => {
  document.querySelectorAll('.report-quick-btn').forEach(b => b.classList.remove('active'));
  loadReport();
});

async function loadReport() {
  const { start, end } = getReportRange();
  if (!start || !end) return;

  try {
    let url = `/api/reports?start=${start}&end=${end}`;
    if (currentWarehouse && currentWarehouse !== '__unassigned__') url += `&warehouse=${encodeURIComponent(currentWarehouse)}`;
    const resp = await fetch(url);
    reportCache = await resp.json();
    renderReport();
  } catch (err) {
    console.error('Report error:', err);
  }
}

function renderReport() {
  if (!reportCache) return;
  const data = reportCache;

  // Range label
  document.getElementById('rpt-range-label').textContent =
    `${data.start_date} to ${data.end_date}`;

  // KPIs
  const totalJobs = data.machine_totals.reduce((a, m) => a + m.total_jobs, 0);
  const totalInches = data.machine_totals.reduce((a, m) => a + m.total_inches, 0);
  const activeMachines = data.machine_totals.filter(m => m.total_jobs > 0).length;
  const dayCount = data.daily_totals.length || 1;
  const avgDaily = totalInches / dayCount;

  document.getElementById('rpt-total-jobs').innerHTML = `${totalJobs}<span class="stat-unit"> jobs</span>`;
  document.getElementById('rpt-total-inches').innerHTML = `${totalInches.toFixed(1)}<span class="stat-unit"> in</span>`;
  document.getElementById('rpt-avg-daily').innerHTML = `${avgDaily.toFixed(1)}<span class="stat-unit"> in</span>`;
  document.getElementById('rpt-machines').innerHTML = `${activeMachines}<span class="stat-unit"> PCs</span>`;

  // Machine breakdown
  const maxInches = Math.max(...data.machine_totals.map(m => m.total_inches), 1);
  document.getElementById('rpt-machine-grid').innerHTML = data.machine_totals.map(m => {
    const pct = Math.round((m.total_inches / maxInches) * 100);
    return `
    <div class="rpt-machine-item">
      <div class="rpt-machine-name">${esc(m.name)}</div>
      <div class="rpt-machine-inches">${m.total_inches.toFixed(1)}</div>
      <div class="rpt-machine-jobs">${m.total_jobs} jobs</div>
      <div class="rpt-machine-bar"><div class="rpt-machine-bar-fill" style="width:${pct}%"></div></div>
    </div>`;
  }).join('');

  // Daily chart
  const maxDayInch = Math.max(...data.daily_totals.map(d => d.total_inches), 1);
  if (data.daily_totals.length === 0) {
    document.getElementById('rpt-daily-chart').innerHTML =
      '<div style="padding:30px;text-align:center;color:var(--text3);font-family:var(--mono);font-size:12px;">No data for this range</div>';
  } else {
    document.getElementById('rpt-daily-chart').innerHTML = data.daily_totals.map(d => {
      const pct = Math.round((d.total_inches / maxDayInch) * 100);
      // Format date as "Mar 12"
      const dt = new Date(d.day + 'T00:00:00');
      const label = dt.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });

      return `
      <div class="rpt-day-row">
        <div class="rpt-day-label">${label}</div>
        <div class="rpt-day-bar-wrap">
          <div class="rpt-day-bar-fill" style="width:${pct}%"></div>
        </div>
        <div class="rpt-day-value">${d.total_inches.toFixed(1)} in</div>
        <div class="rpt-day-jobs">${d.total_jobs} jobs</div>
      </div>`;
    }).join('');
  }

  // Detail table — grouped by date, clickable rows open file detail panel below
  const tbody = document.getElementById('rpt-detail-body');
  if (data.machine_daily.length === 0) {
    tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--text3);padding:30px;font-family:var(--mono);">No data for this range</td></tr>';
  } else {
    // Group rows by day
    const dayGroups = {};
    data.machine_daily.forEach(row => {
      if (!dayGroups[row.day]) dayGroups[row.day] = [];
      dayGroups[row.day].push(row);
    });

    let html = '';
    Object.keys(dayGroups).sort().forEach(day => {
      const rows = dayGroups[day];
      const dt = new Date(day + 'T00:00:00');
      const dayLabel = dt.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' });

      rows.forEach((row, idx) => {
        let firstPrint = '—';
        if (row.first_start) {
          const fs = new Date(row.first_start + 'Z');
          firstPrint = fs.toLocaleTimeString('en-US', { hour:'2-digit', minute:'2-digit', hour12:false });
        }

        const dateCell = idx === 0
          ? `<td rowspan="${rows.length}" style="vertical-align:top;font-weight:600;border-right:2px solid var(--border2);padding-right:16px;">${dayLabel}</td>`
          : '';

        html += `<tr class="rpt-detail-row" data-machine="${esc(row.machine_name)}" data-day="${day}" style="cursor:pointer;${idx === 0 ? 'border-top:2px solid var(--border2);' : ''}">
          ${dateCell}
          <td>${esc(row.machine_name)}</td>
          <td>${row.total_jobs}</td>
          <td style="font-weight:600;color:var(--text);">${row.total_inches.toFixed(1)} in</td>
          <td style="font-family:var(--mono);font-size:12px;color:var(--text2);">${firstPrint}</td>
        </tr>`;
      });
    });

    tbody.innerHTML = html;

    // Click handlers — open file detail panel below
    tbody.querySelectorAll('.rpt-detail-row').forEach(row => {
      row.addEventListener('click', () => openReportFileDetail(row));
    });
  }
}

let activeDetailRow = null;

async function openReportFileDetail(row) {
  const machine = row.dataset.machine;
  const day = row.dataset.day;
  const panel = document.getElementById('rpt-file-detail-panel');
  const title = document.getElementById('rpt-file-detail-title');
  const content = document.getElementById('rpt-file-detail-content');

  // Highlight active row
  if (activeDetailRow) activeDetailRow.classList.remove('rpt-row-active');
  row.classList.add('rpt-row-active');
  activeDetailRow = row;

  const dt = new Date(day + 'T00:00:00');
  const dayLabel = dt.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' });
  title.textContent = `${machine} — ${dayLabel}`;

  panel.style.display = '';
  content.innerHTML = '<div style="padding:24px;text-align:center;color:var(--text3);font-family:var(--mono);font-size:12px;">Loading...</div>';

  // Scroll panel into view
  panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

  try {
    const resp = await fetch(`/api/reports/details?machine_name=${encodeURIComponent(machine)}&day=${day}`);
    const jobs = await resp.json();

    if (jobs.length === 0) {
      content.innerHTML = '<div style="padding:24px;text-align:center;color:var(--text3);font-family:var(--mono);font-size:12px;">No files found</div>';
      return;
    }

    const totalInches = jobs.reduce((a, j) => a + (j.print_inches * (j.copies || 1)), 0);

    let html = `<table class="history-table" style="font-size:12px;">
        <thead>
          <tr>
            <th>#</th>
            <th>Filename</th>
            <th>Size (px)</th>
            <th>DPI</th>
            <th>Height (in)</th>
            <th>Copies</th>
            <th>Total Inches</th>
            <th>Nest</th>
            <th>Completed</th>
            <th>Duration</th>
          </tr>
        </thead>
        <tbody>`;

    jobs.forEach((j, idx) => {
      const copies = j.copies || 1;
      const totalIn = j.print_inches * copies;
      const inchText = copies > 1 ? `${j.print_inches.toFixed(1)} x${copies} = ${totalIn.toFixed(1)}` : `${totalIn.toFixed(1)}`;

      let completedAt = '—';
      if (j.completed_at) {
        const d = new Date(j.completed_at + 'Z');
        completedAt = d.toLocaleTimeString('en-US', { hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false });
      }

      let duration = '—';
      if (j.started_at && j.completed_at) {
        const s = new Date(j.started_at + 'Z');
        const e = new Date(j.completed_at + 'Z');
        const secs = Math.round((e - s) / 1000);
        if (secs < 60) duration = secs + 's';
        else if (secs < 3600) duration = Math.floor(secs/60) + 'm ' + (secs%60) + 's';
        else duration = Math.floor(secs/3600) + 'h ' + Math.floor((secs%3600)/60) + 'm';
      }

      const nestBadge = j.nest_group
        ? '<span style="font-size:9px;font-weight:700;font-family:var(--mono);padding:2px 6px;border-radius:3px;background:rgba(167,139,250,0.15);color:#A78BFA;">NEST</span>'
        : '—';

      html += `<tr>
        <td style="color:var(--text3);">${idx + 1}</td>
        <td style="font-family:var(--mono);max-width:250px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(j.filename)}">${esc(j.filename)}</td>
        <td>${j.width_px} x ${j.height_px}</td>
        <td>${j.dpi_x || '?'} x ${j.dpi_y || '?'}</td>
        <td>${j.print_inches.toFixed(1)}</td>
        <td>${copies > 1 ? copies : '—'}</td>
        <td style="font-weight:600;color:var(--text);">${inchText}</td>
        <td>${nestBadge}</td>
        <td style="font-family:var(--mono);color:var(--text2);">${completedAt}</td>
        <td style="font-family:var(--mono);color:var(--text2);">${duration}</td>
      </tr>`;
    });

    html += `</tbody>
        <tfoot>
          <tr style="border-top:2px solid var(--border2);font-weight:700;">
            <td colspan="6" style="text-align:right;padding-right:12px;">TOTAL</td>
            <td style="color:var(--accent);">${totalInches.toFixed(1)} in</td>
            <td colspan="3"></td>
          </tr>
        </tfoot>
      </table>`;

    content.innerHTML = html;
  } catch (err) {
    content.innerHTML = `<div style="padding:24px;text-align:center;color:var(--red);font-family:var(--mono);font-size:12px;">Failed to load: ${err.message}</div>`;
  }
}

// Close file detail panel
document.getElementById('rpt-file-detail-close')?.addEventListener('click', () => {
  document.getElementById('rpt-file-detail-panel').style.display = 'none';
  if (activeDetailRow) {
    activeDetailRow.classList.remove('rpt-row-active');
    activeDetailRow = null;
  }
});

initReportDates();


// ── Machines Management Tab ──

function renderMachinesTab() {
  const container = document.getElementById('machines-manage-list');

  if (lastState.length === 0) {
    container.innerHTML = '<div style="padding:40px;text-align:center;font-family:var(--mono);font-size:12px;color:var(--text3);">No machines registered.</div>';
    return;
  }

  const machineTypes = ['', 'DTF', 'UV', 'SUBLIMATION', 'UV_FLATBED', 'ECOSOLVENT'];

  container.innerHTML = `
    <table class="history-table">
      <thead>
        <tr>
          <th>Machine Name</th>
          <th>Type</th>
          <th>Warehouse</th>
          <th>Status</th>
          <th>Last Seen</th>
          <th>Queued Jobs</th>
          <th>Today Output</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        ${lastState.map(m => {
          const online = m.machine.is_online;
          const [statusText, statusClass] = online
            ? (m.printing ? ['PRINTING', 'status-printing'] : ['IDLE', 'status-idle'])
            : ['OFFLINE', 'status-offline'];

          let lastSeen = '—';
          if (m.machine.last_seen) {
            const d = new Date(m.machine.last_seen + 'Z');
            lastSeen = d.toLocaleString('en-US', { month:'short', day:'numeric', hour:'2-digit', minute:'2-digit', hour12:false });
          }

          const curWh = m.machine.warehouse || '';
          const curType = m.machine.machine_type || '';

          return `<tr>
            <td style="font-weight:600;">${esc(m.machine.name)}</td>
            <td>
              <select class="machine-type-select" data-machine-id="${m.machine.id}">
                ${machineTypes.map(t => `<option value="${t}"${curType === t ? ' selected' : ''}>${t || '— Select —'}</option>`).join('')}
              </select>
            </td>
            <td>
              <select class="warehouse-select" data-machine-id="${m.machine.id}">
                <option value=""${!curWh ? ' selected' : ''}>— None —</option>
                ${warehouseList.map(w => `<option value="${esc(w)}"${curWh === w ? ' selected' : ''}>${esc(w)}</option>`).join('')}
              </select>
            </td>
            <td><span class="status-badge ${statusClass}">${statusText}</span></td>
            <td>${lastSeen}</td>
            <td>${m.queued.length}</td>
            <td>${m.today_inches.toFixed(1)} in</td>
            <td>
              <button class="manage-delete-btn" data-machine-id="${m.machine.id}" data-machine-name="${esc(m.machine.name)}">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg>
                Delete
              </button>
            </td>
          </tr>`;
        }).join('')}
      </tbody>
    </table>`;
}

// Warehouse and machine type assignment handler
document.getElementById('machines-manage-list').addEventListener('change', async (e) => {
  const whSelect = e.target.closest('.warehouse-select');
  if (whSelect) {
    const machineId = whSelect.dataset.machineId;
    try {
      const resp = await fetch(`/api/machines/${encodeURIComponent(machineId)}/warehouse`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ warehouse: whSelect.value }),
      });
      if (!resp.ok) throw new Error('Failed');
    } catch (err) {
      alert('Failed to update warehouse: ' + err.message);
    }
    return;
  }

  const typeSelect = e.target.closest('.machine-type-select');
  if (typeSelect) {
    const machineId = typeSelect.dataset.machineId;
    try {
      const resp = await fetch(`/api/machines/${encodeURIComponent(machineId)}/type`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ machine_type: typeSelect.value }),
      });
      if (!resp.ok) throw new Error('Failed');
    } catch (err) {
      alert('Failed to update machine type: ' + err.message);
    }
    return;
  }
});

// Delete machine handler (Machines tab only)
document.getElementById('machines-manage-list').addEventListener('click', async (e) => {
  const btn = e.target.closest('.manage-delete-btn');
  if (!btn) return;

  const id = btn.dataset.machineId;
  const name = btn.dataset.machineName;

  if (!confirm(`Delete machine "${name}"?\n\nThis will permanently remove the machine and ALL its print job history.`)) return;
  if (!confirm(`Are you sure? This action cannot be undone.\n\nType OK to confirm deleting "${name}".`)) return;

  try {
    const resp = await fetch(`/api/machines/${encodeURIComponent(id)}`, { method: 'DELETE' });
    if (!resp.ok) throw new Error('Delete failed');
    if (activePcTab === id) activePcTab = null;
  } catch (err) {
    alert('Failed to delete machine: ' + err.message);
  }
});


// ── Customer Management ──

let customerList = [];
let selectedCustomerId = null;

function renderCustomersTab() {
  const container = document.getElementById('customers-manage-list');
  if (!container) return;

  if (customerList.length === 0) {
    container.innerHTML = '<div style="padding:40px;text-align:center;font-family:var(--mono);font-size:12px;color:var(--text3);">No customers yet.</div>';
    return;
  }

  container.innerHTML = `
    <table class="history-table">
      <thead>
        <tr>
          <th>Name</th>
          <th>Email</th>
          <th>Balance</th>
          <th>Pending Files</th>
          <th>Add Credit</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        ${customerList.map(c => `<tr>
          <td style="font-weight:600;">${esc(c.name)}</td>
          <td style="font-family:var(--mono);font-size:11px;">${esc(c.email)}</td>
          <td style="font-weight:700;">${c.balance.toFixed(1)} in</td>
          <td>${c.pending_file_count || 0}</td>
          <td>
            <div style="display:flex;gap:4px;align-items:center;">
              <input type="number" class="cust-credit-input" data-customer-id="${c.id}" placeholder="+/-" step="0.1" style="width:80px;padding:4px 8px;background:var(--bg);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:11px;font-family:var(--mono);outline:none;">
              <button class="action-btn cust-credit-btn" data-customer-id="${c.id}">Apply</button>
            </div>
          </td>
          <td>
            <div style="display:flex;gap:6px;">
              <button class="action-btn cust-detail-btn" data-customer-id="${c.id}" title="Details">Details</button>
              <button class="manage-delete-btn cust-delete-btn" data-customer-id="${c.id}" data-customer-name="${esc(c.name)}">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg>
                Delete
              </button>
            </div>
          </td>
        </tr>`).join('')}
      </tbody>
    </table>`;
}

// Customers tab event delegation
document.getElementById('customers-manage-list')?.addEventListener('click', async (e) => {
  // Credit apply
  const creditBtn = e.target.closest('.cust-credit-btn');
  if (creditBtn) {
    const custId = creditBtn.dataset.customerId;
    const input = document.querySelector(`.cust-credit-input[data-customer-id="${custId}"]`);
    const amount = parseFloat(input.value);
    if (isNaN(amount) || amount === 0) return;
    try {
      await fetch(`/api/admin/customers/${custId}/credit`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ amount, reason: 'manual_adjustment' }),
      });
      input.value = '';
      await loadCustomers();
      renderCustomersTab();
    } catch (err) {
      alert('Failed to add credit: ' + err.message);
    }
    return;
  }

  // Details
  const detailBtn = e.target.closest('.cust-detail-btn');
  if (detailBtn) {
    openCustomerDetail(detailBtn.dataset.customerId);
    return;
  }

  // Delete (double confirmation)
  const deleteBtn = e.target.closest('.cust-delete-btn');
  if (deleteBtn) {
    const custId = deleteBtn.dataset.customerId;
    const name = deleteBtn.dataset.customerName;
    if (!confirm(`Delete customer "${name}"?\n\nThis will deactivate the customer and they will no longer be able to log in.`)) return;
    if (!confirm(`Are you absolutely sure?\n\nAll files and credit history for "${name}" will become inaccessible. This cannot be undone.`)) return;
    try {
      const resp = await fetch(`/api/admin/customers/${custId}`, { method: 'DELETE' });
      if (!resp.ok) throw new Error('Delete failed');
      selectedCustomerId = null;
      await loadCustomers();
      renderCustomersTab();
    } catch (err) {
      alert('Failed to delete customer: ' + err.message);
    }
    return;
  }
});

// New Customer button in Customers tab
document.getElementById('add-customer-btn-tab')?.addEventListener('click', () => {
  document.getElementById('customer-modal').style.display = 'flex';
});

async function loadCustomers() {
  try {
    const resp = await fetch('/api/admin/customers');
    customerList = await resp.json();
    renderCustomerSidebar();
    renderCustomerFiles();
  } catch (err) {
    console.error('Failed to load customers:', err);
  }
}

function renderCustomerSidebar() {
  const list = document.getElementById('customer-list');
  if (!list) return;
  if (customerList.length === 0) {
    list.innerHTML = '<div class="sidebar-empty">No customers yet</div>';
    return;
  }
  list.innerHTML = customerList.map(c => {
    const badge = c.pending_file_count > 0
      ? `<span class="sidebar-badge">${c.pending_file_count}</span>`
      : '';
    return `
    <div class="sidebar-item ${selectedCustomerId === c.id ? 'active' : ''}" data-customer-id="${c.id}">
      <div class="sidebar-icon">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
      </div>
      <div style="flex:1;min-width:0;">
        <div style="font-size:13px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${esc(c.name)} ${badge}</div>
        <div style="font-size:11px;color:#6b6b80;">${c.balance.toFixed(1)} in</div>
      </div>
    </div>`;
  }).join('');

  // Click handlers
  list.querySelectorAll('.sidebar-item').forEach(item => {
    item.addEventListener('click', () => {
      selectedCustomerId = item.dataset.customerId;
      renderCustomerSidebar();
      renderCustomerFiles();
      openCustomerDetail(selectedCustomerId);
    });
  });
}

async function renderCustomerFiles() {
  const body = document.getElementById('cust-files-body');
  const countEl = document.getElementById('cust-files-count');
  if (!body) return;

  try {
    const url = selectedCustomerId
      ? `/api/admin/customer-files?customer_id=${selectedCustomerId}`
      : '/api/admin/customer-files';
    const resp = await fetch(url);
    const allFiles = await resp.json();
    // Show only active files (not completed) in main list
    const files = allFiles.filter(f => f.status !== 'completed');
    countEl.textContent = `${files.length} active`;

    if (files.length === 0) {
      body.innerHTML = '<tr><td colspan="9" style="text-align:center;color:#6b6b80;padding:24px;">No customer files</td></tr>';
      return;
    }

    body.innerHTML = files.map(f => {
      const statusClass = {
        'uploaded': 'status-idle',
        'assigned': 'status-idle',
        'queued': 'status-idle',
        'printing': 'status-printing',
        'completed': 'status-completed',
      }[f.status] || '';

      const uploaded = f.uploaded_at ? new Date(f.uploaded_at + 'Z').toLocaleString('en-US', {
        month:'short', day:'numeric', hour:'2-digit', minute:'2-digit', hour12:false
      }) : '—';

      // Build action buttons
      let actions = `<a href="/api/admin/customer-files/${f.id}/download" class="action-btn" title="Download"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg></a>`;
      if (f.status === 'uploaded') {
        actions += `
          <select class="assign-select" data-file-id="${f.id}" title="Assign to machine">
            <option value="">Assign...</option>
            ${lastState.map(m => `<option value="${m.machine.id}">${esc(m.machine.name)}</option>`).join('')}
          </select>`;
      }

      return `<tr>
        <td>${esc(f.customer_name || '')}</td>
        <td style="font-family:'JetBrains Mono',monospace;font-size:12px;">${esc(f.original_filename)}</td>
        <td>${f.print_inches.toFixed(1)}</td>
        <td>${f.copies}</td>
        <td>${esc(f.assigned_machine_name || '—')}</td>
        <td>${esc(f.assigned_operator || '—')}</td>
        <td><span class="status-badge ${statusClass}">${f.status.toUpperCase()}</span></td>
        <td>${uploaded}</td>
        <td>${actions}</td>
      </tr>`;
    }).join('');

    // Assign handler
    body.querySelectorAll('.assign-select').forEach(select => {
      select.addEventListener('change', async (e) => {
        const fileId = e.target.dataset.fileId;
        const machineId = e.target.value;
        if (!machineId) return;
        try {
          const resp = await fetch(`/api/admin/customer-files/${fileId}/assign`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ machine_id: machineId }),
          });
          const data = await resp.json();
          if (data.warning) alert(data.warning);
          renderCustomerFiles();
        } catch (err) {
          alert('Assign failed: ' + err.message);
        }
      });
    });
  } catch (err) {
    body.innerHTML = '<tr><td colspan="9" style="text-align:center;color:#ef4444;">Failed to load</td></tr>';
  }
}

async function openCustomerDetail(customerId) {
  const modal = document.getElementById('customer-detail-modal');
  const title = document.getElementById('cust-detail-title');
  const content = document.getElementById('cust-detail-content');

  try {
    const resp = await fetch(`/api/admin/customers/${customerId}`);
    const c = await resp.json();
    title.textContent = c.name;

    // Separate files by status
    const pendingFiles = (c.files || []).filter(f => f.status !== 'completed');
    const completedFiles = (c.files || []).filter(f => f.status === 'completed');

    content.innerHTML = `
      <div style="display:grid;grid-template-columns:repeat(auto-fit, minmax(150px, 1fr));gap:12px;margin-bottom:16px;">
        <div class="detail-card"><div class="detail-label">Email</div><div class="detail-value" style="font-size:12px;">${esc(c.email)}</div></div>
        <div class="detail-card"><div class="detail-label">Balance</div><div class="detail-value">${c.balance.toFixed(1)} in</div></div>
        <div class="detail-card"><div class="detail-label">Pending Files</div><div class="detail-value">${pendingFiles.length}</div></div>
      </div>

      <div style="margin-bottom:16px;">
        <strong style="font-size:13px;">Files (${pendingFiles.length} pending, ${completedFiles.length} completed)</strong>
        <div style="max-height:250px;overflow-y:auto;margin-top:6px;">
          <table class="history-table" style="font-size:12px;">
            <thead><tr><th>Filename</th><th>Inches</th><th>Copies</th><th>Machine</th><th>Operator</th><th>Status</th><th>Actions</th></tr></thead>
            <tbody>
              ${(c.files || []).map(f => {
                const sc = {'uploaded':'status-idle','assigned':'status-idle','queued':'status-idle','printing':'status-printing','completed':'status-completed'}[f.status] || '';
                let actions = `<a href="/api/admin/customer-files/${f.id}/download" class="action-btn" title="Download"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg></a>`;
                if (f.status === 'uploaded') {
                  actions += ` <select class="assign-select-modal" data-file-id="${f.id}" style="font-size:11px;padding:2px 4px;background:#1a1a24;border:1px solid rgba(255,255,255,0.08);border-radius:4px;color:#e2e2e8;">
                    <option value="">Assign...</option>
                    ${lastState.map(m => `<option value="${m.machine.id}">${esc(m.machine.name)}</option>`).join('')}
                  </select>`;
                }
                return `<tr>
                  <td style="font-family:'JetBrains Mono',monospace;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(f.original_filename)}</td>
                  <td>${f.print_inches.toFixed(1)}</td>
                  <td>${f.copies}</td>
                  <td>${esc(f.assigned_machine_name || '—')}</td>
                  <td>${esc(f.assigned_operator || '—')}</td>
                  <td><span class="status-badge ${sc}">${f.status.toUpperCase()}</span></td>
                  <td>${actions}</td>
                </tr>`;
              }).join('')}
            </tbody>
          </table>
        </div>
      </div>

      <div style="margin-bottom:12px;">
        <strong style="font-size:13px;">Add/Remove Credit</strong>
        <div style="display:flex;gap:8px;margin-top:6px;">
          <input type="number" id="credit-amount" placeholder="Amount (+ or -)" class="modal-input" style="flex:1;">
          <button class="modal-btn confirm" id="credit-btn">Apply</button>
        </div>
      </div>
      <div>
        <strong style="font-size:13px;">Credit History</strong>
        <div style="max-height:200px;overflow-y:auto;margin-top:6px;">
          <table class="history-table" style="font-size:12px;">
            <thead><tr><th>Date</th><th>Amount</th><th>Balance</th><th>Reason</th></tr></thead>
            <tbody>
              ${(c.credit_history || []).map(h => `<tr>
                <td>${new Date(h.created_at + 'Z').toLocaleString('en-US', {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',hour12:false})}</td>
                <td style="color:${h.amount >= 0 ? '#3DCF82' : '#FF4D4D'}">${h.amount >= 0 ? '+' : ''}${h.amount.toFixed(1)}</td>
                <td>${h.balance_after.toFixed(1)}</td>
                <td>${esc(h.reason)}</td>
              </tr>`).join('')}
            </tbody>
          </table>
        </div>
      </div>

      `;
    modal.style.display = 'flex';

    // Credit button handler
    document.getElementById('credit-btn').addEventListener('click', async () => {
      const amount = parseFloat(document.getElementById('credit-amount').value);
      if (isNaN(amount) || amount === 0) return;
      await fetch(`/api/admin/customers/${customerId}/credit`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ amount, reason: 'manual_adjustment' }),
      });
      openCustomerDetail(customerId);
      loadCustomers();
    });

    // Assign handlers inside modal
    content.querySelectorAll('.assign-select-modal').forEach(select => {
      select.addEventListener('change', async (e) => {
        const fileId = e.target.dataset.fileId;
        const machineId = e.target.value;
        if (!machineId) return;
        try {
          const resp = await fetch(`/api/admin/customer-files/${fileId}/assign`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ machine_id: machineId }),
          });
          const data = await resp.json();
          if (data.warning) alert(data.warning);
          openCustomerDetail(customerId);
          loadCustomers();
          renderCustomerFiles();
        } catch (err) {
          alert('Assign failed: ' + err.message);
        }
      });
    });
  } catch (err) {
    alert('Failed to load customer: ' + err.message);
  }
}

function closeCustomerDetailModal() {
  document.getElementById('customer-detail-modal').style.display = 'none';
}

// Add Customer Modal
document.getElementById('add-customer-btn')?.addEventListener('click', () => {
  document.getElementById('customer-modal').style.display = 'flex';
});

function closeCustomerModal() {
  document.getElementById('customer-modal').style.display = 'none';
  document.getElementById('customer-form').reset();
}

document.getElementById('customer-form')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const form = e.target;
  const data = {
    name: form.name.value,
    email: form.email.value,
    password: form.password.value,
    initial_credit_inches: parseFloat(form.initial_credit_inches.value) || 0,
  };
  try {
    const resp = await fetch('/api/admin/customers', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
    if (!resp.ok) {
      const err = await resp.json();
      throw new Error(err.error || 'Failed');
    }
    closeCustomerModal();
    await loadCustomers();
    renderCustomersTab();
  } catch (err) {
    alert('Failed to create customer: ' + err.message);
  }
});

// Close modals on overlay click
document.getElementById('customer-modal')?.addEventListener('click', (e) => {
  if (e.target === e.currentTarget) closeCustomerModal();
});
document.getElementById('customer-detail-modal')?.addEventListener('click', (e) => {
  if (e.target === e.currentTarget) closeCustomerDetailModal();
});

// ── Stores Report ──

const STORE_COLORS = {
  'C': '#3DCF82', 'P': '#6C8EFF', 'IN': '#FF6B6B', 'MC': '#FFB347',
  'B': '#A78BFA', 'PRO': '#F472B6', 'MS': '#34D399', 'DWC': '#FBBF24',
  'LSV': '#60A5FA', 'G': '#C084FC', 'ETSY': '#FB923C',
};

function getStoreColor(code) {
  return STORE_COLORS[code] || 'var(--accent)';
}

function initStoreDates() {
  const end = new Date();
  const start = new Date();
  start.setDate(end.getDate() - 6);
  document.getElementById('store-start').value = formatDate(start);
  document.getElementById('store-end').value = formatDate(end);
}

document.querySelectorAll('#view-stores .report-quick-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('#view-stores .report-quick-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const range = btn.dataset.range;
    const end = new Date();
    const start = new Date();
    if (range === '7d') start.setDate(end.getDate() - 6);
    else if (range === '30d') start.setDate(end.getDate() - 29);
    else if (range === 'month') start.setDate(1);
    else if (range === 'lastmonth') { start.setMonth(start.getMonth() - 1, 1); end.setDate(0); }
    document.getElementById('store-start').value = formatDate(start);
    document.getElementById('store-end').value = formatDate(end);
    loadStoreReport();
  });
});

document.getElementById('store-start').addEventListener('change', () => {
  document.querySelectorAll('#view-stores .report-quick-btn').forEach(b => b.classList.remove('active'));
  loadStoreReport();
});
document.getElementById('store-end').addEventListener('change', () => {
  document.querySelectorAll('#view-stores .report-quick-btn').forEach(b => b.classList.remove('active'));
  loadStoreReport();
});

async function loadStoreReport() {
  const start = document.getElementById('store-start').value;
  const end = document.getElementById('store-end').value;
  if (!start || !end) return;

  try {
    let url = `/api/reports/stores?start=${start}&end=${end}`;
    if (currentWarehouse && currentWarehouse !== '__unassigned__') url += `&warehouse=${encodeURIComponent(currentWarehouse)}`;
    const resp = await fetch(url);
    const data = await resp.json();
    renderStoreReport(data);
    loadUnrecognizedFiles(start, end);
  } catch (err) {
    console.error('Store report error:', err);
  }
}

async function openStoreCellDetail(store, machineType, warehouse) {
  const panel = document.getElementById('store-cell-detail-panel');
  const title = document.getElementById('store-cell-detail-title');
  const content = document.getElementById('store-cell-detail-content');

  const start = document.getElementById('store-start').value;
  const end = document.getElementById('store-end').value;

  const typeLabel = machineType === 'UNSET' ? 'Not Set' : machineType.replace('_', ' ');
  title.textContent = `${store} — ${typeLabel} — ${warehouse}`;

  panel.style.display = '';
  content.innerHTML = '<div style="padding:24px;text-align:center;color:var(--text3);font-family:var(--mono);font-size:12px;">Loading...</div>';
  panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });

  try {
    const url = `/api/reports/store-cell?store=${encodeURIComponent(store)}&machine_type=${encodeURIComponent(machineType)}&warehouse=${encodeURIComponent(warehouse)}&start=${start}&end=${end}`;
    const resp = await fetch(url);
    const jobs = await resp.json();

    if (!jobs.length) {
      content.innerHTML = '<div style="padding:24px;text-align:center;color:var(--text3);font-family:var(--mono);font-size:12px;">No files found</div>';
      return;
    }

    const totalInches = jobs.reduce((a, j) => a + (j.print_inches * (j.copies || 1)), 0);

    let html = `<table class="history-table" style="font-size:12px;">
      <thead>
        <tr>
          <th>#</th>
          <th>Filename</th>
          <th>Machine</th>
          <th>Operator</th>
          <th>Inches</th>
          <th>Copies</th>
          <th>Total</th>
          <th>Completed</th>
        </tr>
      </thead>
      <tbody>`;

    jobs.forEach((j, idx) => {
      const copies = j.copies || 1;
      const total = j.print_inches * copies;
      let completedAt = '—';
      if (j.completed_at) {
        const d = new Date(j.completed_at + 'Z');
        completedAt = d.toLocaleString('en-US', { month:'short', day:'numeric', hour:'2-digit', minute:'2-digit', hour12:false });
      }
      html += `<tr>
        <td style="color:var(--text3);">${idx + 1}</td>
        <td style="font-family:var(--mono);max-width:400px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(j.filename)}">${esc(j.filename)}</td>
        <td>${esc(j.machine_name || '—')}</td>
        <td>${esc(j.operator || '—')}</td>
        <td>${j.print_inches.toFixed(1)}</td>
        <td>${copies > 1 ? copies : '—'}</td>
        <td style="font-weight:600;">${total.toFixed(1)}</td>
        <td style="font-family:var(--mono);color:var(--text2);">${completedAt}</td>
      </tr>`;
    });

    html += `</tbody>
      <tfoot>
        <tr style="border-top:2px solid var(--border2);font-weight:700;">
          <td colspan="6" style="text-align:right;padding-right:12px;">TOTAL (${jobs.length} jobs)</td>
          <td style="color:var(--accent);">${totalInches.toFixed(1)} in</td>
          <td></td>
        </tr>
      </tfoot>
    </table>`;

    content.innerHTML = html;
  } catch (err) {
    content.innerHTML = `<div style="padding:24px;text-align:center;color:var(--red);font-family:var(--mono);font-size:12px;">Failed to load: ${err.message}</div>`;
  }
}

document.getElementById('store-cell-detail-close')?.addEventListener('click', () => {
  document.getElementById('store-cell-detail-panel').style.display = 'none';
});

async function loadUnrecognizedFiles(start, end) {
  try {
    const resp = await fetch(`/api/reports/unrecognized?start=${start}&end=${end}`);
    const files = await resp.json();
    const panel = document.getElementById('unrecognized-panel');
    const body = document.getElementById('unrecognized-body');
    const countEl = document.getElementById('unrecognized-count');
    const selectAll = document.getElementById('unrecognized-select-all');
    const selectedCount = document.getElementById('unrecognized-selected-count');
    const bulkBtn = document.getElementById('bulk-assign-btn');
    const bulkSelect = document.getElementById('bulk-store-select');

    if (files.length === 0) {
      panel.style.display = 'none';
      return;
    }

    panel.style.display = '';
    countEl.textContent = `${files.length} files`;

    const storeCodes = ['P','C','G','MC','LSV','MS','DWC','IN','B','PRO','ETSY'];

    body.innerHTML = files.map(f => `<tr>
      <td><input type="checkbox" class="unrecognized-check" data-filename="${esc(f.filename)}" style="cursor:pointer;"></td>
      <td style="font-family:var(--mono);font-size:12px;max-width:400px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${esc(f.filename)}">${esc(f.filename)}</td>
      <td>${f.job_count}</td>
      <td style="font-weight:600;">${f.total_inches.toFixed(1)} in</td>
      <td>
        <select class="unrecognized-assign" data-filename="${esc(f.filename)}" style="padding:4px 8px;background:var(--bg);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:11px;font-family:var(--mono);">
          <option value="">— Select —</option>
          ${storeCodes.map(c => `<option value="${c}">${c}</option>`).join('')}
        </select>
      </td>
    </tr>`).join('');

    // Reset selection state
    selectAll.checked = false;
    selectedCount.textContent = '0 selected';
    bulkBtn.disabled = true;
    bulkSelect.value = '';

    const updateSelectedCount = () => {
      const checked = body.querySelectorAll('.unrecognized-check:checked').length;
      selectedCount.textContent = `${checked} selected`;
      bulkBtn.disabled = checked === 0 || !bulkSelect.value;
    };

    // Individual checkbox handlers
    body.querySelectorAll('.unrecognized-check').forEach(cb => {
      cb.addEventListener('change', updateSelectedCount);
    });

    // Select all handler
    selectAll.onchange = () => {
      body.querySelectorAll('.unrecognized-check').forEach(cb => { cb.checked = selectAll.checked; });
      updateSelectedCount();
    };

    // Bulk store dropdown change
    bulkSelect.onchange = updateSelectedCount;

    // Bulk assign button
    bulkBtn.onclick = async () => {
      const storeCode = bulkSelect.value;
      if (!storeCode) return;
      const checked = [...body.querySelectorAll('.unrecognized-check:checked')];
      if (checked.length === 0) return;
      if (!confirm(`Assign ${checked.length} file(s) to store "${storeCode}"?`)) return;

      bulkBtn.disabled = true;
      bulkBtn.textContent = 'Assigning...';
      try {
        await Promise.all(checked.map(cb =>
          fetch('/api/reports/store-override', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filename: cb.dataset.filename, store_code: storeCode }),
          })
        ));
        bulkBtn.textContent = 'Assign Selected';
        loadStoreReport();
      } catch (err) {
        alert('Bulk assign failed: ' + err.message);
        bulkBtn.disabled = false;
        bulkBtn.textContent = 'Assign Selected';
      }
    };

    // Individual assignment handlers (still work for single rows)
    body.querySelectorAll('.unrecognized-assign').forEach(select => {
      select.addEventListener('change', async (e) => {
        const filename = e.target.dataset.filename;
        const storeCode = e.target.value;
        if (!storeCode) return;
        try {
          await fetch('/api/reports/store-override', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ filename, store_code: storeCode }),
          });
          e.target.closest('tr').style.opacity = '0.4';
          e.target.disabled = true;
          setTimeout(() => loadStoreReport(), 500);
        } catch (err) {
          alert('Failed: ' + err.message);
        }
      });
    });
  } catch (err) {
    console.error('Unrecognized files error:', err);
  }
}

let storeReportCache = null;

function renderStoreReport(data) {
  storeReportCache = data;

  // Range label
  document.getElementById('store-range-label').textContent = `${data.start_date} to ${data.end_date}`;

  // KPIs
  const totalJobs = data.store_totals.reduce((a, s) => a + s.total_jobs, 0);
  const totalInches = data.store_totals.reduce((a, s) => a + s.total_inches, 0);
  const activeStores = data.store_totals.filter(s => s.total_jobs > 0).length;

  document.getElementById('store-total-jobs').innerHTML = `${totalJobs}<span class="stat-unit"> jobs</span>`;
  document.getElementById('store-total-inches').innerHTML = `${totalInches.toFixed(1)}<span class="stat-unit"> in</span>`;
  document.getElementById('store-count').textContent = activeStores;

  // Machine type breakdown
  const TYPE_COLORS = {
    'DTF': '#3DCF82', 'UV': '#6C8EFF', 'SUBLIMATION': '#FF6B6B',
    'UV_FLATBED': '#FFB347', 'ECOSOLVENT': '#A78BFA', 'UNSET': 'var(--text3)',
  };
  const maxTypeInch = Math.max(...data.type_totals.map(t => t.total_inches), 1);
  document.getElementById('type-grid').innerHTML = data.type_totals.map(t => {
    const pct = Math.round((t.total_inches / maxTypeInch) * 100);
    const color = TYPE_COLORS[t.machine_type] || 'var(--accent)';
    const label = t.machine_type === 'UNSET' ? 'Not Set' : t.machine_type.replace('_', ' ');
    const share = totalInches > 0 ? ((t.total_inches / totalInches) * 100).toFixed(1) : '0';
    return `
    <div class="rpt-machine-item">
      <div class="rpt-machine-name" style="color:${color};font-weight:700;">${esc(label)}</div>
      <div class="rpt-machine-inches">${t.total_inches.toFixed(1)}</div>
      <div class="rpt-machine-jobs">${t.total_jobs} jobs · ${share}%</div>
      <div class="rpt-machine-bar"><div class="rpt-machine-bar-fill" style="width:${pct}%;background:${color}"></div></div>
    </div>`;
  }).join('');

  // Store breakdown bars
  const maxInches = Math.max(...data.store_totals.map(s => s.total_inches), 1);
  document.getElementById('store-grid').innerHTML = data.store_totals.map(s => {
    const pct = Math.round((s.total_inches / maxInches) * 100);
    const color = getStoreColor(s.store);
    const share = totalInches > 0 ? ((s.total_inches / totalInches) * 100).toFixed(1) : '0';
    return `
    <div class="rpt-machine-item">
      <div class="rpt-machine-name" style="color:${color};font-weight:700;">${esc(s.store)}</div>
      <div class="rpt-machine-inches">${s.total_inches.toFixed(1)}</div>
      <div class="rpt-machine-jobs">${s.total_jobs} jobs · ${share}%</div>
      <div class="rpt-machine-bar"><div class="rpt-machine-bar-fill" style="width:${pct}%;background:${color}"></div></div>
    </div>`;
  }).join('');

  // Build store lookup for matrix
  const store_totals_map = {};
  data.store_totals.forEach(s => { store_totals_map[s.store] = s; });

  // Store x Machine Type x Warehouse matrix (flat table)
  const types = data.type_totals.map(t => t.machine_type);
  const stores = data.store_totals.map(s => s.store);

  // Build warehouse lookup: store -> type -> warehouse -> data
  const stwMatrix = {};
  const allWarehouses = new Set();
  (data.store_type_wh || []).forEach(stw => {
    if (!stwMatrix[stw.store]) stwMatrix[stw.store] = {};
    if (!stwMatrix[stw.store][stw.machine_type]) stwMatrix[stw.store][stw.machine_type] = {};
    stwMatrix[stw.store][stw.machine_type][stw.warehouse] = stw;
    allWarehouses.add(stw.warehouse);
  });
  const warehouseNames = [...allWarehouses].sort();

  // Fallback: if no warehouse data, show simple type-only matrix
  if (warehouseNames.length === 0) {
    document.getElementById('store-type-head').innerHTML = `<tr>
      <th>Store</th>
      ${types.map(t => `<th style="text-align:center;">${t === 'UNSET' ? 'Not Set' : t.replace('_',' ')}</th>`).join('')}
      <th style="text-align:center;">Total</th>
    </tr>`;
    const stMatrix = {};
    data.store_type_totals.forEach(st => { if (!stMatrix[st.store]) stMatrix[st.store] = {}; stMatrix[st.store][st.machine_type] = st; });
    document.getElementById('store-type-body').innerHTML = stores.map(store => {
      const color = getStoreColor(store);
      const sd = store_totals_map[store] || { total_inches: 0 };
      return `<tr><td><span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${color};margin-right:6px;vertical-align:middle;"></span><strong>${esc(store)}</strong></td>
        ${types.map(t => { const c = (stMatrix[store]||{})[t]; return c ? `<td style="text-align:center;font-family:var(--mono);font-size:12px;">${c.total_inches.toFixed(1)}</td>` : `<td style="text-align:center;color:var(--text3);">—</td>`; }).join('')}
        <td style="text-align:center;font-weight:700;">${(sd.total_inches||0).toFixed(1)}</td></tr>`;
    }).join('') + `<tr style="border-top:2px solid var(--border2);font-weight:700;"><td>TOTAL</td>
      ${types.map(t => { const td = data.type_totals.find(x=>x.machine_type===t); return `<td style="text-align:center;">${td?td.total_inches.toFixed(1):'—'}</td>`; }).join('')}
      <td style="text-align:center;color:var(--accent);">${totalInches.toFixed(1)}</td></tr>`;
  } else {

  // Header: Store | type1-wh1 | type1-wh2 | type2-wh1 | type2-wh2 | Total
  document.getElementById('store-type-head').innerHTML = `
    <tr>
      <th rowspan="2" style="vertical-align:bottom;">Store</th>
      ${types.map(t => {
        const label = t === 'UNSET' ? 'Not Set' : t.replace('_', ' ');
        const color = TYPE_COLORS[t] || 'var(--accent)';
        return `<th colspan="${warehouseNames.length}" style="text-align:center;border-bottom:2px solid ${color};color:${color};">${esc(label)}</th>`;
      }).join('')}
      <th rowspan="2" style="text-align:center;vertical-align:bottom;">Total</th>
      <th rowspan="2" style="text-align:center;vertical-align:bottom;min-width:180px;">% Share</th>
    </tr>
    <tr>
      ${types.map(() =>
        warehouseNames.map(wh => `<th style="text-align:center;font-size:10px;font-weight:500;color:var(--text3);">${esc(wh)}</th>`).join('')
      ).join('')}
    </tr>`;

  // Per store+type totals (used for percentage denominator)
  // For each store+type, the sum across all warehouses = denominator
  const storeTypeTotalMap = {};
  data.store_type_totals.forEach(st => {
    storeTypeTotalMap[`${st.store}|${st.machine_type}`] = st.total_inches;
  });

  let matrixHtml = '';
  stores.forEach(store => {
    const color = getStoreColor(store);
    const storeData = store_totals_map[store] || { total_inches: 0 };

    matrixHtml += `<tr>
      <td><span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${color};margin-right:6px;vertical-align:middle;"></span><strong>${esc(store)}</strong></td>
      ${types.map(t => {
        const storeTypeTotal = storeTypeTotalMap[`${store}|${t}`] || 0;
        return warehouseNames.map(wh => {
          const cell = stwMatrix[store]?.[t]?.[wh];
          if (cell) {
            const pct = storeTypeTotal > 0 ? ((cell.total_inches / storeTypeTotal) * 100).toFixed(1) : '0';
            return `<td class="stw-cell" data-store="${esc(store)}" data-type="${esc(t)}" data-warehouse="${esc(wh)}" style="text-align:center;font-family:var(--mono);font-size:12px;cursor:pointer;line-height:1.3;" title="Click to see files">${cell.total_inches.toFixed(1)}<br><span style="font-size:10px;color:var(--text3);">${pct}%</span></td>`;
          }
          return `<td style="text-align:center;color:var(--text3);font-size:11px;">—</td>`;
        }).join('');
      }).join('')}
      <td style="text-align:center;font-weight:700;">${(storeData.total_inches || 0).toFixed(1)}</td>
      <td style="padding:6px 12px;">
        <div style="display:flex;align-items:center;gap:8px;">
          <div style="flex:1;height:6px;background:var(--surface2);border-radius:3px;overflow:hidden;">
            <div style="width:${totalInches > 0 ? ((storeData.total_inches || 0) / totalInches * 100).toFixed(1) : 0}%;height:100%;background:${color};border-radius:3px;"></div>
          </div>
          <span style="font-family:var(--mono);font-size:11px;min-width:45px;text-align:right;">${totalInches > 0 ? ((storeData.total_inches || 0) / totalInches * 100).toFixed(1) : '0'}%</span>
        </div>
      </td>
    </tr>`;
  });

  // Total row
  matrixHtml += `<tr style="border-top:2px solid var(--border2);font-weight:700;">
    <td>TOTAL</td>
    ${types.map(t =>
      warehouseNames.map(wh => {
        const val = stores.reduce((sum, s) => {
          const cell = stwMatrix[s]?.[t]?.[wh];
          return sum + (cell ? cell.total_inches : 0);
        }, 0);
        return `<td style="text-align:center;">${val > 0 ? val.toFixed(1) : '—'}</td>`;
      }).join('')
    ).join('')}
    <td style="text-align:center;color:var(--accent);">${totalInches.toFixed(1)}</td>
    <td style="text-align:center;color:var(--accent);">100%</td>
  </tr>`;

  document.getElementById('store-type-body').innerHTML = matrixHtml;

  // Hover + click handlers for cells
  document.querySelectorAll('#store-type-body .stw-cell').forEach(cell => {
    cell.addEventListener('mouseenter', () => { cell.style.background = 'var(--surface1)'; });
    cell.addEventListener('mouseleave', () => { cell.style.background = ''; });
    cell.addEventListener('click', () => {
      openStoreCellDetail(cell.dataset.store, cell.dataset.type, cell.dataset.warehouse);
    });
  });

  } // end else (warehouseNames.length > 0)

  // Daily breakdown
  const dailyBody = document.getElementById('store-daily-body');
  if (data.store_daily.length === 0) {
    dailyBody.innerHTML = '<tr><td colspan="4" style="text-align:center;color:var(--text3);padding:30px;font-family:var(--mono);">No data</td></tr>';
  } else {
    const dayGroups = {};
    data.store_daily.forEach(row => {
      if (!dayGroups[row.day]) dayGroups[row.day] = [];
      dayGroups[row.day].push(row);
    });

    let html = '';
    Object.keys(dayGroups).sort().forEach(day => {
      const rows = dayGroups[day];
      const dt = new Date(day + 'T00:00:00');
      const dayLabel = dt.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' });

      rows.forEach((row, idx) => {
        const color = getStoreColor(row.store);
        const dateCell = idx === 0
          ? `<td rowspan="${rows.length}" style="vertical-align:top;font-weight:600;border-right:2px solid var(--border2);padding-right:16px;">${dayLabel}</td>`
          : '';
        html += `<tr${idx === 0 ? ' style="border-top:2px solid var(--border2);"' : ''}>
          ${dateCell}
          <td><span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${color};margin-right:6px;vertical-align:middle;"></span>${esc(row.store)}</td>
          <td>${row.total_jobs}</td>
          <td style="font-weight:600;color:var(--text);">${row.total_inches.toFixed(1)} in</td>
        </tr>`;
      });
    });

    dailyBody.innerHTML = html;
  }
}

initStoreDates();

// CSV export for Store x Machine Type matrix
document.getElementById('store-type-export-csv')?.addEventListener('click', () => {
  if (!storeReportCache) return;
  const data = storeReportCache;

  const types = data.type_totals.map(t => t.machine_type);
  const stores = data.store_totals.map(s => s.store);
  const stwMatrix = {};
  const whSet = new Set();
  (data.store_type_wh || []).forEach(stw => {
    if (!stwMatrix[stw.store]) stwMatrix[stw.store] = {};
    if (!stwMatrix[stw.store][stw.machine_type]) stwMatrix[stw.store][stw.machine_type] = {};
    stwMatrix[stw.store][stw.machine_type][stw.warehouse] = stw;
    whSet.add(stw.warehouse);
  });
  const warehouseNames = [...whSet].sort();
  const storeTotalsMap = {};
  data.store_totals.forEach(s => { storeTotalsMap[s.store] = s; });
  const grandTotal = data.store_totals.reduce((a, s) => a + s.total_inches, 0);

  // Per store+type total (denominator for cell percentages)
  const storeTypeTotalMap = {};
  data.store_type_totals.forEach(st => {
    storeTypeTotalMap[`${st.store}|${st.machine_type}`] = st.total_inches;
  });

  const csvEscape = v => {
    const s = String(v == null ? '' : v);
    if (s.includes(',') || s.includes('"') || s.includes('\n')) {
      return '"' + s.replace(/"/g, '""') + '"';
    }
    return s;
  };

  const rows = [];

  if (warehouseNames.length === 0) {
    // Simple type-only mode — no warehouse breakdown, so no per-cell percentage needed
    const header = ['Store', ...types.map(t => t === 'UNSET' ? 'Not Set' : t.replace('_', ' ')), 'Total', '% Share'];
    rows.push(header.map(csvEscape).join(','));
    const stMatrix = {};
    data.store_type_totals.forEach(st => { if (!stMatrix[st.store]) stMatrix[st.store] = {}; stMatrix[st.store][st.machine_type] = st; });
    stores.forEach(store => {
      const sd = storeTotalsMap[store] || { total_inches: 0 };
      const share = grandTotal > 0 ? ((sd.total_inches / grandTotal) * 100).toFixed(1) + '%' : '0%';
      const row = [store, ...types.map(t => { const c = (stMatrix[store]||{})[t]; return c ? c.total_inches.toFixed(1) : ''; }), sd.total_inches.toFixed(1), share];
      rows.push(row.map(csvEscape).join(','));
    });
    const totalRow = ['TOTAL', ...types.map(t => { const td = data.type_totals.find(x=>x.machine_type===t); return td ? td.total_inches.toFixed(1) : ''; }), grandTotal.toFixed(1), '100%'];
    rows.push(totalRow.map(csvEscape).join(','));
  } else {
    // Two header rows: type spans warehouses
    const header1 = ['Store'];
    types.forEach(t => {
      const label = t === 'UNSET' ? 'Not Set' : t.replace('_', ' ');
      header1.push(label);
      // Empty cells for remaining warehouses under this type
      for (let i = 1; i < warehouseNames.length; i++) header1.push('');
    });
    header1.push('Total');
    header1.push('% Share');
    rows.push(header1.map(csvEscape).join(','));

    const header2 = [''];
    types.forEach(() => warehouseNames.forEach(wh => header2.push(wh)));
    header2.push('');
    header2.push('');
    rows.push(header2.map(csvEscape).join(','));

    // Data rows
    stores.forEach(store => {
      const sd = storeTotalsMap[store] || { total_inches: 0 };
      const share = grandTotal > 0 ? ((sd.total_inches / grandTotal) * 100).toFixed(1) + '%' : '0%';
      const row = [store];
      types.forEach(t => {
        const storeTypeTotal = storeTypeTotalMap[`${store}|${t}`] || 0;
        warehouseNames.forEach(wh => {
          const cell = stwMatrix[store]?.[t]?.[wh];
          if (cell) {
            const pct = storeTypeTotal > 0 ? ((cell.total_inches / storeTypeTotal) * 100).toFixed(1) : '0';
            row.push(`${cell.total_inches.toFixed(1)} (${pct}%)`);
          } else {
            row.push('');
          }
        });
      });
      row.push(sd.total_inches.toFixed(1));
      row.push(share);
      rows.push(row.map(csvEscape).join(','));
    });

    // Total row
    const totalRow = ['TOTAL'];
    types.forEach(t => {
      warehouseNames.forEach(wh => {
        const val = stores.reduce((sum, s) => {
          const cell = stwMatrix[s]?.[t]?.[wh];
          return sum + (cell ? cell.total_inches : 0);
        }, 0);
        totalRow.push(val > 0 ? val.toFixed(1) : '');
      });
    });
    totalRow.push(grandTotal.toFixed(1));
    totalRow.push('100%');
    rows.push(totalRow.map(csvEscape).join(','));
  }

  const csv = rows.join('\n');
  const blob = new Blob(['\ufeff' + csv], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `store-machine-type_${data.start_date}_to_${data.end_date}.csv`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
});


// ── Init ──
loadWarehouses().then(() => {
  renderSidebar();
  connectWS();
  loadCustomers();
});
