// Shared state, the API client, formatting and the dynamic form field. UI pieces live in the
// ui-*.js modules and are re-exported here, so a screen can import everything from one place.
// Every value from the API is rendered with textContent, never innerHTML.
import { h } from './dom.js';
import { icon, MODULE_ICON } from './icons.js';
import { formField, adornedInput } from './ui-controls.js';
import { select } from './ui-select.js';
import { datePicker, parseIso } from './ui-datepicker.js';

export { h, svg, mount, paints, uid } from './dom.js';
export { icon, brandMark, MODULE_ICON } from './icons.js';
export {
  button, linkButton, iconButton, busy, formField, textInput, adornedInput, searchInput, choiceCard, checkbox, switchToggle,
} from './ui-controls.js';
export { select } from './ui-select.js';
export { datePicker, parseIso, toIso } from './ui-datepicker.js';
export {
  toast, hideToast, clearToastOnNavigate, confirmDialog, STATUS, badge, pill, emptyState, skeleton,
} from './ui-feedback.js';
export { pageHead, card, menu, fileDrop, segmented, stat } from './ui-layout.js';

export const state = { cfg: null, idToken: null, claims: null, me: null, meAt: 0 };

export const MODULES = {
  PURCHASE: { label: 'Purchasing', blurb: 'Goods and services to buy.' },
  ACCESS: { label: 'Access management', blurb: 'Access to systems and data.' },
  LEAVE: { label: 'Leave', blurb: 'Time off work.' },
  CHANGE: { label: 'Change management', blurb: 'Production and infrastructure changes.' },
  GENERAL: { label: 'General requests', blurb: 'Anything else that needs a yes.' },
};

export const ROLES = {
  requesters: { label: 'Requester', blurb: 'Raises and tracks requests in the modules they hold.' },
  approvers: { label: 'Approver', blurb: 'Can manage people and sit in approver groups. Raises requests in the modules they hold.' },
  admins: { label: 'Admin', blurb: 'Manages users and configuration, stands in where nobody else can approve, and handles escalations. Every module is included.' },
};

export const CURRENCIES = [
  ['USD', 'US dollar'], ['EUR', 'Euro'], ['GBP', 'British pound'], ['INR', 'Indian rupee'], ['SGD', 'Singapore dollar'], ['AED', 'UAE dirham'],
];

export const OPEN_STATUSES = ['SUBMITTED', 'PENDING_APPROVAL', 'ESCALATED'];
export const CLOSED_STATUSES = ['REJECTED', 'EXPIRED', 'FULFILLED', 'UNROUTABLE', 'FAILED'];

export const moduleLabel = (id) => (MODULES[id] || {}).label || id;
export function moduleTile(id, size = '') {
  return h('span', { class: `module-tile ${size}`.trim(), 'aria-hidden': 'true' }, icon(MODULE_ICON[id] || 'form', { size: size === 'sm' ? 15 : 18 }));
}

export const store = {
  get(k) { try { return sessionStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { sessionStorage.setItem(k, v); } catch { /* storage unavailable */ } },
  del(k) { try { sessionStorage.removeItem(k); } catch { /* storage unavailable */ } },
};

let router = () => {};
export function setRouter(fn) { router = fn; }
export function rerender() { return router(); }

// Timers a page starts (countdowns, re-checks) are cleared when the next page is drawn.
const pageTimers = new Set();
export function pageInterval(fn, ms) { const t = setInterval(fn, ms); pageTimers.add(t); return t; }
export function clearPageTimers() { pageTimers.forEach((t) => clearInterval(t)); pageTimers.clear(); }

// ---------- formatting ----------
const toDate = (value) => (typeof value === 'number' ? new Date(value) : new Date(String(value)));

export function fmtTime(value) {
  if (value === undefined || value === null || value === '') return '';
  const d = toDate(value);
  return Number.isNaN(d.getTime()) ? String(value) : d.toLocaleString(undefined, { day: 'numeric', month: 'short', year: 'numeric', hour: 'numeric', minute: '2-digit' });
}

// "Just now", "5 min ago", "3 h ago", "Yesterday", then the date.
export function fmtRelative(value, now = Date.now()) {
  if (value === undefined || value === null || value === '') return '';
  const d = toDate(value);
  if (Number.isNaN(d.getTime())) return String(value);
  const mins = Math.round((now - d.getTime()) / 60000);
  if (mins < 1) return 'Just now';
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  if (days === 1) return 'Yesterday';
  if (days < 7) return `${days} days ago`;
  return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: d.getFullYear() === new Date(now).getFullYear() ? undefined : 'numeric' });
}

// A <time> element with the relative time, and the full time on hover and for screen readers.
export function timeAgo(value) {
  if (value === undefined || value === null || value === '') return null;
  const d = toDate(value);
  const iso = Number.isNaN(d.getTime()) ? '' : d.toISOString();
  return h('time', { class: 'when', datetime: iso, title: fmtTime(value) }, fmtRelative(value));
}

export function fmtNumber(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n.toLocaleString() : String(value ?? '');
}

