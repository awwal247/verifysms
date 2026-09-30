/* ── Django CSRF bridge ─────────────────────────────────────────────────────
   Automatically attaches Django's CSRF cookie to AJAX requests. ── */
(function () {
  const originalFetch = window.fetch;
  window.fetch = function (input, init) {
    init = init || {};
    const method = (init.method || (input && input.method) || 'GET').toUpperCase();
    if (method !== 'GET' && method !== 'HEAD') {
      const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
      init.headers = new Headers(init.headers || {});
      if (match && !init.headers.has('X-CSRFToken')) init.headers.set('X-CSRFToken', decodeURIComponent(match[1]));
    }
    return originalFetch(input, init);
  };
})();

/* ── Theme Toggle ─────────────────────────────────────────────────────────── */
(function() {
  const saved = localStorage.getItem('vsms_theme') || 'dark';
  applyTheme(saved);
})();

function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  const icon = document.getElementById('themeIcon');
  if (icon) icon.className = theme === 'light' ? 'fa-solid fa-moon' : 'fa-solid fa-sun';
  localStorage.setItem('vsms_theme', theme);
}

function toggleTheme() {
  const current = document.documentElement.getAttribute('data-theme') || 'dark';
  applyTheme(current === 'dark' ? 'light' : 'dark');
}

/* ── Custom Confirm Modal ──────────────────────────────────────────────────── */
function showConfirm(message, onConfirm, options) {
  const opts = Object.assign({ title: 'Confirm Action', confirmText: 'Confirm', cancelText: 'Cancel', danger: false }, options || {});
  const overlay  = document.getElementById('vsmsConfirmOverlay');
  const msgEl    = document.getElementById('vsmsModalMsg');
  const titleEl  = document.getElementById('vsmsModalTitle');
  const iconEl   = document.getElementById('vsmsModalIcon');
  const okBtn    = document.getElementById('vsmsModalOk');
  const cancelBtn= document.getElementById('vsmsModalCancel');
  if (!overlay) return; // header not loaded, fallback
  msgEl.textContent    = message;
  titleEl.textContent  = opts.title;
  okBtn.textContent    = opts.confirmText;
  cancelBtn.textContent= opts.cancelText;
  iconEl.className     = opts.danger ? 'vsms-modal-icon danger' : 'vsms-modal-icon';
  iconEl.innerHTML     = opts.danger ? '<i class="fa-solid fa-triangle-exclamation"></i>' : '<i class="fa-solid fa-circle-question"></i>';
  okBtn.className      = opts.danger ? 'btn btn-danger' : 'btn btn-primary';
  overlay.classList.add('active');
  const close = () => overlay.classList.remove('active');
  okBtn.onclick    = () => { close(); if (onConfirm) onConfirm(); };
  cancelBtn.onclick= () => close();
  overlay.onclick  = (e) => { if (e.target === overlay) close(); };
}

/* Intercept a form-submit button with a confirm dialog */
function confirmAndSubmit(event, message, btn, danger) {
  event.preventDefault();
  showConfirm(message, () => btn.closest('form').submit(), { danger: !!danger, title: danger ? 'Warning' : 'Confirm Action' });
}

// ── Sidebar ────────────────────────────────────────────────────────────────
function openSidebar() {
  document.getElementById('sidebar').classList.add('open');
  document.getElementById('sidebarOverlay').classList.add('active');
  document.body.style.overflow = 'hidden';
}
function closeSidebar() {
  document.getElementById('sidebar').classList.remove('open');
  document.getElementById('sidebarOverlay').classList.remove('active');
  document.body.style.overflow = '';
}

// ── Profile Menu ───────────────────────────────────────────────────────────
function toggleProfileMenu() {
  const menu = document.getElementById('profileMenu');
  if (menu) menu.classList.toggle('open');
}
document.addEventListener('click', function(e) {
  const menu = document.getElementById('profileMenu');
  const btn  = document.querySelector('.avatar-btn');
  if (menu && btn && !btn.contains(e.target)) {
    menu.classList.remove('open');
  }
});

