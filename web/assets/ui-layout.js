// Page structure: page header, panels, the account menu, a drop zone for files and segmented filters.
import { h, uid, onOutside } from './dom.js';
import { icon } from './icons.js';

// The page title is the page's only h1. `back` is { href, label }.
export function pageHead({ title, description = '', back = null, actions = [], aside = null }) {
  return h('div', { class: 'section-head' },
    h('div', { class: 'section-title' },
      back ? h('a', { href: back.href, class: 'back-link' }, icon('back', { size: 16 }), back.label) : null,
      h('h1', {}, title),
      description ? h('p', { class: 'muted lead-sm' }, description) : null),
    h('div', { class: 'row section-actions' }, aside, ...[].concat(actions)));
}

export function card({ title = null, actions = null, body = [], cls = '', level = 2, id = null }) {
  return h('section', { class: `card ${cls}`.trim(), id, 'aria-labelledby': title && id ? `${id}-title` : null },
    title ? h('div', { class: 'card-head' }, h(`h${level}`, { id: id ? `${id}-title` : null }, title), actions) : null,
    ...[].concat(body));
}

// A menu button (WAI-ARIA menu pattern): arrows move, Escape closes, Tab leaves.
// The trigger's name comes from its content (WCAG 2.5.3: the visible text is part of the name).
export function menu({ content, items }) {
  const menuId = uid('menu');
  const trigger = h('button', { type: 'button', class: 'menu-trigger', 'aria-haspopup': 'menu', 'aria-expanded': 'false', 'aria-controls': menuId }, content);
  const list = h('ul', { id: menuId, role: 'menu', class: 'menu-pop', hidden: true, 'aria-label': 'Account' });
  let removeOutside = null;
  const buttons = [];
  items.forEach((item) => {
    if (item.header) { list.append(h('li', { role: 'none', class: 'menu-header' }, item.header)); return; }
    const btn = h('button', { type: 'button', role: 'menuitem', tabindex: '-1', class: item.danger ? 'danger-text' : '' }, item.icon ? icon(item.icon, { size: 16 }) : null, item.label);
    btn.addEventListener('click', () => { close(false); item.onClick(); });
    buttons.push(btn);
    list.append(h('li', { role: 'none' }, btn));
  });
  const open = (focusIndex = 0) => {
    list.hidden = false;
    trigger.setAttribute('aria-expanded', 'true');
    if (buttons[focusIndex]) buttons[focusIndex].focus();
    removeOutside = onOutside([trigger, list], () => close(false));
  };
  const close = (returnFocus = true) => {
    list.hidden = true;
    trigger.setAttribute('aria-expanded', 'false');
    if (removeOutside) { removeOutside(); removeOutside = null; }
    if (returnFocus) trigger.focus();
  };
  trigger.addEventListener('click', () => (list.hidden ? open() : close()));
  trigger.addEventListener('keydown', (ev) => {
    if (ev.key === 'ArrowDown' || ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); open(0); }
    if (ev.key === 'ArrowUp') { ev.preventDefault(); open(buttons.length - 1); }
  });
  list.addEventListener('keydown', (ev) => {
    const i = buttons.indexOf(document.activeElement);
    if (ev.key === 'ArrowDown') { ev.preventDefault(); buttons[(i + 1) % buttons.length].focus(); }
    else if (ev.key === 'ArrowUp') { ev.preventDefault(); buttons[(i - 1 + buttons.length) % buttons.length].focus(); }
    else if (ev.key === 'Home') { ev.preventDefault(); buttons[0].focus(); }
    else if (ev.key === 'End') { ev.preventDefault(); buttons[buttons.length - 1].focus(); }
    else if (ev.key === 'Escape') { ev.preventDefault(); close(); }
    else if (ev.key === 'Tab') { close(false); }
  });
  return h('div', { class: 'menu' }, trigger, list);
}

// A file drop zone around a real file input, so the keyboard and screen readers get a normal control.
export function fileDrop({ label, accept = '', hint = '', id = uid('file') }) {
  const input = h('input', { id, type: 'file', accept, class: 'sr-only', 'aria-label': label });
  const chosen = h('span', { class: 'drop-chosen', 'aria-live': 'polite' });
  const zone = h('label', { class: 'drop', for: id },
    icon('upload', { size: 22 }),
    h('span', { class: 'drop-text' }, h('strong', {}, 'Drop a file here'), h('span', { class: 'muted' }, ' or '), h('span', { class: 'link-like' }, 'browse')),
    hint ? h('span', { class: 'muted small' }, hint) : null, chosen);
  const show = () => {
    const f = input.files && input.files[0];
    chosen.textContent = f ? `${f.name} (${f.size < 1024 ? `${f.size} bytes` : `${Math.ceil(f.size / 1024)} KB`})` : '';
    zone.classList.toggle('has-file', Boolean(f));
  };
  input.addEventListener('change', show);
  ['dragenter', 'dragover'].forEach((t) => zone.addEventListener(t, (ev) => { ev.preventDefault(); zone.classList.add('dragging'); }));
  ['dragleave', 'drop'].forEach((t) => zone.addEventListener(t, () => zone.classList.remove('dragging')));
  zone.addEventListener('drop', (ev) => {
    ev.preventDefault();
    if (ev.dataTransfer && ev.dataTransfer.files.length) {
      input.files = ev.dataTransfer.files;
      input.dispatchEvent(new Event('change', { bubbles: true }));
    }
  });
  const node = h('div', { class: 'drop-wrap' }, input, zone);
  node.input = input;
  node.reset = () => { input.value = ''; show(); };
  return node;
}

// A row of toggle buttons for filtering (one pressed at a time).
export function segmented({ label, options, value, onChange }) {
  let current = value;
  const group = h('div', { class: 'segmented', role: 'group', 'aria-label': label });
  const paint = () => [...group.children].forEach((b) => b.setAttribute('aria-pressed', b.dataset.value === current ? 'true' : 'false'));
  options.forEach(([v, text]) => {
    const b = h('button', { type: 'button', 'data-value': v }, text);
    b.addEventListener('click', () => { current = v; paint(); onChange(v); });
    group.append(b);
  });
  paint();
  return group;
}

export function stat(label, value, tone = '') {
  return h('div', { class: `stat ${tone}`.trim() }, h('span', { class: 'stat-value' }, String(value)), h('span', { class: 'stat-label' }, label));
}
