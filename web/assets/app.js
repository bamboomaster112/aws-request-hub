// Request Hub single-page app. No framework and no build step: plain ES modules.
// Auth: Cognito hosted UI with the authorization code flow + PKCE.
// Every value from the API is rendered with textContent (never innerHTML).

const state = { cfg: null, idToken: null, claims: null, groups: new Set(), me: null, meAt: 0 };

const TYPES = {
  PURCHASE: { label: 'Purchase', unit: 'Amount', money: true, hint: 'Up to 499 is auto-approved. 5,000 and above needs an admin.' },
  ACCESS: { label: 'Access', unit: 'Duration (days)', money: false, hint: 'Always needs an approver.' },
  LEAVE: { label: 'Leave', unit: 'Days', money: false, hint: 'Up to 2 days is auto-approved. 15 days or more needs an admin.' },
  CHANGE: { label: 'Change', unit: 'Impact score', money: false, hint: 'Production changes always need an admin.' },
  GENERAL: { label: 'General', unit: 'Amount', money: true, hint: 'Up to 100 is auto-approved. 10,000 and above needs an admin.' },
};

// Modules are the request types a user may raise and (for approvers) review.
// The server is the authority; these labels only drive the screens.
const MODULES = {
  PURCHASE: { label: 'Purchasing', blurb: 'Purchase requests for goods and services.' },
  ACCESS: { label: 'Access management', blurb: 'Requests for system and data access.' },
  LEAVE: { label: 'Leave', blurb: 'Time off and leave requests.' },
  CHANGE: { label: 'Change management', blurb: 'Production and infrastructure changes.' },
  GENERAL: { label: 'General requests', blurb: 'Anything that needs a yes but has no module of its own.' },
};

const ROLES = {
  requesters: { label: 'Requester', blurb: 'Raises and tracks requests in the modules they hold.' },
  approvers: { label: 'Approver', blurb: 'A requester who can also approve requests in the modules they hold.' },
  admins: { label: 'Admin', blurb: 'Approves anything, handles escalations and manages users. Every module is included.' },
};

const SIGN_IN = {
  FORCE_CHANGE_PASSWORD: ['Invite pending', 'warn'],
  CONFIRMED: ['Signed in before', 'ok'],
  RESET_REQUIRED: ['Password reset pending', 'warn'],
  UNCONFIRMED: ['Unconfirmed', 'warn'],
  MISSING: ['Missing in Cognito', 'danger'],
};

const ACCESS_TONE = { ACTIVE: 'ok', DISABLED: 'danger', NO_PROFILE: 'warn' };
const ACCESS_LABEL = { ACTIVE: 'Active', DISABLED: 'Disabled', NO_PROFILE: 'No access set up' };

const HISTORY_LABEL = {
  CREATED: 'Invited', ACCESS_CHANGED: 'Access changed', ACCESS_SET_UP: 'Access set up', DISABLE: 'Disabled',
  ENABLE: 'Enabled', RESET_PASSWORD: 'Password reset', RESEND_INVITE: 'Invitation re-sent',
  RESET_MFA: 'Authenticator reset', SIGN_OUT: 'Signed out everywhere', DELETED: 'Deleted',
};

const STATUS_TONE = {
  SUBMITTED: 'info', PENDING_APPROVAL: 'warn', ESCALATED: 'danger', APPROVED: 'ok', AUTO_APPROVED: 'ok',
  FULFILLED: 'ok', REJECTED: 'muted', EXPIRED: 'muted', FAILED: 'danger',
};

// ---------- tiny DOM helper ----------
function h(tag, attrs = {}, ...children) {
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

const store = {
  get(k) { try { return sessionStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { sessionStorage.setItem(k, v); } catch { /* storage unavailable */ } },
  del(k) { try { sessionStorage.removeItem(k); } catch { /* storage unavailable */ } },
};

function toast(message, tone = 'info') {
  const el = document.getElementById('toast');
  el.textContent = message;
  el.className = `toast ${tone}`;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, 4500);
}

function mount(...nodes) {
  const app = document.getElementById('app');
  app.replaceChildren(...nodes);
}

function fmtTime(value) {
  if (value === undefined || value === null || value === '') return '';
  const d = typeof value === 'number' ? new Date(value) : new Date(String(value));
  return Number.isNaN(d.getTime()) ? String(value) : d.toLocaleString();
}

function fmtAmount(item) {
  const t = TYPES[item.type] || TYPES.GENERAL;
  const n = Number(item.amount || 0);
  if (t.money) {
    try { return new Intl.NumberFormat(undefined, { style: 'currency', currency: item.currency || 'USD' }).format(n); }
    catch { return `${n} ${item.currency || ''}`.trim(); }
  }
  return `${n} (${t.unit.toLowerCase()})`;
}

function badge(status) {
  return h('span', { class: `badge ${STATUS_TONE[status] || 'info'}` }, String(status || '').replace(/_/g, ' '));
}

// ---------- auth (PKCE) ----------
function b64url(bytes) {
  let s = '';
  bytes.forEach((b) => { s += String.fromCharCode(b); });
  return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

async function pkcePair() {
  const verifier = b64url(crypto.getRandomValues(new Uint8Array(32)));
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier));
  return { verifier, challenge: b64url(new Uint8Array(digest)) };
}

