// Admin area: users and access, org hierarchy, approver groups, workflows, form builder, auto-approval.
import {
  state, MODULES, ROLES, MODULE_ICON, h, icon, toast, mount, rerender, fmtTime, fmtNumber, fmtDate, field, api, cachedApi, clearCache,
  isAdmin, confirmDialog, avatar, displayName, renderField, note, button, linkButton, iconButton, busy, formField, textInput,
  searchInput, choiceCard, checkbox, select, datePicker, toIso, pageHead, card, fileDrop, stat, skeleton, emptyState, timeAgo,
} from './core.js';

const SIGN_IN = {
  FORCE_CHANGE_PASSWORD: ['Invite pending', 'warn'],
  CONFIRMED: ['Has signed in', 'ok'],
  RESET_REQUIRED: ['Password reset pending', 'warn'],
  UNCONFIRMED: ['Unconfirmed', 'warn'],
  MISSING: ['Missing sign-in', 'danger'],
};
const ACCESS_TONE = { ACTIVE: 'ok', DISABLED: 'danger', NO_PROFILE: 'warn' };
const ACCESS_LABEL = { ACTIVE: 'Active', DISABLED: 'Disabled', NO_PROFILE: 'No access set up' };
const HISTORY_LABEL = {
  CREATED: 'Invited', ACCESS_CHANGED: 'Access changed', ACCESS_SET_UP: 'Access set up', DISABLE: 'Disabled',
  ENABLE: 'Enabled', RESET_PASSWORD: 'Password reset', RESEND_INVITE: 'Invitation sent again',
  RESET_MFA: 'Authenticator reset', SIGN_OUT: 'Signed out everywhere', DELETED: 'Deleted',
  AUTO_RULE_SET: 'Auto-approval granted', AUTO_RULE_REVOKED: 'Auto-approval revoked',
  FORM_SAVED: 'Form saved', WORKFLOW_SAVED: 'Workflow saved', GROUP_SAVED: 'Group saved', GROUP_DELETED: 'Group deleted',
};
const FIELD_TYPES = [
  ['text', 'Short text'], ['textarea', 'Long text'], ['number', 'Number'], ['amount', 'Money amount'],
  ['date', 'Date'], ['select', 'Dropdown'], ['checkbox', 'Checkbox'],
];
const SECTIONS = [
  ['users', 'Users', 'users'], ['org', 'Org chart', 'org'], ['groups', 'Approver groups', 'layers'],
  ['workflows', 'Workflows', 'route'], ['forms', 'Forms', 'form'], ['auto-approval', 'Auto-approval', 'bolt'],
];

// ---------- shared bits ----------
function rail(active) {
  return h('nav', { class: 'rail', 'aria-label': 'Admin sections' },
    h('div', { class: 'rail-title', 'aria-hidden': 'true' }, 'Admin'),
    SECTIONS.map(([id, label, ic]) => h('a', { href: `#/admin/${id}`, 'aria-current': id === active ? 'page' : null }, icon(ic, { size: 18 }), label)));
}

function page(active, ...nodes) {
  const label = (SECTIONS.find(([id]) => id === active) || [])[1];
  document.title = `${label || 'Admin'} | Request Hub`;
  mount(h('div', { class: 'admin-shell' }, rail(active), h('div', { class: 'admin-main' }, ...nodes)));
}

function roleBadge(roleId) {
  if (!roleId) return h('span', { class: 'badge muted' }, 'No role');
  return h('span', { class: `badge role-${roleId}` }, (ROLES[roleId] || {}).label || roleId);
}

function accessBadge(status) {
  return h('span', { class: `badge ${ACCESS_TONE[status] || 'muted'}` }, h('span', { class: 'dot', 'aria-hidden': 'true' }), ACCESS_LABEL[status] || status);
}

function signInBadge(status) {
  const [label, tone] = SIGN_IN[status] || [String(status || 'Unknown').replace(/_/g, ' ').toLowerCase(), 'muted'];
  return h('span', { class: `badge ${tone}` }, label);
}

function moduleChips(user) {
  if (user.role === 'admins') return h('span', { class: 'chip strong' }, 'All modules');
  const mods = user.modules || [];
  if (!mods.length) return h('span', { class: 'muted small' }, 'None');
  const label = (m) => (MODULES[m] || {}).label || m;
  if (mods.length === Object.keys(MODULES).length) return h('span', { class: 'chip', title: mods.map(label).join(', ') }, 'All modules');
  const shown = mods.slice(0, 2);
  return h('div', { class: 'chips' }, shown.map((m) => h('span', { class: 'chip' }, label(m))),
    mods.length > 2 ? h('span', { class: 'chip', title: mods.slice(2).map(label).join(', ') }, `+${mods.length - 2} more`) : null);
}

function moduleTabs(base, active, onClick = null) {
  return h('nav', { class: 'pills', 'aria-label': 'Modules' }, Object.entries(MODULES).map(([id, m]) => h('a', {
    href: `#/admin/${base}/${id}`, 'aria-current': id === active ? 'page' : null, onclick: onClick,
  }, icon(MODULE_ICON[id], { size: 16 }), m.label)));
}

function historyList(entries, empty = 'No changes recorded yet.') {
  if (!entries || !entries.length) return h('p', { class: 'muted small' }, empty);
  return h('ol', { class: 'timeline' }, entries.map((entry) => h('li', {},
    h('div', { class: 'row spread' }, h('strong', {}, HISTORY_LABEL[entry.action] || entry.action), timeAgo(entry.at)),
    h('div', { class: 'small' }, entry.summary),
    h('div', { class: 'muted small' }, `By ${entry.actorEmail || entry.actorId}`))));
}

// Read-only admin data is cached for 45 seconds; any change made through the API clears it.
const loadDirectory = () => cachedApi('/admin/users');
const loadConfig = () => cachedApi('/admin/config');
const refresh = () => { clearCache(); rerender(); };
const refreshButton = () => button('Refresh', { icon: 'refresh', onClick: refresh });

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

const personLine = (u) => [u.name ? u.email : null, u.jobTitle, u.department].filter(Boolean).join(', ');

// Only active approvers and admins can manage people, and never someone in your own reporting tree.
// Each option shows the name, with the email and department on a second line.
function managerSelect(users, selfId, currentId, id) {
  const below = selfId ? descendantsOf(selfId, users) : new Set();
  const options = users.filter((u) => canManage(u) && u.userId !== selfId && !below.has(u.userId))
    .sort((a, b) => displayName(a).localeCompare(displayName(b)));
  return select({
    id,
    options: [{ value: '', label: 'No manager', description: 'Admins approve level 1' },
      ...options.map((u) => ({ value: u.userId, label: displayName(u), description: personLine(u) || (ROLES[u.role] || {}).label }))],
    value: currentId && options.some((o) => o.userId === currentId) ? currentId : '',
    placeholder: null,
    searchable: options.length > 6,
  });
}

function todayPlus(days) {
  const d = new Date();
  d.setDate(d.getDate() + days);
  return toIso(d);
}