export function fmtMoney(value, currency) {
  try { return new Intl.NumberFormat(undefined, { style: 'currency', currency: currency || 'USD' }).format(Number(value || 0)); }
  catch { return `${fmtNumber(value)} ${currency || ''}`.trim(); }
}

export function fmtDate(iso) {
  const d = parseIso(iso);
  return d ? d.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' }) : String(iso || '');
}

export function fmtField(f, currency) {
  if (f.type === 'checkbox') return f.value ? 'Yes' : 'No';
  if (f.type === 'amount') return fmtMoney(f.value, currency);
  if (f.type === 'number') return fmtNumber(f.value);
  if (f.type === 'date') return fmtDate(f.value);
  return String(f.value ?? '');
}

// A short label for lists: "Duration (days)" and 14 read "14 days"; a trailing "?" is dropped.
export function shortValue(label, text) {
  const clean = String(label || '').replace(/\?+\s*$/, '').trim();
  const unit = clean.match(/^(.*\S)\s*\(([a-z]+)\)$/i);
  if (unit) return `${text} ${unit[2].toLowerCase()}`;
  return clean ? `${clean}: ${text}` : String(text);
}

export function field(label, value, { wide = false } = {}) {
  if (value === undefined || value === null || value === '') return null;
  return h('div', { class: `field${wide ? ' wide' : ''}` }, h('dt', {}, label), h('dd', {}, value));
}

export function initials(user) {
  const source = (user.name || user.email || '?').trim();
  const parts = source.split(/[\s@._-]+/).filter(Boolean);
  return ((parts[0] || '?')[0] + ((parts[1] || '')[0] || '')).toUpperCase();
}

export function avatar(user, size = '') {
  return h('span', { class: `avatar ${size}`.trim(), 'aria-hidden': 'true' }, initials(user));
}

export function displayName(user) {
  return (user && (user.name || user.email)) || 'Unknown';
}

export function note(text, tone = 'info') {
  const name = { ok: 'checkCircle', warn: 'alert', danger: 'xCircle' }[tone] || 'info';
  return h('div', { class: `note ${tone === 'info' ? '' : tone}`.trim() }, icon(name, { size: 16 }), h('span', {}, text));
}

export function newKey() {
  if (crypto.randomUUID) return crypto.randomUUID().replace(/-/g, '');
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, '0')).join('');
}

// ---------- SLA: how long an approver has before the request moves on ----------
export function slaDeadline(it) {
  let plan = it.approvalPlan;
  if (typeof plan === 'string') { try { plan = JSON.parse(plan); } catch { plan = {}; } }
  plan = plan || {};
  if (it.status === 'PENDING_APPROVAL' && it.levelStartedAt) {
    const seconds = Number(it.slaSeconds || plan.slaSeconds || 0);
    const start = Date.parse(it.levelStartedAt);
    return seconds && !Number.isNaN(start) ? { at: start + seconds * 1000, verb: 'Escalates' } : null;
  }
  if (it.status === 'ESCALATED' && it.escalatedAt) {
    const seconds = Number(plan.escalationSeconds || 0);
    const start = Date.parse(it.escalatedAt);
    return seconds && !Number.isNaN(start) ? { at: start + seconds * 1000, verb: 'Expires' } : null;
  }
  return null;
}

function durationText(ms) {
  const mins = Math.ceil(ms / 60000);
  if (mins <= 1) return 'under a minute';
  if (mins < 60) return `${mins} min`;
  const hours = Math.floor(mins / 60);
  const rest = mins % 60;
  if (hours < 24) return rest ? `${hours} h ${rest} min` : `${hours} h`;
  const days = Math.round(hours / 24);
  return `${days} day${days === 1 ? '' : 's'}`;
}

// "Escalates in 23 min", refreshed every 30 seconds while the page is open.
export function countdown(it) {
  const due = slaDeadline(it);
  if (!due) return null;
  const el = h('span', { class: 'countdown' });
  const paint = () => {
    const left = due.at - Date.now();
    const total = Number(it.slaSeconds || 0) * 1000;
    el.replaceChildren(icon('clock', { size: 14 }), left > 0 ? `${due.verb} in ${durationText(left)}` : `${due.verb === 'Escalates' ? 'Overdue: escalating now' : 'Overdue: expiring now'}`);
    el.className = `countdown${left <= 0 ? ' late' : total && left > total / 2 ? ' calm' : ''}`;
    el.title = `${due.verb} at ${fmtTime(due.at)}`;
  };
  paint();
  pageInterval(paint, 30000);
  return el;
}

// ---------- API ----------
export class ApiError extends Error {
  constructor(message, status, data) {
    super(message);
    this.status = status;
    this.data = data || {};
  }
}

// Each page load has a number. A read that comes back after a newer page has started is dropped
// (StaleRoute), so a slow response can never paint over the page someone has moved on to.
export const nav = { seq: 0 };
export class StaleRoute extends Error {}

// Read-only admin data (config and the user directory) is cached briefly; any change clears it.
const cache = new Map();
export function clearCache() { cache.clear(); }

