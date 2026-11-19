// A small original icon set: 24 x 24, 1.75 stroke, round joins. Icons are decorative
// (aria-hidden) unless a label is given, in which case they get role="img".
import { svg } from './dom.js';

const P = {
  plus: ['M12 5v14', 'M5 12h14'],
  refresh: ['M20 11a8 8 0 0 0-14.5-4.5L4 8', 'M4 4v4h4', 'M4 13a8 8 0 0 0 14.5 4.5L20 16', 'M20 20v-4h-4'],
  search: ['M10.5 17a6.5 6.5 0 1 0 0-13 6.5 6.5 0 0 0 0 13z', 'M20 20l-4.8-4.8'],
  check: ['M5 12.5l4.5 4.5L19 7.5'],
  close: ['M6 6l12 12', 'M18 6L6 18'],
  chevronDown: ['M6 9l6 6 6-6'],
  chevronUp: ['M6 15l6-6 6 6'],
  chevronLeft: ['M15 6l-6 6 6 6'],
  chevronRight: ['M9 6l6 6-6 6'],
  back: ['M19 12H5', 'M11 6l-6 6 6 6'],
  calendar: ['M5 6h14a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1z', 'M4 10.5h16', 'M8.5 3.5v4', 'M15.5 3.5v4'],
  upload: ['M12 15V4.5', 'M7.5 9L12 4.5 16.5 9', 'M5 15.5V19h14v-3.5'],
  download: ['M12 4.5V15', 'M7.5 10.5L12 15l4.5-4.5', 'M5 15.5V19h14v-3.5'],
  file: ['M14 3.5H7a1 1 0 0 0-1 1v15a1 1 0 0 0 1 1h10a1 1 0 0 0 1-1V7.5z', 'M14 3.5v4h4', 'M9 12.5h6', 'M9 16h4'],
  inbox: ['M4 13.5l2.5-8h11l2.5 8', 'M4 13.5V19h16v-5.5', 'M4 13.5h4.5a3.5 3.5 0 0 0 7 0H20'],
  user: ['M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8z', 'M4.5 20a7.5 7.5 0 0 1 15 0'],
  users: ['M9 11.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7z', 'M3 19.5a6 6 0 0 1 12 0', 'M15.5 4.8a3.5 3.5 0 0 1 0 6.4', 'M17.5 14a6 6 0 0 1 3.5 5.5'],
  org: ['M10 3.5h4v4h-4z', 'M4 16.5h4v4H4z', 'M16 16.5h4v4h-4z', 'M12 7.5v4.5', 'M6 16.5V12h12v4.5'],
  layers: ['M12 4l8.5 4.5L12 13 3.5 8.5z', 'M3.5 12.5L12 17l8.5-4.5', 'M3.5 16.5L12 21l8.5-4.5'],
  form: ['M6 3.5h12a1 1 0 0 1 1 1v15a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1v-15a1 1 0 0 1 1-1z', 'M8.5 8h7', 'M8.5 12h7', 'M8.5 16h4'],
  bolt: ['M13 3L5 13.5h6L10.5 21 19 10.5h-6z'],
  logout: ['M14.5 4.5H19a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1h-4.5', 'M10 16l4-4-4-4', 'M14 12H4'],
  alert: ['M12 4l9 15.5H3z', 'M12 10v4', 'M12 17v.01'],
  info: ['M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z', 'M12 11v5.5', 'M12 7.5v.01'],
  checkCircle: ['M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z', 'M8 12.5l3 3 5-6'],
  xCircle: ['M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z', 'M9 9l6 6', 'M15 9l-6 6'],
  clock: ['M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z', 'M12 7.5V12l3 2'],
  mail: ['M4 6h16v12H4z', 'M4 7l8 6 8-6'],
  shield: ['M12 3.5l7 2.5v5.5c0 4.5-3 7.8-7 9-4-1.2-7-4.5-7-9V6z', 'M9 12l2 2 4-4.5'],
  key: ['M8.5 16a4.5 4.5 0 1 0 0-9 4.5 4.5 0 0 0 0 9z', 'M12.5 11.5H21', 'M18 11.5V15', 'M15 11.5V14'],
  trash: ['M5 7h14', 'M10 4h4', 'M7 7l1 12.5h8L17 7', 'M10.5 11v5', 'M13.5 11v5'],
  edit: ['M4 20h4L19 9a2.8 2.8 0 0 0-4-4L4 16z', 'M13.5 6.5l4 4'],
  bag: ['M5 8h14l-1 12H6z', 'M9 8V6.5a3 3 0 0 1 6 0V8'],
  sun: ['M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8z', 'M12 2.5v2', 'M12 19.5v2', 'M2.5 12h2', 'M19.5 12h2', 'M5.3 5.3l1.4 1.4', 'M17.3 17.3l1.4 1.4', 'M5.3 18.7l1.4-1.4', 'M17.3 6.7l1.4-1.4'],
  branch: ['M7 4v11', 'M7 20a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5z', 'M17 9a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5z', 'M17 9c0 5-10 3-10 8'],
  chat: ['M5 5h14v10H10l-4 4v-4H5z'],
  stamp: ['M9 4h6l-1 6h-4z', 'M6 13h12v3H6z', 'M5 19.5h14'],
  menu: ['M4 7h16', 'M4 12h16', 'M4 17h16'],
  route: ['M6 19a2 2 0 1 0 0-4 2 2 0 0 0 0 4z', 'M18 9a2 2 0 1 0 0-4 2 2 0 0 0 0 4z', 'M8 17h7a3 3 0 0 0 0-6H9a3 3 0 0 1 0-6h7'],
  filter: ['M4 5h16l-6 7.5V19l-4-2v-4.5z'],
  up: ['M12 19V5', 'M6 11l6-6 6 6'],
  down: ['M12 5v14', 'M6 13l6 6 6-6'],
};

export const MODULE_ICON = { PURCHASE: 'bag', ACCESS: 'key', LEAVE: 'sun', CHANGE: 'branch', GENERAL: 'chat' };

export function icon(name, { size = 18, label = null, cls = '' } = {}) {
  const el = svg('svg', {
    viewBox: '0 0 24 24', width: size, height: size, fill: 'none', stroke: 'currentColor',
    'stroke-width': 1.75, 'stroke-linecap': 'round', 'stroke-linejoin': 'round',
    class: `icon ${cls}`.trim(), focusable: 'false',
    ...(label ? { role: 'img', 'aria-label': label } : { 'aria-hidden': 'true' }),
  }, ...(P[name] || P.info).map((d) => svg('path', { d })));
  return el;
}

// The Request Hub mark: a routing slip with a stamped tick.
export function brandMark(size = 28) {
  return svg('svg', { viewBox: '0 0 32 32', width: size, height: size, class: 'mark', 'aria-hidden': 'true', focusable: 'false' },
    svg('rect', { x: 1, y: 1, width: 30, height: 30, rx: 8, class: 'mark-bg' }),
    svg('path', { d: 'M10 9h12M10 13.5h8', class: 'mark-line' }),
    svg('circle', { cx: 20, cy: 21, r: 5.5, class: 'mark-stamp' }),
    svg('path', { d: 'M17.6 21.2l1.7 1.7 3.2-3.6', class: 'mark-tick' }));
}