async function signIn() {
  const { verifier, challenge } = await pkcePair();
  const nonce = b64url(crypto.getRandomValues(new Uint8Array(16)));
  store.set('pkce_verifier', verifier);
  store.set('oauth_state', nonce);
  const url = new URL(`${state.cfg.cognitoDomain}/oauth2/authorize`);
  url.search = new URLSearchParams({
    response_type: 'code',
    client_id: state.cfg.clientId,
    redirect_uri: state.cfg.redirectUri,
    scope: 'openid email profile',
    code_challenge_method: 'S256',
    code_challenge: challenge,
    state: nonce,
  }).toString();
  window.location.assign(url.toString());
}

function signOut() {
  store.del('id_token');
  const url = new URL(`${state.cfg.cognitoDomain}/logout`);
  url.search = new URLSearchParams({ client_id: state.cfg.clientId, logout_uri: state.cfg.redirectUri }).toString();
  window.location.assign(url.toString());
}

async function completeSignIn(code, returnedState) {
  if (!returnedState || returnedState !== store.get('oauth_state')) {
    throw new Error('Sign-in could not be verified (state mismatch). Please sign in again.');
  }
  const body = new URLSearchParams({
    grant_type: 'authorization_code',
    client_id: state.cfg.clientId,
    code,
    redirect_uri: state.cfg.redirectUri,
    code_verifier: store.get('pkce_verifier') || '',
  });
  const res = await fetch(`${state.cfg.cognitoDomain}/oauth2/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body,
  });
  if (!res.ok) throw new Error('Could not complete sign-in. Please try again.');
  const tokens = await res.json();
  store.set('id_token', tokens.id_token);
  store.del('pkce_verifier');
  store.del('oauth_state');
  window.history.replaceState({}, '', `${state.cfg.redirectUri}#/mine`);
}

function decodeJwt(token) {
  const part = token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
  const bytes = Uint8Array.from(atob(part), (c) => c.charCodeAt(0));
  return JSON.parse(new TextDecoder().decode(bytes));
}

function loadSession() {
  const token = store.get('id_token');
  if (!token) return false;
  try {
    const claims = decodeJwt(token);
    if (!claims.exp || claims.exp * 1000 < Date.now() + 30000) {
      store.del('id_token');
      return false;
    }
    state.idToken = token;
    state.claims = claims;
    state.groups = new Set(claims['cognito:groups'] || []);
    return true;
  } catch {
    store.del('id_token');
    return false;
  }
}

const role = () => (state.me && state.me.role) || null;
const isReviewer = () => role() === 'approvers' || role() === 'admins';
const isAdmin = () => Boolean(state.me && state.me.canManageUsers);
const myModules = () => (state.me && state.me.modules) || [];

// The live access profile decides what the screens offer (the token can be up to an hour old).
async function ensureMe(force = false) {
  if (!force && state.me && Date.now() - state.meAt < 60000) return state.me;
  state.me = await api('/me');
  state.meAt = Date.now();
  return state.me;
}

// ---------- API ----------
async function api(path, options = {}) {
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
    throw new Error('Your session has expired. Please sign in again.');
  }
  if (res.status === 403 && data && data.message === 'Forbidden') {
    throw new Error('Blocked by the web application firewall (WAF). Remove anything that looks like SQL or script.');
  }
  if (!res.ok) {
    const detail = data && data.details ? ` (${data.details})` : '';
    throw new Error(((data && data.message) || `Request failed with status ${res.status}`) + detail);
  }
  return data;
}

// ---------- views ----------
function viewSignedOut() {
  mount(
    h('section', { class: 'hero card' },
      h('h1', {}, 'One place for every request that needs a yes'),
      h('p', { class: 'lead' },
        'Raise purchase, access, leave, change or general requests. Policy rules auto-approve the low-risk ones, ',
        'route the rest to the right approvers, escalate when an SLA is missed, and fulfil approved work automatically.'),
      h('button', { class: 'btn primary', onclick: () => signIn().catch((e) => toast(e.message, 'danger')) }, 'Sign in'),
      h('p', { class: 'muted small' }, 'Sign-in uses Amazon Cognito with mandatory authenticator-app MFA.'),
    ),
  );
}

async function viewMine() {
  mount(h('p', { class: 'muted' }, 'Loading your requests...'));
  const data = await api('/requests');
  const items = data.items || [];
  const header = h('div', { class: 'section-head' },
    h('h2', {}, 'My requests'),
    h('div', { class: 'row' },
      h('button', { class: 'btn', onclick: () => route() }, 'Refresh'),
      h('a', { class: 'btn primary', href: '#/new' }, 'New request')));
  if (!items.length) {
    mount(header, h('div', { class: 'card empty' }, 'No requests yet. Create one to see the workflow in action.'));
    return;
  }
  const rows = items.map((it) => h('tr', { class: 'clickable', onclick: () => { window.location.hash = `#/requests/${it.requestId}`; } },
    h('td', {}, h('div', { class: 'title' }, it.title), h('div', { class: 'muted small mono' }, it.requestId)),
    h('td', {}, (TYPES[it.type] || {}).label || it.type),
    h('td', { class: 'num' }, fmtAmount(it)),
    h('td', {}, badge(it.status)),
    h('td', { class: 'muted small' }, fmtTime(it.createdAt))));
  mount(header, h('div', { class: 'card flush' },
    h('table', { class: 'table' },
      h('thead', {}, h('tr', {}, h('th', {}, 'Request'), h('th', {}, 'Type'), h('th', { class: 'num' }, 'Value'), h('th', {}, 'Status'), h('th', {}, 'Created'))),
      h('tbody', {}, rows))));
}