// ── Toast Notification ─────────────────────────────────────────────────────
function showToast(message, type = 'info', duration = 3500) {
  let container = document.getElementById('toast-container');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toast-container';
    document.body.appendChild(container);
  }
  const icons = { success: 'fa-check-circle', error: 'fa-exclamation-circle', info: 'fa-info-circle', warning: 'fa-exclamation-triangle' };
  const colors = { success: 'var(--success)', error: 'var(--danger)', info: 'var(--info)', warning: 'var(--warning)' };
  const toast = document.createElement('div');
  toast.className = 'toast ' + type;
  toast.innerHTML = `<i class="fa-solid ${icons[type] || icons.info}" style="color:${colors[type]};flex-shrink:0"></i><span>${message}</span>`;
  container.appendChild(toast);
  setTimeout(() => {
    toast.classList.add('hide');
    toast.addEventListener('animationend', () => toast.remove());
  }, duration);
}

// ── Copy to Clipboard ──────────────────────────────────────────────────────
function copyText(text, btn) {
  navigator.clipboard.writeText(text).then(() => {
    if (btn) {
      const orig = btn.innerHTML;
      btn.innerHTML = '<i class="fa-solid fa-check"></i> Copied!';
      btn.style.color = 'var(--success)';
      setTimeout(() => { btn.innerHTML = orig; btn.style.color = ''; }, 2000);
    }
    showToast('Copied to clipboard!', 'success', 2000);
  }).catch(() => {
    // fallback
    const el = document.createElement('textarea');
    el.value = text; el.style.opacity = '0';
    document.body.appendChild(el); el.select();
    document.execCommand('copy');
    document.body.removeChild(el);
    showToast('Copied!', 'success', 2000);
  });
}

// ── Countdown Timer ────────────────────────────────────────────────────────
function startCountdown(elementId, expiresAt) {
  const el = document.getElementById(elementId);
  if (!el) return;
  const expires = new Date(expiresAt).getTime();
  const tick = () => {
    const now = Date.now();
    const diff = Math.max(0, Math.floor((expires - now) / 1000));
    if (diff === 0) { el.textContent = 'Expired'; el.classList.add('urgent'); return; }
    const m = Math.floor(diff / 60);
    const s = diff % 60;
    el.textContent = m + ':' + String(s).padStart(2, '0');
    if (diff < 120) el.classList.add('urgent');
    setTimeout(tick, 1000);
  };
  tick();
}

// ── SMS Polling ────────────────────────────────────────────────────────────
const activePollers = {};

function startSmsPolling(orderId, intervalSec = 5) {
  if (activePollers[orderId]) return;
  const interval = setInterval(() => {
    fetch('/api/orders/?action=check&order_id=' + orderId, {
      headers: { 'X-Requested-With': 'XMLHttpRequest' }
    })
    .then(r => r.json())
    .then(data => {
      if (data.status === 'RECEIVED' || data.sms_code) {
        clearInterval(activePollers[orderId]);
        delete activePollers[orderId];
        updateOrderCard(orderId, data);
        showToast('SMS code received!', 'success', 5000);
        // Play notification sound if supported
        try { new Audio('/assets/notify.mp3').play(); } catch(e) {}
      } else if (data.status === 'CANCELED' || data.status === 'TIMEOUT' || data.status === 'FINISHED') {
        clearInterval(activePollers[orderId]);
        delete activePollers[orderId];
        updateOrderCard(orderId, data);
      } else {
        // Still pending - update status indicator
        const statusEl = document.getElementById('status-' + orderId);
        if (statusEl) statusEl.innerHTML = '<span class="badge badge-pending">Waiting...</span>';
      }
    })
    .catch(err => console.error('Poll error:', err));
  }, intervalSec * 1000);
  activePollers[orderId] = interval;
}

