'use strict';
// The burger menu next to the logo. Its items are actions that I need rarely.
// Uses $, request (app.js), overwriteCookies, cookieCount (browser.js).

const MENU = {
  cookies: () => overwriteCookies(),
  keys: () => { $('#keys').hidden = false; }, // the list is static, in index.html
};

// "Show shortcuts": Esc or a click outside the box closes it. While it shows, no key may
// reach a terminal or a mode: window, capture phase, runs before every document listener
function closeKeys() {
  $('#keys').hidden = true;
  const t = S.selected && S.terms.get(S.selected);
  if (S.mode === 'organize') $('#board').focus();
  else if (S.mode === 'dev' && t) t.term.focus();
}
$('#keys').addEventListener('click', (e) => { if (e.target === $('#keys')) closeKeys(); });
window.addEventListener('keydown', (e) => {
  if ($('#keys').hidden) return;
  e.preventDefault();
  e.stopImmediatePropagation();
  if (e.key === 'Escape') closeKeys();
}, true);

function openMenu() {
  const m = $('#menu');
  const b = $('#menu-btn').getBoundingClientRect();
  m.style.left = `${b.left}px`;
  m.style.top = `${b.bottom + 4}px`;
  m.hidden = false; // covers the browser view: browser.js shows its snapshot meanwhile
  $('#menu-btn').classList.add('on');
  const note = $('#menu-cookies-note');
  note.textContent = '';
  request('browser.cookies.info').then((r) => {
    const t = r.last && new Date(r.last.time * 1000).toLocaleString([], { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
    note.textContent = r.last ? `Last copy: ${t}, ${cookieCount(r.last)}.` : 'Not copied yet.';
  }).catch(() => {});
}

function closeMenu() {
  $('#menu').hidden = true;
  $('#menu-btn').classList.remove('on');
}

$('#menu-btn').addEventListener('click', () => { if ($('#menu').hidden) openMenu(); else closeMenu(); });
$('#menu').addEventListener('click', (e) => {
  const item = e.target.closest('[data-menu]');
  if (!item) return;
  closeMenu();
  MENU[item.dataset.menu]();
});
document.addEventListener('mousedown', (e) => {
  if (!$('#menu').hidden && !e.target.closest('#menu, #menu-btn')) closeMenu();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !$('#menu').hidden) { e.stopPropagation(); closeMenu(); }
}, true);
