// An accessible single-select: a combobox button that opens a listbox (WAI-ARIA "select-only combobox").
// Options can carry a second line and an icon. Long lists get a filter box.
//
//   const s = select({ options: [{ value: 'a', label: 'Alpha', description: 'First' }], value: 'a' });
//   s.value; s.value = 'b'; s.setOptions(list); s.addEventListener('change', ...)
import { h, uid, onOutside, placePopover } from './dom.js';
import { icon } from './icons.js';

export function select({ id = uid('sel'), options = [], value = '', placeholder = 'Choose...', searchable = null, ariaLabel = null, disabled = false, cls = '' } = {}) {
  const listId = `${id}-list`;
  let opts = [];
  let current = value ?? '';
  let active = -1;
  let filter = '';
  let removeOutside = null;
  let typed = '';
  let typedAt = 0;

  const valueEl = h('span', { class: 'select-value' });
  const trigger = h('button', {
    id, type: 'button', class: 'select-trigger', role: 'combobox', 'aria-haspopup': 'listbox', 'aria-expanded': 'false',
    'aria-controls': listId, 'aria-label': ariaLabel, disabled,
  }, valueEl, icon('chevronDown', { size: 16, cls: 'select-chevron' }));
  const list = h('ul', { id: listId, role: 'listbox', tabindex: '-1', class: 'select-list', 'aria-labelledby': id });
  const search = h('input', { type: 'search', class: 'select-search', placeholder: 'Type to filter', 'aria-label': 'Filter the list', 'aria-controls': listId, autocomplete: 'off' });
  const pop = h('div', { class: 'select-pop', hidden: true }, search, list);
  const root = h('div', { class: `select ${cls}`.trim() }, trigger, pop);

  const visible = () => opts.filter((o) => !filter || `${o.label} ${o.description || ''}`.toLowerCase().includes(filter));
  const optionId = (i) => `${id}-opt-${i}`;

  const paintValue = () => {
    const o = opts.find((x) => String(x.value) === String(current));
    valueEl.replaceChildren(...(o ? [o.icon ? icon(o.icon, { size: 16 }) : null, h('span', {}, o.label)].filter(Boolean) : [h('span', { class: 'placeholder' }, placeholder)]));
    trigger.dataset.value = current;
    root.dataset.value = current;
  };

  const paintList = () => {
    const shown = visible();
    list.replaceChildren(...(shown.length ? shown.map((o, i) => {
      const selected = String(o.value) === String(current);
      const li = h('li', {
        id: optionId(i), role: 'option', 'data-value': o.value, 'aria-selected': selected ? 'true' : 'false',
        'aria-disabled': o.disabled ? 'true' : null, class: i === active ? 'active' : '',
      },
      o.icon ? icon(o.icon, { size: 16, cls: 'opt-icon' }) : null,
      h('span', { class: 'opt-text' }, h('span', { class: 'opt-label' }, o.label), o.description ? h('span', { class: 'opt-desc' }, o.description) : null),
      selected ? icon('check', { size: 16, cls: 'opt-check' }) : null);
      li.addEventListener('pointerdown', (ev) => ev.preventDefault());
      li.addEventListener('click', () => { if (!o.disabled) choose(o.value); });
      li.addEventListener('pointermove', () => { if (active !== i) { active = i; markActive(); } });
      return li;
    }) : [h('li', { class: 'select-empty', role: 'presentation' }, 'No matches')]));
    markActive();
  };

  const markActive = () => {
    [...list.children].forEach((li, i) => li.classList.toggle('active', i === active));
    const focusEl = searchable ? search : list;
    if (active >= 0 && list.children[active]) {
      focusEl.setAttribute('aria-activedescendant', optionId(active));
      list.children[active].scrollIntoView({ block: 'nearest' });
    } else {
      focusEl.removeAttribute('aria-activedescendant');
    }
  };

  const move = (delta) => {
    const shown = visible();
    if (!shown.length) return;
    let i = active;
    for (let step = 0; step < shown.length; step += 1) {
      i = Math.max(0, Math.min(shown.length - 1, i + delta));
      if (!shown[i].disabled) break;
    }
    active = i;
    markActive();
  };

  const open = () => {
    if (trigger.disabled || !pop.hidden) return;
    filter = '';
    search.value = '';
    search.hidden = !searchable;
    const shown = visible();
    active = Math.max(0, shown.findIndex((o) => String(o.value) === String(current)));
    paintList();
    pop.hidden = false;
    trigger.setAttribute('aria-expanded', 'true');
    root.classList.add('open');
    placePopover(pop, trigger);
    (searchable ? search : list).focus();
    markActive();
    removeOutside = onOutside([root], () => close(false));
  };

  const close = (returnFocus = true) => {
    if (pop.hidden) return;
    pop.hidden = true;
    trigger.setAttribute('aria-expanded', 'false');
    root.classList.remove('open');
    if (removeOutside) { removeOutside(); removeOutside = null; }
    if (returnFocus) trigger.focus();
  };

  const choose = (v) => {
    const changed = String(v) !== String(current);
    current = v;
    paintValue();
    close(true);
    if (changed) {
      root.dispatchEvent(new Event('input', { bubbles: true }));
      root.dispatchEvent(new Event('change', { bubbles: true }));
    }
  };

  const onListKey = (ev) => {
    const shown = visible();
    if (ev.key === 'ArrowDown') { ev.preventDefault(); move(1); }
    else if (ev.key === 'ArrowUp') { ev.preventDefault(); move(-1); }
    else if (ev.key === 'Home') { ev.preventDefault(); active = -1; move(1); }
    else if (ev.key === 'End') { ev.preventDefault(); active = shown.length; move(-1); }
    else if (ev.key === 'PageDown') { ev.preventDefault(); move(5); }
    else if (ev.key === 'PageUp') { ev.preventDefault(); move(-5); }
    else if (ev.key === 'Enter' || (ev.key === ' ' && ev.target === list)) {
      ev.preventDefault();
      if (shown[active] && !shown[active].disabled) choose(shown[active].value);
    } else if (ev.key === 'Escape') { ev.preventDefault(); close(true); }
    else if (ev.key === 'Tab') { close(false); }
    else if (ev.target === list && ev.key.length === 1 && !ev.ctrlKey && !ev.metaKey) {
      const now = Date.now();
      typed = now - typedAt > 600 ? ev.key.toLowerCase() : typed + ev.key.toLowerCase();
      typedAt = now;
      const hit = shown.findIndex((o) => !o.disabled && o.label.toLowerCase().startsWith(typed));
      if (hit >= 0) { active = hit; markActive(); }
    }
  };

  trigger.addEventListener('click', () => (pop.hidden ? open() : close(true)));
  trigger.addEventListener('keydown', (ev) => {
    if (['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(ev.key)) { ev.preventDefault(); open(); }
  });
  list.addEventListener('keydown', onListKey);
  search.addEventListener('keydown', onListKey);
  search.addEventListener('input', () => { filter = search.value.trim().toLowerCase(); active = 0; paintList(); });

  root.setOptions = (list2, { keepValue = true } = {}) => {
    opts = (list2 || []).map((o) => (typeof o === 'object' ? { ...o, value: String(o.value ?? '') } : { value: String(o), label: String(o) }));
    if (searchable === null) searchable = opts.length > 8;
    if (!keepValue || !opts.some((o) => String(o.value) === String(current))) current = opts.some((o) => o.value === '') ? '' : (placeholder ? '' : (opts[0] || {}).value || '');
    paintValue();
    if (!pop.hidden) paintList();
  };
  Object.defineProperty(root, 'value', {
    get: () => current,
    set: (v) => { current = v === null || v === undefined ? '' : String(v); paintValue(); },
  });
  Object.defineProperty(root, 'disabled', { get: () => trigger.disabled, set: (v) => { trigger.disabled = Boolean(v); } });
  Object.defineProperty(root, 'options', { get: () => opts.slice() });
  root.trigger = trigger;
  root.focus = () => trigger.focus();
  root.close = close;

  root.setOptions(options);
  return root;
}