function viewNew() {
  const allowed = Object.keys(TYPES).filter((key) => myModules().includes(key));
  if (!allowed.length) {
    mount(h('div', { class: 'section-head' }, h('h2', {}, 'New request')),
      h('div', { class: 'card empty' },
        h('h3', {}, 'No request modules yet'),
        h('p', {}, 'Your account does not have access to any request module. Ask an admin to grant the modules you need.')));
    return;
  }
  const typeSelect = h('select', { name: 'type', required: true },
    allowed.map((key) => h('option', { value: key }, TYPES[key].label)));
  const unitLabel = h('span', {}, TYPES[allowed[0]].unit);
  const hint = h('p', { class: 'hint' }, TYPES[allowed[0]].hint);
  const currency = h('select', { name: 'currency' }, ['USD', 'EUR', 'GBP', 'INR', 'SGD', 'AED'].map((c) => h('option', { value: c }, c)));
  const currencyField = h('label', {}, 'Currency', currency);
  const update = () => {
    const t = TYPES[typeSelect.value];
    unitLabel.textContent = t.unit;
    hint.textContent = t.hint;
    currencyField.hidden = !t.money;
  };
  typeSelect.addEventListener('change', update);

  const form = h('form', { class: 'card form' },
    h('div', { class: 'grid2' },
      h('label', {}, 'Type', typeSelect),
      h('label', {}, unitLabel, h('input', { name: 'amount', type: 'number', min: '0', step: 'any', value: '0', required: true }))),
    hint,
    allowed.length < Object.keys(TYPES).length
      ? h('p', { class: 'muted small' }, `You can raise ${allowed.map((k) => TYPES[k].label).join(', ')} requests. Ask an admin if you need another module.`)
      : null,
    h('label', {}, 'Title', h('input', { name: 'title', minlength: '3', maxlength: '120', required: true, placeholder: 'For example: Laptop for new joiner' })),
    h('label', {}, 'Details', h('textarea', { name: 'description', maxlength: '2000', rows: '4', placeholder: 'Why is this needed? Any links or context for the approver.' })),
    currencyField,
    h('div', { class: 'row end' },
      h('a', { class: 'btn', href: '#/mine' }, 'Cancel'),
      h('button', { class: 'btn primary', type: 'submit' }, 'Submit request')));

  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const fd = new FormData(form);
    const t = TYPES[fd.get('type')];
    const payload = {
      type: fd.get('type'),
      title: String(fd.get('title')).trim(),
      description: String(fd.get('description') || '').trim(),
      amount: Number(fd.get('amount') || 0),
    };
    if (t.money) payload.currency = fd.get('currency');
    const button = form.querySelector('button[type=submit]');
    button.disabled = true;
    try {
      const created = await api('/requests', { method: 'POST', body: JSON.stringify(payload) });
      toast('Submitted. The workflow is evaluating it now.', 'ok');
      window.location.hash = `#/requests/${created.requestId}`;
    } catch (e) {
      toast(e.message, 'danger');
      button.disabled = false;
    }
  });
  mount(h('div', { class: 'section-head' }, h('h2', {}, 'New request')), form);
  update();
}

function field(label, value) {
  if (value === undefined || value === null || value === '') return null;
  return h('div', { class: 'field' }, h('dt', {}, label), h('dd', {}, value));
}

async function viewDetail(requestId, attempt = 0) {
  if (attempt === 0) mount(h('p', { class: 'muted' }, 'Loading request...'));
  const it = await api(`/requests/${encodeURIComponent(requestId)}`);

  // The workflow usually moves a new request on within a second or two.
  // Re-check a few times, then stop (each check is a Lambda call, and the playground caps them).
  if (it.status === 'SUBMITTED' && attempt < 3) {
    setTimeout(() => { if (window.location.hash === `#/requests/${requestId}`) viewDetail(requestId, attempt + 1).catch(showError); }, 2500);
  }

  const reasons = Array.isArray(it.policyReasons) ? it.policyReasons : [];
  const facts = h('dl', { class: 'facts' },
    field('Type', (TYPES[it.type] || {}).label || it.type),
    field('Value', fmtAmount(it)),
    field('Requested by', it.requesterEmail),
    field('Created', fmtTime(it.createdAt)),
    field('Last update', fmtTime(it.lastUpdated)),
    field('Risk tier', it.riskTier && it.riskTier !== 'UNASSESSED' ? it.riskTier : null),
    field('Approver group', it.approverGroup && it.approverGroup !== 'none' ? it.approverGroup : null),
    field('Approval SLA', it.slaSeconds ? `${it.slaSeconds} seconds, then escalates to admins` : null),
    field('Escalated', fmtTime(it.escalatedAt)),
    field('Decided by', it.decidedByEmail || (it.decidedBy === 'policy-engine' ? 'Policy engine (auto-approved)' : null)),
    field('Decision note', it.decisionComment),
    field('Fulfilment reference', it.fulfilmentRef),
    field('Fulfilled', fmtTime(it.fulfilledAt)));

  const blocks = [
    h('div', { class: 'section-head' },
      h('div', {}, h('a', { href: '#/mine', class: 'muted small' }, 'Back to my requests'), h('h2', {}, it.title)),
      h('div', { class: 'row' }, badge(it.status), h('button', { class: 'btn', onclick: () => viewDetail(requestId).catch(showError) }, 'Refresh'))),
    h('div', { class: 'card' },
      it.description ? h('p', { class: 'desc' }, it.description) : null,
      facts,
      reasons.length ? h('div', { class: 'reasons' }, h('h3', {}, 'Why the policy engine decided this'), h('ul', {}, reasons.map((r) => h('li', {}, r)))) : null,
      h('p', { class: 'muted small mono' }, `Request ID ${it.requestId}`)),
  ];

  if (it.canDecide) blocks.push(decisionPanel(it));
  blocks.push(attachmentsPanel(it));
  mount(...blocks);
}