function grantFields({ moduleControl, idPrefix }) {
  const limit = textInput({ id: `${idPrefix}-limit`, type: 'number', placeholder: 'For example 500', attrs: { min: '0', step: 'any', required: true } });
  const until = datePicker({ id: `${idPrefix}-until`, value: todayPlus(90), min: todayPlus(1), max: todayPlus(366) });
  const noteInput = textInput({ id: `${idPrefix}-note`, placeholder: 'Why (optional)', attrs: { maxlength: '200' } });
  const fields = {
    module: formField({ label: 'Module', control: moduleControl, controlId: `${idPrefix}-module` }),
    limit: formField({ label: 'Up to (approval value)', control: limit }),
    until: formField({ label: 'Until', control: until, controlId: `${idPrefix}-until` }),
    note: formField({ label: 'Note', control: noteInput }),
  };
  const read = () => {
    Object.values(fields).forEach((f) => f.setError(''));
    const errors = {};
    if (!(Number(limit.value) >= 0) || limit.value === '') errors.limit = 'Enter the highest value to approve automatically';
    if (!until.value || until.value < todayPlus(1) || until.value > todayPlus(366)) errors.until = 'Choose a date between tomorrow and a year from today';
    Object.entries(errors).forEach(([k, msg]) => fields[k].setError(msg));
    if (Object.keys(errors).length) return null;
    return { maxAmount: Number(limit.value), expiresOn: until.value, note: noteInput.value.trim() };
  };
  return { fields, read };
}

// ---------- role and module editor ----------
function accessEditor(initial = { role: 'requesters', modules: [] }) {
  const roleName = `role-${Math.random().toString(36).slice(2, 8)}`;
  const roleInputs = {};
  const roleCards = h('div', { class: 'choice-grid roles', role: 'radiogroup', 'aria-label': 'Role' }, Object.entries(ROLES).map(([id, r]) => {
    const choice = choiceCard({ name: roleName, value: id, title: r.label, description: r.blurb, checked: initial.role === id });
    roleInputs[id] = choice.input;
    return choice.node;
  }));
  const moduleInputs = {};
  const moduleCards = h('div', { class: 'choice-grid', role: 'group', 'aria-label': 'Request modules' }, Object.entries(MODULES).map(([id, m]) => {
    const choice = choiceCard({ type: 'checkbox', value: id, title: m.label, description: m.blurb, iconName: MODULE_ICON[id], checked: (initial.modules || []).includes(id) });
    moduleInputs[id] = choice.input;
    return choice.node;
  }));
  const adminNote = note('Admins can raise requests in every module, so module choices do not apply.');
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
      h('h3', { class: 'section-label' }, 'Role'), roleCards,
      h('div', { class: 'row spread' }, h('h3', { class: 'section-label' }, 'Request modules they can raise'), quick),
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
  const head = pageHead({
    title: 'Users',
    description: 'Invite people and set their role, modules and manager. Access changes apply on their next click.',
    actions: [refreshButton(), linkButton('Invite user', '#/admin/users/new', { variant: 'primary', icon: 'plus' })],
  });
  page('users', head, skeleton(6, 'Loading users'));
  const data = await loadDirectory();
  const users = data.items || [];
  const people = byId(users);
  const summary = data.summary || {};
  const search = searchInput({ placeholder: 'Search by name, email or department', label: 'Search users', onInput: () => paint() });
  const roleFilter = select({ ariaLabel: 'Filter by role', placeholder: null,
    options: [{ value: '', label: 'All roles' }, ...Object.entries(ROLES).map(([id, r]) => ({ value: id, label: r.label }))] });
  const statusFilter = select({ ariaLabel: 'Filter by status', placeholder: null,
    options: [{ value: '', label: 'Any status' }, { value: 'ACTIVE', label: 'Active' }, { value: 'DISABLED', label: 'Disabled' },
      { value: 'NO_PROFILE', label: 'No access set up' }, { value: 'INVITED', label: 'Invite pending' },
      { value: 'NO_MANAGER', label: 'No manager', description: 'Active requesters and approvers only' }] });
  const tbody = h('tbody');
  const count = h('span', { class: 'count', 'aria-live': 'polite' });
  const paint = () => {
    const q = search.input.value.trim().toLowerCase();
    const shown = users.filter((u) => {
      if (q && !`${u.name || ''} ${u.email || ''} ${u.department || ''}`.toLowerCase().includes(q)) return false;
      if (roleFilter.value && u.role !== roleFilter.value) return false;
      if (statusFilter.value === 'INVITED') return u.cognitoStatus === 'FORCE_CHANGE_PASSWORD';
      if (statusFilter.value === 'NO_MANAGER') return u.status === 'ACTIVE' && !u.managerId && u.role !== 'admins';
      if (statusFilter.value && u.status !== statusFilter.value) return false;
      return true;
    });
    count.textContent = `${shown.length} of ${users.length} users`;
    tbody.replaceChildren(...(shown.length ? shown.map((u) => {
      const manager = people[u.managerId];
      const href = `#/admin/users/${u.userId}`;
      const open = () => { window.location.hash = href; };
      return h('tr', { class: `clickable${u.status === 'DISABLED' ? ' dim' : ''}`, tabindex: '0', onclick: open,
        onkeydown: (ev) => { if (ev.key === 'Enter') open(); } },
      h('td', {}, h('div', { class: 'person' }, avatar(u),
        h('div', { class: 'person-text' },
          h('div', { class: 'people-name' }, h('span', { class: 'title' }, u.name || u.email), u.userId === state.me.userId ? h('span', { class: 'chip' }, 'You') : null),
          u.name ? h('div', { class: 'cell-sub' }, u.email) : null,
          h('div', { class: 'row tight mobile-only-block' }, roleBadge(u.role)),
          u.department ? h('div', { class: 'cell-sub' }, u.department) : null))),
      h('td', {}, roleBadge(u.role)),
      h('td', {}, moduleChips(u)),
      h('td', { class: 'small' }, manager ? displayName(manager) : h('span', { class: 'muted' }, u.status === 'ACTIVE' && u.role !== 'admins' ? 'None, admins approve' : 'None'),
        u.reports ? h('div', { class: 'cell-sub' }, `${u.reports} direct report${u.reports === 1 ? '' : 's'}`) : null),
      h('td', {}, signInBadge(u.cognitoStatus)),
      h('td', {}, accessBadge(u.status)));
    }) : [h('tr', {}, h('td', { colspan: '6', class: 'empty' }, 'No users match these filters.'))]));
  };
  [roleFilter, statusFilter].forEach((el) => el.addEventListener('change', paint));
  page('users', head,
    h('div', { class: 'stats' },
      stat('Users', summary.total ?? users.length), stat('Active', summary.active ?? 0, 'ok'),
      stat('Invites pending', summary.invitesPending ?? 0, 'warn'), stat('Without a manager', summary.noManager ?? 0, 'warn'),
      stat('Admins', summary.admins ?? 0, 'tone-brand')),
    h('div', { class: 'toolbar' }, search.node, roleFilter, statusFilter, count),
    h('div', { class: 'card flush' }, h('div', { class: 'table-scroll' }, h('table', { class: 'table users' },
      h('thead', {}, h('tr', {}, h('th', {}, 'User'), h('th', {}, 'Role'), h('th', {}, 'Modules'), h('th', {}, 'Reports to'), h('th', {}, 'Sign-in'), h('th', {}, 'Access'))),
      tbody))));
  paint();
}

