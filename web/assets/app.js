// Request Hub single-page app: sign-in, routing and the requester and approver screens.
// Auth: Cognito hosted UI with the authorization code flow + PKCE.
import {
  state, MODULES, ROLES, CURRENCIES, h, store, toast, mount, setRouter, fmtTime, fmtNumber, fmtMoney, fmtField,
  badge, field, api, ApiError, ensureMe, role, isAdmin, isReviewer, renderField, newKey, displayName,
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
    return true;
  } catch {
    store.del('id_token');
    return false;
  }
}

const moduleLabel = (id) => (MODULES[id] || {}).label || id;

function valueText(it) {
  if (it.approvalField && it.amount !== undefined) {
    const f = (it.fields || []).find((x) => x.label === it.approvalField);
    if (f) return `${it.approvalField}: ${fmtField(f, it.currency)}`;
    return `${it.approvalField}: ${fmtNumber(it.amount)}`;
  }
  return it.amount ? fmtMoney(it.amount, it.currency) : '';
}

// ---------- views ----------
function viewSignedOut() {
  mount(
    h('section', { class: 'hero card' },
      h('h1', {}, 'One place for every request that needs a yes'),
      h('p', { class: 'lead' },
        'Raise purchase, access, leave, change or general requests on forms your admins design. ',
        'Each request follows your reporting line and the approver groups it needs, escalates when an SLA is missed, ',
        'and is fulfilled automatically once approved.'),
      h('button', { class: 'btn primary', onclick: () => signIn().catch((e) => toast(e.message, 'danger')) }, 'Sign in'),
      h('p', { class: 'muted small' }, 'Sign-in uses Amazon Cognito with mandatory authenticator-app MFA.')));
}

function viewNoAccess() {
  const disabled = state.me && state.me.status === 'DISABLED';
  mount(h('section', { class: 'card hero' },
    h('h1', {}, disabled ? 'Your account is disabled' : 'Your access is not set up yet'),
    h('p', { class: 'lead' }, disabled
      ? 'An admin has disabled this account, so requests and approvals are switched off. Contact an admin if you think this is a mistake.'
      : 'You can sign in, but an admin has not given you a role and modules yet. Ask an admin to set up your access.'),
    h('button', { class: 'btn', onclick: () => { state.meAt = 0; route(); } }, 'Check again')));
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
    h('td', {}, moduleLabel(it.type)),
    h('td', { class: 'small' }, valueText(it)),
    h('td', {}, badge(it.status),
      it.levelCount && ['PENDING_APPROVAL', 'ESCALATED'].includes(it.status)
        ? h('div', { class: 'muted small' }, `Level ${it.currentLevel} of ${it.levelCount}`) : null),
    h('td', { class: 'muted small' }, fmtTime(it.createdAt))));
  mount(header, h('div', { class: 'card flush' },
    h('table', { class: 'table' },
      h('thead', {}, h('tr', {}, h('th', {}, 'Request'), h('th', {}, 'Module'), h('th', {}, 'Value'), h('th', {}, 'Status'), h('th', {}, 'Created'))),
      h('tbody', {}, rows))));
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
    } else if (f.type === 'text' || f.type === 'textarea') {
      const limit = f.maxLength || (f.type === 'text' ? 120 : 2000);
      if (value.trim().length > limit) errors[f.key] = `Keep this to ${limit} characters`;
    }
  });
  return errors;
}