function decisionPanel(it) {
  const comment = h('textarea', { rows: '3', maxlength: '500', placeholder: 'Add a note (required when rejecting)' });
  const decide = async (decision, buttons) => {
    buttons.forEach((b) => { b.disabled = true; });
    try {
      await api(`/requests/${encodeURIComponent(it.requestId)}/decision`, {
        method: 'POST', body: JSON.stringify({ decision, comment: comment.value.trim() }),
      });
      toast(`Recorded: ${decision.toLowerCase()}. The workflow is updating the request.`, 'ok');
      setTimeout(() => viewDetail(it.requestId).catch(showError), 1500);
    } catch (e) {
      toast(e.message, 'danger');
      buttons.forEach((b) => { b.disabled = false; });
    }
  };
  const approve = h('button', { class: 'btn ok' }, 'Approve');
  const reject = h('button', { class: 'btn danger' }, 'Reject');
  approve.addEventListener('click', () => decide('APPROVED', [approve, reject]));
  reject.addEventListener('click', () => decide('REJECTED', [approve, reject]));
  return h('div', { class: 'card accent' },
    h('h3', {}, it.status === 'ESCALATED' ? 'Escalated: admin decision needed' : 'Your decision'),
    h('p', { class: 'muted small' }, 'Your decision resumes the paused Step Functions workflow through its task token.'),
    comment,
    h('div', { class: 'row end' }, reject, approve));
}

function attachmentsPanel(it) {
  const list = (it.attachments || []).length
    ? h('ul', { class: 'files' }, it.attachments.map((a) => h('li', {},
      a.uploaded && a.downloadUrl
        ? h('a', { href: a.downloadUrl, rel: 'noopener' }, a.fileName)
        : h('span', { class: 'muted' }, `${a.fileName} (upload not completed)`),
      h('span', { class: 'muted small' }, ` ${a.contentType || ''}`))))
    : h('p', { class: 'muted small' }, 'No attachments.');

  const panel = h('div', { class: 'card' }, h('h3', {}, 'Attachments'), list);
  if (it.isOwner) {
    const input = h('input', { type: 'file', accept: '.pdf,.png,.jpg,.jpeg,.txt,.csv,.docx,.xlsx' });
    const button = h('button', { class: 'btn' }, 'Upload');
    button.addEventListener('click', async () => {
      const file = input.files && input.files[0];
      if (!file) { toast('Choose a file first', 'warn'); return; }
      button.disabled = true;
      try {
        await uploadAttachment(it.requestId, file);
        toast('Uploaded straight to S3 with a presigned POST policy', 'ok');
        await viewDetail(it.requestId);
      } catch (e) {
        toast(e.message, 'danger');
        button.disabled = false;
      }
    });
    panel.append(h('div', { class: 'row' }, input, button), h('p', { class: 'muted small' }, 'PDF, images, TXT, CSV, DOCX or XLSX, up to 10 MB.'));
  }
  return panel;
}

async function uploadAttachment(requestId, file) {
  const meta = await api(`/requests/${encodeURIComponent(requestId)}/attachments`, {
    method: 'POST', body: JSON.stringify({ fileName: file.name, contentType: file.type || 'application/octet-stream' }),
  });
  const form = new FormData();
  Object.entries(meta.upload.fields).forEach(([k, v]) => form.append(k, v));
  form.append('file', file);
  const res = await fetch(meta.upload.url, { method: 'POST', body: form });
  if (!res.ok) throw new Error('S3 rejected the upload (file type or size is not allowed)');
}

async function viewInbox() {
  if (!isReviewer()) { window.location.hash = '#/mine'; return; }
  mount(h('p', { class: 'muted' }, 'Loading approvals...'));
  const data = await api('/approvals');
  const items = data.items || [];
  const header = h('div', { class: 'section-head' }, h('h2', {}, 'Approvals waiting for you'),
    h('button', { class: 'btn', onclick: () => route() }, 'Refresh'));
  if (!items.length) {
    mount(header, h('div', { class: 'card empty' }, 'Nothing is waiting for you right now.'));
    return;
  }
  mount(header, h('div', { class: 'cards' }, items.map((it) => h('a', { class: 'card tile', href: `#/requests/${it.requestId}` },
    h('div', { class: 'row spread' }, h('strong', {}, it.title), badge(it.status)),
    h('div', { class: 'muted small' }, `${(TYPES[it.type] || {}).label || it.type} for ${fmtAmount(it)}, from ${it.requesterEmail}`),
    h('div', { class: 'muted small' }, `Risk ${it.riskTier}, group ${it.approverGroup}, raised ${fmtTime(it.createdAt)}`)))));
}

// ---------- access and user management ----------
function viewNoAccess() {
  const disabled = state.me && state.me.status === 'DISABLED';
  mount(h('section', { class: 'card hero' },
    h('h1', {}, disabled ? 'Your account is disabled' : 'Your access is not set up yet'),
    h('p', { class: 'lead' }, disabled
      ? 'An admin has disabled this account, so requests and approvals are switched off. Contact an admin if you think this is a mistake.'
      : 'You can sign in, but an admin has not given you a role and modules yet. Ask an admin to set up your access.'),
    h('button', { class: 'btn', onclick: () => { state.meAt = 0; route(); } }, 'Check again')));
}

