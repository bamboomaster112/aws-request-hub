// Admin area: users and access, org hierarchy, approver groups, workflows, form builder, auto-approval.
import {
  state, MODULES, ROLES, h, toast, mount, rerender, fmtTime, fmtNumber, field, api, isAdmin,
  confirmDialog, avatar, displayName, renderField,
} from './core.js';

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
  AUTO_RULE_SET: 'Auto-approval granted', AUTO_RULE_REVOKED: 'Auto-approval revoked',
  FORM_SAVED: 'Form saved', WORKFLOW_SAVED: 'Workflow saved', GROUP_SAVED: 'Group saved', GROUP_DELETED: 'Group deleted',
};
const FIELD_TYPES = [
  ['text', 'Short text'], ['textarea', 'Long text'], ['number', 'Number'], ['amount', 'Money amount'],
  ['date', 'Date'], ['select', 'Dropdown'], ['checkbox', 'Checkbox'],
];
const SECTIONS = [
  ['users', 'Users'], ['org', 'Org chart'], ['groups', 'Approver groups'], ['workflows', 'Workflows'],
  ['forms', 'Forms'], ['auto-approval', 'Auto-approval'],
];

// ---------- shared bits ----------
function subnav(active) {
  return h('nav', { class: 'subnav', 'aria-label': 'Admin sections' },
    SECTIONS.map(([id, label]) => h('a', { href: `#/admin/${id}`, class: id === active ? 'active' : '' }, label)));
}

function page(active, ...nodes) {
  mount(subnav(active), ...nodes);
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

function historyList(entries, empty = 'No changes recorded yet.') {
  if (!entries || !entries.length) return h('p', { class: 'muted small' }, empty);
  return h('ol', { class: 'timeline' }, entries.map((entry) => h('li', {},
    h('div', { class: 'row spread' }, h('strong', {}, HISTORY_LABEL[entry.action] || entry.action), h('span', { class: 'muted small' }, fmtTime(entry.at))),
    h('div', {}, entry.summary),
    h('div', { class: 'muted small' }, `by ${entry.actorEmail || entry.actorId}`))));
}

async function loadDirectory() {
  return api('/admin/users');
}

async function loadConfig() {
  return api('/admin/config');
}

function byId(users) {
  return Object.fromEntries(users.map((u) => [u.userId, u]));
}

function canManage(u) {
  return u.managed && u.status === 'ACTIVE' && (u.role === 'approvers' || u.role === 'admins');
}

function descendantsOf(userId, users) {
  const children = {};
  users.forEach((u) => { if (u.managerId) (children[u.managerId] = children[u.managerId] || []).push(u.userId); });
  const out = new Set();
  const todo = [...(children[userId] || [])];
  while (todo.length) {
    const next = todo.pop();
    if (out.has(next)) continue;
    out.add(next);
    todo.push(...(children[next] || []));
  }
  return out;
}

// Only active approvers and admins can manage people, and never someone in your own reporting tree.
function managerSelect(users, selfId, currentId) {
  const below = selfId ? descendantsOf(selfId, users) : new Set();
  const options = users.filter((u) => canManage(u) && u.userId !== selfId && !below.has(u.userId))
    .sort((a, b) => displayName(a).localeCompare(displayName(b)));
  const select = h('select', {}, h('option', { value: '' }, 'No manager (requests go to admins)'),
    options.map((u) => h('option', { value: u.userId }, `${displayName(u)}${u.name ? ` (${u.email})` : ''}${u.department ? `, ${u.department}` : ''}`)));
  select.value = currentId && options.some((o) => o.userId === currentId) ? currentId : '';
  return select;
}

function todayPlus(days) {
  const d = new Date();
  d.setDate(d.getDate() + days);
  return d.toISOString().slice(0, 10);
}

// ---------- role and module editor ----------
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
  const adminNote = h('p', { class: 'note' }, 'Admins can raise requests in every module, so module choices do not apply.');
  const quick = h('div', { class: 'row small' },
    h('button', { type: 'button', class: 'linkish', onclick: () => { Object.values(moduleInputs).forEach((i) => { i.checked = true; }); } }, 'Select all'),
    h('button', { type: 'button', class: 'linkish', onclick: () => { Object.values(moduleInputs).forEach((i) => { i.checked = false; }); } }, 'Clear'));
  const sync = () => {
    const admin = roleInputs.admins.checked;
    moduleCards.classList.toggle('disabled', admin);
    Object.values(moduleInputs).forEach((i) => { i.disabled = admin; });
    adminNote.hidden = !admin;
    quick.hidden = admin;
  };
  Object.values(roleInputs).forEach((i) => i.addEventListener('change', sync));
  sync();
  return {
    node: h('div', { class: 'access-editor' },
      h('h4', {}, 'Role'), roleCards,
      h('div', { class: 'row spread' }, h('h4', {}, 'Request modules (what they can raise)'), quick),
      moduleCards, adminNote),
    value() {
      return {
        role: Object.keys(roleInputs).find((id) => roleInputs[id].checked),
        modules: Object.keys(moduleInputs).filter((id) => moduleInputs[id].checked),
      };
    },
    setDisabled(disabled) {
      Object.values(roleInputs).forEach((i) => { i.disabled = disabled; });
      if (disabled) Object.values(moduleInputs).forEach((i) => { i.disabled = true; }); else sync();
    },
  };
}

