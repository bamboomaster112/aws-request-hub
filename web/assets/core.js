// Shared building blocks for the Request Hub single-page app (no framework, no build step).
// Every value from the API is rendered with textContent, never innerHTML.

export const state = { cfg: null, idToken: null, claims: null, me: null, meAt: 0 };

export const MODULES = {
  PURCHASE: { label: 'Purchasing', blurb: 'Purchase requests for goods and services.' },
  ACCESS: { label: 'Access management', blurb: 'Requests for system and data access.' },
  LEAVE: { label: 'Leave', blurb: 'Time off and leave requests.' },
  CHANGE: { label: 'Change management', blurb: 'Production and infrastructure changes.' },
  GENERAL: { label: 'General requests', blurb: 'Anything that needs a yes but has no module of its own.' },
};

export const ROLES = {
  requesters: { label: 'Requester', blurb: 'Raises and tracks requests in the modules they hold.' },
  approvers: { label: 'Approver', blurb: 'Can manage people and sit in approver groups. Raises requests in the modules they hold.' },
  admins: { label: 'Admin', blurb: 'Manages users and configuration, stands in where nobody else can approve, and handles escalations. Every module is included.' },
};

export const STATUS_TONE = {
  SUBMITTED: 'info', PENDING_APPROVAL: 'warn', ESCALATED: 'danger', APPROVED: 'ok', AUTO_APPROVED: 'ok',
  FULFILLED: 'ok', REJECTED: 'muted', EXPIRED: 'muted', FAILED: 'danger', UNROUTABLE: 'danger',
};

export const CURRENCIES = ['USD', 'EUR', 'GBP', 'INR', 'SGD', 'AED'];

// ---------- DOM ----------
export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') el.className = value;
    else if (key.startsWith('on') && typeof value === 'function') el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? '' : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

export const store = {
  get(k) { try { return sessionStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { sessionStorage.setItem(k, v); } catch { /* storage unavailable */ } },
  del(k) { try { sessionStorage.removeItem(k); } catch { /* storage unavailable */ } },
};

export function toast(message, tone = 'info') {
  const el = document.getElementById('toast');
  el.textContent = message;
  el.className = `toast ${tone}`;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, 5000);
}

export function mount(...nodes) {
  document.getElementById('app').replaceChildren(...nodes.filter((n) => n !== null && n !== undefined && n !== false));
}

let router = () => {};
export function setRouter(fn) { router = fn; }
export function rerender() { return router(); }

// ---------- formatting ----------
export function fmtTime(value) {
  if (value === undefined || value === null || value === '') return '';
  const d = typeof value === 'number' ? new Date(value) : new Date(String(value));
  return Number.isNaN(d.getTime()) ? String(value) : d.toLocaleString();
}

export function fmtNumber(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n.toLocaleString() : String(value ?? '');
}

export function fmtMoney(value, currency) {
  try { return new Intl.NumberFormat(undefined, { style: 'currency', currency: currency || 'USD' }).format(Number(value || 0)); }
  catch { return `${fmtNumber(value)} ${currency || ''}`.trim(); }
}

export function fmtField(f, currency) {
  if (f.type === 'checkbox') return f.value ? 'Yes' : 'No';
  if (f.type === 'amount') return fmtMoney(f.value, currency);
  if (f.type === 'number') return fmtNumber(f.value);
  if (f.type === 'date') {
    const d = new Date(`${f.value}T00:00:00`);
    return Number.isNaN(d.getTime()) ? f.value : d.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
  }
  return String(f.value ?? '');
}

export function badge(status) {
  return h('span', { class: `badge ${STATUS_TONE[status] || 'info'}` }, String(status || '').replace(/_/g, ' '));
}

export function field(label, value) {
  if (value === undefined || value === null || value === '') return null;
  return h('div', { class: 'field' }, h('dt', {}, label), h('dd', {}, value));
}

export function initials(user) {
  const source = (user.name || user.email || '?').trim();
  const parts = source.split(/[\s@._-]+/).filter(Boolean);
  return ((parts[0] || '?')[0] + ((parts[1] || '')[0] || '')).toUpperCase();
}

export function avatar(user, size = '') {
  return h('span', { class: `avatar ${size}`, 'aria-hidden': 'true' }, initials(user));
}

export function displayName(user) {
  return (user && (user.name || user.email)) || 'Unknown';
}

export function newKey() {
  if (crypto.randomUUID) return crypto.randomUUID().replace(/-/g, '');
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, '0')).join('');
}

// ---------- API ----------
export class ApiError extends Error {
  constructor(message, status, data) {
    super(message);
    this.status = status;
    this.data = data || {};
  }
}

