// Request Hub single-page app: sign-in, routing and the requester and approver screens.
// Auth: Cognito managed login with the authorization code flow + PKCE.
import {
  state, MODULES, ROLES, CURRENCIES, OPEN_STATUSES, h, icon, store, toast, clearToastOnNavigate, mount, setRouter,
  clearPageTimers, fmtTime, fmtNumber, fmtMoney, fmtField, shortValue, timeAgo, badge, field, note, api, ApiError,
  ensureMe, role, isAdmin, isReviewer, renderField, newKey, displayName, avatar, moduleLabel, moduleTile, countdown,
  button, linkButton, iconButton, busy, formField, textInput, select, choiceCard, searchInput, parseIso,
  emptyState, skeleton, pageHead, card, menu, fileDrop, segmented, MODULE_ICON, nav, StaleRoute,
} from './core.js';
import { adminRoute } from './admin.js';

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
    throw new Error('Sign-in could not be verified. Sign in again.');
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
  if (!res.ok) throw new Error('Could not complete sign-in. Try again.');
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
    return true;
  } catch {
    store.del('id_token');
    return false;
  }
}

function setTitle(text) {
  document.title = text ? `${text} | Request Hub` : 'Request Hub';
}

function valueText(it) {
  if (it.approvalField && it.amount !== undefined) {
    const f = (it.fields || []).find((x) => x.label === it.approvalField);
    if (f) return f.type === 'amount' ? fmtField(f, it.currency) : shortValue(it.approvalField, fmtField(f, it.currency));
    return shortValue(it.approvalField, fmtNumber(it.amount));
  }
  return it.amount ? fmtMoney(it.amount, it.currency) : '';
}

const params = () => new URLSearchParams(window.location.hash.split('?')[1] || '');
const RAISE_AGAIN = ['REJECTED', 'EXPIRED', 'UNROUTABLE', 'FAILED'];

// ---------- the routing slip ----------
function stampFor(tone, n, size = '') {
  const glyph = { done: icon('check', { size: size ? 14 : 18 }), rejected: icon('close', { size: size ? 14 : 18 }), active: icon('clock', { size: size ? 14 : 18 }), escalated: icon('alert', { size: size ? 14 : 18 }) }[tone];
  return h('span', { class: `stamp ${tone} ${size}`.trim(), 'aria-hidden': 'true' }, glyph || String(n));
}

const TONE_WORD = { done: 'Approved', rejected: 'Rejected', active: 'Waiting', escalated: 'Escalated', upcoming: 'Not reached yet', skipped: 'Not reached' };

function slipStep({ tone, n, title, line, extra = null, size = '', level = '' }) {
  return h('li', { class: `slip-step ${tone}`, 'data-tone': tone },
    stampFor(tone, n, size),
    h('div', { class: 'slip-body' },
      h('div', { class: 'slip-title' }, h('span', { class: 'sr-only' }, `${TONE_WORD[tone] || ''}: `), h('strong', {}, title),
        level ? h('span', { class: 'slip-level' }, level) : null),
      line ? h('div', { class: 'slip-line' }, line) : null,
      extra));
}

// ---------- views ----------
function viewSignedOut() {
  setTitle('');
  const sample = card({
    cls: 'slip sample',
    body: [
      h('div', { class: 'sample-head' }, moduleTile('PURCHASE'),
        h('div', {}, h('div', { class: 'title' }, 'Laptop for a new starter'), h('div', { class: 'muted small' }, 'Purchasing, 1,850.00 USD'))),
      h('ol', { class: 'slip-steps' },
        slipStep({ tone: 'done', n: 1, title: 'Line manager', line: 'Approved by Priya Raman' }),
        slipStep({ tone: 'active', n: 2, title: 'Finance', line: 'Waiting for any member of Finance' }),
        slipStep({ tone: 'upcoming', n: 3, title: 'Fulfilment', line: 'Starts automatically once approved' })),
    ],
  });
  sample.setAttribute('role', 'img');
  sample.setAttribute('aria-label', 'Example: a laptop request approved by the line manager and waiting for Finance');
  [...sample.children].forEach((c) => c.setAttribute('aria-hidden', 'true'));

  const go = button('Sign in', { variant: 'primary', icon: 'key', onClick: () => signIn().catch((e) => toast(e.message, 'danger')) });
  mount(h('section', { class: 'landing' },
    h('div', {},
      h('h1', {}, 'Every request that needs a yes, in one place'),
      h('p', { class: 'lead' },
        'Raise purchase, access, leave, change and general requests on forms your admins design. ',
        'Each one follows your reporting line and the approver groups it needs, moves on if nobody decides in time, ',
        'and is fulfilled automatically once approved.'),
      h('div', { class: 'actions' }, go),
      h('div', { class: 'first-time' },
        h('h2', {}, 'Signing in for the first time?'),
        h('ol', { class: 'steps' },
          h('li', {}, h('span', {}, h('strong', {}, 'Find your invitation email. '), 'It has your username and a temporary password.')),
          h('li', {}, h('span', {}, h('strong', {}, 'Sign in and choose a new password. '), 'The temporary one works once.')),
          h('li', {}, h('span', {}, h('strong', {}, 'Set up an authenticator app. '), 'Scan the QR code with an app such as Google Authenticator or Microsoft Authenticator, then enter the 6-digit code. You will be asked for a code each time you sign in.'))))),
    sample));
}