// ---------- users ----------
async function viewUsers() {
  page('users', h('p', { class: 'muted' }, 'Loading users...'));
  const data = await loadDirectory();
  const users = data.items || [];
  const people = byId(users);
  const summary = data.summary || {};
  const search = h('input', { type: 'search', placeholder: 'Search by name, email or department', 'aria-label': 'Search users' });
  const roleFilter = h('select', { 'aria-label': 'Filter by role' }, h('option', { value: '' }, 'All roles'),
    Object.entries(ROLES).map(([id, r]) => h('option', { value: id }, r.label)));
  const statusFilter = h('select', { 'aria-label': 'Filter by status' },
    h('option', { value: '' }, 'Any status'), h('option', { value: 'ACTIVE' }, 'Active'), h('option', { value: 'DISABLED' }, 'Disabled'),
    h('option', { value: 'NO_PROFILE' }, 'No access set up'), h('option', { value: 'INVITED' }, 'Invite pending'),
    h('option', { value: 'NO_MANAGER' }, 'No manager'));
  const tbody = h('tbody');
  const count = h('span', { class: 'muted small' });
  const paint = () => {
    const q = search.value.trim().toLowerCase();
    const shown = users.filter((u) => {
      if (q && !`${u.name || ''} ${u.email || ''} ${u.department || ''}`.toLowerCase().includes(q)) return false;
      if (roleFilter.value && u.role !== roleFilter.value) return false;
      if (statusFilter.value === 'INVITED') return u.cognitoStatus === 'FORCE_CHANGE_PASSWORD';
      if (statusFilter.value === 'NO_MANAGER') return u.status === 'ACTIVE' && !u.managerId;
      if (statusFilter.value && u.status !== statusFilter.value) return false;
      return true;
    });
    count.textContent = `${shown.length} of ${users.length} users`;
    tbody.replaceChildren(...(shown.length ? shown.map((u) => {
      const manager = people[u.managerId];
      const open = () => { window.location.hash = `#/admin/users/${u.userId}`; };
      return h('tr', { class: `clickable${u.status === 'DISABLED' ? ' dim' : ''}`, tabindex: '0', onclick: open,
        onkeydown: (ev) => { if (ev.key === 'Enter') open(); } },
      h('td', {}, h('div', { class: 'person' }, avatar(u),
        h('div', { class: 'person-text' }, h('div', { class: 'title' }, u.name || u.email), u.name ? h('div', { class: 'muted small' }, u.email) : null,
          h('div', { class: 'row tight' },
            u.userId === state.me.userId ? h('span', { class: 'chip' }, 'You') : null,
            u.department ? h('span', { class: 'muted small' }, u.department) : null,
            h('span', { class: 'mobile-only' }, roleBadge(u.role)))))),
      h('td', {}, roleBadge(u.role)),
      h('td', {}, moduleChips(u)),
      h('td', { class: 'small' }, manager ? displayName(manager) : h('span', { class: 'muted' }, u.status === 'ACTIVE' && u.role !== 'admins' ? 'None (admins approve)' : 'None'),
        u.reports ? h('div', { class: 'muted small' }, `${u.reports} direct report${u.reports === 1 ? '' : 's'}`) : null),
      h('td', {}, signInBadge(u.cognitoStatus)),
      h('td', {}, accessBadge(u.status)));
    }) : [h('tr', {}, h('td', { colspan: '6', class: 'empty' }, 'No users match these filters.'))]));
  };
  [search, roleFilter, statusFilter].forEach((el) => el.addEventListener('input', paint));
  page('users',
    h('div', { class: 'section-head' },
      h('div', {}, h('h2', {}, 'Users'), h('p', { class: 'muted small' }, 'Invite people, set their role, modules and manager. Access changes apply on their next click.')),
      h('div', { class: 'row' }, h('button', { class: 'btn', onclick: () => rerender() }, 'Refresh'),
        h('a', { class: 'btn primary', href: '#/admin/users/new' }, 'Invite user'))),
    h('div', { class: 'stats' },
      stat('Users', summary.total ?? users.length), stat('Active', summary.active ?? 0, 'ok'),
      stat('Invites pending', summary.invitesPending ?? 0, 'warn'), stat('Without a manager', summary.noManager ?? 0, 'warn'),
      stat('Admins', summary.admins ?? 0, 'tone-brand')),
    h('div', { class: 'toolbar' }, search, roleFilter, statusFilter, count),
    h('div', { class: 'card flush' }, h('table', { class: 'table users' },
      h('thead', {}, h('tr', {}, h('th', {}, 'User'), h('th', {}, 'Role'), h('th', {}, 'Modules'), h('th', {}, 'Reports to'), h('th', {}, 'Sign-in'), h('th', {}, 'Access'))),
      tbody)));
  paint();
}

async function viewUserNew() {
  page('users', h('p', { class: 'muted' }, 'Loading...'));
  const { items: users } = await loadDirectory();
  const editor = accessEditor({ role: 'requesters', modules: ['GENERAL'] });
  const manager = managerSelect(users, null, null);
  const invite = h('input', { type: 'checkbox', checked: true });
  const form = h('form', { class: 'card form' },
    h('div', { class: 'grid2' },
      h('label', {}, 'Email', h('input', { name: 'email', type: 'email', required: true, maxlength: '254', placeholder: 'name@company.com', autocomplete: 'off' })),
      h('label', {}, 'Full name', h('input', { name: 'name', maxlength: '80', placeholder: 'Optional', autocomplete: 'off' }))),
    h('h4', {}, 'Reporting line'),
    h('div', { class: 'grid3' },
      h('label', {}, 'Manager (level 1 approver)', manager),
      h('label', {}, 'Department', h('input', { name: 'department', maxlength: '60' })),
      h('label', {}, 'Job title', h('input', { name: 'jobTitle', maxlength: '60' }))),
    editor.node,
    h('label', { class: 'check' }, invite, h('span', {}, 'Email the invitation now. Cognito sends a temporary password; the person sets a new one and enrols an authenticator app at first sign-in.')),
    h('div', { class: 'row end' }, h('a', { class: 'btn', href: '#/admin/users' }, 'Cancel'), h('button', { class: 'btn primary', type: 'submit' }, 'Invite user')));
  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const fd = new FormData(form);
    const access = editor.value();
    const payload = {
      email: String(fd.get('email')).trim(), name: String(fd.get('name') || '').trim(), role: access.role,
      modules: access.role === 'admins' ? [] : access.modules, sendInvite: invite.checked,
      managerId: manager.value || null, department: String(fd.get('department') || '').trim(), jobTitle: String(fd.get('jobTitle') || '').trim(),
    };
    if (!payload.managerId && payload.role !== 'admins') {
      const go = await confirmDialog({ title: 'Invite without a manager?', body: 'Level 1 approvals for this person will go to admins until you set a manager.', confirmLabel: 'Invite anyway' });
      if (!go) return;
    }
    if (payload.role === 'admins') {
      const go = await confirmDialog({ title: 'Invite a new admin?', body: `${payload.email} will manage every user and the approval configuration. The ops team is alerted about new admins.`, confirmLabel: 'Invite admin', danger: true });
      if (!go) return;
    }
    const button = form.querySelector('button[type=submit]');
    button.disabled = true;
    try {
      const res = await api('/admin/users', { method: 'POST', body: JSON.stringify(payload) });
      toast(res.invited ? `Invitation emailed to ${payload.email}` : `${payload.email} created. Send the invitation when they are ready.`, 'ok');
      window.location.hash = `#/admin/users/${res.user.userId}`;
    } catch (e) {
      toast(e.message, 'danger');
      button.disabled = false;
    }
  });
  page('users', h('div', { class: 'section-head' }, h('div', {}, h('a', { href: '#/admin/users', class: 'muted small' }, 'Back to users'), h('h2', {}, 'Invite a user'))), form);
}