async function viewUserNew() {
  const head = pageHead({ title: 'Invite a user', back: { href: '#/admin/users', label: 'Back to users' },
    description: 'They get an email with a temporary password, then set their own password and an authenticator app at first sign-in.' });
  page('users', head, skeleton(6, 'Loading'));
  const { items: users } = await loadDirectory();
  const editor = accessEditor({ role: 'requesters', modules: ['GENERAL'] });
  const manager = managerSelect(users, null, null, 'invite-manager');
  const invite = checkbox('Email the invitation now', { checked: true, description: 'Cognito sends a temporary password. Clear this to create the account and send the invitation later.' });
  const email = textInput({ id: 'invite-email', type: 'email', placeholder: 'name@company.com', attrs: { name: 'email', required: true, maxlength: '254' } });
  const name = textInput({ id: 'invite-name', placeholder: 'Optional', attrs: { name: 'name', maxlength: '80' } });
  const department = textInput({ id: 'invite-department', attrs: { name: 'department', maxlength: '60' } });
  const jobTitle = textInput({ id: 'invite-job', attrs: { name: 'jobTitle', maxlength: '60' } });
  const emailField = formField({ label: 'Email', control: email, required: true });
  const submit = button('Invite user', { variant: 'primary', type: 'submit', icon: 'mail' });
  const form = h('form', { class: 'card form', novalidate: true },
    h('div', { class: 'grid2' }, emailField.node, formField({ label: 'Full name', control: name }).node),
    h('h2', { class: 'section-label' }, 'Reporting line'),
    h('div', { class: 'grid3' },
      formField({ label: 'Manager (level 1 approver)', control: manager, controlId: 'invite-manager' }).node,
      formField({ label: 'Department', control: department }).node,
      formField({ label: 'Job title', control: jobTitle }).node),
    editor.node,
    h('hr', { class: 'divider' }),
    invite.node,
    h('div', { class: 'form-actions' }, linkButton('Cancel', '#/admin/users', { variant: 'ghost' }), submit));
  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    emailField.setError('');
    const access = editor.value();
    const payload = {
      email: email.value.trim(), name: name.value.trim(), role: access.role,
      modules: access.role === 'admins' ? [] : access.modules, sendInvite: invite.input.checked,
      managerId: manager.value || null, department: department.value.trim(), jobTitle: jobTitle.value.trim(),
    };
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(payload.email)) { emailField.setError('Enter an email address like name@company.com'); email.focus(); return; }
    if (!payload.managerId && payload.role !== 'admins') {
      const go = await confirmDialog({ title: 'Invite without a manager?', body: 'Level 1 approvals for this person go to admins until you set a manager.', confirmLabel: 'Invite anyway' });
      if (!go) return;
    }
    if (payload.role === 'admins') {
      const go = await confirmDialog({ title: 'Invite a new admin?', body: `${payload.email} will manage every user and the approval configuration. The ops team is alerted about new admins.`, confirmLabel: 'Invite admin', danger: true });
      if (!go) return;
    }
    await busy(submit, async () => {
      try {
        const res = await api('/admin/users', { method: 'POST', body: JSON.stringify(payload) });
        toast(res.invited ? `Invitation emailed to ${payload.email}` : `${payload.email} created. Send the invitation when they are ready.`, 'ok', { persist: true });
        window.location.hash = `#/admin/users/${res.user.userId}`;
      } catch (e) {
        toast(e.message, 'danger');
      }
    });
  });
  page('users', head, form);
}