function viewNoAccess() {
  const disabled = state.me && state.me.status === 'DISABLED';
  setTitle(disabled ? 'Account disabled' : 'Access not set up');
  mount(card({
    body: emptyState({
      icon: disabled ? 'shield' : 'key',
      title: disabled ? 'Your account is disabled' : 'Your access is not set up yet',
      body: disabled
        ? 'An admin has disabled this account, so requests and approvals are switched off. Contact an admin if you think this is a mistake.'
        : 'You are signed in, but an admin has not given you a role and request modules yet. Ask an admin to set up your access, then check again.',
      action: button('Check again', { icon: 'refresh', onClick: () => { state.meAt = 0; route(); } }),
    }),
  }));
}

// ---------- my requests ----------
const FILTERS = {
  all: { label: 'All', test: () => true },
  open: { label: 'In progress', test: (it) => OPEN_STATUSES.includes(it.status) },
  approved: { label: 'Approved', test: (it) => ['APPROVED', 'AUTO_APPROVED', 'FULFILLED'].includes(it.status) },
  closed: { label: 'Not approved', test: (it) => ['REJECTED', 'EXPIRED', 'UNROUTABLE', 'FAILED'].includes(it.status) },
};

async function viewMine() {
  setTitle('My requests');
  const head = pageHead({
    title: 'My requests',
    description: 'Everything you have raised, newest first.',
    actions: [
      button('Refresh', { icon: 'refresh', onClick: () => route() }),
      linkButton('New request', '#/new', { variant: 'primary', icon: 'plus' }),
    ],
  });
  mount(head, skeleton(5, 'Loading your requests'));
  const data = await api('/requests');
  const items = data.items || [];
  if (!items.length) {
    mount(head, card({ body: emptyState({
      icon: 'inbox', title: 'No requests yet',
      body: 'Raise your first request and follow it here as it moves through approval.',
      action: linkButton('New request', '#/new', { variant: 'primary', icon: 'plus' }),
    }) }));
    return;
  }
  let filter = 'all';
  const tbody = h('tbody');
  const count = h('span', { class: 'count', 'aria-live': 'polite' });
  const search = searchInput({ placeholder: 'Search by title, module or answer', label: 'Search my requests', onInput: () => paint() });
  const counts = Object.fromEntries(Object.entries(FILTERS).map(([k, f]) => [k, items.filter(f.test).length]));
  const filters = segmented({
    label: 'Show',
    options: Object.entries(FILTERS).map(([k, f]) => [k, `${f.label} (${counts[k]})`]),
    value: filter,
    onChange: (v) => { filter = v; paint(); },
  });
  const haystack = (it) => [it.title, moduleLabel(it.type), (MODULES[it.type] || {}).label, ...(it.fields || []).map((f) => String(f.value ?? ''))].join(' ').toLowerCase();
  const paint = () => {
    const q = search.input.value.trim().toLowerCase();
    const shown = items.filter((it) => FILTERS[filter].test(it) && (!q || haystack(it).includes(q)));
    count.textContent = `${shown.length} of ${items.length}`;
    tbody.replaceChildren(...(shown.length ? shown.map((it) => {
      const href = `#/requests/${it.requestId}`;
      const open = () => { window.location.hash = href; };
      return h('tr', { class: 'clickable', onclick: (ev) => { if (!ev.target.closest('a')) open(); } },
        h('td', {}, h('div', { class: 'req-cell' }, moduleTile(it.type, 'sm'),
          h('div', { class: 'person-text' }, h('a', { class: 'title', href }, it.title),
            h('div', { class: 'cell-sub mobile-only-block' }, [moduleLabel(it.type), valueText(it)].filter(Boolean).join(', '))))),
        h('td', {}, moduleLabel(it.type)),
        h('td', { class: 'num' }, valueText(it)),
        h('td', {}, badge(it.status),
          it.levelCount && OPEN_STATUSES.includes(it.status) && it.status !== 'SUBMITTED'
            ? h('div', { class: 'cell-sub' }, `Level ${it.currentLevel} of ${it.levelCount}`) : null),
        h('td', {}, timeAgo(it.lastUpdated || it.createdAt)));
    }) : [h('tr', {}, h('td', { colspan: '5', class: 'empty' }, 'No requests match. Clear the search or choose another filter.'))]));
  };
  mount(head,
    h('div', { class: 'toolbar' }, search.node, filters, count),
    h('div', { class: 'card flush' }, h('div', { class: 'table-scroll' }, h('table', { class: 'table mine' },
      h('thead', {}, h('tr', {}, h('th', {}, 'Request'), h('th', {}, 'Module'), h('th', { class: 'num' }, 'Value'), h('th', {}, 'Status'), h('th', {}, 'Updated'))),
      tbody))));
  paint();
}