// ---------- new request: the module decides which fields appear ----------
async function viewNew() {
  mount(h('p', { class: 'muted' }, 'Loading forms...'));
  const { forms } = await api('/forms');
  const head = h('div', { class: 'section-head' }, h('h2', {}, 'New request'));
  if (!forms.length) {
    mount(head, h('div', { class: 'card empty' }, h('h3', {}, 'No request modules yet'),
      h('p', {}, 'Your account does not have access to any request module, or no form has been set up. Ask an admin.')));
    return;
  }
  const preselect = new URLSearchParams(window.location.hash.split('?')[1] || '').get('module');
  let current = forms.find((f) => f.module === preselect) || (forms.length === 1 ? forms[0] : null);
  const picker = h('div', { class: 'choice-grid modules', role: 'radiogroup', 'aria-label': 'Module' });
  const formHost = h('div');

  const paintPicker = () => {
    picker.replaceChildren(...forms.map((f) => {
      const input = h('input', { type: 'radio', name: 'module', value: f.module, checked: current && current.module === f.module });
      input.addEventListener('change', () => { current = f; paintPicker(); paintForm(); });
      return h('label', { class: 'choice' }, input, h('span', { class: 'choice-body' },
        h('strong', {}, f.label), h('span', { class: 'muted small' }, (MODULES[f.module] || {}).blurb || '')));
    }));
  };

  const paintForm = () => {
    if (!current) { formHost.replaceChildren(h('p', { class: 'muted' }, 'Choose a module to see its form.')); return; }
    const form = current;
    const key = newKey();
    const title = h('input', { id: 'request-title', name: 'title', minlength: '3', maxlength: '120', required: true, placeholder: 'A short summary approvers will see first' });
    const titleError = h('span', { class: 'field-error', role: 'alert' });
    const hasAmount = form.fields.some((f) => f.type === 'amount');
    const currency = h('select', { id: 'request-currency' }, CURRENCIES.map((c) => h('option', { value: c }, c)));
    const inputs = form.fields.map((f) => renderField(f));
    const approvalInput = inputs.find((i) => i.key === form.approvalField);
    const approvalLabel = (form.fields.find((f) => f.key === form.approvalField) || {}).label;

    const routeList = h('ol', { class: 'route' });
    const autoNote = h('p', { class: 'note ok' });
    const paintRoute = () => {
      const value = approvalInput ? Number(approvalInput.input.value || 0) : 0;
      const auto = form.autoApproval && (!approvalInput || value <= Number(form.autoApproval.maxAmount));
      autoNote.hidden = !form.autoApproval;
      if (form.autoApproval) {
        autoNote.textContent = auto
          ? `An admin lets you self-approve ${form.label} up to ${fmtNumber(form.autoApproval.maxAmount)} until ${form.autoApproval.expiresOn}, so this request will be approved automatically.`
          : `Your auto-approval covers up to ${fmtNumber(form.autoApproval.maxAmount)}. This value is higher, so it follows the route below.`;
        autoNote.className = auto ? 'note ok' : 'note';
      }
      routeList.classList.toggle('dimmed', Boolean(auto));
      routeList.replaceChildren(...form.route.map((step) => {
        const applies = !step.minAmount || value >= step.minAmount;
        return h('li', { class: applies ? 'applies' : 'skipped' }, step.text, applies ? null : h('span', { class: 'muted small' }, ' (not needed for this value)'));
      }));
    };
    if (approvalInput) approvalInput.input.addEventListener('input', paintRoute);

    const submit = h('button', { class: 'btn primary', type: 'submit' }, 'Submit request');
    const el = h('form', { class: 'card form', novalidate: true },
      h('div', { class: 'row spread' }, h('h3', {}, form.label), h('span', { class: 'muted small' }, `Form version ${form.version}`)),
      h('div', { class: 'dyn-field' }, h('label', { for: 'request-title' }, 'Title', h('span', { class: 'req' }, ' *')), title, titleError),
      hasAmount ? h('div', { class: 'dyn-field narrow' }, h('label', { for: 'request-currency' }, 'Currency'), currency) : null,
      h('div', { class: 'dyn-grid' }, inputs.map((i) => i.node)),
      h('div', { class: 'route-box' },
        h('h4', {}, approvalLabel ? `Approval route (depends on ${approvalLabel})` : 'Approval route'),
        autoNote, routeList),
      h('div', { class: 'row end' }, h('a', { class: 'btn', href: '#/mine' }, 'Cancel'), submit));
    paintRoute();

    const showErrors = (errors) => {
      Object.entries(errors).forEach(([k, msg]) => {
        if (k === 'title') { titleError.textContent = msg; title.parentElement.classList.add('has-error'); }
        const target = inputs.find((i) => i.key === k);
        if (target) target.setError(msg);
      });
      const first = el.querySelector('.has-error input, .has-error select, .has-error textarea') || (errors.title ? title : null);
      if (first) first.focus();
    };

    el.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      inputs.forEach((i) => i.setError(''));
      titleError.textContent = '';
      title.parentElement.classList.remove('has-error');
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
      submit.disabled = true;
      try {
        const created = await api('/requests', { method: 'POST', body: JSON.stringify(payload), headers: { 'Idempotency-Key': key } });
        toast('Submitted. The workflow is routing it now.', 'ok');
        window.location.hash = `#/requests/${created.requestId}`;
      } catch (e) {
        showErrors((e instanceof ApiError && e.data && e.data.fieldErrors) || {});
        toast(e instanceof ApiError && e.status === 400 && e.message.startsWith('Invalid request body')
          ? 'Some answers are not in the format this form expects. Check the fields and try again.' : e.message, 'danger');
        submit.disabled = false;
      }
    });
    formHost.replaceChildren(el);
    title.focus();
  };

  mount(head, forms.length > 1 ? h('div', { class: 'card' }, h('h4', {}, 'Module'), picker) : null, formHost);
  paintPicker();
  paintForm();
}

