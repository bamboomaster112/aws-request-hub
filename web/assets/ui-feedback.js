// Toasts, the confirm dialog, status badges, empty states and loading placeholders.
import { h } from './dom.js';
import { icon } from './icons.js';

const TONE_ICON = { ok: 'checkCircle', danger: 'xCircle', warn: 'alert', info: 'info' };

// One message at a time, announced politely. `persist` keeps it through the next page change
// (for a message shown just before navigating, such as "Submitted").
export function toast(message, tone = 'info', { persist = false } = {}) {
  const el = document.getElementById('toast');
  const close = h('button', { type: 'button', class: 'toast-close', 'aria-label': 'Dismiss message' }, icon('close', { size: 16 }));
  close.addEventListener('click', () => hideToast());
  el.replaceChildren(icon(TONE_ICON[tone] || 'info', { size: 18, cls: 'toast-icon' }), h('span', { class: 'toast-text' }, message), close);
  el.className = `toast ${tone}`;
  el.dataset.persist = persist ? '1' : '';
  el.hidden = false;
  clearTimeout(toast.timer);
  const ms = tone === 'danger' ? 9000 : 5000;
  const arm = () => { clearTimeout(toast.timer); toast.timer = setTimeout(hideToast, ms); };
  el.onmouseenter = () => clearTimeout(toast.timer);
  el.onmouseleave = arm;
  arm();
}

export function hideToast() {
  const el = document.getElementById('toast');
  if (el) el.hidden = true;
}

// Called on every page change: page-specific messages go, a persisting one survives once.
export function clearToastOnNavigate() {
  const el = document.getElementById('toast');
  if (!el || el.hidden) return;
  if (el.dataset.persist) { el.dataset.persist = ''; return; }
  hideToast();
}

export function confirmDialog({ title, body, confirmLabel = 'Confirm', danger = false, typeToConfirm = null }) {
  const dialog = document.getElementById('confirm');
  const ok = document.getElementById('confirmOk');
  const typeWrap = document.getElementById('confirmTypeWrap');
  const typeInput = document.getElementById('confirmType');
  document.getElementById('confirmIcon').replaceChildren(icon(danger ? 'alert' : 'info', { size: 22 }));
  document.getElementById('confirmIcon').className = `dialog-icon ${danger ? 'danger' : 'info'}`;
  document.getElementById('confirmTitle').textContent = title;
  document.getElementById('confirmBody').textContent = body;
  ok.textContent = confirmLabel;
  ok.className = `btn ${danger ? 'danger solid' : 'primary'}`;
  typeWrap.hidden = !typeToConfirm;
  typeInput.value = '';
  if (typeToConfirm) {
    document.getElementById('confirmTypeLabel').textContent = `Type ${typeToConfirm} to confirm`;
    ok.disabled = true;
    typeInput.oninput = () => { ok.disabled = typeInput.value.trim().toLowerCase() !== typeToConfirm.toLowerCase(); };
  } else {
    ok.disabled = false;
  }
  return new Promise((resolve) => {
    dialog.addEventListener('close', () => resolve(dialog.returnValue === 'ok'), { once: true });
    dialog.returnValue = '';
    dialog.showModal();
    (typeToConfirm ? typeInput : ok).focus();
  });
}

export const STATUS = {
  SUBMITTED: ['Submitted', 'info'],
  PENDING_APPROVAL: ['Pending approval', 'warn'],
  ESCALATED: ['Escalated', 'danger'],
  APPROVED: ['Approved', 'ok'],
  AUTO_APPROVED: ['Auto-approved', 'ok'],
  FULFILLED: ['Fulfilled', 'ok'],
  REJECTED: ['Rejected', 'danger'],
  EXPIRED: ['Expired', 'muted'],
  FAILED: ['Failed', 'danger'],
  UNROUTABLE: ['No approver found', 'danger'],
};

export function badge(status) {
  const [label, tone] = STATUS[status] || [String(status || '').replace(/_/g, ' ').toLowerCase(), 'info'];
  return h('span', { class: `badge ${tone}`, 'data-status': status }, h('span', { class: 'dot', 'aria-hidden': 'true' }), label);
}

export function pill(text, tone = 'info') {
  return h('span', { class: `badge ${tone}` }, text);
}

export function emptyState({ icon: iconName = 'inbox', title, body = '', action = null }) {
  return h('div', { class: 'empty-state' },
    h('span', { class: 'empty-icon' }, icon(iconName, { size: 28 })),
    h('h2', { class: 'empty-title' }, title),
    body ? h('p', { class: 'muted' }, body) : null,
    action);
}

export function skeleton(lines = 4, label = 'Loading') {
  return h('div', { class: 'skeleton-card', role: 'status', 'aria-busy': 'true', 'aria-label': label },
    [...Array(lines)].map((_, i) => h('span', { class: `skeleton-line w${(i % 3) + 1}` })));
}