// Mirrors the intake Lambda's checks, so people see every problem next to its field at once.
// In AWS, API Gateway's request model rejects some bodies (no title, say) before the Lambda runs,
// and its generic "Invalid request body" cannot point at a field. The server stays authoritative.
function formErrors(form, inputs, titleText) {
  const errors = {};
  if (titleText.length < 3 || titleText.length > 120) errors.title = 'Give the request a title of 3 to 120 characters';
  form.fields.forEach((f) => {
    const input = inputs.find((i) => i.key === f.key);
    const value = input ? input.read() : undefined;
    if (f.type === 'checkbox') {
      if (f.required && value !== true) errors[f.key] = 'Please confirm this';
      return;
    }
    if (value === undefined || (typeof value === 'string' && !value.trim())) {
      if (f.required) errors[f.key] = 'This field is required';
      return;
    }
    if (f.type === 'number' || f.type === 'amount') {
      const low = f.min ?? 0;
      const high = f.max ?? 1000000;
      if (!Number.isFinite(value)) errors[f.key] = 'Enter a number';
      else if (value < low || value > high) errors[f.key] = `Enter a value between ${fmtNumber(low)} and ${fmtNumber(high)}`;
    } else if (f.type === 'date') {
      if (!parseIso(value)) errors[f.key] = 'Enter a date as YYYY-MM-DD, or pick one from the calendar';
    } else if (f.type === 'text' || f.type === 'textarea') {
      const limit = f.maxLength || (f.type === 'text' ? 120 : 2000);
      if (value.trim().length > limit) errors[f.key] = `Keep this to ${limit} characters`;
    }
  });
  return errors;
}