function updateOrderCard(orderId, data) {
  const card = document.getElementById('order-card-' + orderId);
  if (!card) return;

  // Update status
  const statusEl = document.getElementById('status-' + orderId);
  if (statusEl) {
    const badges = {
      'RECEIVED': '<span class="badge badge-success">SMS Received</span>',
      'CANCELED': '<span class="badge badge-danger">Cancelled</span>',
      'TIMEOUT':  '<span class="badge badge-warning">Expired</span>',
      'FINISHED': '<span class="badge badge-info">Finished</span>',
    };
    statusEl.innerHTML = badges[data.status] || statusEl.innerHTML;
  }

  // Show SMS code
  if (data.sms_code) {
    const smsEl = document.getElementById('sms-area-' + orderId);
    if (smsEl) {
      smsEl.innerHTML = `
        <div class="sms-code-box">
          <div class="sms-code-label"><i class="fa-solid fa-envelope-open-text"></i> Verification Code</div>
          <div class="sms-code-value">${data.sms_code}</div>
          ${data.sms_text ? '<div class="sms-text">' + data.sms_text + '</div>' : ''}
        </div>`;
      smsEl.style.display = 'block';
    }
  }

  // Refresh action buttons
  if (data.status !== 'PENDING') {
    const actionsEl = document.getElementById('actions-' + orderId);
    if (actionsEl) actionsEl.innerHTML = '';
  }
}

// ── Order Actions ──────────────────────────────────────────────────────────
function orderAction(orderId, action, btn) {
  const confirmMsgs = {
    cancel: 'Cancel this order? Unused number will be refunded.',
    finish: 'Mark this order as finished?',
    ban:    'Report this number as bad? This will cancel and flag the number.',
  };
  const isDanger = action === 'cancel' || action === 'ban';
  showConfirm(confirmMsgs[action] || 'Are you sure?', () => {
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span>';
    fetch('/api/orders/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-Requested-With': 'XMLHttpRequest' },
      body: 'action=' + action + '&order_id=' + orderId + '&csrf_token=' + encodeURIComponent(window._csrf || '')
    })
    .then(r => r.json())
    .then(data => {
      if (data.success) {
        showToast(data.message || 'Done!', 'success');
        setTimeout(() => location.reload(), 1200);
      } else {
        showToast(data.message || 'Error occurred.', 'error');
        btn.disabled = false;
      }
    })
    .catch(() => { showToast('Request failed.', 'error'); btn.disabled = false; });
  }, { danger: isDanger, title: isDanger ? 'Warning' : 'Confirm' });
}

// ── Country/Service filter ─────────────────────────────────────────────────
function filterServices(query) {
  const q = query.toLowerCase();
  document.querySelectorAll('.service-card').forEach(card => {
    const name = card.dataset.service || '';
    card.style.display = name.includes(q) ? '' : 'none';
  });
}

// ── Form Loading ───────────────────────────────────────────────────────────
function setLoading(formOrBtn, loading) {
  const btn = formOrBtn.tagName === 'FORM' ? formOrBtn.querySelector('[type=submit]') : formOrBtn;
  if (!btn) return;
  if (loading) {
    btn._origHTML = btn.innerHTML;
    btn.innerHTML = '<span class="spinner"></span> Please wait...';
    btn.disabled = true;
  } else {
    btn.innerHTML = btn._origHTML || 'Submit';
    btn.disabled = false;
  }
}

// ── Auto-init ──────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', function() {
  // Start polling for all active order cards
  document.querySelectorAll('[data-poll-order]').forEach(el => {
    startSmsPolling(el.dataset.pollOrder, parseInt(el.dataset.interval || '5'));
  });
  // Countdowns
  document.querySelectorAll('[data-countdown]').forEach(el => {
    startCountdown(el.id, el.dataset.countdown);
  });
  // Auto-dismiss alerts after 5s
  document.querySelectorAll('.alert:not(.alert-danger)').forEach(el => {
    setTimeout(() => { if (el.parentNode) el.style.transition = 'opacity 0.5s'; el.style.opacity = '0'; setTimeout(() => el.remove(), 500); }, 5000);
  });
  // Store CSRF for JS use
  const csrfMeta = document.querySelector('meta[name=csrf]');
  if (csrfMeta) window._csrf = csrfMeta.content;
});