function autoRulesCard(user, isSelf) {
  const rules = user.autoRules || [];
  const allowed = user.role === 'admins' ? Object.keys(MODULES) : (user.modules || []);
  const list = rules.length
    ? h('table', { class: 'table compact' },
      h('thead', {}, h('tr', {}, h('th', {}, 'Module'), h('th', {}, 'Up to'), h('th', {}, 'Until'), h('th', {}, ''), h('th', {}, ''))),
      h('tbody', {}, rules.map((r) => h('tr', { class: r.active ? '' : 'dim' },
        h('td', {}, (MODULES[r.module] || {}).label || r.module),
        h('td', {}, fmtNumber(r.maxAmount)),
        h('td', {}, r.expiresOn, r.active ? null : h('span', { class: 'badge muted' }, 'expired')),
        h('td', { class: 'muted small' }, r.note || ''),
        h('td', {}, isSelf ? null : h('button', {
          class: 'linkish danger-text',
          onclick: async () => {
            const go = await confirmDialog({ title: `Revoke auto-approval for ${(MODULES[r.module] || {}).label}?`, body: 'Their next request in this module goes through the approval chain.', confirmLabel: 'Revoke', danger: true });
            if (!go) return;
            try {
              await api(`/admin/users/${user.userId}/auto-rules/${r.module}`, { method: 'DELETE' });
              toast('Auto-approval revoked', 'ok');
              rerender();
            } catch (e) { toast(e.message, 'danger'); }
          },
        }, 'Revoke'))))))
    : h('p', { class: 'muted small' }, 'None. Every request this person raises goes through its approval chain.');
  const card = h('div', { class: 'card' }, h('h3', {}, 'Auto-approval'),
    h('p', { class: 'muted small' }, 'Off unless an admin grants it: requests in the module up to the limit are approved without anyone, until the expiry date.'), list);
  if (isSelf) {
    card.append(h('p', { class: 'note' }, 'You cannot grant yourself auto-approval. Another admin has to do it.'));
    return card;
  }
  if (user.status !== 'ACTIVE' || !user.managed || !allowed.length) {
    card.append(h('p', { class: 'note' }, 'Grant modules to this active user first.'));
    return card;
  }
  const module = h('select', {}, allowed.map((m) => h('option', { value: m }, (MODULES[m] || {}).label || m)));
  const limit = h('input', { type: 'number', min: '0', step: 'any', required: true, placeholder: 'e.g. 500' });
  const until = h('input', { type: 'date', required: true, min: todayPlus(1), max: todayPlus(366), value: todayPlus(90) });
  const note = h('input', { maxlength: '200', placeholder: 'Why (optional)' });
  const form = h('form', { class: 'form inline-form' },
    h('div', { class: 'grid4' }, h('label', {}, 'Module', module), h('label', {}, 'Up to (approval value)', limit),
      h('label', {}, 'Until', until), h('label', {}, 'Note', note)),
    h('div', { class: 'row end' }, h('button', { class: 'btn', type: 'submit' }, 'Grant auto-approval')));
  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const go = await confirmDialog({
      title: 'Grant auto-approval?',
      body: `${user.email}'s ${(MODULES[module.value] || {}).label} requests up to ${fmtNumber(limit.value)} will be approved with no approver until ${until.value}. This is recorded in their history.`,
      confirmLabel: 'Grant', danger: true,
    });
    if (!go) return;
    try {
      const res = await api(`/admin/users/${user.userId}/auto-rules/${module.value}`, {
        method: 'PUT', body: JSON.stringify({ maxAmount: Number(limit.value), expiresOn: until.value, note: note.value.trim() }),
      });
      toast(res.message, 'ok');
      rerender();
    } catch (e) { toast(e.message, 'danger'); }
  });
  card.append(form);
  return card;
}

async function viewUserDetail(userId) {
  page('users', h('p', { class: 'muted' }, 'Loading user...'));
  const [{ user }, directory] = await Promise.all([api(`/admin/users/${encodeURIComponent(userId)}`), loadDirectory()]);
  const users = directory.items || [];
  const people = byId(users);
  const isSelf = user.userId === state.me.userId;
  const invited = user.cognitoStatus === 'FORCE_CHANGE_PASSWORD';

  const editor = accessEditor({ role: user.role || 'requesters', modules: user.modules || [] });
  const nameInput = h('input', { value: user.name || '', maxlength: '80', placeholder: 'Full name' });
  const manager = managerSelect(users, user.userId, user.managerId);
  const department = h('input', { value: user.department || '', maxlength: '60' });
  const jobTitle = h('input', { value: user.jobTitle || '', maxlength: '60' });
  const save = h('button', { class: 'btn primary', type: 'submit' }, user.managed ? 'Save' : 'Set up access');
  const accessForm = h('form', { class: 'card form' },
    h('div', { class: 'row spread' }, h('h3', {}, 'Access and reporting line'), h('span', { class: 'muted small' }, 'Takes effect on their next click')),
    !user.managed ? h('p', { class: 'note warn' }, 'This account was created outside Request Hub and has no access profile, so it cannot use the app yet. Choose a role and modules to set it up.') : null,
    h('label', {}, 'Full name', nameInput),
    h('div', { class: 'grid3' }, h('label', {}, 'Manager (level 1 approver)', manager), h('label', {}, 'Department', department), h('label', {}, 'Job title', jobTitle)),
    editor.node,
    isSelf ? h('p', { class: 'note' }, 'You cannot change your own role. Another admin has to do that.') : null,
    h('div', { class: 'row end' }, save));
  if (isSelf) editor.setDisabled(true);
  accessForm.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const access = editor.value();
    const payload = { name: nameInput.value.trim(), managerId: manager.value || null, department: department.value.trim(), jobTitle: jobTitle.value.trim() };
    if (!isSelf) { payload.role = access.role; payload.modules = access.role === 'admins' ? (user.modules || []) : access.modules; }
    if (user.managed && user.version !== undefined && user.version !== null) payload.version = user.version;
    if (!isSelf && access.role === 'admins' && user.role !== 'admins') {
      const go = await confirmDialog({ title: `Make ${user.email} an admin?`, body: 'Admins manage every user and the approval configuration. The ops team is alerted about new admins.', confirmLabel: 'Make admin', danger: true });
      if (!go) return;
    }
    save.disabled = true;
    try {
      const res = await api(`/admin/users/${encodeURIComponent(user.userId)}`, { method: 'PATCH', body: JSON.stringify(payload) });
      toast(res.changed ? 'Saved. It applies immediately.' : 'Nothing changed.', res.changed ? 'ok' : 'info');
      rerender();
    } catch (e) {
      toast(e.message, 'danger');
      save.disabled = false;
    }
  });

  const act = async (action, confirm) => {
    if (confirm && !(await confirmDialog(confirm))) return;
    try {
      const res = await api(`/admin/users/${encodeURIComponent(user.userId)}/actions`, { method: 'POST', body: JSON.stringify({ action }) });
      toast(res.message, 'ok');
      rerender();
    } catch (e) { toast(e.message, 'danger'); }
  };
  const actions = [];
  if (user.status === 'DISABLED') actions.push(h('button', { class: 'btn ok', onclick: () => act('ENABLE') }, 'Enable account'));
  else if (!isSelf) {
    actions.push(h('button', { class: 'btn danger', onclick: () => act('DISABLE', { title: `Disable ${user.email}?`, body: `Sign-in is blocked, sessions are signed out, and API access stops on their next click.${user.reports ? ` ${user.reports} people report to them: their requests go to admins until you set a new manager.` : ''}`, confirmLabel: 'Disable', danger: true }) }, 'Disable account'));
  }
  if (invited) actions.push(h('button', { class: 'btn', onclick: () => act('RESEND_INVITE', { title: 'Resend the invitation?', body: `Cognito emails ${user.email} a new temporary password. The old one stops working.`, confirmLabel: 'Resend' }) }, 'Resend invitation'));
  else if (user.cognitoStatus !== 'MISSING') {
    actions.push(h('button', { class: 'btn', onclick: () => act('RESET_PASSWORD', { title: 'Reset the password?', body: `Cognito emails ${user.email} a verification code. They choose a new password at their next sign-in.`, confirmLabel: 'Reset password' }) }, 'Reset password'));
    actions.push(h('button', { class: 'btn', onclick: () => act('RESET_MFA', { title: 'Reset the authenticator?', body: 'Use this when someone loses their phone. They are signed out and enrol a new authenticator app at their next sign-in.', confirmLabel: 'Reset authenticator' }) }, 'Reset authenticator'));
  }
  if (user.cognitoStatus !== 'MISSING') actions.push(h('button', { class: 'btn', onclick: () => act('SIGN_OUT', { title: 'Sign out everywhere?', body: 'Revokes every refresh token for this user.', confirmLabel: 'Sign out everywhere' }) }, 'Sign out everywhere'));

  const managerUser = people[user.managerId];
  const account = h('div', { class: 'card' }, h('h3', {}, 'Account'),
    h('dl', { class: 'facts' },
      field('Sign-in', SIGN_IN[user.cognitoStatus] ? SIGN_IN[user.cognitoStatus][0] : user.cognitoStatus),
      field('MFA', user.mfa),
      field('Reports to', managerUser ? displayName(managerUser) : 'Nobody (admins approve level 1)'),
      field('Direct reports', (user.directReports || []).length ? user.directReports.map((r) => r.name || r.email).join(', ') : 'None'),
      field('Approver groups', (user.approverGroups || []).length ? user.approverGroups.map((g) => g.name).join(', ') : 'None'),
      field('Created', fmtTime(user.createdAt)),
      field('Last change', user.updatedAt ? `${fmtTime(user.updatedAt)}${user.updatedByEmail ? ` by ${user.updatedByEmail}` : ''}` : null)),
    h('div', { class: 'row' }, actions));
  const danger = isSelf ? null : h('div', { class: 'card danger-zone' }, h('h3', {}, 'Delete user'),
    h('p', { class: 'muted small' }, 'Removes the sign-in, the access profile and any auto-approval. Their requests and this history are kept. People who report to them or approver group seats must be reassigned first.'),
    h('button', {
      class: 'btn danger',
      onclick: async () => {
        const go = await confirmDialog({ title: `Delete ${user.email}?`, body: 'This cannot be undone. You would need to invite them again.', confirmLabel: 'Delete user', danger: true, typeToConfirm: user.email });
        if (!go) return;
        try {
          await api(`/admin/users/${encodeURIComponent(user.userId)}`, { method: 'DELETE' });
          toast(`${user.email} deleted`, 'ok');
          window.location.hash = '#/admin/users';
        } catch (e) { toast(e.message, 'danger'); }
      },
    }, 'Delete user'));

  page('users',
    h('div', { class: 'section-head' },
      h('div', {}, h('a', { href: '#/admin/users', class: 'muted small' }, 'Back to users'),
        h('div', { class: 'person big' }, avatar(user), h('div', {}, h('h2', {}, user.name || user.email),
          h('div', { class: 'muted' }, [user.name ? user.email : null, user.jobTitle, user.department].filter(Boolean).join(' · '))))),
      h('div', { class: 'row' }, roleBadge(user.role), accessBadge(user.status), signInBadge(user.cognitoStatus))),
    h('div', { class: 'split' },
      h('div', {}, accessForm, autoRulesCard(user, isSelf)),
      h('div', {}, account, h('div', { class: 'card' }, h('h3', {}, 'Access history'), historyList(user.history)), danger)));
}