function autoRulesCard(user, isSelf) {
  const rules = user.autoRules || [];
  const allowed = user.role === 'admins' ? Object.keys(MODULES) : (user.modules || []);
  const list = rules.length
    ? h('table', { class: 'table compact' },
      h('thead', {}, h('tr', {}, h('th', {}, 'Module'), h('th', { class: 'num' }, 'Up to'), h('th', {}, 'Until'), h('th', {}, 'Note'), h('th', {}, h('span', { class: 'sr-only' }, 'Actions')))),
      h('tbody', {}, rules.map((r) => h('tr', { class: r.active ? '' : 'dim' },
        h('td', {}, (MODULES[r.module] || {}).label || r.module),
        h('td', { class: 'num' }, fmtNumber(r.maxAmount)),
        h('td', {}, fmtDate(r.expiresOn), r.active ? null : h('span', { class: 'badge muted' }, 'Expired')),
        h('td', { class: 'muted small' }, r.note || ''),
        h('td', {}, isSelf ? null : h('button', {
          class: 'linkish danger-text', type: 'button',
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
  const body = [h('p', { class: 'muted small' }, 'Off unless an admin grants it: requests in the module up to the limit are approved with no approver, until the end date.'), list];
  if (isSelf) {
    body.push(note('You cannot grant yourself auto-approval. Another admin has to do it.'));
    return card({ title: 'Auto-approval', body });
  }
  if (user.status !== 'ACTIVE' || !user.managed || !allowed.length) {
    body.push(note('Give this user modules and make sure the account is active first.'));
    return card({ title: 'Auto-approval', body });
  }
  const module = select({ id: 'rule-module', options: allowed.map((m) => ({ value: m, label: (MODULES[m] || {}).label || m })), value: allowed[0], placeholder: null });
  const grant = grantFields({ moduleControl: module, idPrefix: 'rule' });
  const submit = button('Grant auto-approval', { type: 'submit', icon: 'bolt' });
  const form = h('form', { class: 'form', novalidate: true },
    h('hr', { class: 'divider' }),
    h('h3', { class: 'section-label' }, 'Grant auto-approval'),
    h('div', { class: 'grid2' }, grant.fields.module.node, grant.fields.limit.node, grant.fields.until.node, grant.fields.note.node),
    h('div', { class: 'row end' }, submit));
  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const values = grant.read();
    if (!values) return;
    const go = await confirmDialog({
      title: 'Grant auto-approval?',
      body: `${user.email}'s ${(MODULES[module.value] || {}).label} requests up to ${fmtNumber(values.maxAmount)} will be approved with no approver until ${fmtDate(values.expiresOn)}. This is recorded in their history.`,
      confirmLabel: 'Grant', danger: true,
    });
    if (!go) return;
    await busy(submit, async () => {
      try {
        const res = await api(`/admin/users/${user.userId}/auto-rules/${module.value}`, { method: 'PUT', body: JSON.stringify(values) });
        toast(res.message, 'ok');
        rerender();
      } catch (e) { toast(e.message, 'danger'); }
    });
  });
  body.push(form);
  return card({ title: 'Auto-approval', body });
}

async function viewUserDetail(userId) {
  page('users', skeleton(3, 'Loading user'), skeleton(6, 'Loading user'));
  const [{ user }, directory] = await Promise.all([api(`/admin/users/${encodeURIComponent(userId)}`), loadDirectory()]);
  const users = directory.items || [];
  const people = byId(users);
  const isSelf = user.userId === state.me.userId;
  const invited = user.cognitoStatus === 'FORCE_CHANGE_PASSWORD';

  const editor = accessEditor({ role: user.role || 'requesters', modules: user.modules || [] });
  const nameInput = textInput({ id: 'user-name', value: user.name || '', placeholder: 'Full name', attrs: { maxlength: '80' } });
  const manager = managerSelect(users, user.userId, user.managerId, 'user-manager');
  const department = textInput({ id: 'user-department', value: user.department || '', attrs: { maxlength: '60' } });
  const jobTitle = textInput({ id: 'user-job', value: user.jobTitle || '', attrs: { maxlength: '60' } });
  const save = button(user.managed ? 'Save' : 'Set up access', { variant: 'primary', type: 'submit' });
  const accessForm = h('form', { class: 'card form', 'aria-labelledby': 'access-title' },
    h('div', { class: 'card-head' }, h('h2', { id: 'access-title' }, 'Access and reporting line'), h('span', { class: 'meta' }, 'Takes effect on their next click')),
    !user.managed ? note('This account was created outside Request Hub and has no access profile, so it cannot use the app yet. Choose a role and modules to set it up.', 'warn') : null,
    formField({ label: 'Full name', control: nameInput }).node,
    formField({ label: 'Manager (level 1 approver)', control: manager, controlId: 'user-manager' }).node,
    h('div', { class: 'grid2' },
      formField({ label: 'Department', control: department }).node,
      formField({ label: 'Job title', control: jobTitle }).node),
    editor.node,
    isSelf ? note('You cannot change your own role. Another admin has to do that.') : null,
    h('div', { class: 'form-actions' }, save));
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
    await busy(save, async () => {
      try {
        const res = await api(`/admin/users/${encodeURIComponent(user.userId)}`, { method: 'PATCH', body: JSON.stringify(payload) });
        toast(res.changed ? 'Saved. It applies immediately.' : 'Nothing changed.', res.changed ? 'ok' : 'info');
        rerender();
      } catch (e) {
        toast(e.message, 'danger');
      }
    });
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
  if (user.status === 'DISABLED') actions.push(button('Enable account', { variant: 'success', icon: 'check', onClick: () => act('ENABLE') }));
  else if (!isSelf) {
    actions.push(button('Disable account', { variant: 'danger', onClick: () => act('DISABLE', { title: `Disable ${user.email}?`, body: `Sign-in is blocked, sessions are signed out, and API access stops on their next click.${user.reports ? ` ${user.reports} people report to them: their requests go to admins until you set a new manager.` : ''}`, confirmLabel: 'Disable', danger: true }) }));
  }
  if (invited) actions.push(button('Resend invitation', { icon: 'mail', onClick: () => act('RESEND_INVITE', { title: 'Resend the invitation?', body: `Cognito emails ${user.email} a new temporary password. The old one stops working.`, confirmLabel: 'Resend' }) }));
  else if (user.cognitoStatus !== 'MISSING') {
    actions.push(button('Reset password', { icon: 'key', onClick: () => act('RESET_PASSWORD', { title: 'Reset the password?', body: `Cognito emails ${user.email} a verification code. They choose a new password at their next sign-in.`, confirmLabel: 'Reset password' }) }));
    actions.push(button('Reset authenticator', { icon: 'shield', onClick: () => act('RESET_MFA', { title: 'Reset the authenticator?', body: 'Use this when someone loses their phone. They are signed out and set up a new authenticator app at their next sign-in.', confirmLabel: 'Reset authenticator' }) }));
  }
  if (user.cognitoStatus !== 'MISSING') actions.push(button('Sign out everywhere', { icon: 'logout', onClick: () => act('SIGN_OUT', { title: 'Sign out everywhere?', body: 'Ends every session this user has. They need to sign in again on each device.', confirmLabel: 'Sign out everywhere' }) }));

  const managerUser = people[user.managerId];
  const account = card({
    title: 'Account',
    body: [
      h('dl', { class: 'facts' },
        field('Sign-in', SIGN_IN[user.cognitoStatus] ? SIGN_IN[user.cognitoStatus][0] : user.cognitoStatus),
        field('MFA', user.mfa),
        field('Reports to', managerUser ? displayName(managerUser) : 'Nobody, so admins approve level 1'),
        field('Direct reports', (user.directReports || []).length ? user.directReports.map((r) => r.name || r.email).join(', ') : 'None'),
        field('Approver groups', (user.approverGroups || []).length ? user.approverGroups.map((g) => g.name).join(', ') : 'None'),
        field('Created', fmtTime(user.createdAt)),
        field('Last change', user.updatedAt ? `${fmtTime(user.updatedAt)}${user.updatedByEmail ? ` by ${user.updatedByEmail}` : ''}` : null)),
      h('hr', { class: 'divider' }),
      h('div', { class: 'row' }, actions),
    ],
  });
  const danger = isSelf ? null : card({
    cls: 'danger-zone', title: 'Delete user',
    body: [
      h('p', { class: 'muted small' }, 'Removes the sign-in, the access profile and any auto-approval. Their requests and this history are kept. Reassign anyone who reports to them, and their approver group seats, first.'),
      button('Delete user', {
        variant: 'danger', icon: 'trash',
        onClick: async () => {
          const go = await confirmDialog({ title: `Delete ${user.email}?`, body: 'This cannot be undone. You would need to invite them again.', confirmLabel: 'Delete user', danger: true, typeToConfirm: user.email });
          if (!go) return;
          try {
            await api(`/admin/users/${encodeURIComponent(user.userId)}`, { method: 'DELETE' });
            toast(`${user.email} deleted`, 'ok', { persist: true });
            window.location.hash = '#/admin/users';
          } catch (e) { toast(e.message, 'danger'); }
        },
      }),
    ],
  });

  page('users',
    pageHead({
      title: h('span', { class: 'person big' }, avatar(user, 'lg'), h('span', {}, user.name || user.email)),
      back: { href: '#/admin/users', label: 'Back to users' },
      description: personLine(user),
      aside: h('div', { class: 'row' }, roleBadge(user.role), accessBadge(user.status), signInBadge(user.cognitoStatus)),
    }),
    h('div', { class: 'layout-2' },
      h('div', { class: 'col' }, accessForm, autoRulesCard(user, isSelf)),
      h('div', { class: 'col' }, account, card({ title: 'Access history', body: [historyList(user.history)] }), danger)));
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
  const area = h('textarea', { id: 'csv-rows', rows: '6', 'aria-label': 'CSV rows', placeholder: 'email,managerEmail,department,jobTitle\nana@company.com,priya@company.com,Finance,Finance lead' });
  const drop = fileDrop({ label: 'Choose a CSV file', accept: '.csv,text/csv', hint: 'Or paste the rows below' });
  const results = h('div', { 'aria-live': 'polite' });
  const checkBtn = button('Check');
  const apply = button('Apply', { variant: 'primary', attrs: { disabled: true } });
  let rows = [];
  drop.input.addEventListener('change', async () => { if (drop.input.files[0]) area.value = await drop.input.files[0].text(); });
  const run = async (dryRun, btn) => busy(btn, async () => {
    try {
      rows = parseCsv(area.value);
      if (!rows.length) { toast('Paste or choose a CSV with at least one row', 'warn'); return; }
      const res = await fetch(`${state.cfg.apiBase}/admin/users/import`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: state.idToken },
        body: JSON.stringify({ rows, dryRun }),
      });
      const body = await res.json();
      if (!res.ok && res.status !== 422) throw new Error(body.message || `Import failed (${res.status})`);
      const allOk = body.results.every((r) => r.ok);
      results.replaceChildren(
        note(body.message, allOk ? 'ok' : 'warn'),
        h('table', { class: 'table compact' }, h('thead', {}, h('tr', {}, h('th', {}, 'Row'), h('th', {}, 'Email'), h('th', {}, 'Manager'), h('th', {}, 'Result'))),
          h('tbody', {}, body.results.map((r) => h('tr', {}, h('td', { class: 'num' }, String(r.row)), h('td', {}, r.email), h('td', {}, r.managerEmail || 'None'),
            h('td', { class: r.ok ? 'ok-text' : 'danger-text' }, r.ok ? 'OK' : r.error))))));
      if (!dryRun && res.ok) { clearCache(); toast(body.message, 'ok'); setTimeout(() => rerender(), 800); }
      return dryRun && allOk;
    } catch (e) { toast(e.message, 'danger'); return false; }
  });
  checkBtn.addEventListener('click', async () => { const ready = await run(true, checkBtn); apply.disabled = !ready; });
  apply.addEventListener('click', async () => { await run(false, apply); apply.disabled = true; });
  return card({
    title: 'Import reporting lines from CSV',
    body: [
      h('p', { class: 'muted small' }, 'Columns: email, managerEmail, department, jobTitle. People must already exist. Everything is checked first (unknown people, managers who are not approvers, and loops across rows); nothing is saved until you apply.'),
      drop,
      formField({ label: 'CSV rows', control: area, controlId: 'csv-rows' }).node,
      h('div', { class: 'row end' }, checkBtn, apply), results,
    ],
  });
}

async function viewOrg() {
  const head = pageHead({ title: 'Org chart', description: 'Level 1 of every approval is the requester\'s manager here, so an approver\'s own requests go to their manager. People at the top fall back to admins.', actions: [refreshButton()] });
  page('org', head, skeleton(8, 'Loading the org chart'));
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
          h('span', { class: 'muted small' }, [u.jobTitle, u.department].filter(Boolean).join(', ') || u.email)),
        roleBadge(u.role), kids.length ? h('span', { class: 'chip', title: `${kids.length} direct report${kids.length === 1 ? '' : 's'}` }, `${kids.length}`) : null),
      kids.length && depth < 12 ? h('ul', {}, kids.map((k) => nodeFor(k, depth + 1))) : null);
  };
  const unmanaged = roots.filter((u) => u.status === 'ACTIVE' && u.role !== 'admins');
  page('org', head,
    unmanaged.length ? note(`${unmanaged.length} active non-admin user${unmanaged.length === 1 ? ' has' : 's have'} no manager, so admins approve their requests: ${unmanaged.map(displayName).join(', ')}.`, 'warn') : null,
    h('div', { class: 'card org' }, h('ul', { class: 'org-tree' }, sortPeople(roots).map((u) => nodeFor(u)))),
    importPanel());
}

