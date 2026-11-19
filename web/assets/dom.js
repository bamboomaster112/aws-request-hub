// DOM helpers shared by every module. Values are always rendered as text, never as HTML.
// The Content Security Policy forbids inline styles, so nothing here sets a style attribute;
// components that position popovers use the CSSOM (el.style.x), which the policy allows.

const SVG_NS = 'http://www.w3.org/2000/svg';

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  setAttrs(el, attrs);
  append(el, children);
  return el;
}

export function svg(tag, attrs = {}, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value !== null && value !== undefined && value !== false) el.setAttribute(key, String(value));
  }
  children.flat().forEach((c) => c && el.append(c));
  return el;
}

function setAttrs(el, attrs) {
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') el.className = value;
    else if (key.startsWith('on') && typeof value === 'function') el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? '' : String(value));
  }
}

function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

export function mount(...nodes) {
  document.getElementById('app').replaceChildren(...nodes.flat().filter((n) => n !== null && n !== undefined && n !== false));
}

let counter = 0;
export function uid(prefix = 'ui') {
  counter += 1;
  return `${prefix}-${counter}-${Math.random().toString(36).slice(2, 6)}`;
}

// Calls fn when a pointer goes down outside every element in `inside`. Returns a remover.
export function onOutside(inside, fn) {
  const handler = (ev) => { if (!inside.some((el) => el && el.contains(ev.target))) fn(ev); };
  document.addEventListener('pointerdown', handler, true);
  return () => document.removeEventListener('pointerdown', handler, true);
}

// Places a popover under (or, when there is no room, over) its anchor, inside the viewport.
export function placePopover(pop, anchor) {
  const a = anchor.getBoundingClientRect();
  const room = window.innerHeight - a.bottom;
  pop.classList.toggle('above', room < pop.offsetHeight + 16 && a.top > room);
  const overflowRight = a.left + pop.offsetWidth - (window.innerWidth - 8);
  pop.style.left = overflowRight > 0 ? `${-overflowRight}px` : '0px';
}

export const reducedMotion = () => window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