// ---------- org chart and CSV import ----------
function parseCsv(text) {
  const rows = [];
  let row = [];
  let cell = '';
  let quoted = false;
  for (let i = 0; i < text.length; i += 1) {
    const c = text[i];
    if (quoted) {
      if (c === '"' && text[i + 1] === '"') { cell += '"'; i += 1; } else if (c === '"') quoted = false; else cell += c;
    } else if (c === '"') quoted = true;
    else if (c === ',') { row.push(cell); cell = ''; }
    else if (c === '\n' || c === '\r') {
      if (c === '\r' && text[i + 1] === '\n') i += 1;
      row.push(cell); cell = '';
      if (row.some((v) => v.trim())) rows.push(row);
      row = [];
    } else cell += c;
  }
  row.push(cell);
  if (row.some((v) => v.trim())) rows.push(row);
  if (!rows.length) return [];
  const header = rows[0].map((x) => x.trim().toLowerCase().replace(/[^a-z]/g, ''));
  const col = (names) => header.findIndex((x) => names.includes(x));
  const idx = { email: col(['email', 'useremail']), managerEmail: col(['manageremail', 'manager', 'reportsto']), department: col(['department', 'dept']), jobTitle: col(['jobtitle', 'title']) };
  if (idx.email < 0) throw new Error('The first row must be a header with at least an "email" column');
  return rows.slice(1).map((r) => {
    const out = { email: (r[idx.email] || '').trim(), managerEmail: idx.managerEmail >= 0 ? (r[idx.managerEmail] || '').trim() : '' };
    if (idx.department >= 0) out.department = (r[idx.department] || '').trim();
    if (idx.jobTitle >= 0) out.jobTitle = (r[idx.jobTitle] || '').trim();
    return out;
  });
}

function importPanel() {
  const area = h('textarea', { rows: '6', placeholder: 'email,managerEmail,department,jobTitle\nana@company.com,priya@company.com,Finance,Finance lead' });
  const file = h('input', { type: 'file', accept: '.csv,text/csv' });
  const results = h('div');
  const check = h('button', { class: 'btn', type: 'button' }, 'Check');
  const apply = h('button', { class: 'btn primary', type: 'button', disabled: true }, 'Apply');
  let rows = [];
  file.addEventListener('change', async () => { if (file.files[0]) area.value = await file.files[0].text(); });
  const run = async (dryRun) => {
    try {
      rows = parseCsv(area.value);
      if (!rows.length) { toast('Paste or choose a CSV with at least one row', 'warn'); return; }
      const res = await fetch(`${state.cfg.apiBase}/admin/users/import`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: state.idToken },
        body: JSON.stringify({ rows, dryRun }),
      });
      const body = await res.json();
      if (!res.ok && res.status !== 422) throw new Error(body.message || `Import failed (${res.status})`);
      results.replaceChildren(
        h('p', { class: body.results.every((r) => r.ok) ? 'note ok' : 'note warn' }, body.message),
        h('table', { class: 'table compact' }, h('thead', {}, h('tr', {}, h('th', {}, 'Row'), h('th', {}, 'Email'), h('th', {}, 'Manager'), h('th', {}, 'Result'))),
          h('tbody', {}, body.results.map((r) => h('tr', {}, h('td', {}, String(r.row)), h('td', {}, r.email), h('td', {}, r.managerEmail || 'none'),
            h('td', { class: r.ok ? 'ok-text' : 'danger-text' }, r.ok ? 'OK' : r.error))))));
      apply.disabled = !(dryRun && body.results.every((r) => r.ok));
      if (!dryRun && res.ok) { toast(body.message, 'ok'); setTimeout(() => rerender(), 800); }
    } catch (e) { toast(e.message, 'danger'); }
  };
  check.addEventListener('click', () => run(true));
  apply.addEventListener('click', () => run(false));
  return h('div', { class: 'card' }, h('h3', {}, 'Import reporting lines from CSV'),
    h('p', { class: 'muted small' }, 'Columns: email, managerEmail, department, jobTitle. People must already exist. Everything is checked first (unknown people, managers who are not approvers, and loops across rows); nothing is saved until you apply.'),
    h('div', { class: 'row' }, file), area, h('div', { class: 'row end' }, check, apply), results);
}