// ---------- approver groups ----------
async function viewGroups() {
  const addHost = h('div');
  const head = pageHead({
    title: 'Approver groups',
    description: 'Fixed groups such as Finance or a change board. A workflow level can require any member of a group to approve. The requester, and anyone who already approved an earlier level, never count.',
    actions: [refreshButton()],
  });
  page('groups', head, skeleton(6, 'Loading groups'));
  const [config, directory] = await Promise.all([loadConfig(), loadDirectory()]);
  const users = directory.items || [];
  const people = byId(users);
  const eligible = users.filter(canManage).sort((a, b) => displayName(a).localeCompare(displayName(b)));
  const usage = {};
  Object.values(config.workflows).forEach((w) => (w.levels || []).forEach((l) => {
    if (l.groupId) (usage[l.groupId] = usage[l.groupId] || []).push((MODULES[w.module] || {}).label || w.module);
  }));

  const editor = (group, onCancel) => {
    const uidPart = group ? group.groupId : 'new';
    const name = textInput({ id: `g-name-${uidPart}`, value: group ? group.name : '', attrs: { maxlength: '60', required: true } });
    const description = textInput({ id: `g-desc-${uidPart}`, value: group ? group.description || '' : '', attrs: { maxlength: '200' } });
    const nameField = formField({ label: 'Name', control: name, required: true });
    const picks = {};
    const members = h('div', { class: 'choice-grid members', role: 'group', 'aria-label': 'Members' }, eligible.map((u) => {
      const choice = choiceCard({ type: 'checkbox', title: displayName(u), description: [(ROLES[u.role] || {}).label, u.department].filter(Boolean).join(', '), checked: group ? (group.members || []).includes(u.userId) : false });
      picks[u.userId] = choice.input;
      return choice.node;
    }));
    const submit = button(group && !group.unsaved ? 'Save group' : 'Create group', { variant: 'primary', type: 'submit' });
    const form = h('form', { class: 'form', novalidate: true },
      h('div', { class: 'grid2' }, nameField.node, formField({ label: 'Description', control: description }).node),
      h('h3', { class: 'section-label' }, 'Members (active approvers and admins)'),
      eligible.length ? members : h('p', { class: 'muted small' }, 'Nobody is eligible yet. Make someone an approver first.'),
      h('div', { class: 'row end' }, button('Cancel', { variant: 'ghost', onClick: onCancel }), submit));
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      nameField.setError('');
      if (!name.value.trim()) { nameField.setError('Give the group a name'); name.focus(); return; }
      const body = { name: name.value.trim(), description: description.value.trim(), members: Object.keys(picks).filter((id) => picks[id].checked) };
      if (group && group.unsaved) body.groupId = group.groupId;
      if (group && !group.unsaved) body.version = group.version;
      await busy(submit, async () => {
        try {
          const res = group && !group.unsaved
            ? await api(`/admin/groups/${group.groupId}`, { method: 'PUT', body: JSON.stringify(body) })
            : await api('/admin/groups', { method: 'POST', body: JSON.stringify(body) });
          toast(res.message, 'ok');
          rerender();
        } catch (e) { toast(e.message, 'danger'); }
      });
    });
    return form;
  };

  const cards = config.groups.map((g) => {
    const body = h('div', { class: 'group-body' });
    const edit = button(g.unsaved ? 'Create' : 'Edit', { size: 'sm', icon: g.unsaved ? 'plus' : 'edit' });
    const view = () => {
      edit.hidden = false;
      body.replaceChildren(
        g.description ? h('p', { class: 'muted small' }, g.description) : null,
        (g.members || []).length
          ? h('div', { class: 'chips' }, g.members.map((m) => h('span', { class: 'chip' }, displayName(people[m] || { email: 'Removed user' }))))
          : note('No members: levels that use this group fall back to admins.', 'warn'),
        h('p', { class: 'muted small' }, usage[g.groupId] ? `Used by: ${usage[g.groupId].join(', ')}` : 'Not used by any workflow'));
    };
    view();
    edit.addEventListener('click', () => { body.replaceChildren(editor(g, view)); edit.hidden = true; });
    const del = g.unsaved ? null : button('Delete', {
      size: 'sm', variant: 'danger',
      onClick: async () => {
        if (!(await confirmDialog({ title: `Delete ${g.name}?`, body: 'Change any workflow that uses this group first.', confirmLabel: 'Delete', danger: true }))) return;
        try { await api(`/admin/groups/${g.groupId}`, { method: 'DELETE' }); toast(`${g.name} deleted`, 'ok'); rerender(); } catch (e) { toast(e.message, 'danger'); }
      },
    });
    return card({
      title: [g.name, g.unsaved ? h('span', { class: 'badge warn' }, 'Not created yet') : null],
      actions: h('div', { class: 'row' }, edit, del),
      body: [body],
    });
  });
  const newGroup = button('New group', {
    variant: 'primary', icon: 'plus',
    onClick: () => {
      const close = () => addHost.replaceChildren();
      addHost.replaceChildren(card({ title: 'New approver group', body: [editor(null, close)] }));
      addHost.querySelector('input').focus();
    },
  });
  head.querySelector('.section-actions').append(newGroup);
  page('groups', head, addHost, h('div', { class: 'group-cards' }, cards));
}

