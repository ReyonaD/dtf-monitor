// ── DTF Customer Portal JS ──

// ── i18n ──
const translations = {
  en: {
    portal_title: 'Customer Portal',
    portal_sub: 'Sign in to your account',
    login_btn: 'Sign In',
    login_error: 'Invalid email or password',
    logout: 'Logout',
    remaining_credit: 'Remaining Credit',
    initial_credit: 'Initial credit',
    upload_files: 'Upload Files',
    upload_hint: 'Drag & drop <strong>PNG/TIFF</strong> files here or click to browse',
    my_files: 'My Files',
    no_files: 'No files uploaded yet',
    credit_history: 'Credit History',
    date: 'Date', amount: 'Amount', balance: 'Balance', reason: 'Reason',
    uploading: 'Uploading...',
    uploaded: 'UPLOADED', queued: 'QUEUED', printing: 'PRINTING', completed: 'COMPLETED',
    reason_monthly_allocation: 'Credit Added',
    reason_print_deduction: 'Print Deduction',
    reason_manual_adjustment: 'Manual Adjustment',
    delete_confirm: 'Delete this file?',
    inches: 'inches',
    copies: 'copies',
    copies_label: 'Copies',
    available: 'Available',
    pending: 'pending',
    credit_exceeded: 'Warning: This file exceeds your available credit!',
  },
  tr: {
    portal_title: 'Musteri Paneli',
    portal_sub: 'Hesabiniza giris yapin',
    login_btn: 'Giris Yap',
    login_error: 'Gecersiz email veya sifre',
    logout: 'Cikis',
    remaining_credit: 'Kalan Kredi',
    initial_credit: 'Baslangic kredisi',
    upload_files: 'Dosya Yukle',
    upload_hint: '<strong>PNG/TIFF</strong> dosyalari surukleyip birakin veya tiklayin',
    my_files: 'Dosyalarim',
    no_files: 'Henuz dosya yuklenmedi',
    credit_history: 'Kredi Gecmisi',
    date: 'Tarih', amount: 'Miktar', balance: 'Bakiye', reason: 'Aciklama',
    uploading: 'Yukleniyor...',
    uploaded: 'YUKLENDI', queued: 'KUYRUKTA', printing: 'BASILIYOR', completed: 'TAMAMLANDI',
    reason_monthly_allocation: 'Kredi Eklendi',
    reason_print_deduction: 'Baski Kesintisi',
    reason_manual_adjustment: 'Manuel Duzeltme',
    delete_confirm: 'Bu dosya silinsin mi?',
    inches: 'inc',
    copies: 'kopya',
    copies_label: 'Kopya',
    available: 'Kullanilabilir',
    pending: 'beklemede',
    credit_exceeded: 'Uyari: Bu dosya kullanilabilir kredinizi asiyor!',
  }
};

let currentLang = localStorage.getItem('dtf_lang') || 'en';
let customer = null;
let ws = null;
let reconnectTimer = null;

function t(key) {
  return translations[currentLang]?.[key] || translations['en']?.[key] || key;
}

function applyI18n() {
  document.querySelectorAll('[data-i18n]').forEach(el => {
    const key = el.dataset.i18n;
    const text = t(key);
    if (el.tagName === 'INPUT' || el.tagName === 'BUTTON') {
      if (el.type === 'submit' || el.tagName === 'BUTTON') el.textContent = text;
      else el.placeholder = text;
    } else {
      el.innerHTML = text;
    }
  });
}

function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

// ── Language toggle ──
document.querySelectorAll('.lang-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    currentLang = btn.dataset.lang;
    localStorage.setItem('dtf_lang', currentLang);
    document.querySelectorAll('.lang-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    applyI18n();
    if (customer) renderFiles();
    if (customer) renderCreditHistory();
  });
});

// Set initial active lang button
document.querySelectorAll('.lang-btn').forEach(b => {
  b.classList.toggle('active', b.dataset.lang === currentLang);
});

// ── Login ──
document.getElementById('login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const form = e.target;
  const email = form.email.value;
  const password = form.password.value;

  try {
    const resp = await fetch('/api/customer/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
    });
    if (!resp.ok) throw new Error('Login failed');
    const data = await resp.json();
    customer = data.customer;
    showPortal();
  } catch (err) {
    document.getElementById('login-error').classList.add('show');
  }
});

// ── Logout ──
document.getElementById('logout-btn').addEventListener('click', async () => {
  await fetch('/api/customer/auth/logout', { method: 'POST' });
  customer = null;
  document.getElementById('portal-view').style.display = 'none';
  document.getElementById('login-view').style.display = 'flex';
});