export async function api(path, options = {}) {
  const res = await fetch(`${state.cfg.apiBase}${path}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', Authorization: state.idToken, ...(options.headers || {}) },
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty or non-JSON body */ }
  if (res.status === 401) {
    store.del('id_token');
    state.idToken = null;
    state.me = null;
    throw new ApiError('Your session has expired. Please sign in again.', 401, data);
  }
  if (res.status === 403 && data && data.message === 'Forbidden') {
    throw new ApiError('Blocked by the web application firewall (WAF). Remove anything that looks like SQL or script.', 403, data);
  }
  if (!res.ok) {
    const detail = data && data.details && typeof data.details === 'string' ? ` (${data.details})` : '';
    throw new ApiError(((data && data.message) || `Request failed with status ${res.status}`) + detail, res.status, data);
  }
  return data;
}

// The live access profile decides what the screens offer (the token can be up to an hour old).
export async function ensureMe(force = false) {
  if (!force && state.me && Date.now() - state.meAt < 60000) return state.me;
  state.me = await api('/me');
  state.meAt = Date.now();
  return state.me;
}

export const role = () => (state.me && state.me.role) || null;
export const isAdmin = () => Boolean(state.me && state.me.canManageUsers);
export const isReviewer = () => role() === 'approvers' || role() === 'admins';

// ---------- dialogs ----------
export function confirmDialog({ title, body, confirmLabel = 'Confirm', danger = false, typeToConfirm = null }) {
  const dialog = document.getElementById('confirm');
  const ok = document.getElementById('confirmOk');
  const typeWrap = document.getElementById('confirmTypeWrap');
  const typeInput = document.getElementById('confirmType');
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
    dialog.showModal();
    (typeToConfirm ? typeInput : ok).focus();
  });
}

// ---------- dynamic form fields (used by the request form and the form builder preview) ----------
export function renderField(f, { value, preview = false, currency = null } = {}) {
  const id = `f-${f.key}-${Math.random().toString(36).slice(2, 7)}`;
  const error = h('span', { class: 'field-error', role: 'alert' });
  let input;
  if (f.type === 'textarea') {
    input = h('textarea', { id, rows: '4', maxlength: f.maxLength || 2000 });
    if (value !== undefined) input.value = value;
  } else if (f.type === 'select') {
    input = h('select', { id }, h('option', { value: '' }, 'Choose...'), (f.options || []).map((o) => h('option', { value: o }, o)));
    if (value !== undefined) input.value = value;
  } else if (f.type === 'checkbox') {
    input = h('input', { id, type: 'checkbox' });
    input.checked = value === true;
  } else if (f.type === 'number' || f.type === 'amount') {
    input = h('input', { id, type: 'number', step: 'any', min: f.min ?? 0, max: f.max ?? '', inputmode: 'decimal' });
    if (value !== undefined) input.value = value;
  } else if (f.type === 'date') {
    input = h('input', { id, type: 'date' });
    if (value !== undefined) input.value = value;
  } else {
    input = h('input', { id, type: 'text', maxlength: f.maxLength || 120 });
    if (value !== undefined) input.value = value;
  }
  if (f.required && f.type !== 'checkbox') input.required = true;
  if (preview) input.tabIndex = -1;

  const range = (f.type === 'number' || f.type === 'amount') && (f.min !== undefined || f.max !== undefined)
    ? `${fmtNumber(f.min ?? 0)} and ${fmtNumber(f.max ?? 1000000)}` : '';
  const hints = [f.help ? String(f.help).replace(/[.\s]+$/, '') : '', range ? `Between ${range}` : ''].filter(Boolean).join('. ');
  let node;
  if (f.type === 'checkbox') {
    node = h('div', { class: 'dyn-field check-field' },
      h('label', { class: 'check', for: id }, input, h('span', {}, f.label, f.required ? h('span', { class: 'req' }, ' *') : null)),
      hints ? h('span', { class: 'hint-inline' }, hints) : null, error);
  } else {
    const control = f.type === 'amount' && currency
      ? h('div', { class: 'amount-row' }, h('span', { class: 'currency-tag' }, currency), input)
      : input;
    node = h('div', { class: 'dyn-field' },
      h('label', { for: id }, f.label, f.required ? h('span', { class: 'req' }, ' *') : null),
      control,
      hints ? h('span', { class: 'hint-inline' }, hints) : null, error);
  }
  return {
    key: f.key,
    node,
    input,
    read() {
      if (f.type === 'checkbox') return input.checked;
      const raw = input.value;
      if (raw === '' || raw === null) return undefined;
      if (f.type === 'number' || f.type === 'amount') return Number(raw);
      return raw;
    },
    setError(message) {
      error.textContent = message || '';
      node.classList.toggle('has-error', Boolean(message));
    },
  };
}