function confirmDialog({ title, body, confirmLabel = 'Confirm', danger = false, typeToConfirm = null }) {
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

function initials(user) {
  const source = (user.name || user.email || '?').trim();
  const parts = source.split(/[\s@._-]+/).filter(Boolean);
  return ((parts[0] || '?')[0] + ((parts[1] || '')[0] || '')).toUpperCase();
}

function avatar(user) {
  return h('span', { class: 'avatar', 'aria-hidden': 'true' }, initials(user));
}

function roleBadge(roleId) {
  if (!roleId) return h('span', { class: 'badge muted' }, 'No role');
  return h('span', { class: `badge role-${roleId}` }, (ROLES[roleId] || {}).label || roleId);
}

function accessBadge(status) {
  return h('span', { class: `badge ${ACCESS_TONE[status] || 'muted'}` }, ACCESS_LABEL[status] || status);
}

function signInBadge(status) {
  const [label, tone] = SIGN_IN[status] || [String(status || 'Unknown').replace(/_/g, ' ').toLowerCase(), 'muted'];
  return h('span', { class: `badge ${tone}` }, label);
}

function moduleChips(user) {
  if (user.role === 'admins') return h('span', { class: 'chip strong' }, 'All modules');
  if (!user.modules || !user.modules.length) return h('span', { class: 'muted small' }, 'None');
  return h('div', { class: 'chips' }, user.modules.map((m) => h('span', { class: 'chip' }, (MODULES[m] || {}).label || m)));
}

function stat(label, value, tone = '') {
  return h('div', { class: `stat ${tone}` }, h('div', { class: 'stat-value' }, String(value)), h('div', { class: 'stat-label' }, label));
}

async function viewUsers() {
  if (!isAdmin()) { window.location.hash = '#/mine'; return; }
  mount(h('p', { class: 'muted' }, 'Loading users...'));
  const data = await api('/admin/users');
  const users = data.items || [];
  const summary = data.summary || {};

  const search = h('input', { type: 'search', placeholder: 'Search by name or email', 'aria-label': 'Search users' });
  const roleFilter = h('select', { 'aria-label': 'Filter by role' },
    h('option', { value: '' }, 'All roles'),
    Object.entries(ROLES).map(([id, r]) => h('option', { value: id }, r.label)));
  const statusFilter = h('select', { 'aria-label': 'Filter by status' },
    h('option', { value: '' }, 'Any status'),
    h('option', { value: 'ACTIVE' }, 'Active'),
    h('option', { value: 'DISABLED' }, 'Disabled'),
    h('option', { value: 'NO_PROFILE' }, 'No access set up'),
    h('option', { value: 'INVITED' }, 'Invite pending'));
  const tbody = h('tbody');
  const count = h('span', { class: 'muted small' });

  const paint = () => {
    const q = search.value.trim().toLowerCase();
    const shown = users.filter((u) => {
      if (q && !`${u.name || ''} ${u.email || ''}`.toLowerCase().includes(q)) return false;
      if (roleFilter.value && u.role !== roleFilter.value) return false;
      if (statusFilter.value === 'INVITED') return u.cognitoStatus === 'FORCE_CHANGE_PASSWORD';
      if (statusFilter.value && u.status !== statusFilter.value) return false;
      return true;
    });
    count.textContent = `${shown.length} of ${users.length} users`;
    tbody.replaceChildren(...(shown.length ? shown.map((u) => h('tr', {
      class: `clickable${u.status === 'DISABLED' ? ' dim' : ''}`,
      tabindex: '0',
      onclick: () => { window.location.hash = `#/users/${u.userId}`; },
      onkeydown: (ev) => { if (ev.key === 'Enter') window.location.hash = `#/users/${u.userId}`; },
    },
    h('td', {}, h('div', { class: 'person' }, avatar(u),
      h('div', { class: 'person-text' }, h('div', { class: 'title' }, u.name || u.email), u.name ? h('div', { class: 'muted small' }, u.email) : null,
        h('div', { class: 'row tight' },
          u.userId === state.me.userId ? h('span', { class: 'chip' }, 'You') : null,
          h('span', { class: 'mobile-only' }, roleBadge(u.role)))))),
    h('td', {}, roleBadge(u.role)),
    h('td', {}, moduleChips(u)),
    h('td', {}, signInBadge(u.cognitoStatus)),
    h('td', {}, accessBadge(u.status)),
    h('td', { class: 'muted small' }, fmtTime(u.updatedAt || u.createdAt))))
      : [h('tr', {}, h('td', { colspan: '6', class: 'empty' }, 'No users match these filters.'))]));
  };
  [search, roleFilter, statusFilter].forEach((el) => el.addEventListener('input', paint));

  mount(
    h('div', { class: 'section-head' },
      h('div', {}, h('h2', {}, 'Users'), h('p', { class: 'muted small' }, 'Invite people, choose their role and decide which request modules they can use. Changes apply on their next click, not their next sign-in.')),
      h('div', { class: 'row' },
        h('button', { class: 'btn', onclick: () => route() }, 'Refresh'),
        h('a', { class: 'btn primary', href: '#/users/new' }, 'Invite user'))),
    h('div', { class: 'stats' },
      stat('Users', summary.total ?? users.length),
      stat('Active', summary.active ?? 0, 'ok'),
      stat('Invites pending', summary.invitesPending ?? 0, 'warn'),
      stat('Disabled', summary.disabled ?? 0, 'danger'),
      stat('Admins', summary.admins ?? 0, 'tone-brand')),
    h('div', { class: 'toolbar' }, search, roleFilter, statusFilter, count),
    h('div', { class: 'card flush' },
      h('table', { class: 'table users' },
        h('thead', {}, h('tr', {}, h('th', {}, 'User'), h('th', {}, 'Role'), h('th', {}, 'Modules'), h('th', {}, 'Sign-in'), h('th', {}, 'Access'), h('th', {}, 'Updated'))),
        tbody)));
  paint();
}

// Role picker and module checkboxes, shared by the invite form and the user page.
function accessEditor(initial = { role: 'requesters', modules: [] }) {
  const roleName = `role-${Math.random().toString(36).slice(2, 8)}`;
  const roleInputs = {};
  const roleCards = h('div', { class: 'choice-grid roles' }, Object.entries(ROLES).map(([id, r]) => {
    const input = h('input', { type: 'radio', name: roleName, value: id, checked: initial.role === id });
    roleInputs[id] = input;
    return h('label', { class: 'choice' }, input, h('span', { class: 'choice-body' }, h('strong', {}, r.label), h('span', { class: 'muted small' }, r.blurb)));
  }));
  const moduleInputs = {};
  const moduleCards = h('div', { class: 'choice-grid' }, Object.entries(MODULES).map(([id, m]) => {
    const input = h('input', { type: 'checkbox', value: id, checked: (initial.modules || []).includes(id) });
    moduleInputs[id] = input;
    return h('label', { class: 'choice' }, input, h('span', { class: 'choice-body' }, h('strong', {}, m.label), h('span', { class: 'muted small' }, m.blurb)));
  }));
  const adminNote = h('p', { class: 'note' }, 'Admins can use every module, so module choices do not apply.');
  const quick = h('div', { class: 'row small' },
    h('button', { type: 'button', class: 'linkish', onclick: () => { Object.values(moduleInputs).forEach((i) => { i.checked = true; }); } }, 'Select all'),
    h('button', { type: 'button', class: 'linkish', onclick: () => { Object.values(moduleInputs).forEach((i) => { i.checked = false; }); } }, 'Clear'));

  const sync = () => {
    const isAdminRole = roleInputs.admins.checked;
    moduleCards.classList.toggle('disabled', isAdminRole);
    Object.values(moduleInputs).forEach((i) => { i.disabled = isAdminRole; });
    adminNote.hidden = !isAdminRole;
    quick.hidden = isAdminRole;
  };
  Object.values(roleInputs).forEach((i) => i.addEventListener('change', sync));
  sync();

  return {
    node: h('div', { class: 'access-editor' },
      h('h4', {}, 'Role'), roleCards,
      h('div', { class: 'row spread' }, h('h4', {}, 'Request modules'), quick),
      moduleCards, adminNote),
    value() {
      const roleId = Object.keys(roleInputs).find((id) => roleInputs[id].checked);
      const modules = Object.keys(moduleInputs).filter((id) => moduleInputs[id].checked);
      return { role: roleId, modules };
    },
    setDisabled(disabled) {
      Object.values(roleInputs).forEach((i) => { i.disabled = disabled; });
      if (disabled) Object.values(moduleInputs).forEach((i) => { i.disabled = true; }); else sync();
    },
  };
}

function viewUserNew() {
  if (!isAdmin()) { window.location.hash = '#/mine'; return; }
  const editor = accessEditor({ role: 'requesters', modules: ['GENERAL'] });
  const invite = h('input', { type: 'checkbox', name: 'sendInvite', checked: true });
  const form = h('form', { class: 'card form' },
    h('div', { class: 'grid2' },
      h('label', {}, 'Email', h('input', { name: 'email', type: 'email', required: true, maxlength: '254', placeholder: 'name@company.com', autocomplete: 'off' })),
      h('label', {}, 'Full name', h('input', { name: 'name', maxlength: '80', placeholder: 'Optional', autocomplete: 'off' }))),
    editor.node,
    h('label', { class: 'check' }, invite, h('span', {}, 'Email the invitation now. Cognito sends a temporary password; the person sets a new one and enrols an authenticator app at first sign-in.')),
    h('div', { class: 'row end' },
      h('a', { class: 'btn', href: '#/users' }, 'Cancel'),
      h('button', { class: 'btn primary', type: 'submit' }, 'Invite user')));

  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const fd = new FormData(form);
    const access = editor.value();
    const payload = {
      email: String(fd.get('email')).trim(),
      name: String(fd.get('name') || '').trim(),
      role: access.role,
      modules: access.role === 'admins' ? [] : access.modules,
      sendInvite: invite.checked,
    };
    if (payload.role !== 'admins' && !payload.modules.length) {
      const go = await confirmDialog({ title: 'Invite without any module?', body: 'This person will be able to sign in but not raise or approve any request until you grant a module.', confirmLabel: 'Invite anyway' });
      if (!go) return;
    }
    if (payload.role === 'admins') {
      const go = await confirmDialog({ title: 'Invite a new admin?', body: `${payload.email} will be able to approve anything and manage every user, including you. The ops team is alerted about new admins.`, confirmLabel: 'Invite admin', danger: true });
      if (!go) return;
    }
    const button = form.querySelector('button[type=submit]');
    button.disabled = true;
    try {
      const res = await api('/admin/users', { method: 'POST', body: JSON.stringify(payload) });
      toast(res.invited ? `Invitation emailed to ${payload.email}` : `${payload.email} created. Send the invitation when they are ready.`, 'ok');
      window.location.hash = `#/users/${res.user.userId}`;
    } catch (e) {
      toast(e.message, 'danger');
      button.disabled = false;
    }
  });

  mount(h('div', { class: 'section-head' },
    h('div', {}, h('a', { href: '#/users', class: 'muted small' }, 'Back to users'), h('h2', {}, 'Invite a user'))), form);
}