// ---------- new request: the module decides which fields appear ----------
async function viewNew() {
  setTitle('New request');
  const head = pageHead({ title: 'New request', description: 'Choose what you need, answer the questions, and see who will approve it before you submit.' });
  mount(head, skeleton(6, 'Loading forms'));
  const fromId = params().get('from');
  const [{ forms }, source] = await Promise.all([
    api('/forms'),
    fromId ? api(`/requests/${encodeURIComponent(fromId)}`).catch(() => null) : Promise.resolve(null),
  ]);
  if (!forms.length) {
    mount(head, card({ body: emptyState({
      icon: 'form', title: 'No request forms for you yet',
      body: 'Your account has no request modules, or no form has been set up for them. Ask an admin.',
    }) }));
    return;
  }
  if (fromId && !source) toast('The earlier request could not be loaded, so the form starts empty.', 'warn');
  const prefill = source && forms.some((f) => f.module === source.type) ? source : null;
  const preselect = prefill ? prefill.type : params().get('module');
  let current = forms.find((f) => f.module === preselect) || (forms.length === 1 ? forms[0] : null);
  const picker = h('div', { class: 'choice-grid modules', role: 'radiogroup', 'aria-label': 'Module' });
  const formHost = h('div');

  const paintPicker = () => {
    picker.replaceChildren(...forms.map((f) => {
      const choice = choiceCard({
        name: 'module', value: f.module, title: f.label, description: (MODULES[f.module] || {}).blurb || '',
        iconName: MODULE_ICON[f.module] || 'form',
        checked: Boolean(current && current.module === f.module),
      });
      choice.input.addEventListener('change', () => { current = f; paintPicker(); paintForm(); });
      return choice.node;
    }));
  };

  const paintForm = () => {
    if (!current) {
      formHost.replaceChildren(card({ body: h('p', { class: 'muted' }, 'Choose a module above to see its form.') }));
      return;
    }
    const form = current;
    const key = newKey();
    const usePrefill = prefill && prefill.type === form.module;
    const old = usePrefill ? Object.fromEntries((prefill.fields || []).map((f) => [f.key, f.value])) : {};
    const hasAmount = form.fields.some((f) => f.type === 'amount');
    const currency = select({
      id: 'request-currency',
      options: CURRENCIES.map(([code, name]) => ({ value: code, label: code, description: name })),
      value: (usePrefill && prefill.currency) || 'USD', placeholder: null,
    });
    const title = textInput({ id: 'request-title', value: usePrefill ? prefill.title : '', placeholder: 'A short summary approvers will see first', attrs: { name: 'title', minlength: '3', maxlength: '120', required: true } });
    const titleField = formField({ label: 'Title', control: title, required: true, wide: true });
    const inputs = form.fields.map((f) => renderField(f, { value: old[f.key], currency: hasAmount ? currency.value : null }));
    currency.addEventListener('change', () => {
      formHost.querySelectorAll('.adorned .adorn').forEach((el) => { el.textContent = currency.value; });
    });
    const approvalInput = inputs.find((i) => i.key === form.approvalField);
    const approvalLabel = (form.fields.find((f) => f.key === form.approvalField) || {}).label;

    const routeList = h('ol', { class: 'slip-steps' });
    const autoNote = h('div');
    const preview = card({
      cls: 'slip route-preview sticky',
      title: 'Who will approve it',
      body: [h('p', { class: 'muted small' }, approvalLabel ? `The route depends on ${approvalLabel.replace(/\?+$/, '').toLowerCase()}.` : 'Every request in this module follows the same route.'), autoNote, routeList],
    });
    const paintRoute = () => {
      const value = approvalInput ? Number(approvalInput.input.value || 0) : 0;
      const auto = form.autoApproval && (!approvalInput || value <= Number(form.autoApproval.maxAmount));
      autoNote.replaceChildren(form.autoApproval
        ? note(auto
          ? `You can self-approve ${form.label} up to ${fmtNumber(form.autoApproval.maxAmount)} until ${form.autoApproval.expiresOn}, so this request will be approved automatically.`
          : `Your auto-approval covers up to ${fmtNumber(form.autoApproval.maxAmount)}. This value is higher, so it follows the route below.`, auto ? 'ok' : 'info')
        : '');
      preview.classList.toggle('dimmed', Boolean(auto));
      let n = 0;
      routeList.replaceChildren(...form.route.map((step) => {
        const applies = !step.minAmount || value >= step.minAmount;
        if (applies) n += 1;
        return h('li', { class: `slip-step ${applies ? 'upcoming applies' : 'skipped'}` },
          stampFor(applies ? 'upcoming' : 'skipped', applies ? n : '', 'sm'),
          h('div', { class: 'slip-body' }, step.text, applies ? null : h('span', { class: 'muted' }, ' (not needed for this value)')));
      }));
    };
    if (approvalInput) approvalInput.input.addEventListener('input', paintRoute);

    const submit = button('Submit request', { variant: 'primary', type: 'submit', icon: 'check' });
    const el = h('form', { class: 'card form', novalidate: true, 'aria-labelledby': 'form-title' },
      h('div', { class: 'card-head' },
        h('h2', { id: 'form-title' }, moduleTile(form.module, 'sm'), form.label),
        h('span', { class: 'meta' }, `Form version ${form.version}`)),
      usePrefill ? note(`Copied from "${prefill.title}". Check the answers and change what needs fixing before you submit.`, 'info') : null,
      h('div', { class: 'dyn-grid' }, titleField.node,
        hasAmount ? formField({ label: 'Currency', control: currency, controlId: 'request-currency' }).node : null,
        inputs.map((i) => i.node)),
      h('div', { class: 'form-actions' }, linkButton('Cancel', '#/mine', { variant: 'ghost' }), submit));
    paintRoute();

    const showErrors = (errors) => {
      Object.entries(errors).forEach(([k, msg]) => {
        if (k === 'title') titleField.setError(msg);
        const target = inputs.find((i) => i.key === k);
        if (target) target.setError(msg);
      });
      const first = el.querySelector('.has-error input, .has-error textarea, .has-error .select-trigger');
      if (first) first.focus();
    };

    el.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      inputs.forEach((i) => i.setError(''));
      titleField.setError('');
      const problems = formErrors(form, inputs, title.value.trim());
      if (Object.keys(problems).length) {
        showErrors(problems);
        toast(`Check the highlighted field${Object.keys(problems).length > 1 ? 's' : ''} before submitting.`, 'warn');
        return;
      }
      const fields = {};
      inputs.forEach((i) => { const v = i.read(); if (v !== undefined) fields[i.key] = v; });
      const payload = { type: form.module, title: title.value.trim(), fields };
      if (hasAmount) payload.currency = currency.value;
      await busy(submit, async () => {
        try {
          const created = await api('/requests', { method: 'POST', body: JSON.stringify(payload), headers: { 'Idempotency-Key': key } });
          toast('Submitted. The workflow is routing it now.', 'ok', { persist: true });
          window.location.hash = `#/requests/${created.requestId}`;
        } catch (e) {
          showErrors((e instanceof ApiError && e.data && e.data.fieldErrors) || {});
          toast(e instanceof ApiError && e.status === 400 && e.message.startsWith('Invalid request body')
            ? 'Some answers are not in the format this form expects. Check the fields and try again.' : e.message, 'danger');
        }
      });
    });
    formHost.replaceChildren(h('div', { class: 'layout-2' }, el, preview));
    title.focus();
  };

  mount(head, h('div', { class: 'stack' },
    forms.length > 1 ? h('section', { class: 'card', 'aria-labelledby': 'pick-title' },
      h('h2', { id: 'pick-title', class: 'section-label' }, 'What do you need?'), picker) : null,
    formHost));
  paintPicker();
  paintForm();
}