// ---------- workflows ----------
async function viewWorkflows(moduleId) {
  const module = MODULES[moduleId] ? moduleId : 'PURCHASE';
  const head = pageHead({ title: 'Approval workflows', description: 'Level 1 is the requester\'s line manager; later levels are approver groups or admins, each switched on by a threshold on the form\'s approval value. Nothing is approved automatically unless you grant someone auto-approval.' });
  page('workflows', head, moduleTabs('workflows', module), skeleton(6, 'Loading workflows'));
  const config = await loadConfig();
  const wf = config.workflows[module];
  const form = config.forms[module];
  const approvalField = (form.fields || []).find((f) => f.key === form.approvalField);
  const groupName = (id) => (config.groups.find((g) => g.groupId === id) || {}).name || id;
  const levels = (wf.levels || []).map((l) => ({ ...l }));
  const host = h('div', { class: 'levels' });
  const sentence = note('');
  const describe = (l) => {
    const who = l.kind === 'MANAGER' ? 'the requester\'s line manager' : l.kind === 'GROUP' ? `any member of ${groupName(l.groupId)}` : 'any admin';
    return Number(l.minAmount) > 0 ? `${who} when ${approvalField ? approvalField.label.replace(/\?+$/, '') : 'the value'} is ${fmtNumber(l.minAmount)} or more` : who;
  };
  const paintSentence = () => { sentence.lastChild.textContent = `Every ${MODULES[module].label} request needs ${levels.map(describe).join(', then ')}.`; };
  const paint = () => {
    paintSentence();
    host.replaceChildren(...levels.map((l, i) => {
      const kind = select({ ariaLabel: `Level ${i + 1} approver`, placeholder: null, value: l.kind,
        options: [{ value: 'MANAGER', label: 'Line manager', icon: 'user' }, { value: 'GROUP', label: 'Approver group', icon: 'users' }, { value: 'ADMINS', label: 'Admins', icon: 'shield' }] });
      const group = select({ ariaLabel: `Level ${i + 1} group`, placeholder: null,
        options: config.groups.map((g) => ({ value: g.groupId, label: g.name })), value: l.groupId || (config.groups[0] || {}).groupId || '' });
      group.hidden = l.kind !== 'GROUP';
      const threshold = textInput({ type: 'number', value: l.minAmount || 0, attrs: { min: '0', step: 'any', 'aria-label': `Level ${i + 1} threshold`, disabled: !approvalField || i === 0 } });
      kind.addEventListener('change', () => { l.kind = kind.value; if (l.kind === 'GROUP') l.groupId = group.value; else delete l.groupId; paint(); });
      group.addEventListener('change', () => { l.groupId = group.value; paint(); });
      threshold.addEventListener('input', () => { l.minAmount = Number(threshold.value || 0); paintSentence(); });
      const move = (d) => { const j = i + d; if (j < 0 || j >= levels.length) return; [levels[i], levels[j]] = [levels[j], levels[i]]; if (levels[0]) levels[0].minAmount = 0; paint(); };
      return h('div', { class: 'level-row' },
        h('span', { class: 'stamp sm upcoming', 'aria-hidden': 'true' }, String(i + 1)),
        h('div', { class: 'level-who' }, kind, group),
        h('div', { class: 'level-threshold' }, h('span', { class: 'small muted' }, i === 0 ? 'Applies to every request' : `When ${approvalField ? approvalField.label.replace(/\?+$/, '') : 'the value'} is at least`), i === 0 ? null : threshold),
        h('div', { class: 'row tight level-actions' },
          iconButton('chevronUp', 'Move up', { attrs: { disabled: i === 0 }, onClick: () => move(-1) }),
          iconButton('chevronDown', 'Move down', { attrs: { disabled: i === levels.length - 1 }, onClick: () => move(1) }),
          iconButton('trash', 'Remove level', { variant: 'danger', attrs: { disabled: levels.length === 1 }, onClick: () => { levels.splice(i, 1); if (levels[0]) levels[0].minAmount = 0; paint(); } })));
    }));
  };
  paint();
  const sla = textInput({ id: 'wf-sla', type: 'number', value: Math.round((wf.slaSeconds || 180) / 60), attrs: { min: '1', max: '10080' } });
  const esc = textInput({ id: 'wf-esc', type: 'number', value: Math.round((wf.escalationSeconds || 180) / 60), attrs: { min: '1', max: '10080' } });
  const add = button('Add level', {
    icon: 'plus',
    onClick: () => {
      if (levels.length >= 5) { toast('A workflow can have at most 5 levels', 'warn'); return; }
      const unused = config.groups.find((g) => !levels.some((l) => l.groupId === g.groupId));
      levels.push(unused ? { kind: 'GROUP', groupId: unused.groupId, minAmount: 0 } : { kind: 'ADMINS', minAmount: 0 });
      paint();
    },
  });
  const save = button('Save workflow', { variant: 'primary' });
  save.addEventListener('click', () => busy(save, async () => {
    const body = { levels: levels.map((l) => ({ kind: l.kind, minAmount: Number(l.minAmount || 0), ...(l.kind === 'GROUP' ? { groupId: l.groupId } : {}) })),
      slaSeconds: Math.round(Number(sla.value) * 60), escalationSeconds: Math.round(Number(esc.value) * 60) };
    if (!wf.unsaved) body.version = wf.version;
    try {
      const res = await api(`/admin/workflows/${module}`, { method: 'PUT', body: JSON.stringify(body) });
      toast(res.message, 'ok');
      rerender();
    } catch (e) { toast(e.message, 'danger'); }
  }));
  const history = (config.history || []).filter((e) => e.subject === module && e.action === 'WORKFLOW_SAVED');
  page('workflows', head, moduleTabs('workflows', module),
    h('div', { class: 'layout-2 even' },
      h('div', { class: 'col' }, card({
        title: `${MODULES[module].label} approval levels`,
        actions: h('span', { class: 'meta' }, wf.unsaved ? 'Default, not saved yet' : `Version ${wf.version}${wf.updatedByEmail ? `, saved by ${wf.updatedByEmail}` : ''}`),
        body: [
          approvalField ? h('p', { class: 'muted small' }, `Thresholds compare against "${approvalField.label}" on the ${MODULES[module].label} form.`)
            : note('This module\'s form has no approval value field, so every level applies to every request. Set one in Forms to use thresholds.', 'warn'),
          host, h('div', { class: 'row' }, add), sentence,
          h('div', { class: 'grid2' },
            formField({ label: 'Minutes per level before escalating to admins', control: sla }).node,
            formField({ label: 'Minutes admins have before the request expires', control: esc }).node),
          h('div', { class: 'form-actions' }, save),
        ],
      })),
      h('div', { class: 'col' },
        card({ title: 'Rules every chain follows', body: [h('ul', { class: 'rules' },
          h('li', {}, 'Nobody approves their own request.'),
          h('li', {}, 'The same person cannot approve two levels of one request.'),
          h('li', {}, 'An approver\'s own request goes to their manager, like everyone else\'s.'),
          h('li', {}, 'If a level has nobody who can approve (no manager, an empty group, disabled people), admins stand in.'),
          h('li', {}, 'Changes apply to new requests. Requests already in flight keep the chain they started with.'))] }),
        card({ title: 'Recent changes', body: [historyList(history, 'No saved versions yet.')] }))));
}