async function viewOrg() {
  page('org', h('p', { class: 'muted' }, 'Loading the org chart...'));
  const { items } = await loadDirectory();
  const users = items.filter((u) => u.managed && u.status !== 'DELETED');
  const people = byId(users);
  const children = {};
  const roots = [];
  users.forEach((u) => {
    if (u.managerId && people[u.managerId]) (children[u.managerId] = children[u.managerId] || []).push(u);
    else roots.push(u);
  });
  const sortPeople = (list) => list.sort((a, b) => displayName(a).localeCompare(displayName(b)));
  const nodeFor = (u, depth = 0) => {
    const kids = sortPeople(children[u.userId] || []);
    return h('li', {},
      h('a', { class: `org-card${u.status === 'DISABLED' ? ' dim' : ''}`, href: `#/admin/users/${u.userId}` },
        avatar(u), h('span', { class: 'org-text' }, h('strong', {}, displayName(u)),
          h('span', { class: 'muted small' }, [u.jobTitle, u.department].filter(Boolean).join(' · ') || u.email)),
        roleBadge(u.role), kids.length ? h('span', { class: 'chip' }, `${kids.length}`) : null),
      kids.length && depth < 12 ? h('ul', {}, kids.map((k) => nodeFor(k, depth + 1))) : null);
  };
  const unmanagedRequesters = roots.filter((u) => u.status === 'ACTIVE' && u.role !== 'admins');
  page('org',
    h('div', { class: 'section-head' }, h('div', {}, h('h2', {}, 'Org chart'),
      h('p', { class: 'muted small' }, 'Level 1 of every approval is the requester\'s manager here, so an approver\'s own requests go to their manager. People at the top fall back to admins.'))),
    unmanagedRequesters.length ? h('p', { class: 'note warn' }, `${unmanagedRequesters.length} active non-admin user(s) have no manager, so admins approve their requests: ${unmanagedRequesters.map(displayName).join(', ')}.`) : null,
    h('div', { class: 'card org' }, h('ul', { class: 'org-tree' }, sortPeople(roots).map((u) => nodeFor(u)))),
    importPanel());
}

// ---------- approver groups ----------
async function viewGroups() {
  page('groups', h('p', { class: 'muted' }, 'Loading groups...'));
  const [config, directory] = await Promise.all([loadConfig(), loadDirectory()]);
  const users = directory.items || [];
  const people = byId(users);
  const eligible = users.filter(canManage).sort((a, b) => displayName(a).localeCompare(displayName(b)));
  const usage = {};
  Object.values(config.workflows).forEach((w) => (w.levels || []).forEach((l) => {
    if (l.groupId) (usage[l.groupId] = usage[l.groupId] || []).push((MODULES[w.module] || {}).label || w.module);
  }));

  const editor = (group) => {
    const name = h('input', { value: group ? group.name : '', maxlength: '60', required: true });
    const description = h('input', { value: group ? group.description || '' : '', maxlength: '200' });
    const picks = {};
    const members = h('div', { class: 'choice-grid members' }, eligible.map((u) => {
      const input = h('input', { type: 'checkbox', checked: group ? (group.members || []).includes(u.userId) : false });
      picks[u.userId] = input;
      return h('label', { class: 'choice' }, input, h('span', { class: 'choice-body' }, h('strong', {}, displayName(u)), h('span', { class: 'muted small' }, [(ROLES[u.role] || {}).label, u.department].filter(Boolean).join(' · '))));
    }));
    const form = h('form', { class: 'form' },
      h('div', { class: 'grid2' }, h('label', {}, 'Name', name), h('label', {}, 'Description', description)),
      h('h4', {}, 'Members (active Approvers and Admins)'), eligible.length ? members : h('p', { class: 'muted small' }, 'Nobody is eligible yet. Make someone an Approver first.'),
      h('div', { class: 'row end' }, h('button', { class: 'btn primary', type: 'submit' }, group && !group.unsaved ? 'Save group' : 'Create group')));
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const body = { name: name.value.trim(), description: description.value.trim(), members: Object.keys(picks).filter((id) => picks[id].checked) };
      if (group && group.unsaved) body.groupId = group.groupId;
      if (group && !group.unsaved) body.version = group.version;
      try {
        const res = group && !group.unsaved
          ? await api(`/admin/groups/${group.groupId}`, { method: 'PUT', body: JSON.stringify(body) })
          : await api('/admin/groups', { method: 'POST', body: JSON.stringify(body) });
        toast(res.message, 'ok');
        rerender();
      } catch (e) { toast(e.message, 'danger'); }
    });
    return form;
  };

  const cards = config.groups.map((g) => {
    const body = h('div', { class: 'group-body' });
    const view = () => body.replaceChildren(
      h('p', { class: 'muted small' }, g.description || ''),
      (g.members || []).length
        ? h('div', { class: 'chips' }, g.members.map((m) => h('span', { class: 'chip' }, displayName(people[m] || { email: 'removed user' }))))
        : h('p', { class: 'note warn' }, 'No members: levels that use this group fall back to admins.'),
      h('p', { class: 'muted small' }, usage[g.groupId] ? `Used by: ${usage[g.groupId].join(', ')}` : 'Not used by any workflow'));
    view();
    const edit = h('button', { class: 'btn', onclick: () => { body.replaceChildren(editor(g)); edit.hidden = true; } }, g.unsaved ? 'Create' : 'Edit');
    const del = g.unsaved ? null : h('button', {
      class: 'btn danger',
      onclick: async () => {
        if (!(await confirmDialog({ title: `Delete ${g.name}?`, body: 'Workflows that use this group must be changed first.', confirmLabel: 'Delete', danger: true }))) return;
        try { await api(`/admin/groups/${g.groupId}`, { method: 'DELETE' }); toast(`${g.name} deleted`, 'ok'); rerender(); } catch (e) { toast(e.message, 'danger'); }
      },
    }, 'Delete');
    return h('div', { class: 'card' }, h('div', { class: 'row spread' }, h('h3', {}, g.name, g.unsaved ? h('span', { class: 'badge warn' }, 'not created yet') : null), h('div', { class: 'row' }, edit, del)), body);
  });
  const addHost = h('div');
  page('groups',
    h('div', { class: 'section-head' },
      h('div', {}, h('h2', {}, 'Approver groups'), h('p', { class: 'muted small' }, 'Fixed groups such as Finance or a change board. A workflow level can require any member of a group to approve. The requester and anyone who already approved an earlier level are never counted.')),
      h('button', { class: 'btn primary', onclick: () => addHost.replaceChildren(h('div', { class: 'card' }, h('h3', {}, 'New approver group'), editor(null))) }, 'New group')),
    addHost, h('div', { class: 'cards wide' }, cards));
}