// ---------- request detail ----------
function chainPanel(it) {
  const plan = it.approvalPlan || {};
  const levels = plan.levels || [];
  const decisions = Object.fromEntries((it.approvals || []).map((a) => [Number(a.level), a]));
  // Auto-approved requests have no levels; after fulfilment the status moves on but the decision stays.
  if (it.status === 'AUTO_APPROVED' || it.decidedBy === 'policy-engine') {
    return h('div', { class: 'card' }, h('h3', {}, 'Approval'),
      h('p', { class: 'note ok' }, 'Approved automatically under an auto-approval rule an admin granted to the requester.'));
  }
  if (it.status === 'UNROUTABLE') {
    return h('div', { class: 'card' }, h('h3', {}, 'Approval'),
      h('p', { class: 'note warn' }, 'Nobody was eligible to approve this request, so it stopped. An admin needs to fix the reporting line or approver groups; then raise it again.'));
  }
  if (!levels.length) {
    return h('div', { class: 'card' }, h('h3', {}, 'Approval'), h('p', { class: 'muted' }, it.status === 'SUBMITTED'
      ? 'Working out the approval route...' : 'No approval route was recorded for this request.'));
  }
  const current = Number(it.currentLevel || 0);
  const open = ['PENDING_APPROVAL', 'ESCALATED'].includes(it.status);
  const steps = levels.map((level) => {
    const d = decisions[level.index];
    let tone = 'upcoming';
    let line = `Next, after the level above: ${(level.approverNames || []).join(' or ')}`;
    if (d) {
      tone = d.decision === 'APPROVED' ? 'done' : 'rejected';
      line = `${d.decision === 'APPROVED' ? 'Approved' : 'Rejected'} by ${d.decidedByEmail || d.decidedBy} on ${fmtTime(d.at)}`;
    } else if (open && level.index === current) {
      tone = it.status === 'ESCALATED' ? 'escalated' : 'active';
      line = it.status === 'ESCALATED'
        ? `SLA missed, escalated to admins: ${(plan.escalateToNames || []).join(', ')}`
        : `Waiting for ${(level.approverNames || []).join(' or ')} since ${fmtTime(it.levelStartedAt)}`;
    } else if (!open) {
      tone = 'skipped';
      line = 'Not reached';
    }
    return h('li', { class: `step ${tone}` },
      h('div', { class: 'step-head' }, h('strong', {}, `Level ${level.index}: ${level.label}`),
        level.fallback ? h('span', { class: 'chip' }, 'admins standing in') : null),
      h('div', { class: 'small' }, line),
      d && d.comment ? h('div', { class: 'muted small quote' }, d.comment) : null);
  });
  return h('div', { class: 'card' },
    h('div', { class: 'row spread' }, h('h3', {}, 'Approval chain'),
      h('span', { class: 'muted small' }, `${plan.version || ''}${plan.slaSeconds ? `, ${Math.round(plan.slaSeconds / 60)} min per level before escalation` : ''}`)),
    h('ol', { class: 'chain' }, steps));
}

async function viewDetail(requestId, attempt = 0) {
  if (attempt === 0) mount(h('p', { class: 'muted' }, 'Loading request...'));
  const it = await api(`/requests/${encodeURIComponent(requestId)}`);

  // The workflow usually routes a new request within a second or two.
  // Re-check a few times, then stop (each check is a Lambda call, and the playground caps them).
  if (it.status === 'SUBMITTED' && attempt < 3) {
    setTimeout(() => { if (window.location.hash === `#/requests/${requestId}`) viewDetail(requestId, attempt + 1).catch(showError); }, 2500);
  }

  const reasons = Array.isArray(it.policyReasons) ? it.policyReasons : [];
  const fields = h('dl', { class: 'facts' },
    (it.fields || []).map((f) => field(f.label, f.type === 'textarea' ? h('span', { class: 'desc' }, fmtField(f, it.currency)) : fmtField(f, it.currency))));
  const facts = h('dl', { class: 'facts' },
    field('Module', moduleLabel(it.type)),
    field('Requested by', it.requesterName ? `${it.requesterName} (${it.requesterEmail})` : it.requesterEmail),
    field('Created', fmtTime(it.createdAt)),
    field('Last update', fmtTime(it.lastUpdated)),
    field('Decided by', it.decidedByEmail || (it.decidedBy === 'policy-engine' ? 'Auto-approval rule' : null)),
    field('Decision note', it.decisionComment),
    field('Fulfilment reference', it.fulfilmentRef),
    field('Fulfilled', fmtTime(it.fulfilledAt)));

  const blocks = [
    h('div', { class: 'section-head' },
      h('div', {}, h('a', { href: '#/mine', class: 'muted small' }, 'Back to my requests'), h('h2', {}, it.title)),
      h('div', { class: 'row' }, badge(it.status), h('button', { class: 'btn', onclick: () => viewDetail(requestId).catch(showError) }, 'Refresh'))),
    h('div', { class: 'card' },
      h('div', { class: 'row spread' }, h('h3', {}, 'Details'), it.formVersion ? h('span', { class: 'muted small' }, `Form version ${it.formVersion}`) : null),
      fields, facts,
      reasons.length ? h('div', { class: 'reasons' }, h('h3', {}, 'How the route was decided'), h('ul', {}, reasons.map((r) => h('li', {}, r)))) : null,
      h('p', { class: 'muted small mono' }, `Request ID ${it.requestId}`)),
    chainPanel(it),
  ];
  if (it.canDecide) blocks.splice(2, 0, decisionPanel(it));
  blocks.push(attachmentsPanel(it));
  mount(...blocks);
}

