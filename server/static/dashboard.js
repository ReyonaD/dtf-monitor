// ── DTF Floor Monitor — Dashboard JS ──

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
    document.getElementById('view-machines').style.display = view === 'machines' ? '' : 'none';

    if (view === 'reports') loadReport();
    if (view === 'machines') renderMachinesTab();
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

  // Detail table — grouped by date
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

        html += `<tr${idx === 0 ? ' style="border-top:2px solid var(--border2);"' : ''}>
          ${dateCell}
          <td>${esc(row.machine_name)}</td>
          <td>${row.total_jobs}</td>
          <td style="font-weight:600;color:var(--text);">${row.total_inches.toFixed(1)} in</td>
          <td style="font-family:var(--mono);font-size:12px;color:var(--text2);">${firstPrint}</td>
        </tr>`;
      });
    });

    tbody.innerHTML = html;
  }
}

initReportDates();


// ── Machines Management Tab ──

function renderMachinesTab() {
  const container = document.getElementById('machines-manage-list');

  if (lastState.length === 0) {
    container.innerHTML = '<div style="padding:40px;text-align:center;font-family:var(--mono);font-size:12px;color:var(--text3);">No machines registered.</div>';
    return;
  }

  container.innerHTML = `
    <table class="history-table">
      <thead>
        <tr>
          <th>Machine Name</th>
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

          return `<tr>
            <td style="font-weight:600;">${esc(m.machine.name)}</td>
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

// Warehouse assignment handler
document.getElementById('machines-manage-list').addEventListener('change', async (e) => {
  const select = e.target.closest('.warehouse-select');
  if (!select) return;
  const machineId = select.dataset.machineId;
  const warehouse = select.value;

  try {
    const resp = await fetch(`/api/machines/${encodeURIComponent(machineId)}/warehouse`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ warehouse }),
    });
    if (!resp.ok) throw new Error('Failed to update warehouse');
  } catch (err) {
    alert('Failed to update warehouse: ' + err.message);
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


// ── Init ──
loadWarehouses().then(() => {
  renderSidebar();
  connectWS();
});
