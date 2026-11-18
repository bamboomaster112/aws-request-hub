// Request Hub single-page app. No framework and no build step: plain ES modules.
// Auth: Cognito hosted UI with the authorization code flow + PKCE.
// Every value from the API is rendered with textContent (never innerHTML).

const state = { cfg: null, idToken: null, claims: null, groups: new Set() };

const TYPES = {
  PURCHASE: { label: 'Purchase', unit: 'Amount', money: true, hint: 'Up to 499 is auto-approved. 5,000 and above needs an admin.' },
  ACCESS: { label: 'Access', unit: 'Duration (days)', money: false, hint: 'Always needs an approver.' },
  LEAVE: { label: 'Leave', unit: 'Days', money: false, hint: 'Up to 2 days is auto-approved. 15 days or more needs an admin.' },
  CHANGE: { label: 'Change', unit: 'Impact score', money: false, hint: 'Production changes always need an admin.' },
  GENERAL: { label: 'General', unit: 'Amount', money: true, hint: 'Up to 100 is auto-approved. 10,000 and above needs an admin.' },
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

const isReviewer = () => state.groups.has('approvers') || state.groups.has('admins');

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
  const typeSelect = h('select', { name: 'type', required: true },
    Object.entries(TYPES).map(([key, t]) => h('option', { value: key }, t.label)));
  const unitLabel = h('span', {}, TYPES.PURCHASE.unit);
  const hint = h('p', { class: 'hint' }, TYPES.PURCHASE.hint);
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
  nav.hidden = false;
  document.getElementById('inboxTab').hidden = !isReviewer();
  const current = (window.location.hash.split('/')[1] || 'mine');
  nav.querySelectorAll('a').forEach((a) => a.classList.toggle('active', a.dataset.view === current));
  who.replaceChildren(
    h('span', { class: 'small' }, state.claims.email || ''),
    ...[...state.groups].map((g) => h('span', { class: 'chip' }, g)),
    h('button', { class: 'btn ghost', onclick: signOut }, 'Sign out'));
}

async function route() {
  if (!loadSession()) {
    state.idToken = null;
    paintChrome();
    viewSignedOut();
    return;
  }
  paintChrome();
  const parts = window.location.hash.replace(/^#\/?/, '').split('/');
  try {
    if (parts[0] === 'new') viewNew();
    else if (parts[0] === 'inbox') await viewInbox();
    else if (parts[0] === 'requests' && parts[1]) await viewDetail(parts[1]);
    else await viewMine();
  } catch (e) {
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