// ── Show portal after login ──
async function showPortal() {
  document.getElementById('login-view').style.display = 'none';
  document.getElementById('portal-view').style.display = 'block';
  document.getElementById('user-name').textContent = customer.name;
  document.getElementById('user-email').textContent = customer.email;
  applyI18n();
  await loadProfile();
  await loadFiles();
  await loadCreditHistory();
  connectWS();
}

// ── Check existing session on load ──
async function checkSession() {
  try {
    const resp = await fetch('/api/customer/me');
    if (resp.ok) {
      const data = await resp.json();
      customer = { id: data.id, name: data.name, email: data.email };
      document.getElementById('credit-balance').textContent = data.balance.toFixed(1);
      showPortal();
    }
  } catch (err) {
    // Not logged in
  }
  applyI18n();
}

async function loadProfile() {
  try {
    const resp = await fetch('/api/customer/me');
    const data = await resp.json();
    document.getElementById('credit-balance').textContent = data.balance.toFixed(1);
    // Show available/pending if there are pending files
    const pending = data.pending_inches || 0;
    const available = data.available_balance || data.balance;
    const availRow = document.getElementById('credit-available-row');
    if (pending > 0) {
      availRow.style.display = 'block';
      document.getElementById('credit-available').textContent = available.toFixed(1);
      document.getElementById('credit-pending').textContent = pending.toFixed(1);
    } else {
      availRow.style.display = 'none';
    }
  } catch (err) {}
}

// ── Files ──
let fileList = [];

async function loadFiles() {
  try {
    const resp = await fetch('/api/customer/files');
    fileList = await resp.json();
    renderFiles();
  } catch (err) {
    console.error('Failed to load files:', err);
  }
}

function renderFiles() {
  const grid = document.getElementById('file-grid');
  if (fileList.length === 0) {
    grid.innerHTML = `<div style="text-align:center;color:var(--muted);padding:24px;">${t('no_files')}</div>`;
    return;
  }

  grid.innerHTML = fileList.map(f => {
    const statusLabel = t(f.status);
    const uploaded = f.uploaded_at ? new Date(f.uploaded_at + 'Z').toLocaleString() : '';
    const deleteBtn = f.status === 'uploaded'
      ? `<button class="file-delete" data-file-id="${f.id}" title="Delete">&times;</button>`
      : '';

    const copiesInput = f.status === 'uploaded'
      ? `<div class="file-copies">
           <label style="font-size:11px;color:var(--muted);margin-right:4px;">${t('copies_label')}:</label>
           <input type="number" min="1" value="${f.copies}" class="copies-input" data-file-id="${f.id}" style="width:50px;padding:3px 6px;background:var(--bg);border:1px solid rgba(255,255,255,0.1);border-radius:5px;color:var(--text);font-size:13px;font-family:var(--mono);text-align:center;">
         </div>`
      : '';

    const totalInches = f.print_inches * f.copies;

    return `
      <div class="file-card">
        <div class="file-icon ${f.status}">
          ${f.status === 'uploaded' ? 'UP' : f.status === 'queued' ? 'QU' : f.status === 'printing' ? 'PR' : 'OK'}
        </div>
        <div class="file-info">
          <div class="file-name">${esc(f.original_filename)}</div>
          <div class="file-meta">
            ${f.print_inches.toFixed(1)} ${t('inches')}${f.copies > 1 ? ` x${f.copies} = ${totalInches.toFixed(1)} ${t('inches')}` : ''}
            &middot; ${f.width_px}&times;${f.height_px}px
            &middot; ${uploaded}
          </div>
        </div>
        ${copiesInput}
        <div class="file-status" style="color:${
          f.status === 'completed' ? '#3DCF82' :
          f.status === 'printing' ? '#E8FF47' :
          f.status === 'queued' ? '#FF7A35' : '#4A9EFF'
        }">${statusLabel}</div>
        ${deleteBtn}
      </div>`;
  }).join('');

  // Delete handlers
  grid.querySelectorAll('.file-delete').forEach(btn => {
    btn.addEventListener('click', async () => {
      if (!confirm(t('delete_confirm'))) return;
      try {
        await fetch(`/api/customer/files/${btn.dataset.fileId}`, { method: 'DELETE' });
        loadFiles();
        loadProfile();
      } catch (err) {
        alert('Delete failed');
      }
    });
  });

  // Copies input handlers
  grid.querySelectorAll('.copies-input').forEach(input => {
    input.addEventListener('change', async (e) => {
      const fileId = e.target.dataset.fileId;
      let copies = parseInt(e.target.value) || 1;
      if (copies < 1) copies = 1;
      e.target.value = copies;
      try {
        const resp = await fetch(`/api/customer/files/${fileId}/copies`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ copies }),
        });
        const data = await resp.json();
        if (data.credit_warning) alert(data.credit_warning);
        loadFiles();
        loadProfile();
      } catch (err) {
        alert('Failed to update copies');
      }
    });
  });
}