// ---------- workflows ----------
async function viewWorkflows(moduleId) {
  page('workflows', h('p', { class: 'muted' }, 'Loading workflows...'));
  const config = await loadConfig();
  const module = MODULES[moduleId] ? moduleId : 'PURCHASE';
  const wf = config.workflows[module];
  const form = config.forms[module];
  const approvalField = (form.fields || []).find((f) => f.key === form.approvalField);
  const groupName = (id) => (config.groups.find((g) => g.groupId === id) || {}).name || id;
  let levels = (wf.levels || []).map((l) => ({ ...l }));
  const host = h('div', { class: 'levels' });
  const sentence = h('p', { class: 'note' });
  const describe = (l) => {
    const who = l.kind === 'MANAGER' ? 'the requester\'s line manager' : l.kind === 'GROUP' ? `any member of ${groupName(l.groupId)}` : 'any admin';
    return Number(l.minAmount) > 0 ? `${who} when ${approvalField ? approvalField.label : 'the value'} is ${fmtNumber(l.minAmount)} or more` : who;
  };
  const paint = () => {
    sentence.textContent = `Every ${MODULES[module].label} request needs ${levels.map(describe).join(', then ')}.`;
    host.replaceChildren(...levels.map((l, i) => {
      const kind = h('select', { 'aria-label': `Level ${i + 1} approver` },
        h('option', { value: 'MANAGER' }, 'Line manager'), h('option', { value: 'GROUP' }, 'Approver group'), h('option', { value: 'ADMINS' }, 'Admins'));
      kind.value = l.kind;
      const group = h('select', { 'aria-label': `Level ${i + 1} group` }, config.groups.map((g) => h('option', { value: g.groupId }, g.name)));
      group.value = l.groupId || (config.groups[0] || {}).groupId || '';
      group.hidden = l.kind !== 'GROUP';
      const threshold = h('input', { type: 'number', min: '0', step: 'any', value: l.minAmount || 0, disabled: !approvalField || i === 0, 'aria-label': `Level ${i + 1} threshold` });
      kind.addEventListener('change', () => { l.kind = kind.value; if (l.kind === 'GROUP') l.groupId = group.value; else delete l.groupId; paint(); });
      group.addEventListener('change', () => { l.groupId = group.value; paint(); });
      threshold.addEventListener('input', () => { l.minAmount = Number(threshold.value || 0); sentence.textContent = `Every ${MODULES[module].label} request needs ${levels.map(describe).join(', then ')}.`; });
      const move = (d) => { const j = i + d; if (j < 0 || j >= levels.length) return; [levels[i], levels[j]] = [levels[j], levels[i]]; if (levels[0]) levels[0].minAmount = 0; paint(); };
      return h('div', { class: 'level-row' },
        h('span', { class: 'level-no' }, `L${i + 1}`),
        h('div', { class: 'level-who' }, kind, group),
        h('label', { class: 'level-threshold' }, h('span', { class: 'small muted' }, i === 0 ? 'Applies to every request' : `When ${approvalField ? approvalField.label : 'value'} is at least`), i === 0 ? null : threshold),
        h('div', { class: 'row tight' },
          h('button', { type: 'button', class: 'btn icon', title: 'Move up', 'aria-label': 'Move up', disabled: i === 0, onclick: () => move(-1) }, '↑'),
          h('button', { type: 'button', class: 'btn icon', title: 'Move down', 'aria-label': 'Move down', disabled: i === levels.length - 1, onclick: () => move(1) }, '↓'),
          h('button', { type: 'button', class: 'btn icon danger', title: 'Remove level', 'aria-label': 'Remove level', disabled: levels.length === 1, onclick: () => { levels.splice(i, 1); if (levels[0]) levels[0].minAmount = 0; paint(); } }, '×')));
    }));
  };
  paint();
  const sla = h('input', { type: 'number', min: '1', max: '10080', value: Math.round((wf.slaSeconds || 180) / 60) });
  const esc = h('input', { type: 'number', min: '1', max: '10080', value: Math.round((wf.escalationSeconds || 180) / 60) });
  const add = h('button', { type: 'button', class: 'btn', onclick: () => { if (levels.length >= 5) { toast('At most 5 levels', 'warn'); return; } const unused = config.groups.find((g) => !levels.some((l) => l.groupId === g.groupId));
    levels.push(unused ? { kind: 'GROUP', groupId: unused.groupId, minAmount: 0 } : { kind: 'ADMINS', minAmount: 0 }); paint(); } }, 'Add level');
  const save = h('button', { class: 'btn primary', type: 'button' }, 'Save workflow');
  save.addEventListener('click', async () => {
    const body = { levels: levels.map((l) => ({ kind: l.kind, minAmount: Number(l.minAmount || 0), ...(l.kind === 'GROUP' ? { groupId: l.groupId } : {}) })),
      slaSeconds: Math.round(Number(sla.value) * 60), escalationSeconds: Math.round(Number(esc.value) * 60) };
    if (!wf.unsaved) body.version = wf.version;
    try {
      const res = await api(`/admin/workflows/${module}`, { method: 'PUT', body: JSON.stringify(body) });
      toast(res.message, 'ok');
      rerender();
    } catch (e) { toast(e.message, 'danger'); }
  });
  const tabs = h('div', { class: 'pills' }, Object.entries(MODULES).map(([id, m]) => h('a', { href: `#/admin/workflows/${id}`, class: id === module ? 'active' : '' }, m.label)));
  const history = (config.history || []).filter((e) => e.subject === module && e.action === 'WORKFLOW_SAVED');
  page('workflows',
    h('div', { class: 'section-head' }, h('div', {}, h('h2', {}, 'Approval workflows'),
      h('p', { class: 'muted small' }, 'Level 1 is the requester\'s line manager; later levels are approver groups or admins, each switched on by a threshold on the form\'s approval value. Nothing is auto-approved unless you grant someone auto-approval.'))),
    tabs,
    h('div', { class: 'split' },
      h('div', {}, h('div', { class: 'card' },
        h('div', { class: 'row spread' }, h('h3', {}, `${MODULES[module].label} approval levels`),
          h('span', { class: 'muted small' }, wf.unsaved ? 'Default, not saved yet' : `Version ${wf.version}${wf.updatedByEmail ? `, saved by ${wf.updatedByEmail}` : ''}`)),
        approvalField ? h('p', { class: 'muted small' }, `Thresholds compare against "${approvalField.label}" on the ${MODULES[module].label} form.`)
          : h('p', { class: 'note warn' }, 'This module\'s form has no approval value field, so every level applies to every request. Set one in Forms to use thresholds.'),
        host, h('div', { class: 'row' }, add), sentence,
        h('div', { class: 'grid2' }, h('label', { class: 'stack' }, 'Minutes per level before escalating to admins', sla), h('label', { class: 'stack' }, 'Minutes admins have before the request expires', esc)),
        h('div', { class: 'row end' }, save))),
      h('div', {}, h('div', { class: 'card' }, h('h3', {}, 'Rules every chain follows'),
        h('ul', { class: 'rules' },
          h('li', {}, 'Nobody approves their own request.'),
          h('li', {}, 'The same person cannot approve two levels of one request.'),
          h('li', {}, 'An approver\'s own request goes to their manager, like everyone else\'s.'),
          h('li', {}, 'If a level has nobody eligible (no manager, empty group, disabled people), admins stand in.'),
          h('li', {}, 'Changes apply to new requests. Requests already in flight keep the chain they started with.'))),
      h('div', { class: 'card' }, h('h3', {}, 'Recent changes'), historyList(history, 'No saved versions yet.')))));
}