async function viewUserDetail(userId) {
  if (!isAdmin()) { window.location.hash = '#/mine'; return; }
  mount(h('p', { class: 'muted' }, 'Loading user...'));
  const { user } = await api(`/admin/users/${encodeURIComponent(userId)}`);
  const isSelf = user.userId === state.me.userId;
  const invited = user.cognitoStatus === 'FORCE_CHANGE_PASSWORD';

  // Access card
  const editor = accessEditor({ role: user.role || 'requesters', modules: user.modules || [] });
  const nameInput = h('input', { value: user.name || '', maxlength: '80', placeholder: 'Full name' });
  const save = h('button', { class: 'btn primary', type: 'submit' }, user.managed ? 'Save access' : 'Set up access');
  const accessForm = h('form', { class: 'card form' },
    h('div', { class: 'row spread' }, h('h3', {}, 'Access'), h('span', { class: 'muted small' }, 'Takes effect on the next click. No sign-out needed.')),
    !user.managed ? h('p', { class: 'note warn' }, 'This account was created outside Request Hub and has no access profile, so it cannot use the app yet. Choose a role and modules to set it up.') : null,
    h('label', {}, 'Full name', nameInput),
    editor.node,
    isSelf ? h('p', { class: 'note' }, 'You cannot remove your own admin role. Another admin has to do that.') : null,
    h('div', { class: 'row end' }, save));
  if (isSelf) editor.setDisabled(true);

  accessForm.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const access = editor.value();
    const payload = { name: nameInput.value.trim() };
    if (!isSelf) { payload.role = access.role; payload.modules = access.role === 'admins' ? (user.modules || []) : access.modules; }
    if (user.managed && user.version !== undefined && user.version !== null) payload.version = user.version;
    if (!isSelf && access.role === 'admins' && user.role !== 'admins') {
      const go = await confirmDialog({ title: `Make ${user.email} an admin?`, body: 'Admins can approve anything and manage every user. The ops team is alerted about new admins.', confirmLabel: 'Make admin', danger: true });
      if (!go) return;
    }
    save.disabled = true;
    try {
      const res = await api(`/admin/users/${encodeURIComponent(user.userId)}`, { method: 'PATCH', body: JSON.stringify(payload) });
      toast(res.changed ? 'Access saved. It applies immediately.' : 'Nothing changed.', res.changed ? 'ok' : 'info');
      await viewUserDetail(user.userId);
    } catch (e) {
      toast(e.message, 'danger');
      save.disabled = false;
    }
  });

  // Account card with lifecycle actions
  const act = async (action, opts) => {
    if (opts.confirm) {
      const go = await confirmDialog(opts.confirm);
      if (!go) return;
    }
    try {
      const res = await api(`/admin/users/${encodeURIComponent(user.userId)}/actions`, { method: 'POST', body: JSON.stringify({ action }) });
      toast(res.message, 'ok');
      await viewUserDetail(user.userId);
    } catch (e) {
      toast(e.message, 'danger');
    }
  };
  const actions = [];
  if (user.status === 'DISABLED') {
    actions.push(h('button', { class: 'btn ok', onclick: () => act('ENABLE', {}) }, 'Enable account'));
  } else if (!isSelf) {
    actions.push(h('button', { class: 'btn danger', onclick: () => act('DISABLE', { confirm: { title: `Disable ${user.email}?`, body: 'Sign-in is blocked, every session is signed out, and API access stops on their very next click. You can enable the account again later.', confirmLabel: 'Disable', danger: true } }) }, 'Disable account'));
  }
  if (invited) {
    actions.push(h('button', { class: 'btn', onclick: () => act('RESEND_INVITE', { confirm: { title: 'Resend the invitation?', body: `Cognito emails ${user.email} a new temporary password. The old one stops working.`, confirmLabel: 'Resend' } }) }, 'Resend invitation'));
  } else if (user.cognitoStatus !== 'MISSING') {
    actions.push(h('button', { class: 'btn', onclick: () => act('RESET_PASSWORD', { confirm: { title: 'Reset the password?', body: `Cognito emails ${user.email} a verification code. They must choose a new password at their next sign-in.`, confirmLabel: 'Reset password' } }) }, 'Reset password'));
    actions.push(h('button', { class: 'btn', onclick: () => act('RESET_MFA', { confirm: { title: 'Reset the authenticator?', body: 'Use this when someone loses their phone. They are signed out and enrol a new authenticator app at their next sign-in.', confirmLabel: 'Reset authenticator' } }) }, 'Reset authenticator'));
  }
  if (user.cognitoStatus !== 'MISSING') {
    actions.push(h('button', { class: 'btn', onclick: () => act('SIGN_OUT', { confirm: { title: 'Sign out everywhere?', body: 'Revokes every refresh token for this user. They sign in again with their password and authenticator.', confirmLabel: 'Sign out everywhere' } }) }, 'Sign out everywhere'));
  }

  const account = h('div', { class: 'card' },
    h('h3', {}, 'Account'),
    h('dl', { class: 'facts' },
      field('Sign-in', SIGN_IN[user.cognitoStatus] ? SIGN_IN[user.cognitoStatus][0] : user.cognitoStatus),
      field('MFA', user.mfa),
      field('Cognito groups', (user.groups || []).join(', ') || 'None'),
      field('Created', fmtTime(user.createdAt)),
      field('Last access change', user.updatedAt ? `${fmtTime(user.updatedAt)}${user.updatedByEmail ? ` by ${user.updatedByEmail}` : ''}` : null),
      field('User ID', h('span', { class: 'mono small' }, user.userId))),
    h('div', { class: 'row' }, actions));

  // Danger zone
  const danger = isSelf ? null : h('div', { class: 'card danger-zone' },
    h('h3', {}, 'Delete user'),
    h('p', { class: 'muted small' }, 'Removes the sign-in and the access profile. Their requests and this access history are kept for audit.'),
    h('button', {
      class: 'btn danger',
      onclick: async () => {
        const go = await confirmDialog({ title: `Delete ${user.email}?`, body: 'This cannot be undone. They lose access immediately, and you would need to invite them again.', confirmLabel: 'Delete user', danger: true, typeToConfirm: user.email });
        if (!go) return;
        try {
          await api(`/admin/users/${encodeURIComponent(user.userId)}`, { method: 'DELETE' });
          toast(`${user.email} deleted`, 'ok');
          window.location.hash = '#/users';
        } catch (e) {
          toast(e.message, 'danger');
        }
      },
    }, 'Delete user'));

  // History
  const historyCard = h('div', { class: 'card' }, h('h3', {}, 'Access history'),
    (user.history || []).length
      ? h('ol', { class: 'timeline' }, user.history.map((entry) => h('li', {},
        h('div', { class: 'row spread' }, h('strong', {}, HISTORY_LABEL[entry.action] || entry.action), h('span', { class: 'muted small' }, fmtTime(entry.at))),
        h('div', {}, entry.summary),
        h('div', { class: 'muted small' }, `by ${entry.actorEmail || entry.actorId}`))))
      : h('p', { class: 'muted small' }, 'No changes recorded yet.'));

  mount(
    h('div', { class: 'section-head' },
      h('div', {}, h('a', { href: '#/users', class: 'muted small' }, 'Back to users'),
        h('div', { class: 'person big' }, avatar(user), h('div', {}, h('h2', {}, user.name || user.email), user.name ? h('div', { class: 'muted' }, user.email) : null))),
      h('div', { class: 'row' }, roleBadge(user.role), accessBadge(user.status), signInBadge(user.cognitoStatus))),
    h('div', { class: 'split' }, h('div', {}, accessForm), h('div', {}, account, historyCard, danger)));
}