// ── Customer Care floating button ───────────────────────────────────────────
(function initSupportFab() {
  try {
    fetch('/api/support/', { headers: { 'Accept': 'application/json' } })
      .then(r => r.json())
      .then(d => {
        if (!d || !d.whatsapp) return;
        const wa = String(d.whatsapp).replace(/[^0-9]/g, '');
        if (!wa) return;
        const a = document.createElement('a');
        a.id = 'supportFab';
        a.href = 'https://wa.me/' + wa + '?text=' + encodeURIComponent('Hello, I need help on VerifySMS');
        a.target = '_blank';
        a.rel = 'noopener';
        a.title = d.label || 'Customer Care';
        a.setAttribute('aria-label', d.label || 'Customer Care');
        a.style.cssText = 'position:fixed;right:16px;bottom:16px;z-index:9999;width:56px;height:56px;border-radius:50%;' +
          'background:var(--success,#22c55e);color:#fff;display:flex;align-items:center;justify-content:center;' +
          'box-shadow:0 4px 14px rgba(0,0,0,.25);text-decoration:none;';
        a.innerHTML = '<svg viewBox="0 0 32 32" width="30" height="30" fill="currentColor" aria-hidden="true">' +
          '<path d="M16 .8C7.6.8.8 7.6.8 16c0 2.7.7 5.3 2 7.6L.9 31.1l7.7-1.9c2.2 1.2 4.7 1.9 7.4 1.9 8.4 0 15.2-6.8 15.2-15.2S24.4.8 16 .8zm0 27.7c-2.4 0-4.6-.6-6.5-1.7l-.5-.3-4.5 1.2 1.2-4.4-.3-.5C4 20.9 3.4 18.5 3.4 16 3.4 9 9 3.4 16 3.4S28.6 9 28.6 16 23 28.5 16 28.5zm7-8.5c-.4-.2-2.3-1.1-2.7-1.2-.3-.1-.6-.2-.8.2-.2.4-.9 1.2-1.1 1.5-.2.2-.4.3-.8.1-2.3-1.1-3.8-2-5.3-4.5-.4-.7.4-.7 1.1-2.2.1-.3 0-.5 0-.7-.1-.2-.8-2-1.1-2.7-.3-.7-.6-.6-.8-.6h-.7c-.2 0-.6.1-.9.4-1.5 1.5-2 3.6-.4 6 1.6 2.7 3.2 4.3 5.7 5.5 2.4 1.1 4.8 1.4 5.8 1.5.7.1 2.3.2 2.9-1 .6-1.2.8-2 .9-2.1-.1-.1-.4-.2-.8-.4z"/></svg>';
        document.body.appendChild(a);
        addChannelButton(d);
        addLogsButton(d);
      })
      .catch(() => {});
  } catch (e) {}
})();

// ── "Join our channel" button ───────────────────────────────────────────────
// Rides above the customer-care FAB. The channel URL comes from /api/support/
// (admin → Settings → WhatsApp Channel), so it can be changed without a deploy.
function addChannelButton(d) {
  try {
    const url = (d && d.channel) ? String(d.channel).trim() : '';
    if (!url) return;
    if (document.getElementById('channelFab')) return;

    const a = document.createElement('a');
    a.id = 'channelFab';
    a.href = url;
    a.target = '_blank';
    a.rel = 'noopener';
    a.title = (d && d.channel_label) || 'Join our channel';
    a.setAttribute('aria-label', a.title);
    a.style.cssText = 'position:fixed;right:16px;bottom:82px;z-index:9999;display:inline-flex;align-items:center;gap:8px;' +
      'height:44px;padding:0 18px 0 14px;border-radius:22px;background:#25D366;color:#fff;font-size:14px;font-weight:600;' +
      'font-family:inherit;text-decoration:none;white-space:nowrap;box-shadow:0 4px 14px rgba(0,0,0,.25);';
    a.innerHTML = '<svg viewBox="0 0 32 32" width="22" height="22" fill="currentColor" aria-hidden="true">' +
      '<path d="M16 .8C7.6.8.8 7.6.8 16c0 2.7.7 5.3 2 7.6L.9 31.1l7.7-1.9c2.2 1.2 4.7 1.9 7.4 1.9 8.4 0 15.2-6.8 15.2-15.2S24.4.8 16 .8zm0 27.7c-2.4 0-4.6-.6-6.5-1.7l-.5-.3-4.5 1.2 1.2-4.4-.3-.5C4 20.9 3.4 18.5 3.4 16 3.4 9 9 3.4 16 3.4S28.6 9 28.6 16 23 28.5 16 28.5zm7-8.5c-.4-.2-2.3-1.1-2.7-1.2-.3-.1-.6-.2-.8.2-.2.4-.9 1.2-1.1 1.5-.2.2-.4.3-.8.1-2.3-1.1-3.8-2-5.3-4.5-.4-.7.4-.7 1.1-2.2.1-.3 0-.5 0-.7-.1-.2-.8-2-1.1-2.7-.3-.7-.6-.6-.8-.6h-.7c-.2 0-.6.1-.9.4-1.5 1.5-2 3.6-.4 6 1.6 2.7 3.2 4.3 5.7 5.5 2.4 1.1 4.8 1.4 5.8 1.5.7.1 2.3.2 2.9-1 .6-1.2.8-2 .9-2.1-.1-.1-.4-.2-.8-.4z"/></svg>' +
      '<span></span>';
    a.querySelector('span').textContent = a.title;
    document.body.appendChild(a);
    stackFabs();
  } catch (e) {}
}

