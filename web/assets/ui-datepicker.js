// A date field: type YYYY-MM-DD or pick from a calendar. The value is always an ISO date string.
// Keyboard in the calendar: arrows move by day or week, Home/End to the week's start or end,
// Page Up/Down by month (with Shift by year), Enter picks, Escape closes.
import { h, uid, onOutside, placePopover } from './dom.js';
import { icon } from './icons.js';
import { iconButton } from './ui-controls.js';

const ISO = /^\d{4}-\d{2}-\d{2}$/;
const pad = (n) => String(n).padStart(2, '0');
export const toIso = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
export function parseIso(text) {
  if (!ISO.test(String(text || '').trim())) return null;
  const [y, m, d] = text.trim().split('-').map(Number);
  const date = new Date(y, m - 1, d);
  return date.getFullYear() === y && date.getMonth() === m - 1 && date.getDate() === d ? date : null;
}
const longDate = (d) => d.toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' });

function firstDayOfWeek() {
  try {
    const loc = new Intl.Locale(navigator.language || 'en-GB');
    const info = (loc.getWeekInfo && loc.getWeekInfo()) || loc.weekInfo;
    if (info && info.firstDay) return info.firstDay % 7; // 7 = Sunday
  } catch { /* older browsers */ }
  return 1;
}