// ---------- form builder ----------
function slug(label) {
  const words = String(label || '').replace(/[^A-Za-z0-9 ]/g, ' ').trim().split(/\s+/).filter(Boolean);
  if (!words.length) return '';
  const key = words.map((w, i) => (i === 0 ? w.toLowerCase() : w[0].toUpperCase() + w.slice(1).toLowerCase())).join('');
  return /^[a-z]/.test(key) ? key.slice(0, 40) : `f${key}`.slice(0, 40);
}

async function viewForms(moduleId) {
  const module = MODULES[moduleId] ? moduleId : 'PURCHASE';
  const head = pageHead({ title: 'Request forms', description: 'Each module has its own fields. Requesters only see the fields of the module they pick. Saving creates a new version; requests already submitted keep the version they used.' });
  page('forms', head, moduleTabs('forms', module), skeleton(8, 'Loading forms'));
  const config = await loadConfig();
  const saved = config.forms[module];
  const fields = (saved.fields || []).map((f) => ({ ...f, options: f.options ? [...f.options] : undefined }));
  let approvalField = saved.approvalField || '';
  let dirty = false;
  const list = h('div', { class: 'builder-list' });
  const preview = h('div', { class: 'dyn-grid preview', 'aria-hidden': 'true' });
  const approvalSelect = select({ id: 'approval-field', placeholder: null, options: [{ value: '', label: 'None (thresholds not used)' }] });
  const status = h('span', { class: 'meta' });

  const markDirty = () => { dirty = true; status.textContent = 'Unsaved changes'; status.className = 'meta warn-text'; };
  const paintPreview = () => {
    preview.replaceChildren(h('div', { class: 'dyn-field wide' }, h('span', { class: 'field-label' }, 'Title', h('span', { class: 'req' }, ' *')), h('input', { type: 'text', tabindex: '-1', placeholder: 'Always asked', 'aria-label': 'Title (preview)' })),
      ...fields.filter((f) => f.label).map((f) => renderField(f, { preview: true }).node));
    const numeric = fields.filter((f) => f.type === 'number' || f.type === 'amount');
    if (!numeric.some((f) => f.key === approvalField)) approvalField = '';
    approvalSelect.setOptions([{ value: '', label: 'None (thresholds not used)' }, ...numeric.map((f) => ({ value: f.key, label: f.label || f.key }))], { keepValue: false });
    approvalSelect.value = approvalField;
  };
  approvalSelect.addEventListener('change', () => { approvalField = approvalSelect.value; markDirty(); });

  const paintList = () => {
    list.replaceChildren(...fields.map((f, i) => {
      const n = `${i}-${Math.random().toString(36).slice(2, 6)}`;
      const label = textInput({ id: `b-label-${n}`, value: f.label || '', placeholder: 'Label people see', attrs: { maxlength: '60' } });
      const key = textInput({ id: `b-key-${n}`, value: f.key || '', placeholder: 'key', attrs: { maxlength: '40', class: 'mono' } });
      const type = select({ id: `b-type-${n}`, placeholder: null, value: f.type || 'text', options: FIELD_TYPES.map(([id, text]) => ({ value: id, label: text })) });
      const required = checkbox('Required', { checked: Boolean(f.required) });
      const help = textInput({ id: `b-help-${n}`, value: f.help || '', placeholder: 'Help text (optional)', attrs: { maxlength: '160' } });
      const extras = h('div', { class: 'grid2 extras' });
      const paintExtras = () => {
        extras.replaceChildren();
        if (f.type === 'text' || f.type === 'textarea') {
          const max = textInput({ id: `b-max-${n}`, type: 'number', value: f.maxLength || (f.type === 'text' ? 120 : 2000), attrs: { min: '1' } });
          max.addEventListener('input', () => { f.maxLength = Number(max.value); markDirty(); });
          extras.append(formField({ label: 'Max characters', control: max }).node);
        }
        if (f.type === 'number' || f.type === 'amount') {
          const min = textInput({ id: `b-min-${n}`, type: 'number', value: f.min ?? 0, attrs: { step: 'any' } });
          const max = textInput({ id: `b-maxv-${n}`, type: 'number', value: f.max ?? 1000000, attrs: { step: 'any' } });
          min.addEventListener('input', () => { f.min = Number(min.value); markDirty(); paintPreview(); });
          max.addEventListener('input', () => { f.max = Number(max.value); markDirty(); paintPreview(); });
          extras.append(formField({ label: 'Minimum', control: min }).node, formField({ label: 'Maximum', control: max }).node);
        }
        if (f.type === 'select') {
          const options = h('textarea', { id: `b-opts-${n}`, rows: '3', placeholder: 'One option per line' });
          options.value = (f.options || []).join('\n');
          options.addEventListener('input', () => { f.options = options.value.split('\n').map((x) => x.trim()).filter(Boolean); markDirty(); paintPreview(); });
          extras.append(formField({ label: 'Options', control: options, wide: true, cls: 'span-all' }).node);
        }
      };
      paintExtras();
      label.addEventListener('input', () => {
        if (!f.key || f.autoKey) { f.key = slug(label.value); f.autoKey = true; key.value = f.key; }
        f.label = label.value; markDirty(); paintPreview();
      });
      key.addEventListener('input', () => { f.key = key.value.trim(); f.autoKey = false; markDirty(); paintPreview(); });
      type.addEventListener('change', () => { f.type = type.value; if (f.type === 'select' && !f.options) f.options = ['Option 1']; markDirty(); paintExtras(); paintPreview(); });
      required.input.addEventListener('change', () => { f.required = required.input.checked; markDirty(); paintPreview(); });
      help.addEventListener('input', () => { f.help = help.value; markDirty(); paintPreview(); });
      const move = (d) => { const j = i + d; if (j < 0 || j >= fields.length) return; [fields[i], fields[j]] = [fields[j], fields[i]]; markDirty(); paintList(); paintPreview(); };
      return h('div', { class: 'builder-field' },
        h('div', { class: 'row spread' }, h('strong', {}, `${i + 1}. ${f.label || 'New field'}`),
          h('div', { class: 'row tight' },
            iconButton('chevronUp', 'Move up', { attrs: { disabled: i === 0 }, onClick: () => move(-1) }),
            iconButton('chevronDown', 'Move down', { attrs: { disabled: i === fields.length - 1 }, onClick: () => move(1) }),
            iconButton('trash', 'Remove field', { variant: 'danger', onClick: () => { fields.splice(i, 1); markDirty(); paintList(); paintPreview(); } }))),
        h('div', { class: 'grid2' }, formField({ label: 'Label', control: label }).node, formField({ label: 'Type', control: type, controlId: type.trigger.id }).node),
        h('div', { class: 'grid2' }, formField({ label: 'Key', control: key }).node, formField({ label: 'Help text', control: help }).node),
        extras,
        h('div', { class: 'dyn-field' }, required.node));
    }));
  };
  paintList();
  paintPreview();
  status.textContent = saved.unsaved ? 'Default, not saved yet' : `Version ${saved.version}${saved.updatedByEmail ? `, saved by ${saved.updatedByEmail}` : ''}`;

  const add = button('Add field', {
    icon: 'plus',
    onClick: () => {
      if (fields.length >= 20) { toast('A form can have at most 20 fields', 'warn'); return; }
      fields.push({ key: '', label: '', type: 'text', required: false, autoKey: true });
      markDirty(); paintList(); paintPreview();
      const inputs = list.querySelectorAll('.builder-field');
      inputs[inputs.length - 1].querySelector('input').focus();
    },
  });
  const save = button('Save new version', { variant: 'primary' });
  save.addEventListener('click', () => busy(save, async () => {
    const body = { fields: fields.map(({ autoKey, ...f }) => f), approvalField: approvalField || null };
    if (!saved.unsaved) body.version = saved.version;
    try {
      const res = await api(`/admin/forms/${module}`, { method: 'PUT', body: JSON.stringify(body) });
      dirty = false;
      toast(res.message, 'ok');
      rerender();
    } catch (e) { toast(e.message, 'danger'); }
  }));
  // Switching modules with unsaved changes asks first.
  const guard = async (ev) => {
    if (!dirty) return;
    ev.preventDefault();
    const href = ev.currentTarget.getAttribute('href');
    const go = await confirmDialog({ title: 'Discard unsaved changes?', body: `Your changes to the ${MODULES[module].label} form have not been saved.`, confirmLabel: 'Discard changes', danger: true });
    if (go) { dirty = false; window.location.hash = href; }
  };
  page('forms', head, moduleTabs('forms', module, guard),
    h('div', { class: 'layout-2 even' },
      h('div', { class: 'col' }, card({
        cls: 'form', title: `${MODULES[module].label} fields`, actions: status,
        body: [list, h('div', { class: 'row' }, add),
          h('hr', { class: 'divider' }),
          formField({ label: 'Approval value (drives workflow thresholds and auto-approval limits)', control: approvalSelect, controlId: 'approval-field' }).node,
          h('div', { class: 'form-actions' }, save)],
      })),
      h('div', { class: 'col' }, card({ cls: 'sticky', title: 'Preview', body: [h('p', { class: 'muted small' }, 'What a requester sees for this module.'), preview] }))));
}