// ── "Buy Social Media Logs" button ──────────────────────────────────────────
// Deliberately the loudest of the three: a pulsing accent pill sitting on top
// of the stack, because it is the one action we most want the eye to catch.
// The bot URL comes from /api/support/ (admin → Settings → Logs Bot URL), so
// it can be repointed or hidden without a deploy.
function addLogsButton(d) {
  try {
    const url = (d && d.logs) ? String(d.logs).trim() : '';
    if (!url) return;
    if (document.getElementById('logsFab')) return;

    const a = document.createElement('a');
    a.id = 'logsFab';
    a.href = url;
    a.target = '_blank';
    a.rel = 'noopener';
    a.title = (d && d.logs_label) || 'Buy Social Media Logs';
    a.setAttribute('aria-label', a.title);
    a.style.cssText = 'position:fixed;right:16px;bottom:148px;z-index:9999;display:inline-flex;align-items:center;gap:8px;' +
      'height:46px;padding:0 20px 0 14px;border-radius:23px;background:linear-gradient(135deg,#f97316,#ea6a0a);' +
      'color:#fff;font-size:14px;font-weight:700;font-family:inherit;text-decoration:none;white-space:nowrap;' +
      'box-shadow:0 6px 20px rgba(249,115,22,.45);animation:logsFabPulse 2.4s ease-in-out infinite;';
    a.innerHTML = '<svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor" aria-hidden="true">' +
      '<path d="M12 2 4 6v6c0 5 3.4 9.2 8 10 4.6-.8 8-5 8-10V6l-8-4zm0 4.2 5 2.5v3.3c0 3.4-2.1 6.4-5 7.2-2.9-.8-5-3.8-5-7.2V8.7l5-2.5z"/>' +
      '<path d="M11.2 9.2h1.6v4.2h-1.6zM11.2 14.6h1.6v1.6h-1.6z"/></svg>' +
      '<span></span>';
    a.querySelector('span').textContent = a.title;
    document.body.appendChild(a);
    stackFabs();
  } catch (e) {}
}

// Keep the floating buttons in one clean column, tallest stack element on top,
// regardless of which of them happen to be enabled at the moment.
function stackFabs() {
  try {
    const order = [['logsFab', '#f97316'], ['channelFab', '#25D366'], ['supportFab', 'var(--success,#22c55e)']];
    let bottom = 16;
    for (const [id] of order) {
      const el = document.getElementById(id);
      if (!el) continue;
      el.style.bottom = bottom + 'px';
      bottom += el.offsetHeight ? el.offsetHeight + 12 : 56;
    }
  } catch (e) {}
}

// Pulsing halo for the logs button. Injected once into <head>.
(function injectFabStyles() {
  try {
    if (document.getElementById('vsmsFabStyles')) return;
    const s = document.createElement('style');
    s.id = 'vsmsFabStyles';
    s.textContent = '@keyframes logsFabPulse{' +
      '0%,100%{box-shadow:0 6px 20px rgba(249,115,22,.45);transform:scale(1)}' +
      '50%{box-shadow:0 6px 26px rgba(249,115,22,.75);transform:scale(1.03)}}' +
      '@media (prefers-reduced-motion:reduce){#logsFab{animation:none!important}}';
    document.head.appendChild(s);
  } catch (e) {}
})();