export function datePicker({ id = uid('date'), value = '', min = '', max = '', placeholder = 'YYYY-MM-DD' } = {}) {
  const friendlyId = `${id}-friendly`;
  const input = h('input', { id, type: 'text', inputmode: 'numeric', placeholder, autocomplete: 'off', class: 'date-input', 'aria-describedby': friendlyId, maxlength: '10' });
  input.value = value || '';
  const friendly = h('span', { class: 'date-friendly', id: friendlyId, 'aria-live': 'polite' });
  const toggle = iconButton('calendar', 'Choose date', { attrs: { 'aria-haspopup': 'dialog', 'aria-expanded': 'false', class: 'btn icon date-toggle' } });
  const title = h('div', { class: 'cal-title', 'aria-live': 'polite' });
  const prev = iconButton('chevronLeft', 'Previous month');
  const next = iconButton('chevronRight', 'Next month');
  const grid = h('table', { class: 'cal-grid', role: 'grid' });
  const today = h('button', { type: 'button', class: 'btn sm ghost' }, 'Today');
  const clear = h('button', { type: 'button', class: 'btn sm ghost' }, 'Clear');
  const pop = h('div', { class: 'date-pop', role: 'dialog', 'aria-label': 'Choose a date', hidden: true },
    h('div', { class: 'cal-head' }, prev, title, next), grid, h('div', { class: 'cal-foot' }, today, clear));
  const root = h('div', { class: 'date' }, h('div', { class: 'date-box' }, icon('calendar', { size: 16, cls: 'date-icon' }), input, toggle), friendly, pop);

  const startDay = firstDayOfWeek();
  let view = parseIso(input.value) || new Date();
  let focusDate = new Date(view);
  let removeOutside = null;
  let minDate = parseIso(min);
  let maxDate = parseIso(max);

  const inRange = (d) => (!minDate || d >= minDate) && (!maxDate || d <= maxDate);

  const paintFriendly = () => {
    const raw = input.value.trim();
    const d = parseIso(raw);
    friendly.textContent = !raw ? '' : d ? longDate(d) : 'Use the format YYYY-MM-DD, or pick from the calendar';
    root.classList.toggle('invalid', Boolean(raw) && !d);
  };

  const paintMonth = () => {
    const y = focusDate.getFullYear();
    const m = focusDate.getMonth();
    title.textContent = new Date(y, m, 1).toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
    const names = [...Array(7)].map((_, i) => {
      const d = new Date(2024, 0, 7 + ((startDay + i) % 7)); // 7 Jan 2024 was a Sunday
      return h('th', { scope: 'col', abbr: d.toLocaleDateString(undefined, { weekday: 'long' }) }, d.toLocaleDateString(undefined, { weekday: 'narrow' }));
    });
    const first = new Date(y, m, 1);
    const offset = (first.getDay() - startDay + 7) % 7;
    const start = new Date(y, m, 1 - offset);
    const selected = parseIso(input.value);
    const todayIso = toIso(new Date());
    const rows = [];
    for (let w = 0; w < 6; w += 1) {
      const cells = [];
      for (let d = 0; d < 7; d += 1) {
        const date = new Date(start.getFullYear(), start.getMonth(), start.getDate() + w * 7 + d);
        const iso = toIso(date);
        const btn = h('button', {
          type: 'button', class: `cal-day${date.getMonth() !== m ? ' outside' : ''}`, 'data-date': iso,
          tabindex: iso === toIso(focusDate) ? '0' : '-1', 'aria-label': longDate(date),
          'aria-pressed': selected && iso === toIso(selected) ? 'true' : 'false', 'aria-current': iso === todayIso ? 'date' : null,
          disabled: !inRange(date),
        }, String(date.getDate()));
        btn.addEventListener('click', () => pick(date));
        cells.push(h('td', { role: 'gridcell' }, btn));
      }
      rows.push(h('tr', {}, cells));
    }
    grid.replaceChildren(h('thead', {}, h('tr', {}, names)), h('tbody', {}, rows));
  };

  const focusDay = () => {
    const btn = grid.querySelector(`[data-date="${toIso(focusDate)}"]`);
    if (btn) btn.focus();
  };

  const shift = (days = 0, months = 0) => {
    const d = new Date(focusDate.getFullYear(), focusDate.getMonth() + months, focusDate.getDate() + days);
    if (months && d.getDate() !== focusDate.getDate()) d.setDate(0); // 31 Jan + 1 month = 28/29 Feb
    focusDate = d;
    paintMonth();
    focusDay();
  };

  const open = () => {
    if (!pop.hidden) return;
    focusDate = parseIso(input.value) || (minDate && new Date() < minDate ? new Date(minDate) : new Date());
    paintMonth();
    pop.hidden = false;
    toggle.setAttribute('aria-expanded', 'true');
    placePopover(pop, root.querySelector('.date-box'));
    focusDay();
    removeOutside = onOutside([root], () => close(false));
  };

  const close = (returnFocus = true) => {
    if (pop.hidden) return;
    pop.hidden = true;
    toggle.setAttribute('aria-expanded', 'false');
    if (removeOutside) { removeOutside(); removeOutside = null; }
    if (returnFocus) input.focus();
  };

  const commit = (text) => {
    input.value = text;
    paintFriendly();
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
  };

  const pick = (date) => {
    if (!inRange(date)) return;
    commit(toIso(date));
    close(true);
  };

  toggle.addEventListener('click', () => (pop.hidden ? open() : close(true)));
  prev.addEventListener('click', () => shift(0, -1));
  next.addEventListener('click', () => shift(0, 1));
  today.addEventListener('click', () => pick(new Date()));
  clear.addEventListener('click', () => { commit(''); close(true); });
  input.addEventListener('input', paintFriendly);
  input.addEventListener('keydown', (ev) => { if (ev.key === 'ArrowDown' && ev.altKey) { ev.preventDefault(); open(); } });
  grid.addEventListener('keydown', (ev) => {
    const keys = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [-7, 0], ArrowDown: [7, 0] };
    if (keys[ev.key]) { ev.preventDefault(); shift(...keys[ev.key]); return; }
    if (ev.key === 'Home') { ev.preventDefault(); shift(-((focusDate.getDay() - startDay + 7) % 7)); }
    else if (ev.key === 'End') { ev.preventDefault(); shift(6 - ((focusDate.getDay() - startDay + 7) % 7)); }
    else if (ev.key === 'PageUp') { ev.preventDefault(); shift(0, ev.shiftKey ? -12 : -1); }
    else if (ev.key === 'PageDown') { ev.preventDefault(); shift(0, ev.shiftKey ? 12 : 1); }
  });
  pop.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') { ev.preventDefault(); close(true); } });

  Object.defineProperty(root, 'value', { get: () => input.value.trim(), set: (v) => { input.value = v || ''; paintFriendly(); } });
  root.setRange = (lo, hi) => { minDate = parseIso(lo); maxDate = parseIso(hi); };
  root.input = input;
  paintFriendly();
  return root;
}