// ---------- form builder ----------
function slug(label) {
  const words = String(label || '').replace(/[^A-Za-z0-9 ]/g, ' ').trim().split(/\s+/).filter(Boolean);
  if (!words.length) return '';
  const key = words.map((w, i) => (i === 0 ? w.toLowerCase() : w[0].toUpperCase() + w.slice(1).toLowerCase())).join('');
  return /^[a-z]/.test(key) ? key.slice(0, 40) : `f${key}`.slice(0, 40);
}

async function viewForms(moduleId) {
  page('forms', h('p', { class: 'muted' }, 'Loading forms...'));
  const config = await loadConfig();
  const module = MODULES[moduleId] ? moduleId : 'PURCHASE';
  const saved = config.forms[module];
  let fields = (saved.fields || []).map((f) => ({ ...f, options: f.options ? [...f.options] : undefined }));
  let approvalField = saved.approvalField || '';
  let dirty = false;
  const list = h('div', { class: 'builder-list' });
  const preview = h('div', { class: 'dyn-grid preview' });
  const approvalSelect = h('select');
  const status = h('span', { class: 'muted small' });

  const markDirty = () => { dirty = true; status.textContent = 'Unsaved changes'; status.className = 'warn-text small'; };
  const paintPreview = () => {
    preview.replaceChildren(h('div', { class: 'dyn-field' }, h('label', {}, 'Title', h('span', { class: 'req' }, ' *')), h('input', { tabindex: '-1', placeholder: 'Always asked' })),
      ...fields.filter((f) => f.label).map((f) => renderField(f, { preview: true }).node));
    const numeric = fields.filter((f) => f.type === 'number' || f.type === 'amount');
    approvalSelect.replaceChildren(h('option', { value: '' }, 'None (thresholds not used)'), ...numeric.map((f) => h('option', { value: f.key }, f.label || f.key)));
    if (!numeric.some((f) => f.key === approvalField)) approvalField = '';
    approvalSelect.value = approvalField;
  };
  approvalSelect.addEventListener('change', () => { approvalField = approvalSelect.value; markDirty(); });

  const paintList = () => {
    list.replaceChildren(...fields.map((f, i) => {
      const label = h('input', { value: f.label || '', maxlength: '60', placeholder: 'Label people see' });
      const key = h('input', { value: f.key || '', maxlength: '40', class: 'mono', placeholder: 'key' });
      const type = h('select', {}, FIELD_TYPES.map(([id, text]) => h('option', { value: id }, text)));
      type.value = f.type || 'text';
      const required = h('input', { type: 'checkbox', checked: Boolean(f.required) });
      const help = h('input', { value: f.help || '', maxlength: '160', placeholder: 'Help text (optional)' });
      const extras = h('div', { class: 'grid3 extras' });
      const paintExtras = () => {
        extras.replaceChildren();
        if (f.type === 'text' || f.type === 'textarea') {
          const max = h('input', { type: 'number', min: '1', value: f.maxLength || (f.type === 'text' ? 120 : 2000) });
          max.addEventListener('input', () => { f.maxLength = Number(max.value); markDirty(); });
          extras.append(h('label', {}, 'Max characters', max));
        }
        if (f.type === 'number' || f.type === 'amount') {
          const min = h('input', { type: 'number', step: 'any', value: f.min ?? 0 });
          const max = h('input', { type: 'number', step: 'any', value: f.max ?? 1000000 });
          min.addEventListener('input', () => { f.min = Number(min.value); markDirty(); paintPreview(); });
          max.addEventListener('input', () => { f.max = Number(max.value); markDirty(); paintPreview(); });
          extras.append(h('label', {}, 'Minimum', min), h('label', {}, 'Maximum', max));
        }
        if (f.type === 'select') {
          const options = h('textarea', { rows: '3', placeholder: 'One option per line' });
          options.value = (f.options || []).join('\n');
          options.addEventListener('input', () => { f.options = options.value.split('\n').map((x) => x.trim()).filter(Boolean); markDirty(); paintPreview(); });
          extras.append(h('label', { class: 'span3' }, 'Options', options));
        }
      };
      paintExtras();
      label.addEventListener('input', () => {
        if (!f.key || f.autoKey) { f.key = slug(label.value); f.autoKey = true; key.value = f.key; }
        f.label = label.value; markDirty(); paintPreview();
      });
      key.addEventListener('input', () => { f.key = key.value.trim(); f.autoKey = false; markDirty(); paintPreview(); });
      type.addEventListener('change', () => { f.type = type.value; if (f.type === 'select' && !f.options) f.options = ['Option 1']; markDirty(); paintExtras(); paintPreview(); });
      required.addEventListener('change', () => { f.required = required.checked; markDirty(); paintPreview(); });
      help.addEventListener('input', () => { f.help = help.value; markDirty(); paintPreview(); });
      const move = (d) => { const j = i + d; if (j < 0 || j >= fields.length) return; [fields[i], fields[j]] = [fields[j], fields[i]]; markDirty(); paintList(); paintPreview(); };
      return h('div', { class: 'builder-field' },
        h('div', { class: 'row spread' }, h('strong', {}, `${i + 1}. ${f.label || 'New field'}`),
          h('div', { class: 'row tight' },
            h('button', { type: 'button', class: 'btn icon', 'aria-label': 'Move up', disabled: i === 0, onclick: () => move(-1) }, '↑'),
            h('button', { type: 'button', class: 'btn icon', 'aria-label': 'Move down', disabled: i === fields.length - 1, onclick: () => move(1) }, '↓'),
            h('button', { type: 'button', class: 'btn icon danger', 'aria-label': 'Remove field', onclick: () => { fields.splice(i, 1); markDirty(); paintList(); paintPreview(); } }, '×'))),
        h('div', { class: 'grid3' }, h('label', {}, 'Label', label), h('label', {}, 'Type', type), h('label', {}, 'Key', key)),
        h('div', { class: 'grid2' }, h('label', {}, 'Help text', help), h('label', { class: 'check inline' }, required, h('span', {}, 'Required'))),
        extras);
    }));
  };
  paintList();
  paintPreview();
  status.textContent = saved.unsaved ? 'Default, not saved yet' : `Version ${saved.version}${saved.updatedByEmail ? `, saved by ${saved.updatedByEmail}` : ''}`;

  const add = h('button', { type: 'button', class: 'btn', onclick: () => { if (fields.length >= 20) { toast('At most 20 fields', 'warn'); return; } fields.push({ key: '', label: '', type: 'text', required: false, autoKey: true }); markDirty(); paintList(); paintPreview(); } }, 'Add field');
  const save = h('button', { type: 'button', class: 'btn primary' }, 'Save new version');
  save.addEventListener('click', async () => {
    const body = { fields: fields.map(({ autoKey, ...f }) => f), approvalField: approvalField || null };
    if (!saved.unsaved) body.version = saved.version;
    try {
      const res = await api(`/admin/forms/${module}`, { method: 'PUT', body: JSON.stringify(body) });
      dirty = false;
      toast(res.message, 'ok');
      rerender();
    } catch (e) { toast(e.message, 'danger'); }
  });
  const tabs = h('div', { class: 'pills' }, Object.entries(MODULES).map(([id, m]) => h('a', {
    href: `#/admin/forms/${id}`, class: id === module ? 'active' : '',
    onclick: (ev) => { if (dirty && !window.confirm('Discard unsaved changes to this form?')) ev.preventDefault(); },
  }, m.label)));
  page('forms',
    h('div', { class: 'section-head' }, h('div', {}, h('h2', {}, 'Request forms'),
      h('p', { class: 'muted small' }, 'Each module has its own fields. Requesters only see the fields of the module they pick. Saving creates a new version; requests already submitted keep the version they used.'))),
    tabs,
    h('div', { class: 'split builder' },
      h('div', {}, h('div', { class: 'card form' },
        h('div', { class: 'row spread' }, h('h3', {}, `${MODULES[module].label} fields`), status),
        list, h('div', { class: 'row' }, add),
        h('label', {}, 'Approval value (drives workflow thresholds and auto-approval limits)', approvalSelect),
        h('div', { class: 'row end' }, save))),
      h('div', {}, h('div', { class: 'card sticky' }, h('h3', {}, 'Preview'), h('p', { class: 'muted small' }, 'What a requester sees for this module.'), preview))));
}