// ---------- request page ----------
function slipPanel(it) {
  const plan = it.approvalPlan || {};
  const levels = plan.levels || [];
  const decisions = Object.fromEntries((it.approvals || []).map((a) => [Number(a.level), a]));
  const reasons = Array.isArray(it.policyReasons) ? it.policyReasons : [];
  const why = reasons.length
    ? h('details', { class: 'disclosure reasons' }, h('summary', {}, icon('chevronRight', { size: 14 }), 'Why this route?'),
      h('ul', {}, reasons.map((r) => h('li', {}, r))))
    : null;
  const meta = plan.slaSeconds ? `${Math.round(plan.slaSeconds / 60)} min per level` : '';
  const panel = (body) => card({ cls: 'slip', id: 'slip', title: 'Routing slip', actions: meta ? h('span', { class: 'meta' }, meta) : null, body: [...body, why] });

  // Auto-approved requests have no levels; after fulfilment the status moves on but the decision stays.
  if (it.status === 'AUTO_APPROVED' || it.decidedBy === 'policy-engine') {
    return panel([h('ol', { class: 'slip-steps' }, slipStep({ tone: 'done', n: 1, title: 'Approved automatically', line: 'An admin granted the requester auto-approval for this module and value.' })), outcome(it)]);
  }
  if (it.status === 'UNROUTABLE') {
    return panel([note('Nobody could approve this request, so it stopped. An admin needs to set a manager or add approver group members; then raise it again.', 'warn')]);
  }
  if (!levels.length) {
    return panel([it.status === 'SUBMITTED'
      ? h('p', { class: 'muted' }, 'Working out who needs to approve this...')
      : h('p', { class: 'muted' }, 'No approval route was recorded for this request.')]);
  }
  const current = Number(it.currentLevel || 0);
  const open = ['PENDING_APPROVAL', 'ESCALATED'].includes(it.status);
  const steps = levels.map((level) => {
    const d = decisions[level.index];
    let tone = 'upcoming';
    let line = `After the level above: ${(level.approverNames || []).join(' or ')}`;
    let extra = null;
    if (d) {
      tone = d.decision === 'APPROVED' ? 'done' : 'rejected';
      line = `${d.decision === 'APPROVED' ? 'Approved' : 'Rejected'} by ${d.decidedByEmail || d.decidedBy}, ${fmtTime(d.at)}`;
      extra = d.comment ? h('blockquote', { class: 'quote' }, d.comment) : null;
    } else if (open && level.index === current) {
      tone = it.status === 'ESCALATED' ? 'escalated' : 'active';
      line = it.status === 'ESCALATED'
        ? `Missed its deadline, so it went to admins: ${(plan.escalateToNames || []).join(', ')}`
        : `Waiting for ${(level.approverNames || []).join(' or ')}`;
      extra = countdown(it);
    } else if (!open) {
      tone = 'skipped';
      line = 'Not reached';
    }
    return slipStep({ tone, n: level.index, line, extra, title: level.label, level: `Level ${level.index}` });
  });
  return panel([h('ol', { class: 'slip-steps' }, steps), outcome(it)]);
}