// ---------- auto-approval overview ----------
async function viewAutoRules() {
  const head = pageHead({ title: 'Auto-approval', description: 'Off by default. A rule lets one person\'s requests in one module skip approval up to a limit, until an end date at most a year away. Nobody can grant one to themselves, and every grant and revocation is in the person\'s history.' });
  page('auto-approval', head, skeleton(6, 'Loading auto-approval rules'));
  const [config, directory] = await Promise.all([loadConfig(), loadDirectory()]);
  const users = (directory.items || []).filter((u) => u.managed && u.status === 'ACTIVE' && u.userId !== state.me.userId)
    .sort((a, b) => displayName(a).localeCompare(displayName(b)));
  const rules = config.autoRules || [];
  const who = select({ id: 'auto-person', placeholder: users.length ? null : 'Nobody to choose', options: users.map((u) => ({ value: u.userId, label: displayName(u), description: personLine(u) })), value: (users[0] || {}).userId || '' });
  const module = select({ id: 'auto-module', placeholder: null, options: [] });
  const syncModules = () => {
    const u = users.find((x) => x.userId === who.value) || {};
    const allowed = u.role === 'admins' ? Object.keys(MODULES) : (u.modules || []);
    module.setOptions(allowed.length ? allowed.map((m) => ({ value: m, label: (MODULES[m] || {}).label || m })) : [{ value: '', label: 'No modules' }], { keepValue: false });
  };
  who.addEventListener('change', syncModules);
  syncModules();
  const grant = grantFields({ moduleControl: module, idPrefix: 'auto' });
  const submit = button('Grant', { variant: 'primary', type: 'submit', icon: 'bolt', attrs: { disabled: !users.length } });
  const form = h('form', { class: 'card form', novalidate: true, 'aria-labelledby': 'grant-title' },
    h('div', { class: 'card-head' }, h('h2', { id: 'grant-title' }, 'Grant auto-approval')),
    h('div', { class: 'grid4' }, formField({ label: 'Person', control: who, controlId: 'auto-person' }).node, grant.fields.module.node, grant.fields.limit.node, grant.fields.until.node),
    grant.fields.note.node,
    h('div', { class: 'form-actions' }, submit));
  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    if (!module.value) { toast('That person has no modules', 'warn'); return; }
    const values = grant.read();
    if (!values) return;
    const u = users.find((x) => x.userId === who.value);
    const go = await confirmDialog({ title: 'Grant auto-approval?', body: `${displayName(u)}'s ${(MODULES[module.value] || {}).label} requests up to ${fmtNumber(values.maxAmount)} will be approved with no approver until ${fmtDate(values.expiresOn)}.`, confirmLabel: 'Grant', danger: true });
    if (!go) return;
    await busy(submit, async () => {
      try {
        const res = await api(`/admin/users/${who.value}/auto-rules/${module.value}`, { method: 'PUT', body: JSON.stringify(values) });
        toast(res.message, 'ok');
        rerender();
      } catch (e) { toast(e.message, 'danger'); }
    });
  });
  const table = rules.length ? h('div', { class: 'card flush' }, h('div', { class: 'table-scroll' }, h('table', { class: 'table rules-table' },
    h('thead', {}, h('tr', {}, h('th', {}, 'Person'), h('th', {}, 'Module'), h('th', { class: 'num' }, 'Up to'), h('th', {}, 'Until'), h('th', {}, 'Granted by'), h('th', {}, h('span', { class: 'sr-only' }, 'Actions')))),
    h('tbody', {}, rules.map((r) => h('tr', { class: r.active ? '' : 'dim' },
      h('td', {}, h('a', { href: `#/admin/users/${r.userId}`, class: 'title' }, r.name || r.email)),
      h('td', {}, (MODULES[r.module] || {}).label || r.module),
      h('td', { class: 'num' }, fmtNumber(r.maxAmount)),
      h('td', {}, fmtDate(r.expiresOn), ' ', r.active ? null : h('span', { class: 'badge muted' }, 'Expired')),
      h('td', { class: 'muted small' }, r.createdByEmail || '', h('div', {}, timeAgo(r.createdAt))),
      h('td', {}, h('button', {
        class: 'linkish danger-text', type: 'button',
        onclick: async () => {
          if (!(await confirmDialog({ title: 'Revoke this auto-approval?', body: `${r.email}'s next ${(MODULES[r.module] || {}).label} request goes through approval.`, confirmLabel: 'Revoke', danger: true }))) return;
          try { await api(`/admin/users/${r.userId}/auto-rules/${r.module}`, { method: 'DELETE' }); toast('Revoked', 'ok'); rerender(); } catch (e) { toast(e.message, 'danger'); }
        },
      }, 'Revoke'))))))))
    : card({ body: emptyState({ icon: 'bolt', title: 'No auto-approval rules', body: 'Every request goes through its approval chain.' }) });
  page('auto-approval', head, form, h('h2', { class: 'section-label' }, 'Current rules'), table);
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