function decisionPanel(it) {
  const comment = h('textarea', { rows: '3', maxlength: '500', 'aria-label': 'Decision note', placeholder: 'Add a note (required when rejecting)' });
  const decide = async (decision, buttons) => {
    buttons.forEach((b) => { b.disabled = true; });
    try {
      await api(`/requests/${encodeURIComponent(it.requestId)}/decision`, {
        method: 'POST', body: JSON.stringify({ decision, comment: comment.value.trim() }),
      });
      toast(`Recorded: ${decision.toLowerCase()}. The workflow is moving the request on.`, 'ok');
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
  const more = Number(it.levelCount || 1) > Number(it.currentLevel || 1);
  return h('div', { class: 'card accent' },
    h('h3', {}, it.status === 'ESCALATED' ? 'Escalated to you: decision needed' : `Your decision (level ${it.currentLevel} of ${it.levelCount})`),
    h('p', { class: 'muted small' }, more
      ? 'Approving passes the request to the next level. Rejecting ends it.'
      : 'This is the last level: approving completes the approval and starts fulfilment.'),
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
  const header = h('div', { class: 'section-head' },
    h('div', {}, h('h2', {}, 'Approvals waiting for you'),
      h('p', { class: 'muted small' }, 'Requests where you are named on the current level: as a line manager, a member of an approver group, or an admin standing in.')),
    h('button', { class: 'btn', onclick: () => route() }, 'Refresh'));
  if (!items.length) {
    mount(header, h('div', { class: 'card empty' }, 'Nothing is waiting for you right now.'));
    return;
  }
  mount(header, h('div', { class: 'cards' }, items.map((it) => h('a', { class: 'card tile', href: `#/requests/${it.requestId}` },
    h('div', { class: 'row spread' }, h('strong', {}, it.title), badge(it.status)),
    h('div', { class: 'muted small' }, `${moduleLabel(it.type)} from ${it.requesterName || it.requesterEmail}. ${valueText(it)}`),
    h('div', { class: 'small' }, `Level ${it.currentLevel} of ${it.levelCount}: ${it.approverGroup}`),
    h('div', { class: 'muted small' }, `Raised ${fmtTime(it.createdAt)}`)))));
}

// ---------- chrome and router ----------
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
  document.getElementById('adminTab').hidden = !isAdmin();
  const current = (window.location.hash.split('/')[1] || 'mine').split('?')[0];
  nav.querySelectorAll('a').forEach((a) => a.classList.toggle('active', a.dataset.view === current));
  who.replaceChildren(...[
    h('span', { class: 'small' }, displayName(state.me || state.claims || {})),
    role() ? h('span', { class: 'chip' }, (ROLES[role()] || {}).label || role()) : null,
    h('button', { class: 'btn ghost', onclick: signOut }, 'Sign out')].filter(Boolean));
}

// Views load their data asynchronously. If someone navigates while a view is still loading,
// the older view can finish last and paint over the newer one. Each route has a sequence number;
// a route that finishes after a newer one has already painted re-runs the router for the current URL.
let routeSeq = 0;
let paintedSeq = 0;

async function route() {
  const seq = ++routeSeq;
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
    await ensureMe(parts[0] === 'admin');
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
    mount(h('div', { class: 'card' }, h('h2', {}, 'Not configured yet'), h('p', {}, e.message)));
    return;
  }
  setRouter(route);
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
