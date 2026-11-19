// Buttons, text inputs, field wrappers and choice cards.
import { h, uid } from './dom.js';
import { icon } from './icons.js';

// variant: primary | secondary | ghost | danger | danger-solid | success
export function button(label, { variant = 'secondary', icon: iconName = null, iconRight = null, size = '', type = 'button', onClick = null, attrs = {} } = {}) {
  const cls = ['btn', variant === 'secondary' ? '' : variant.replace('danger-solid', 'danger solid').replace('success', 'ok'), size].filter(Boolean).join(' ');
  return h('button', { class: cls, type, onclick: onClick, ...attrs },
    iconName ? icon(iconName, { size: size === 'sm' ? 16 : 18 }) : null,
    label ? h('span', { class: 'btn-label' }, label) : null,
    iconRight ? icon(iconRight, { size: 16 }) : null);
}

export function linkButton(label, href, { variant = 'secondary', icon: iconName = null, attrs = {} } = {}) {
  const cls = ['btn', variant === 'secondary' ? '' : variant].filter(Boolean).join(' ');
  return h('a', { class: cls, href, ...attrs }, iconName ? icon(iconName) : null, h('span', { class: 'btn-label' }, label));
}

export function iconButton(iconName, label, { onClick = null, variant = 'ghost', attrs = {} } = {}) {
  return h('button', { class: `btn icon ${variant === 'ghost' ? '' : variant}`.trim(), type: 'button', 'aria-label': label, title: label, onclick: onClick, ...attrs },
    icon(iconName, { size: 16 }));
}

// Puts a spinner in a button while an async action runs, and restores it afterwards.
export async function busy(btn, fn) {
  btn.disabled = true;
  btn.classList.add('loading');
  try { return await fn(); } finally { btn.disabled = false; btn.classList.remove('loading'); }
}

// A labelled field: label, control, optional hint, and an error line that a screen reader announces.
export function formField({ label, control, controlId = null, hint = '', required = false, wide = false, cls = '' }) {
  const id = controlId || control.id || (control.querySelector && control.querySelector('[id]') && control.querySelector('[id]').id);
  const error = h('span', { class: 'field-error', role: 'alert' });
  const hintEl = hint ? h('span', { class: 'hint-inline', id: `${id}-hint` }, hint) : null;
  const node = h('div', { class: `dyn-field${wide ? ' wide' : ''} ${cls}`.trim() },
    h('label', { for: id }, label, required ? h('span', { class: 'req', 'aria-hidden': 'true' }, ' *') : null),
    control, hintEl, error);
  return {
    node,
    error,
    setError(message) {
      error.textContent = message || '';
      node.classList.toggle('has-error', Boolean(message));
      const input = node.querySelector('input, textarea, button[role=combobox]');
      if (input) input.setAttribute('aria-invalid', message ? 'true' : 'false');
    },
  };
}

export function textInput({ id = uid('in'), value = '', placeholder = '', type = 'text', attrs = {} } = {}) {
  const el = h('input', { id, type, placeholder, autocomplete: 'off', ...attrs });
  if (value !== undefined && value !== null) el.value = value;
  return el;
}

// An input with a fixed prefix (currency code) or suffix (unit) inside the box.
export function adornedInput(input, { prefix = null, suffix = null } = {}) {
  return h('div', { class: 'adorned' }, prefix ? h('span', { class: 'adorn' }, prefix) : null, input, suffix ? h('span', { class: 'adorn' }, suffix) : null);
}

export function searchInput({ placeholder, label, onInput }) {
  const input = h('input', { type: 'search', placeholder, 'aria-label': label, autocomplete: 'off' });
  input.addEventListener('input', onInput);
  return { node: h('div', { class: 'search' }, icon('search', { size: 16 }), input), input };
}

// A selectable card holding a native radio or checkbox, so keyboard and screen readers work as usual.
export function choiceCard({ type = 'radio', name = '', value = '', title, description = '', iconName = null, checked = false }) {
  const input = h('input', { type, name, value, checked });
  return {
    input,
    node: h('label', { class: 'choice' }, input,
      iconName ? h('span', { class: 'choice-icon' }, icon(iconName, { size: 20 })) : null,
      h('span', { class: 'choice-body' }, h('strong', {}, title), description ? h('span', { class: 'muted small' }, description) : null)),
  };
}

export function checkbox(label, { checked = false, description = '', attrs = {} } = {}) {
  const input = h('input', { type: 'checkbox', checked, ...attrs });
  return { input, node: h('label', { class: 'check' }, input, h('span', {}, label, description ? h('span', { class: 'muted small block' }, description) : null)) };
}

export function switchToggle(label, { checked = false } = {}) {
  const input = h('input', { type: 'checkbox', role: 'switch', class: 'switch', checked });
  return { input, node: h('label', { class: 'switch-row' }, input, h('span', {}, label)) };
}