// ---------- router ----------
function showError(e) {
  toast(e.message, 'danger');
  if (!state.idToken) viewSignedOut();
}

function paintChrome() {
  const nav = document.getElementById('nav');
  const who = document.getElementById('who');
  if (!state.idToken) {
    nav.hidden = true;
    who.replaceChildren();
    return;
  }
  const active = state.me && state.me.status === 'ACTIVE';
  nav.hidden = !active;
  document.getElementById('inboxTab').hidden = !isReviewer();
  document.getElementById('usersTab').hidden = !isAdmin();
  const current = (window.location.hash.split('/')[1] || 'mine');
  nav.querySelectorAll('a').forEach((a) => a.classList.toggle('active', a.dataset.view === current));
  who.replaceChildren(
    h('span', { class: 'small' }, (state.me && state.me.email) || state.claims.email || ''),
    role() ? h('span', { class: 'chip' }, (ROLES[role()] || {}).label || role()) : null,
    h('button', { class: 'btn ghost', onclick: signOut }, 'Sign out'));
}

async function route() {
  if (!loadSession()) {
    state.idToken = null;
    paintChrome();
    viewSignedOut();
    return;
  }
  const parts = window.location.hash.replace(/^#\/?/, '').split('/');
  try {
    await ensureMe(parts[0] === 'users');
    paintChrome();
    if (state.me.status !== 'ACTIVE') { viewNoAccess(); return; }
    if (parts[0] === 'new') viewNew();
    else if (parts[0] === 'inbox') await viewInbox();
    else if (parts[0] === 'requests' && parts[1]) await viewDetail(parts[1]);
    else if (parts[0] === 'users' && parts[1] === 'new') viewUserNew();
    else if (parts[0] === 'users' && parts[1]) await viewUserDetail(parts[1]);
    else if (parts[0] === 'users') await viewUsers();
    else await viewMine();
  } catch (e) {
    paintChrome();
    showError(e);
  }
}

async function start() {
  try {
    const res = await fetch('config.json', { cache: 'no-store' });
    if (!res.ok) throw new Error('config.json is missing. Run scripts/publish-web.sh.');
    state.cfg = await res.json();
  } catch (e) {
    mount(h('div', { class: 'card' }, h('h2', {}, 'Not configured yet'), h('p', {}, e.message)));
    return;
  }
  const params = new URLSearchParams(window.location.search);
  if (params.get('code')) {
    try {
      await completeSignIn(params.get('code'), params.get('state'));
    } catch (e) {
      toast(e.message, 'danger');
      window.history.replaceState({}, '', state.cfg.redirectUri);
    }
  }
  window.addEventListener('hashchange', route);
  await route();
}

start();