function outcome(it) {
  if (it.status === 'FULFILLED') {
    return h('div', { class: 'slip-outcome' }, stampFor('done', '', 'sm'),
      h('div', {}, h('strong', {}, 'Fulfilled'), h('div', { class: 'muted small' }, [it.fulfilmentRef, fmtTime(it.fulfilledAt)].filter(Boolean).join(', '))));
  }
  if (it.status === 'APPROVED' || it.status === 'AUTO_APPROVED') {
    return h('div', { class: 'slip-outcome' }, stampFor('active', '', 'sm'), h('div', {}, h('strong', {}, 'Approved'), h('div', { class: 'muted small' }, 'Fulfilment is running.')));
  }
  if (it.status === 'EXPIRED') {
    return h('div', { class: 'slip-outcome' }, stampFor('rejected', '', 'sm'), h('div', {}, h('strong', {}, 'Expired'), h('div', { class: 'muted small' }, 'Nobody decided before the deadline.')));
  }
  return null;
}

// True while someone is in the middle of something on the page that a redraw would throw away.
function pageBusy() {
  return [...document.querySelectorAll('#app input[type=file]')].some((i) => i.files && i.files.length)
    || [...document.querySelectorAll('#app textarea')].some((t) => t.value.trim());
}

let detailTimer = null;

async function viewDetail(requestId, attempt = 0) {
  clearTimeout(detailTimer);
  if (attempt === 0) mount(skeleton(3, 'Loading request'), skeleton(4, 'Loading request'));
  const it = await api(`/requests/${encodeURIComponent(requestId)}`);
  setTitle(it.title);

  // The workflow usually routes a new request within a second or two.
  // Re-check a few times, then stop (each check is a Lambda call, and the playground caps them).
  // An automatic re-check never redraws over a chosen file or a note being typed.
  if (it.status === 'SUBMITTED' && attempt < 3) {
    detailTimer = setTimeout(() => {
      if (window.location.hash === `#/requests/${requestId}` && !pageBusy()) viewDetail(requestId, attempt + 1).catch(showError);
    }, 2500);
  }

  const answers = h('dl', { class: 'answers' },
    (it.fields || []).map((f) => field(f.label.replace(/\?+$/, ''), f.type === 'textarea' ? h('span', { class: 'desc' }, fmtField(f, it.currency)) : fmtField(f, it.currency), { wide: f.type === 'textarea' })));
  const about = h('dl', { class: 'facts' },
    field('Module', moduleLabel(it.type)),
    field('Requested by', it.requesterName ? `${it.requesterName} (${it.requesterEmail})` : it.requesterEmail),
    field('Created', fmtTime(it.createdAt)),
    field('Last update', fmtTime(it.lastUpdated)),
    field('Decided by', it.decidedByEmail || (it.decidedBy === 'policy-engine' ? 'Auto-approval rule' : null)),
    field('Decision note', it.decisionComment),
    field('Fulfilment reference', it.fulfilmentRef),
    field('Form version', it.formVersion ? String(it.formVersion) : null),
    field('Request ID', h('span', { class: 'mono small' }, it.requestId)));

  const actions = [iconButton('refresh', 'Refresh', { variant: 'secondary', onClick: () => viewDetail(requestId).catch(showError) })];
  if (it.isOwner && RAISE_AGAIN.includes(it.status)) {
    actions.unshift(linkButton('Raise again', `#/new?from=${encodeURIComponent(it.requestId)}`, { variant: 'primary', icon: 'refresh' }));
  }
  const back = it.isOwner || !isReviewer() ? { href: '#/mine', label: 'My requests' } : { href: '#/inbox', label: 'Approvals' };
  mount(
    pageHead({ title: it.title, back, aside: badge(it.status), actions }),
    h('div', { class: 'layout-2 request-layout' },
      h('div', { class: 'col' },
        it.canDecide ? decisionPanel(it) : null,
        card({ cls: 'answers-card', title: 'Answers', body: [answers] }),
        attachmentsPanel(it)),
      h('div', { class: 'col' }, slipPanel(it), card({ cls: 'about-card', title: 'About this request', body: [about] }))));
}