// ---------- auto-approval overview ----------
async function viewAutoRules() {
  page('auto-approval', h('p', { class: 'muted' }, 'Loading auto-approval rules...'));
  const [config, directory] = await Promise.all([loadConfig(), loadDirectory()]);
  const users = (directory.items || []).filter((u) => u.managed && u.status === 'ACTIVE' && u.userId !== state.me.userId);
  const rules = config.autoRules || [];
  const who = h('select', {}, users.map((u) => h('option', { value: u.userId }, displayName(u))));
  const module = h('select');
  const syncModules = () => {
    const u = users.find((x) => x.userId === who.value) || {};
    const allowed = u.role === 'admins' ? Object.keys(MODULES) : (u.modules || []);
    module.replaceChildren(...(allowed.length ? allowed.map((m) => h('option', { value: m }, (MODULES[m] || {}).label || m)) : [h('option', { value: '' }, 'No modules')]));
  };
  who.addEventListener('change', syncModules);
  syncModules();
  const limit = h('input', { type: 'number', min: '0', step: 'any', required: true, placeholder: 'e.g. 500' });
  const until = h('input', { type: 'date', required: true, min: todayPlus(1), max: todayPlus(366), value: todayPlus(90) });
  const note = h('input', { maxlength: '200', placeholder: 'Why (optional)' });
  const form = h('form', { class: 'card form' }, h('h3', {}, 'Grant auto-approval'),
    h('div', { class: 'grid4' }, h('label', {}, 'Person', who), h('label', {}, 'Module', module), h('label', {}, 'Up to (approval value)', limit), h('label', {}, 'Until', until)),
    h('label', {}, 'Note', note),
    h('div', { class: 'row end' }, h('button', { class: 'btn primary', type: 'submit', disabled: !users.length }, 'Grant')));
  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    if (!module.value) { toast('That person has no modules', 'warn'); return; }
    const u = users.find((x) => x.userId === who.value);
    const go = await confirmDialog({ title: 'Grant auto-approval?', body: `${displayName(u)}'s ${(MODULES[module.value] || {}).label} requests up to ${fmtNumber(limit.value)} will be approved with no approver until ${until.value}.`, confirmLabel: 'Grant', danger: true });
    if (!go) return;
    try {
      const res = await api(`/admin/users/${who.value}/auto-rules/${module.value}`, { method: 'PUT', body: JSON.stringify({ maxAmount: Number(limit.value), expiresOn: until.value, note: note.value.trim() }) });
      toast(res.message, 'ok');
      rerender();
    } catch (e) { toast(e.message, 'danger'); }
  });
  const table = rules.length ? h('div', { class: 'card flush' }, h('table', { class: 'table' },
    h('thead', {}, h('tr', {}, h('th', {}, 'Person'), h('th', {}, 'Module'), h('th', {}, 'Up to'), h('th', {}, 'Until'), h('th', {}, 'Granted by'), h('th', {}, ''))),
    h('tbody', {}, rules.map((r) => h('tr', { class: r.active ? '' : 'dim' },
      h('td', {}, h('a', { href: `#/admin/users/${r.userId}` }, r.name || r.email)),
      h('td', {}, (MODULES[r.module] || {}).label || r.module),
      h('td', {}, fmtNumber(r.maxAmount)),
      h('td', {}, r.expiresOn, ' ', r.active ? null : h('span', { class: 'badge muted' }, 'expired')),
      h('td', { class: 'muted small' }, `${r.createdByEmail || ''} ${fmtTime(r.createdAt)}`),
      h('td', {}, h('button', {
        class: 'linkish danger-text',
        onclick: async () => {
          if (!(await confirmDialog({ title: 'Revoke this auto-approval?', body: `${r.email}'s next ${(MODULES[r.module] || {}).label} request goes through approval.`, confirmLabel: 'Revoke', danger: true }))) return;
          try { await api(`/admin/users/${r.userId}/auto-rules/${r.module}`, { method: 'DELETE' }); toast('Revoked', 'ok'); rerender(); } catch (e) { toast(e.message, 'danger'); }
        },
      }, 'Revoke')))))))
    : h('div', { class: 'card empty' }, 'No auto-approval rules. Every request goes through its approval chain.');
  page('auto-approval',
    h('div', { class: 'section-head' }, h('div', {}, h('h2', {}, 'Auto-approval'),
      h('p', { class: 'muted small' }, 'Off by default. A rule lets one person\'s requests in one module skip approval up to a limit, until an expiry date (at most a year). Nobody can grant one to themselves, and every grant and revocation is in the person\'s history.'))),
    form, table);
}

// ---------- router ----------
export async function adminRoute(parts) {
  if (!isAdmin()) { window.location.hash = '#/mine'; return; }
  const [section, a] = parts;
  if (!section || section === 'users') {
    if (a === 'new') return viewUserNew();
    if (a) return viewUserDetail(a);
    return viewUsers();
  }
  if (section === 'org') return viewOrg();
  if (section === 'groups') return viewGroups();
  if (section === 'workflows') return viewWorkflows(a);
  if (section === 'forms') return viewForms(a);
  if (section === 'auto-approval') return viewAutoRules();
  window.location.hash = '#/admin/users';
  return undefined;
}