export async function api(path, options = {}) {
  const method = (options.method || 'GET').toUpperCase();
  const seq = nav.seq;
  const res = await fetch(`${state.cfg.apiBase}${path}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', Authorization: state.idToken, ...(options.headers || {}) },
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty or non-JSON body */ }
  if (method !== 'GET') clearCache();
  else if (seq !== nav.seq && res.status !== 401) throw new StaleRoute('Superseded by a newer page');
  if (res.status === 401) {
    store.del('id_token');
    state.idToken = null;
    state.me = null;
    throw new ApiError('Your session has expired. Sign in again.', 401, data);
  }
  if (res.status === 403 && data && data.message === 'Forbidden') {
    throw new ApiError('Blocked by the web application firewall. Remove anything that looks like SQL or script and try again.', 403, data);
  }
  if (!res.ok) {
    const detail = data && data.details && typeof data.details === 'string' ? ` (${data.details})` : '';
    throw new ApiError(((data && data.message) || `Request failed with status ${res.status}`) + detail, res.status, data);
  }
  return data;
}

export async function cachedApi(path, ttlMs = 45000) {
  const hit = cache.get(path);
  if (hit && Date.now() - hit.at < ttlMs) return hit.data;
  const data = await api(path);
  cache.set(path, { at: Date.now(), data });
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

// ---------- dynamic form fields (used by the request form and the form builder preview) ----------
// Returns { key, node, input, read(), setError() }. `input` has a .value and fires input events,
// whichever control it is (text box, custom select, date picker, checkbox).
export function renderField(f, { value, preview = false, currency = null } = {}) {
  const id = `f-${f.key || 'new'}-${Math.random().toString(36).slice(2, 7)}`;
  let input;
  let control;
  if (f.type === 'textarea') {
    input = h('textarea', { id, rows: '4', maxlength: f.maxLength || 2000 });
    if (value !== undefined) input.value = value;
  } else if (f.type === 'select') {
    input = select({ id, options: (f.options || []).map((o) => ({ value: o, label: o })), value: value ?? '', placeholder: 'Choose an option' });
  } else if (f.type === 'checkbox') {
    input = h('input', { id, type: 'checkbox' });
    input.checked = value === true;
  } else if (f.type === 'number' || f.type === 'amount') {
    input = h('input', { id, type: 'number', step: 'any', min: f.min ?? 0, max: f.max ?? '', inputmode: 'decimal' });
    if (value !== undefined) input.value = value;
    if (f.type === 'amount' && currency) control = adornedInput(input, { prefix: currency });
  } else if (f.type === 'date') {
    input = datePicker({ id, value: value || '' });
  } else {
    input = h('input', { id, type: 'text', maxlength: f.maxLength || 120 });
    if (value !== undefined) input.value = value;
  }
  if (f.required && f.type !== 'checkbox' && f.type !== 'select' && f.type !== 'date') input.required = true;
  if (f.required && f.type === 'date') input.input.required = true;
  if (preview) {
    [input, ...(input.querySelectorAll ? input.querySelectorAll('input, button, textarea') : [])].forEach((el) => { el.tabIndex = -1; });
  }

  const range = (f.type === 'number' || f.type === 'amount') && (f.min !== undefined || f.max !== undefined)
    ? `${fmtNumber(f.min ?? 0)} and ${fmtNumber(f.max ?? 1000000)}` : '';
  const hints = [f.help ? String(f.help).replace(/[.\s]+$/, '') : '', range ? `Between ${range}` : ''].filter(Boolean).join('. ');

  let node;
  let wrapped;
  if (f.type === 'checkbox') {
    const error = h('span', { class: 'field-error', role: 'alert' });
    node = h('div', { class: 'dyn-field check-field' },
      h('label', { class: 'check', for: id }, input, h('span', {}, f.label, f.required ? h('span', { class: 'req', 'aria-hidden': 'true' }, ' *') : null)),
      hints ? h('span', { class: 'hint-inline' }, hints) : null, error);
    wrapped = {
      setError(message) {
        error.textContent = message || '';
        node.classList.toggle('has-error', Boolean(message));
        input.setAttribute('aria-invalid', message ? 'true' : 'false');
      },
    };
  } else {
    wrapped = formField({ label: f.label, control: control || input, controlId: id, hint: hints, required: f.required, wide: f.type === 'textarea' });
    node = wrapped.node;
    if (hints) {
      const target = f.type === 'select' ? input.trigger : f.type === 'date' ? input.input : input;
      target.setAttribute('aria-describedby', [target.getAttribute('aria-describedby'), `${id}-hint`].filter(Boolean).join(' '));
    }
  }
  return {
    key: f.key,
    node,
    input,
    read() {
      if (f.type === 'checkbox') return input.checked;
      const raw = input.value;
      if (raw === '' || raw === null || raw === undefined) return undefined;
      if (f.type === 'number' || f.type === 'amount') return Number(raw);
      return raw;
    },
    setError(message) { wrapped.setError(message); },
    focus() { (input.focus ? input : input.input).focus(); },
  };
}