function decisionPanel(it) {
  const comment = h('textarea', { id: 'decision-note', rows: '3', maxlength: '500', placeholder: 'Optional when approving. Say why when rejecting.' });
  const noteField = formField({ label: 'Decision note', control: comment, controlId: 'decision-note' });
  const approve = button('Approve', { variant: 'success', icon: 'check' });
  const reject = button('Reject', { variant: 'danger', icon: 'close' });
  const decide = async (decision, btn) => {
    noteField.setError('');
    if (decision === 'REJECTED' && !comment.value.trim()) {
      noteField.setError('Add a note so the requester knows why');
      comment.focus();
      return;
    }
    const other = btn === approve ? reject : approve;
    other.disabled = true;
    await busy(btn, async () => {
      try {
        await api(`/requests/${encodeURIComponent(it.requestId)}/decision`, {
          method: 'POST', body: JSON.stringify({ decision, comment: comment.value.trim() }),
        });
        toast(`${decision === 'APPROVED' ? 'Approved' : 'Rejected'}. The workflow is moving the request on.`, 'ok');
        setTimeout(() => viewDetail(it.requestId).catch(showError), 1500);
      } catch (e) {
        toast(e.message, 'danger');
        other.disabled = false;
      }
    });
  };
  approve.addEventListener('click', () => decide('APPROVED', approve));
  reject.addEventListener('click', () => decide('REJECTED', reject));
  const more = Number(it.levelCount || 1) > Number(it.currentLevel || 1);
  return card({
    cls: 'accent decision',
    title: it.status === 'ESCALATED' ? 'Escalated to you: decision needed' : 'Your decision',
    actions: h('div', { class: 'decision-meta' }, h('span', { class: 'meta' }, `Level ${it.currentLevel} of ${it.levelCount}`), countdown(it)),
    body: [
      h('p', { class: 'muted small' }, more
        ? 'Approving passes the request to the next level. Rejecting ends it.'
        : 'This is the last level: approving completes the approval and starts fulfilment.'),
      noteField.node,
      h('div', { class: 'row end' }, reject, approve),
    ],
  });
}

function attachmentsPanel(it) {
  const files = it.attachments || [];
  const list = files.length
    ? h('ul', { class: 'files' }, files.map((a) => h('li', {}, icon('file', { size: 18 }),
      a.uploaded && a.downloadUrl
        ? h('a', { href: a.downloadUrl, rel: 'noopener' }, a.fileName)
        : h('span', { class: 'file-name muted' }, `${a.fileName} (upload not completed)`),
      h('span', { class: 'muted small' }, a.contentType || ''))))
    : h('p', { class: 'muted small' }, 'No attachments.');

  const body = [list];
  const closed = ['REJECTED', 'EXPIRED', 'FULFILLED', 'UNROUTABLE', 'FAILED'].includes(it.status);
  if (it.isOwner && closed) body.push(h('p', { class: 'muted small' }, 'This request is closed, so no more files can be added.'));
  if (it.isOwner && !closed) {
    const drop = fileDrop({ label: 'Choose a file to attach', accept: '.pdf,.png,.jpg,.jpeg,.txt,.csv,.docx,.xlsx', hint: 'PDF, images, TXT, CSV, DOCX or XLSX, up to 10 MB' });
    const upload = button('Upload', { icon: 'upload' });
    upload.addEventListener('click', async () => {
      const file = drop.input.files && drop.input.files[0];
      if (!file) { toast('Choose a file first.', 'warn'); return; }
      await busy(upload, async () => {
        try {
          await uploadAttachment(it.requestId, file);
          toast(`${file.name} uploaded.`, 'ok');
          drop.reset();
          await viewDetail(it.requestId);
        } catch (e) {
          toast(e.message, 'danger');
        }
      });
    });
    body.push(h('div', { class: 'upload-row' }, drop, upload));
  }
  return card({ cls: 'attachments-card', title: 'Attachments', actions: files.length ? h('span', { class: 'meta' }, String(files.length)) : null, body });
}

async function uploadAttachment(requestId, file) {
  const meta = await api(`/requests/${encodeURIComponent(requestId)}/attachments`, {
    method: 'POST', body: JSON.stringify({ fileName: file.name, contentType: file.type || 'application/octet-stream' }),
  });
  const form = new FormData();
  Object.entries(meta.upload.fields).forEach(([k, v]) => form.append(k, v));
  form.append('file', file);
  const res = await fetch(meta.upload.url, { method: 'POST', body: form });
  if (!res.ok) throw new Error('The file was refused: check its type and that it is under 10 MB.');
}