// ── Credit History ──
async function loadCreditHistory() {
  try {
    const resp = await fetch('/api/customer/credits');
    const history = await resp.json();
    renderCreditHistory(history);
  } catch (err) {}
}

function renderCreditHistory(history) {
  if (!history) return;
  const body = document.getElementById('credit-history-body');
  if (history.length === 0) {
    body.innerHTML = '<tr><td colspan="4" style="text-align:center;color:var(--muted);padding:16px;">—</td></tr>';
    return;
  }
  body.innerHTML = history.map(h => {
    const reasonKey = `reason_${h.reason}`;
    const reasonText = t(reasonKey) !== reasonKey ? t(reasonKey) : h.reason;
    return `<tr>
      <td>${new Date(h.created_at + 'Z').toLocaleString()}</td>
      <td style="color:${h.amount >= 0 ? '#3DCF82' : '#FF4D4D'};font-weight:600;">
        ${h.amount >= 0 ? '+' : ''}${h.amount.toFixed(1)}
      </td>
      <td>${h.balance_after.toFixed(1)}</td>
      <td>${esc(reasonText)}</td>
    </tr>`;
  }).join('');
}

// ── File Upload ──
const uploadZone = document.getElementById('upload-zone');
const fileInput = document.getElementById('file-input');
const uploadProgress = document.getElementById('upload-progress');

uploadZone.addEventListener('click', () => fileInput.click());

uploadZone.addEventListener('dragover', (e) => {
  e.preventDefault();
  uploadZone.classList.add('drag-over');
});

uploadZone.addEventListener('dragleave', () => {
  uploadZone.classList.remove('drag-over');
});

uploadZone.addEventListener('drop', (e) => {
  e.preventDefault();
  uploadZone.classList.remove('drag-over');
  handleFiles(e.dataTransfer.files);
});

fileInput.addEventListener('change', () => {
  handleFiles(fileInput.files);
  fileInput.value = '';
});

async function handleFiles(files) {
  for (const file of files) {
    const ext = file.name.split('.').pop().toLowerCase();
    if (!['png', 'tiff', 'tif'].includes(ext)) {
      alert(`${file.name}: Only PNG/TIFF files allowed`);
      continue;
    }
    if (file.size > 50 * 1024 * 1024) {
      alert(`${file.name}: File too large (max 50MB)`);
      continue;
    }
    uploadProgress.style.display = 'block';
    uploadProgress.textContent = `${t('uploading')} ${file.name}`;

    const formData = new FormData();
    formData.append('file', file);

    try {
      const resp = await fetch('/api/customer/files/upload', {
        method: 'POST',
        body: formData,
      });
      if (!resp.ok) {
        const err = await resp.json();
        throw new Error(err.error || 'Upload failed');
      }
      const result = await resp.json();
      if (result.credit_warning) {
        alert(t('credit_exceeded') + '\n' + result.credit_warning);
      }
    } catch (err) {
      alert(`Upload failed: ${err.message}`);
    }
  }
  uploadProgress.style.display = 'none';
  loadFiles();
  loadProfile();
}

// ── WebSocket ──
function connectWS() {
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${protocol}//${location.host}/ws/customer`);

  ws.onmessage = (event) => {
    const data = JSON.parse(event.data);
    if (data.type === 'initial_state') {
      fileList = data.files;
      renderFiles();
      document.getElementById('credit-balance').textContent = data.balance.toFixed(1);
    }
    if (data.type === 'file_update') {
      // Update file status in local list
      const file = fileList.find(f => f.id === data.file_id);
      if (file) {
        file.status = data.status;
        renderFiles();
      } else {
        loadFiles();
      }
      if (data.balance !== undefined) {
        document.getElementById('credit-balance').textContent = data.balance.toFixed(1);
      }
      if (data.credit_deducted) {
        loadCreditHistory();
      }
    }
    if (data.type === 'credit_update') {
      document.getElementById('credit-balance').textContent = data.balance.toFixed(1);
      loadCreditHistory();
    }
  };

  ws.onclose = () => {
    if (!reconnectTimer) {
      reconnectTimer = setInterval(() => {
        if (customer) connectWS();
      }, 5000);
    }
  };

  ws.onopen = () => {
    if (reconnectTimer) { clearInterval(reconnectTimer); reconnectTimer = null; }
  };

  ws.onerror = () => ws.close();
}

// ── Init ──
checkSession();