// ---------- approvals ----------
async function viewInbox() {
  if (!isReviewer()) { window.location.hash = '#/mine'; return; }
  setTitle('Approvals');
  const head = pageHead({
    title: 'Approvals',
    description: 'Requests waiting for your decision: as a line manager, a member of an approver group, or an admin standing in.',
    actions: [button('Refresh', { icon: 'refresh', onClick: () => route() })],
  });
  mount(head, skeleton(4, 'Loading approvals'));
  const data = await api('/approvals');
  const items = data.items || [];
  if (!items.length) {
    mount(head, card({ body: emptyState({ icon: 'checkCircle', title: 'Nothing is waiting for you', body: 'When a request needs your decision, it appears here with how long you have before it moves on.' }) }));
    return;
  }
  mount(head, h('div', { class: 'tickets' }, items.map((it) => h('a', { class: 'card ticket', href: `#/requests/${it.requestId}` },
    moduleTile(it.type),
    h('div', { class: 'ticket-main' },
      h('div', { class: 'row spread' }, h('span', { class: 'title' }, it.title), badge(it.status)),
      h('div', { class: 'muted small' }, `${it.requesterName || it.requesterEmail}, ${moduleLabel(it.type)}`),
      valueText(it) ? h('div', { class: 'small num' }, valueText(it)) : null),
    h('div', { class: 'ticket-foot' },
      h('span', {}, `Level ${it.currentLevel} of ${it.levelCount}: ${it.approverGroup}`),
      countdown(it) || h('span', { class: 'muted' }, `Raised ${fmtTime(it.createdAt)}`))))));
}

// ---------- chrome and router ----------
function showError(e) {
  if (e instanceof StaleRoute) return; // a newer page is already loading
  toast(e.message, 'danger');
  if (!state.idToken) { viewSignedOut(); return; }
  // A page that failed while loading says so, instead of showing placeholders forever.
  if (document.querySelector('#app .skeleton-card')) {
    mount(card({ body: emptyState({
      icon: 'alert', title: e.status === 404 ? 'Not found' : 'This page could not be loaded', body: e.message,
      action: button('Try again', { icon: 'refresh', onClick: () => route() }),
    }) }));
  }
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
  document.getElementById('adminTab').hidden = !isAdmin();
  const current = (window.location.hash.split('/')[1] || 'mine').split('?')[0];
  const view = current === 'requests' ? null : current;
  nav.querySelectorAll('a').forEach((a) => {
    if (a.dataset.view === view) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  });
  const person = state.me || state.claims || {};
  const roleId = role();
  who.replaceChildren(menu({
    content: [avatar(person, 'sm'), h('span', { class: 'sr-only' }, 'Account menu, '), h('span', { class: 'who-name' }, displayName(person)), icon('chevronDown', { size: 14 })],
    items: [
      { header: [h('strong', {}, displayName(person)), person.name && person.email ? h('span', { class: 'muted small' }, person.email) : null,
        roleId ? h('span', { class: `badge role-${roleId}` }, (ROLES[roleId] || {}).label || roleId) : null] },
      { label: 'Sign out', icon: 'logout', onClick: signOut },
    ],
  }));
}

// Views load their data asynchronously. If someone navigates while a view is still loading,
// the older view must not paint over the newer one. Each route has a sequence number: reads made
// for an older route are dropped when they return (see api() in core.js), and as a second guard a
// route that finishes after a newer one has painted re-runs the router for the current URL.
let routeSeq = 0;
let paintedSeq = 0;

async function route() {
  const seq = ++routeSeq;
  nav.seq = seq;
  clearPageTimers();
  try {
    await renderRoute();
  } finally {
    if (paintedSeq > seq) route();
    else paintedSeq = seq;
  }
}

async function renderRoute() {
  if (!loadSession()) {
    state.idToken = null;
    paintChrome();
    viewSignedOut();
    return;
  }
  const parts = window.location.hash.replace(/^#\/?/, '').split('?')[0].split('/');
  if (parts[0] === 'users') { window.location.hash = `#/admin/${parts.join('/')}`; return; }
  try {
    await ensureMe();
    paintChrome();
    if (state.me.status !== 'ACTIVE') { viewNoAccess(); return; }
    if (parts[0] === 'new') await viewNew();
    else if (parts[0] === 'inbox') await viewInbox();
    else if (parts[0] === 'requests' && parts[1]) await viewDetail(parts[1]);
    else if (parts[0] === 'admin') await adminRoute(parts.slice(1));
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
    mount(card({ body: emptyState({ icon: 'alert', title: 'Not configured yet', body: e.message }) }));
    return;
  }
  setRouter(route);
  const query = new URLSearchParams(window.location.search);
  if (query.get('code')) {
    try {
      await completeSignIn(query.get('code'), query.get('state'));
    } catch (e) {
      toast(e.message, 'danger');
      window.history.replaceState({}, '', state.cfg.redirectUri);
    }
  }
  // The skip link moves focus without touching the address (the hash is the router's).
  document.querySelector('.skip-link').addEventListener('click', (ev) => {
    ev.preventDefault();
    document.getElementById('app').focus();
  });
  window.addEventListener('hashchange', () => {
    clearToastOnNavigate();
    window.scrollTo(0, 0);
    route();
  });
  await route();
}

start();
