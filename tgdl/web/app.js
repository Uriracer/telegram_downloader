/**
 * Telegram Downloader — web UI.
 *
 * Vanilla ES2020 module, no build step, no external resources. It only talks to the
 * local REST API + SSE (/api/events) with relative URLs.
 *
 * SECURITY: chat names, file names and message texts come from Telegram. The DOM is
 * always built with createElement/textContent (see `h()`); innerHTML is never used.
 *
 * User-facing strings are in Catalan; code and comments are in English.
 * The helpers and the password flow come from 3Cat Downloader.
 */

const MIN_PASSWORD = 8;

const state = {
  /** @type {Record<string, any> | null} GET /api/settings */
  settings: null,
  /** @type {Record<string, any> | null} Telegram session status (GET /api/telegram/status, "telegram" events) */
  telegram: null,
  /** @type {any[] | null} */
  dialogs: null,
  dialogsLoading: false,
  dialogFilter: '',
  dialogKind: '',
  loginTab: 'qr',
  /** @type {Map<number, any>} sources by id */
  sources: new Map(),
  /** @type {number | null} */
  openSource: null,
  msgQuery: { q: '', state: '', offset: 0 },
  /** @type {Record<string, any> | null} GET /api/downloads, "downloads" events */
  downloads: null,
  view: 'telegram',
  /** @type {Record<string, any> | null} */
  auth: null,
  locked: false,
  started: false,
};

// ---------------------------------------------------------------------------
// Small utilities (from 3Cat Downloader)
// ---------------------------------------------------------------------------

const $ = (sel, root = document) => root.querySelector(sel);

/** Properties that must be assigned as DOM properties rather than attributes. */
const DOM_PROPS = new Set(['value', 'checked', 'disabled', 'hidden', 'selected', 'indeterminate', 'readOnly']);

/**
 * Build a DOM element safely. Strings become text nodes, never HTML.
 * @param {string} tag
 * @param {Record<string, any>} [props] `class`, `text`, `dataset`, `onxxx` handlers, DOM props or attributes
 * @param {...any} children Nodes, strings, numbers, arrays, null/false (skipped)
 * @returns {HTMLElement}
 */
function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') el.className = value;
    else if (key === 'text') el.textContent = String(value);
    else if (key === 'dataset') Object.assign(el.dataset, value);
    else if (key.startsWith('on') && typeof value === 'function') el.addEventListener(key.slice(2), value);
    else if (DOM_PROPS.has(key)) el[key] = value;
    else el.setAttribute(key, value === true ? '' : String(value));
  }
  appendChildren(el, children);
  return el;
}

/** Replace all children of `el`, skipping null/false entries (unlike Element.replaceChildren). */
function setChildren(el, ...children) {
  el.replaceChildren();
  appendChildren(el, children);
}

function appendChildren(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : String(child));
  }
}

const pad2 = (n) => String(n).padStart(2, '0');
const numberFmt = new Intl.NumberFormat('ca', { maximumFractionDigits: 1 });

/** Catalan plural helper: plural(3, 'capítol', 'capítols') → "3 capítols". */
function plural(n, one, many) {
  return `${n} ${n === 1 ? one : many}`;
}

/** ISO "YYYY-MM-DD" → "DD/MM/YYYY". */
function formatDate(iso) {
  if (typeof iso !== 'string') return '';
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  return m ? `${m[3]}/${m[2]}/${m[1]}` : iso;
}

/** Seconds → "1 h 05 min", "45 min", "1 min 46 s", "40 s". */
function formatDuration(seconds) {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0) return '';
  const s = Math.round(seconds);
  if (s < 60) return `${s} s`;
  if (s < 600) return `${Math.floor(s / 60)} min ${pad2(s % 60)} s`;
  const totalMin = Math.round(s / 60);
  if (totalMin < 60) return `${totalMin} min`;
  return `${Math.floor(totalMin / 60)} h ${pad2(totalMin % 60)} min`;
}

/** Bytes → "450 MB". */
function formatBytes(bytes) {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes) || bytes < 0) return '';
  const units = ['B', 'kB', 'MB', 'GB', 'TB'];
  let v = bytes;
  let i = 0;
  while (v >= 1000 && i < units.length - 1) { v /= 1000; i += 1; }
  return `${numberFmt.format(i === 0 ? Math.round(v) : v)} ${units[i]}`;
}

/** Remaining seconds → "Queden 3 min 20 s". */
function formatEta(seconds) {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0) return '';
  const s = Math.round(seconds);
  if (s < 60) return `Queden ${s} s`;
  if (s < 3600) return `Queden ${Math.floor(s / 60)} min ${pad2(s % 60)} s`;
  return `Queden ${Math.floor(s / 3600)} h ${pad2(Math.floor((s % 3600) / 60))} min`;
}

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------

class ApiError extends Error {
  /** @param {string} message @param {number} [status] */
  constructor(message, status = 0) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

/** Turn a FastAPI error body into a readable message. */
function detailMessage(data) {
  const detail = data && typeof data === 'object' ? data.detail : null;
  if (typeof detail === 'string') return detail;
  // Raw validation lists are English: show a generic Catalan message instead.
  if (Array.isArray(detail)) return 'Les dades enviades no són vàlides.';
  if (detail && typeof detail === 'object') return detail.message ? String(detail.message) : JSON.stringify(detail);
  return null;
}

/**
 * Call the local REST API (relative URL "api/<path>").
 * @param {string} path e.g. "resolve" or "downloads/abc/cancel"
 * @param {{method?: string, body?: any}} [options]
 */
async function api(path, { method = 'GET', body } = {}) {
  const init = { method, headers: { Accept: 'application/json' } };
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(`api/${path}`, init);
  } catch {
    throw new ApiError("No s'ha pogut connectar amb el servidor de l'aplicació. Comprova que el contenidor està en marxa.");
  }
  let data = null;
  const text = await res.text();
  if (text) {
    try { data = JSON.parse(text); } catch { data = null; }
  }
  if (!res.ok) {
    if (res.status === 401 && !path.startsWith('auth/')) handleUnauthorized();
    throw new ApiError(detailMessage(data) || `El servidor ha retornat un error (${res.status}).`, res.status);
  }
  return data;
}

function errorMessage(err) {
  if (err instanceof ApiError) return err.message;
  return 'Hi ha hagut un error inesperat.';
}

// ---------------------------------------------------------------------------
// Toasts & confirm dialog
// ---------------------------------------------------------------------------

/**
 * Show a toast notification.
 * @param {string} message
 * @param {{kind?: 'info'|'success'|'error'|'warning', title?: string, timeout?: number, action?: {label: string, run: () => void}}} [opts]
 */
function toast(message, { kind = 'info', title, timeout, action } = {}) {
  const container = $('#toasts');
  while (container.children.length >= 5) container.firstElementChild.remove();
  const close = () => {
    el.classList.add('toast-leaving');
    setTimeout(() => el.remove(), 200);
  };
  const el = h('div', { class: `toast toast-${kind}`, role: kind === 'error' ? 'alert' : 'status' },
    h('div', { class: 'toast-body' },
      title ? h('strong', { class: 'toast-title', text: title }) : null,
      h('span', { class: 'toast-msg', text: message })),
    action ? h('button', { type: 'button', class: 'btn btn-small', text: action.label, onclick: () => { action.run(); close(); } }) : null,
    h('button', { type: 'button', class: 'toast-close', 'aria-label': 'Tanca la notificació', text: '×', onclick: close }));
  container.append(el);
  const ms = timeout ?? (kind === 'error' ? 9000 : 5000);
  if (ms > 0) setTimeout(close, ms);
}

function showError(err, title) {
  toast(errorMessage(err), { kind: 'error', title });
}

/**
 * Modal confirmation using <dialog>; falls back to window.confirm.
 * @returns {Promise<boolean>}
 */
function confirmDialog({ title, message, okText = "D'acord", cancelText = 'Cancel·la' }) {
  const dialog = /** @type {HTMLDialogElement} */ ($('#confirm-dialog'));
  if (!dialog || typeof dialog.showModal !== 'function') {
    return Promise.resolve(window.confirm(`${title}\n\n${message}`));
  }
  $('#confirm-title').textContent = title;
  $('#confirm-message').textContent = message;
  $('#confirm-ok').textContent = okText;
  $('#confirm-cancel').textContent = cancelText;
  dialog.returnValue = 'cancel';
  return new Promise((resolve) => {
    dialog.addEventListener('close', () => resolve(dialog.returnValue === 'ok'), { once: true });
    dialog.showModal();
    $('#confirm-ok').focus();
  });
}

// ---------------------------------------------------------------------------
// Telegram session
// ---------------------------------------------------------------------------

const MESSAGE_STATES = {
  new: ['Nou', ''],
  queued: ['En cua', 'pill-info'],
  downloading: ['Baixant', 'pill-info'],
  downloaded: ['Baixat', 'pill-accent'],
  extracting: ['Descomprimint', 'pill-info'],
  extracted: ['Descomprimit', 'pill-accent'],
  in_library: ['A la biblioteca', 'pill-ok'],
  ignored: ['Ignorat', ''],
  error: ['Error', 'pill-err'],
};
const DIALOG_KINDS = { channel: 'Canal', group: 'Grup', user: 'Persona', bot: 'Bot', self: 'Missatges desats' };
const SOURCE_KINDS = { series: 'Sèries', movies: 'Pel·lícules', other: 'Altres vídeos' };
const SOURCE_MODES = { arr: 'A través de Sonarr / Radarr', library: 'Directament a la biblioteca' };
const LANGUAGES = { ca: 'Català', es: 'Castellà', en: 'Anglès', ja: 'Japonès', fr: 'Francès', it: 'Italià', de: 'Alemany', pt: 'Portuguès' };
const MESSAGE_PAGE = 200;
const NUMBERINGS = { seasons: 'Per temporades (1x05 = T1 E5)', absolute: 'Absoluta, com a l\'anime (13x501 = episodi 501)' };

function pill(text, kind = '') {
  return h('span', { class: `pill ${kind}`.trim(), text });
}

function apiHelp() {
  return h('div', { class: 'help' },
    h('p', {}, "Per connectar-se com a usuari (MTProto), Telegram demana que cada aplicació tingui el seu ",
      h('code', { text: 'api_id' }), ' i ', h('code', { text: 'api_hash' }),
      ". L'eina tdl no els demanava perquè fa servir els de l'aplicació oficial d'escriptori. Són gratuïts i només cal fer-ho un cop:"),
    h('ol', { class: 'steps-list' },
      h('li', {}, 'Entra a ', h('a', { href: 'https://my.telegram.org', target: '_blank', rel: 'noopener noreferrer', text: 'my.telegram.org' }),
        ' amb el teu número de telèfon (el codi arriba a l\'aplicació de Telegram, no per SMS).'),
      h('li', {}, 'Ves a ', h('strong', { text: 'API development tools' }), '.'),
      h('li', {}, 'Omple el formulari: App title «Telegram Downloader», Short name «tgdl», Platform «Desktop». La resta pot quedar buida.'),
      h('li', {}, 'Copia aquí l\'', h('strong', { text: 'App api_id' }), ' (un número) i l\'', h('strong', { text: 'App api_hash' }), ' (32 caràcters).')),
    h('p', {}, "No els comparteixis: identifiquen la teva aplicació. La sessió de Telegram es desa a la carpeta de configuració (",
      h('code', { text: 'telegram.session' }), "): qui tingui aquest fitxer té accés al compte."));
}

function apiCredentialsForm() {
  const id = h('input', { type: 'text', inputmode: 'numeric', id: 'tg-api-id', autocomplete: 'off', spellcheck: 'false' });
  const hash = h('input', { type: 'password', id: 'tg-api-hash', autocomplete: 'off', spellcheck: 'false', maxlength: '32' });
  const err = h('p', { class: 'form-error', role: 'alert', hidden: true });
  const submit = h('button', { type: 'submit', class: 'btn btn-primary', text: 'Desa i connecta' });
  const locked = (state.settings?.locked || []).filter((f) => f.startsWith('telegram_api'));
  if (locked.length) {
    return h('p', { class: 'help lock-hint', text: 'Les credencials vénen de les variables TGDL_API_ID i TGDL_API_HASH.' });
  }
  return h('form', {
    class: 'stack', novalidate: true,
    onsubmit: async (ev) => {
      ev.preventDefault();
      err.hidden = true;
      submit.disabled = true;
      try {
        state.settings = await api('settings', { method: 'PUT', body: { telegram_api_id: id.value.trim(), telegram_api_hash: hash.value.trim() } });
        toast("S'han desat les credencials. Connectant amb Telegram…", { kind: 'success' });
      } catch (e) {
        err.textContent = errorMessage(e);
        err.hidden = false;
      } finally { submit.disabled = false; }
    },
  },
  h('div', { class: 'grid-2' },
    h('div', {}, h('label', { for: 'tg-api-id', text: 'api_id' }), id),
    h('div', {}, h('label', { for: 'tg-api-hash', text: 'api_hash' }), hash)),
  err, h('div', { class: 'settings-actions' }, submit));
}

async function tgAction(path, body, button) {
  if (button) button.disabled = true;
  try {
    state.telegram = await api(`telegram/${path}`, { method: 'POST', body });
    renderTelegram();
    return true;
  } catch (e) {
    showError(e);
    return false;
  } finally {
    if (button) button.disabled = false;
  }
}

let qrTimer = 0;

function qrPanel(tg) {
  const countdown = h('p', { class: 'muted', 'aria-live': 'off' });
  const tick = () => {
    const left = Math.max(0, Math.round((Date.parse(tg.qr_expires) - Date.now()) / 1000));
    countdown.textContent = left > 0 ? `El codi es renova sol cada pocs segons (aquest caduca d'aquí a ${left} s).` : 'Renovant el codi…';
  };
  clearInterval(qrTimer);
  tick();
  qrTimer = setInterval(tick, 1000);
  return h('div', { class: 'qr-layout' },
    h('div', { class: 'qr-box' }, h('img', { src: `api/telegram/qr.svg?v=${encodeURIComponent(tg.qr_url)}`, alt: 'Codi QR per iniciar la sessió a Telegram' })),
    h('div', {},
      h('ol', { class: 'steps-list' },
        h('li', {}, 'Obre Telegram al mòbil.'),
        h('li', {}, 'Ves a ', h('strong', { text: 'Configuració → Dispositius → Connecta un dispositiu' }), '.'),
        h('li', {}, 'Escaneja aquest codi.')),
      countdown,
      h('button', { type: 'button', class: 'btn', text: 'Cancel·la', onclick: (ev) => tgAction('cancel', undefined, ev.currentTarget) })));
}

function phonePanel(tg) {
  if (tg.state === 'code') {
    const code = h('input', { type: 'text', inputmode: 'numeric', id: 'tg-code', autocomplete: 'one-time-code', maxlength: '16' });
    setTimeout(() => code.focus(), 0);
    return h('form', {
      class: 'stack', novalidate: true,
      onsubmit: (ev) => { ev.preventDefault(); tgAction('code/verify', { code: code.value.trim() }, ev.submitter); },
    },
    h('p', {}, `Telegram ha enviat un codi a ${tg.phone || 'el teu compte'} (normalment a l'aplicació, no per SMS).`),
    h('div', {}, h('label', { for: 'tg-code', text: 'Codi' }), code),
    h('div', { class: 'row' },
      h('button', { type: 'submit', class: 'btn btn-primary', text: 'Entra' }),
      h('button', { type: 'button', class: 'btn', text: 'Cancel·la', onclick: (ev) => tgAction('cancel', undefined, ev.currentTarget) })));
  }
  const phone = h('input', { type: 'tel', id: 'tg-phone', autocomplete: 'tel', placeholder: '+34 600 000 000', maxlength: '32' });
  return h('form', {
    class: 'stack', novalidate: true,
    onsubmit: (ev) => { ev.preventDefault(); tgAction('code', { phone: phone.value.trim() }, ev.submitter); },
  },
  h('div', {}, h('label', { for: 'tg-phone', text: 'Número de telèfon (amb el prefix del país)' }), phone),
  h('div', {}, h('button', { type: 'submit', class: 'btn btn-primary', text: 'Envia el codi' })));
}

function passwordPanel(tg) {
  const pw = h('input', { type: 'password', id: 'tg-2fa', autocomplete: 'current-password', maxlength: '1024' });
  setTimeout(() => pw.focus(), 0);
  return h('form', {
    class: 'stack', novalidate: true,
    onsubmit: (ev) => { ev.preventDefault(); tgAction('password', { password: pw.value }, ev.submitter); },
  },
  h('p', {}, "Aquest compte té la verificació en dos passos activada. Escriu-ne la contrasenya (no es desa: només s'envia a Telegram)."),
  h('div', {}, h('label', { for: 'tg-2fa', text: 'Contrasenya de Telegram' }), pw,
    tg.password_hint ? h('p', { class: 'help', text: `Pista: ${tg.password_hint}` }) : null),
  h('div', { class: 'row' },
    h('button', { type: 'submit', class: 'btn btn-primary', text: 'Entra' }),
    h('button', { type: 'button', class: 'btn', text: 'Cancel·la', onclick: (ev) => tgAction('cancel', undefined, ev.currentTarget) })));
}

function loginCard(tg) {
  if (tg.state === 'password') {
    return h('section', { class: 'card' }, h('h2', { text: 'Contrasenya de verificació en dos passos' }), passwordPanel(tg));
  }
  const tab = tg.state === 'qr' ? 'qr' : (tg.state === 'code' ? 'phone' : state.loginTab);
  const tabs = h('div', { class: 'tabs', role: 'tablist' },
    ...[['qr', 'Amb codi QR (recomanat)'], ['phone', 'Amb el número de telèfon']].map(([key, label]) => h('button', {
      type: 'button', class: 'tab', role: 'tab', 'aria-selected': String(tab === key), text: label,
      disabled: (tg.state === 'qr' || tg.state === 'code') && tab !== key,
      onclick: () => { state.loginTab = key; renderTelegram(); },
    })));
  let body;
  if (tab === 'qr') {
    body = tg.state === 'qr' && tg.qr_url ? qrPanel(tg) : h('div', { class: 'stack' },
      h('p', {}, "La manera més fàcil: escanejaràs un codi amb l'aplicació de Telegram del mòbil."),
      h('div', {}, h('button', { type: 'button', class: 'btn btn-primary', text: 'Mostra el codi QR', onclick: (ev) => tgAction('qr', undefined, ev.currentTarget) })));
  } else {
    body = phonePanel(tg);
  }
  return h('section', { class: 'card' }, h('h2', { text: 'Inicia la sessió a Telegram' }),
    tg.error ? h('div', { class: 'banner banner-warn', text: tg.error }) : null, tabs, body);
}

function accountCard(tg) {
  const me = tg.me || {};
  const initial = (me.name || '?').trim().charAt(0).toUpperCase() || '?';
  return h('section', { class: 'card' },
    h('div', { class: 'row-between' },
      h('div', { class: 'account' },
        h('div', { class: 'avatar', 'aria-hidden': 'true', text: initial }),
        h('div', {},
          h('strong', { text: me.name || 'Compte de Telegram' }), ' ',
          me.premium ? pill('Premium', 'pill-ok') : pill('Sense Premium', 'pill-warn'),
          h('div', { class: 'muted', text: [me.username ? `@${me.username}` : '', me.phone ? `+${me.phone}` : ''].filter(Boolean).join(' · ') }))),
      h('button', {
        type: 'button', class: 'btn btn-danger-ghost', text: 'Tanca la sessió de Telegram',
        onclick: async (ev) => {
          const ok = await confirmDialog({ title: 'Tancar la sessió de Telegram?', message: "Caldrà tornar a escanejar el codi QR. L'índex de les fonts es manté.", okText: 'Tanca la sessió' });
          if (ok) tgAction('logout', undefined, ev.currentTarget);
        },
      })),
    me.premium ? null : h('p', { class: 'help', text: "Sense Premium, Telegram limita la velocitat de baixada (en la pràctica, uns 700 KB/s després de baixar molt). Si actives Premium, l'aplicació ho detecta sola." }),
    tg.fake ? h('div', { class: 'banner banner-info', text: 'Mode de prova: Telegram simulat (TGDL_FAKE_TELEGRAM).' }) : null);
}

function renderTelegram() {
  const tg = state.telegram;
  const box = $('#telegram-content');
  if (tg?.state !== 'qr') clearInterval(qrTimer);
  if (!tg) { setChildren(box, spinner('Carregant…')); return; }
  switch (tg.state) {
    case 'unconfigured':
      setChildren(box, h('section', { class: 'card' }, h('h2', { text: 'Primer pas: les credencials de l\'API' }), apiHelp(), apiCredentialsForm()));
      setChildren($('#dialogs-content'));
      return;
    case 'connecting':
      setChildren(box, h('section', { class: 'card' }, spinner('Connectant amb Telegram…'),
        tg.error ? h('p', { class: 'help', text: tg.error }) : null));
      return;
    case 'error':
      setChildren(box, h('section', { class: 'card' },
        h('div', { class: 'banner banner-error', text: tg.error || "No s'ha pogut connectar amb Telegram." }),
        h('p', {}, "Revisa l'api_id i l'api_hash:"), apiCredentialsForm()));
      return;
    case 'ready':
      setChildren(box, accountCard(tg));
      if (!state.dialogs && !state.dialogsLoading) loadDialogs();
      renderDialogs();
      return;
    default:
      state.dialogs = null;
      setChildren(box, loginCard(tg));
      setChildren($('#dialogs-content'));
  }
}

function spinner(message) {
  return h('div', { class: 'loading', role: 'status' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), h('span', { text: message }));
}

// ---------------------------------------------------------------------------
// Chats (dialogs)
// ---------------------------------------------------------------------------

async function loadDialogs() {
  state.dialogsLoading = true;
  renderDialogs();
  try {
    const data = await api('telegram/dialogs');
    state.dialogs = data.dialogs;
  } catch (e) {
    showError(e, "No s'han pogut carregar els xats");
  } finally {
    state.dialogsLoading = false;
    renderDialogs();
  }
}

function normalise(text) {
  return String(text || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
}

function renderDialogs() {
  const box = $('#dialogs-content');
  if (state.telegram?.state !== 'ready') { setChildren(box); return; }
  if (state.dialogsLoading && !state.dialogs) { setChildren(box, h('section', { class: 'card' }, spinner('Carregant els xats…'))); return; }
  const all = state.dialogs || [];
  const filter = h('input', { type: 'search', placeholder: 'Filtra per nom o ID', value: state.dialogFilter, 'aria-label': 'Filtra els xats' });
  const kindSel = h('select', { 'aria-label': 'Tipus de xat' },
    h('option', { value: '', text: 'Tots els tipus' }),
    ...Object.entries(DIALOG_KINDS).map(([k, label]) => h('option', { value: k, text: label, selected: state.dialogKind === k })));
  const tbody = h('tbody');
  const fill = () => {
    const q = normalise(state.dialogFilter);
    const rows = all.filter((d) => (!state.dialogKind || d.kind === state.dialogKind)
      && (!q || normalise(d.title).includes(q) || String(d.id).includes(q) || normalise(d.username).includes(q)));
    setChildren(tbody, rows.length ? rows.slice(0, 500).map(dialogRow) : h('tr', {}, h('td', { colspan: '4', class: 'muted', text: 'Cap xat coincideix amb el filtre.' })));
  };
  filter.addEventListener('input', () => { state.dialogFilter = filter.value; fill(); });
  kindSel.addEventListener('change', () => { state.dialogKind = kindSel.value; fill(); });
  fill();
  setChildren(box, h('section', { class: 'card' },
    h('div', { class: 'row-between' },
      h('h2', { text: `Els teus xats (${all.length})` }),
      h('button', { type: 'button', class: 'btn btn-small', text: 'Actualitza', disabled: state.dialogsLoading, onclick: loadDialogs })),
    h('p', { class: 'help', text: 'Tria el canal o el xat on tens les còpies i afegeix-lo com a font. Es mostra el nom sencer.' }),
    h('div', { class: 'filters' }, filter, kindSel),
    h('div', { class: 'table-wrap' }, h('table', { class: 'list' },
      h('thead', {}, h('tr', {}, h('th', { text: 'Nom' }), h('th', { text: 'Tipus' }), h('th', { class: 'num', text: 'ID' }), h('th', { text: '' }))),
      tbody))));
}

function dialogRow(d) {
  const action = d.source_id
    ? h('button', { type: 'button', class: 'btn btn-small', text: 'Veure la font', onclick: () => openSource(d.source_id) })
    : h('button', { type: 'button', class: 'btn btn-small btn-primary', text: 'Afegeix com a font', onclick: (ev) => addSource(d, ev.currentTarget) });
  return h('tr', {},
    h('td', { class: 'name' }, d.title || '(sense nom)', d.username ? h('div', { class: 'muted', text: `@${d.username}` }) : null,
      d.archived ? h('div', {}, pill('Arxivat')) : null),
    h('td', { text: DIALOG_KINDS[d.kind] || d.kind }),
    h('td', { class: 'num mono', text: String(d.id) }),
    h('td', { class: 'num' }, action));
}

async function addSource(dialog, button) {
  button.disabled = true;
  try {
    const src = await api('sources', { method: 'POST', body: { chat_id: dialog.id } });
    dialog.source_id = src.id;
    state.sources.set(src.id, src);
    toast(`«${src.title}» afegida. S'està indexant…`, { kind: 'success' });
    renderDialogs();
    openSource(src.id);
  } catch (e) {
    showError(e, "No s'ha pogut afegir la font");
    button.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// Sources
// ---------------------------------------------------------------------------

async function loadSources() {
  const data = await api('sources');
  state.sources = new Map(data.sources.map((s) => [s.id, s]));
  renderSources();
}

function openSource(id) {
  state.openSource = id;
  state.msgQuery = { q: '', state: '', offset: 0 };
  showView('sources');
  renderSources();
  loadMessages();
}

function countPills(src) {
  return Object.entries(src.counts || {}).sort().map(([st, n]) => {
    const [label, kind] = MESSAGE_STATES[st] || [st, ''];
    return pill(`${label}: ${n}`, kind);
  });
}

function indexingLine(src) {
  if (src.indexing) {
    return h('span', { class: 'row' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }),
      `Indexant${src.indexing.full ? ' (sencer)' : ''}… ${src.indexing.seen} fitxers revisats, ${src.indexing.new} de nous`);
  }
  if (src.index_error) return h('span', { class: 'pill pill-err', text: src.index_error });
  return src.indexed_at ? `Indexada el ${formatDateTime(src.indexed_at)}` : 'Encara no indexada';
}

function formatDateTime(iso) {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString('ca', { dateStyle: 'short', timeStyle: 'short' });
}

function sourceSummary(src, { detail = false } = {}) {
  const meta = [DIALOG_KINDS[src.chat_kind] || src.chat_kind, `ID ${src.chat_id}`,
    plural(src.files || 0, 'fitxer', 'fitxers'), formatBytes(src.total_size || 0)].filter(Boolean).join(' · ');
  return h('section', { class: 'card source-card' },
    h('div', { class: 'row-between' },
      h('div', { class: 'grow' },
        detail ? h('h2', { text: src.title }) : h('h2', {}, h('button', { type: 'button', class: 'link-btn', text: src.title, onclick: () => openSource(src.id) })),
        h('div', { class: 'source-meta', text: meta }),
        h('div', { class: 'source-meta' }, indexingLine(src)),
        placingLine(src),
        h('div', { class: 'counts' }, countPills(src))),
      h('div', { class: 'row' },
        src.options?.wikipedia_list && src.options?.kind === 'series' ? h('button', { type: 'button', class: 'btn btn-small', text: 'Posa els títols en català', title: 'Posa el títol i la sinopsi de la Viquipèdia als .nfo que ha creat aquesta aplicació (els altres no es toquen)', onclick: (ev) => updateTitles(src, ev.currentTarget) }) : null,
        canPlace(src) ? h('button', { type: 'button', class: 'btn btn-small', text: 'Col·loca a la biblioteca', title: 'Descomprimeix els episodis baixats i els posa a la carpeta de la sèrie, amb el nom que entén Jellyfin', disabled: Boolean(src.placing), onclick: (ev) => placeSource(src, ev.currentTarget) }) : null,
        h('button', { type: 'button', class: 'btn btn-small btn-primary', text: 'Baixa el que falta', title: 'Posa a la cua la millor còpia de cada episodi que no és a la biblioteca', onclick: (ev) => queueMissing(src, ev.currentTarget) }),
        h('button', { type: 'button', class: 'btn btn-small', text: 'Indexa ara', disabled: Boolean(src.indexing), onclick: () => indexSource(src.id, false) }),
        detail ? h('button', { type: 'button', class: 'btn btn-small', text: 'Reindexa sencer', title: 'Torna a llegir tot el canal (detecta missatges esborrats)', disabled: Boolean(src.indexing), onclick: () => indexSource(src.id, true) }) : null,
        detail ? h('button', { type: 'button', class: 'btn btn-small btn-danger-ghost', text: 'Elimina', onclick: () => deleteSource(src) }) : null)),
    detail ? sourceOptionsForm(src) : null);
}

function sourceOptionsForm(src) {
  const o = src.options || {};
  const select = (name, labels, value) => h('select', { id: `opt-${name}`, 'data-opt': name },
    ...Object.entries(labels).map(([k, label]) => h('option', { value: k, text: label, selected: value === k })));
  const auto = h('input', { type: 'checkbox', id: 'opt-auto', checked: o.auto_index !== false });
  const folderInput = h('input', { id: 'opt-library_folder', 'data-opt': 'library_folder', type: 'text', spellcheck: 'false', value: o.library_folder || '', placeholder: 'automàtica ([tvdbid-…])' });
  const form = h('form', {
    class: 'grid-2', novalidate: true,
    onchange: async () => {
      const options = { auto_index: auto.checked };
      for (const el of form.querySelectorAll('[data-opt]')) options[el.dataset.opt] = el.value;
      try {
        const updated = await api(`sources/${src.id}`, { method: 'PATCH', body: { options } });
        state.sources.set(updated.id, updated);
        toast("S'han desat les opcions de la font.", { kind: 'success', timeout: 2500 });
        renderSources();
        loadMessages();
      } catch (e) { showError(e); }
    },
  },
  h('div', {}, h('label', { for: 'opt-kind', text: 'Contingut' }), select('kind', SOURCE_KINDS, o.kind)),
  h('div', {}, h('label', { for: 'opt-mode', text: 'Destí' }), select('mode', SOURCE_MODES, o.mode)),
  h('div', {}, h('label', { for: 'opt-language', text: "Idioma de l'àudio" }), select('language', LANGUAGES, o.language)),
  h('div', {}, h('label', { for: 'opt-numbering', text: 'Numeració dels noms' }), select('numbering', NUMBERINGS, o.numbering)),
  h('div', {}, h('label', { for: 'opt-tvdb_id', text: 'Id de TheTVDB' }),
    h('input', { id: 'opt-tvdb_id', 'data-opt': 'tvdb_id', type: 'text', inputmode: 'numeric', pattern: '[0-9]*', value: o.tvdb_id ? String(o.tvdb_id) : '', placeholder: 'p. ex. 72454 (Detectiu Conan)' }),
    h('p', { class: 'help', text: "El número de l'adreça de la sèrie a thetvdb.com, o el que surt a la carpeta de Jellyfin «[tvdbid-…]». 0 = cap." })),
  h('div', {}, h('label', { for: 'opt-wikipedia_list', text: "Llista d'episodis a la Viquipèdia" }),
    h('input', { id: 'opt-wikipedia_list', 'data-opt': 'wikipedia_list', type: 'text', value: o.wikipedia_list || '', placeholder: "p. ex. Llista d'episodis d'El detectiu Conan" }),
    h('p', { class: 'help', text: "Títols i sinopsis en català per als .nfo (pel número japonès). Buida = el títol del fitxer o el de TheTVDB." })),
  h('div', {}, h('label', { for: 'opt-tmdb_collection', text: 'Col·lecció de TMDB (pel·lícules)' }),
    h('input', { id: 'opt-tmdb_collection', 'data-opt': 'tmdb_collection', type: 'text', inputmode: 'numeric', pattern: '[0-9]*', value: o.tmdb_collection ? String(o.tmdb_collection) : '', placeholder: 'p. ex. 39199 (Detectiu Conan)' }),
    h('p', { class: 'help', text: "Per a les fonts de pel·lícules: «DC-Movie 22» és la 22a pel·lícula de la col·lecció per data d'estrena. Així Radarr la troba pel seu id de TMDB. 0 = cap." })),
  h('div', {}, h('label', { for: 'opt-library_folder', text: 'Carpeta de la sèrie a la biblioteca' }),
    h('div', { class: 'path-field' }, folderInput, browseButton(folderInput, 'Carpeta de la sèrie a Jellyfin')),
    h('p', { class: 'help', text: "La que llegeix Jellyfin. Buida = la que porta «[tvdbid-…]» dins la biblioteca de sèries. Els episodis que hi ha no es baixaran mai." })),
  h('div', {}, h('label', { class: 'check' }, auto, h('span', { text: "Refresca l'índex automàticament" }))));
  return h('details', { class: 'source-options' }, h('summary', { text: 'Opcions de la font' }), form);
}

function canPlace(src) {
  return src.options?.kind === 'series' && ((src.counts || {}).downloaded || 0) > 0;
}

function placingLine(src) {
  const p = src.placing;
  if (p) {
    return h('div', { class: 'source-meta row' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }),
      `Col·locant a la biblioteca… ${p.done + p.failed} de ${p.total}${p.current ? ` (${p.current})` : ''}`);
  }
  const last = src.placed;
  if (!last) return null;
  const text = `Última col·locació: ${plural(last.done, 'episodi', 'episodis')} a la biblioteca${last.failed ? `, ${last.failed} amb errors` : ''}.`;
  if (!last.errors?.length) return h('div', { class: 'source-meta', text });
  return h('details', { class: 'source-meta' }, h('summary', {}, text, ' ', pill('Errors', 'pill-err')),
    h('ul', {}, last.errors.map((e) => h('li', {}, h('strong', { text: e.name }), `: ${e.error}`))));
}

async function updateTitles(src, button) {
  button.disabled = true;
  try {
    const res = await api(`sources/${src.id}/titles`, { method: 'POST', body: {} });
    const extra = res.missing.length ? ` ${plural(res.missing.length, 'episodi no és', 'episodis no són')} a la Viquipèdia.` : '';
    toast(`${plural(res.updated, '.nfo actualitzat', '.nfo actualitzats')}; ${res.unchanged} ja els tenien.${extra}`, { kind: 'success' });
  } catch (e) { showError(e, "No s'han pogut posar els títols"); } finally { button.disabled = false; }
}

async function placeSource(src, button) {
  const n = (src.counts || {}).downloaded || 0;
  const ok = await confirmDialog({
    title: 'Col·locar a la biblioteca?',
    message: `Hi ha ${plural(n, 'fitxer baixat', 'fitxers baixats')}. Es descomprimiran i cada episodi complet anirà a la carpeta de la sèrie (Season N/…), amb el seu .nfo. No se sobreescriu res del que ja hi ha.${state.settings?.delete_archives === false ? '' : ' Els arxius s\'esborraran quan el seu episodi ja sigui a la biblioteca.'}`,
    okText: 'Col·loca',
  });
  if (!ok) return;
  button.disabled = true;
  try {
    const res = await api(`sources/${src.id}/place`, { method: 'POST', body: {} });
    if (res.source) state.sources.set(src.id, res.source);
    if (!res.started) toast('Ja s\'hi estava treballant: es tornarà a mirar quan acabi.', { kind: 'info' });
    renderSources();
  } catch (e) { showError(e, "No s'ha pogut col·locar"); button.disabled = false; }
}

async function queueMissing(src, button) {
  button.disabled = true;
  try {
    const res = await api(`sources/${src.id}/queue`, { method: 'POST', body: { missing: true } });
    if (res.source) state.sources.set(src.id, res.source);
    const notes = [];
    if (res.in_library) notes.push(`${plural(res.in_library, 'episodi ja és', 'episodis ja són')} a la biblioteca`);
    if (res.skipped_unrecognized) notes.push(`${plural(res.skipped_unrecognized, 'arxiu', 'arxius')} sense número (tria'ls a mà a «Fitxers»)`);
    if (res.skipped_incomplete) notes.push(plural(res.skipped_incomplete, 'arxiu incomplet', 'arxius incomplets'));
    const extra = notes.length ? ` ${notes.join('; ')}.` : '';
    if (res.queued) {
      toast(`${plural(res.queued, 'fitxer', 'fitxers')} a la cua.${extra}`,
        { kind: 'success', action: { label: 'Mira les baixades', run: () => showView('downloads') } });
    } else {
      toast(`No hi ha res de nou per baixar.${extra}`, { kind: 'info' });
    }
    renderSources();
    loadMessages();
  } catch (e) { showError(e, "No s'ha pogut posar a la cua"); } finally { button.disabled = false; }
}

async function queueFiles(sourceId, msgIds, queue = true) {
  try {
    const res = await api(`sources/${sourceId}/${queue ? 'queue' : 'unqueue'}`, { method: 'POST', body: { msg_ids: msgIds } });
    if (res.source) state.sources.set(sourceId, res.source);
    renderSources();
    loadMessages();
  } catch (e) { showError(e); }
}

async function indexSource(id, full) {
  try {
    const res = await api(`sources/${id}/index`, { method: 'POST', body: { full } });
    if (res.source) state.sources.set(id, res.source);
    if (!res.started) toast('Aquesta font ja s\'està indexant.', { kind: 'info' });
    renderSources();
  } catch (e) { showError(e, "No s'ha pogut indexar"); }
}

async function deleteSource(src) {
  const ok = await confirmDialog({
    title: `Eliminar «${src.title}»?`,
    message: "S'esborra l'índex i l'estat de tots els seus fitxers en aquesta aplicació. No s'esborra res de Telegram ni del disc.",
    okText: 'Elimina',
  });
  if (!ok) return;
  try {
    await api(`sources/${src.id}`, { method: 'DELETE' });
    state.sources.delete(src.id);
    state.openSource = null;
    if (state.dialogs) for (const d of state.dialogs) if (d.source_id === src.id) d.source_id = null;
    renderSources();
  } catch (e) { showError(e); }
}

function renderSources() {
  const box = $('#sources-content');
  let indexing = 0;
  for (const s of state.sources.values()) if (s.indexing) indexing += 1;
  const badge = $('#sources-badge');
  badge.hidden = indexing === 0;
  badge.textContent = String(indexing);
  if (state.openSource && state.sources.has(state.openSource)) {
    const src = state.sources.get(state.openSource);
    let detail = $('#source-detail');
    if (!detail || Number(detail.dataset.id) !== src.id) {
      detail = h('div', { id: 'source-detail', class: 'stack', dataset: { id: String(src.id) } },
        h('div', {}, h('button', { type: 'button', class: 'btn btn-small back-btn', text: '← Totes les fonts', onclick: () => { state.openSource = null; renderSources(); } })),
        h('div', { id: 'source-summary' }), reviewPanel(src), messagesPanel());
      setChildren(box, detail);
    }
    const open = $('#source-summary details')?.open;
    setChildren($('#source-summary'), sourceSummary(src, { detail: true }));
    if (open) $('#source-summary details').open = true;
    return;
  }
  if (!state.sources.size) {
    setChildren(box, h('section', { class: 'card empty-state' },
      h('p', { class: 'empty-title', text: 'Encara no hi ha cap font.' }),
      h('p', { class: 'muted', text: 'Ves a «Telegram», tria el canal on tens les còpies i afegeix-lo.' }),
      h('button', { type: 'button', class: 'btn btn-primary', text: 'Ves a Telegram', onclick: () => showView('telegram') })));
    return;
  }
  setChildren(box, h('div', { class: 'stack' }, [...state.sources.values()].map((s) => sourceSummary(s))));
}

// ---------------------------------------------------------------------------
// Check before downloading, tdl import, library
// ---------------------------------------------------------------------------

function tdlPathKey(id) { return `tgdl.tdlPath.${id}`; }

function storedTdlPath(id) {
  try { return localStorage.getItem(tdlPathKey(id)) || ''; } catch { return ''; }
}

function reviewPanel(src) {
  const path = h('input', { type: 'text', id: 'tdl-path', value: storedTdlPath(src.id) || '/data/_conan/zips', 'aria-label': 'Carpeta on tdl ha deixat els fitxers', placeholder: '/data/_conan/zips' });
  const run = (fn) => async (event) => {
    const btn = event.currentTarget;
    btn.disabled = true;
    try { await fn(); } catch (e) { showError(e); } finally { btn.disabled = false; }
  };
  return h('section', { class: 'card', id: 'review-card' },
    h('h2', { text: 'Revisió abans de baixar' }),
    h('p', { class: 'muted', text: "Agrupa els volums de cada arxiu, els compara amb TheTVDB i amb la biblioteca, i diu què falta, què està repetit i què no s'entén. No baixa ni esborra res." }),
    h('div', { class: 'review-actions' },
      h('button', { type: 'button', class: 'btn btn-primary btn-small', text: 'Revisa', onclick: run(() => loadReview(src.id)) }),
      h('button', { type: 'button', class: 'btn btn-small', text: 'Torna a mirar la biblioteca', title: 'Els fitxers dels episodis que ja tens a la carpeta de la sèrie no es baixaran mai', onclick: run(() => reconcileLibrary(src.id)) })),
    h('div', { class: 'review-actions' },
      path, browseButton(path, 'Carpeta on tdl ha deixat els fitxers'),
      h('button', { type: 'button', class: 'btn btn-small', text: 'Importa el que ha baixat tdl', title: 'Fitxers «<canal>_<missatge>_<nom>» complets: no es tornaran a baixar', onclick: run(() => importTdl(src.id, path.value.trim())) })),
    h('div', { id: 'review-result' }));
}

async function loadReview(id, { refresh = false } = {}) {
  const box = $('#review-result');
  setChildren(box, spinner('Revisant…'));
  const data = await api(`sources/${id}/review${refresh ? '?refresh=true' : ''}`);
  if (state.openSource === id) renderReview(data);
}

function reviewList(title, items, kind, render) {
  if (!items.length) return null;
  return h('details', { class: 'review-section' },
    h('summary', {}, pill(String(items.length), kind), ' ', title),
    h('ul', {}, items.map((it) => h('li', {}, render(it)))));
}

function renderReview(r) {
  const s = r.summary;
  const notes = [];
  if (r.tvdb) notes.push(h('p', { class: 'muted', text: `TheTVDB: «${r.tvdb.title}», ${r.tvdb.episodes} episodis${r.tvdb.stale ? ' (còpia desada: TheTVDB no respon)' : ''}.` }));
  else if (r.tvdb_error) notes.push(h('p', { class: 'pill pill-err', text: r.tvdb_error }));
  else notes.push(h('p', { class: 'muted', text: "Sense l'id de TheTVDB només es poden veure els forats de la numeració. Posa'l a «Opcions de la font»." }));
  const lib = r.library || {};
  if (lib.folder) notes.push(h('p', { class: 'muted', text: `Biblioteca (el que veu Jellyfin): ${lib.folder}, ${plural(lib.episodes, 'episodi', 'episodis')}. Es comprova sola després de cada indexació.` }));
  else if (lib.problem) notes.push(h('p', { class: 'muted', text: `Biblioteca: ${lib.problem}` }));

  const summary = h('div', { class: 'review-summary' },
    pill(`${plural(s.episodes, 'episodi', 'episodis')} a Telegram`, 'pill-accent'),
    pill(`${plural(s.releases, 'arxiu', 'arxius')} (${plural(s.files, 'fitxer', 'fitxers')})`),
    s.in_library ? pill(`${s.in_library} ja a la biblioteca`, 'pill-ok') : null,
    s.library_only ? pill(`${s.library_only} només a la biblioteca`, 'pill-info') : null,
    s.missing ? pill(`${s.missing} falten`, 'pill-warn') : pill('No en falta cap', 'pill-ok'),
    s.beyond ? pill(`${s.beyond} posteriors encara no penjats`, 'pill-info') : null,
    s.duplicates ? pill(`${s.duplicates} repetits`, 'pill-warn') : null,
    s.unrecognized ? pill(`${s.unrecognized} sense número`, 'pill-err') : null,
    s.not_in_tvdb ? pill(`${s.not_in_tvdb} no són a TheTVDB`, 'pill-err') : null,
    s.incomplete ? pill(`${s.incomplete} incomplets`, 'pill-err') : null);

  setChildren($('#review-result'), ...notes, summary,
    reviewList('Episodis que falten', r.missing, 'pill-warn', (m) => (m.count === 1 ? m.from : `${m.from} → ${m.to} (${m.count})`)),
    reviewList('Repetits (es baixarà només el marcat)', r.duplicates, 'pill-warn', (d) => h('span', {},
      h('strong', { text: d.label }), ': ',
      ...d.names.map((n, i) => h('span', {}, i ? ' · ' : '', n === d.keep ? h('strong', { text: `✔ ${n}` }) : n)),
      d.reuploaded.length ? h('span', { class: 'muted', text: ' (algun volum penjat dues vegades)' }) : null)),
    reviewList("Sense número d'episodi", r.unrecognized, 'pill-err', (u) => `${u.name} (missatge ${u.msg_ids.join(', ')})`),
    reviewList('No són a TheTVDB', r.not_in_tvdb, 'pill-err', (rel) => `${rel.name} → ${rel.label}`),
    reviewList('Arxius incomplets (falten volums)', r.incomplete, 'pill-err', (rel) => `${rel.name}: hi ha els volums ${rel.volumes.join(', ')}`),
    reviewList('Altres fitxers (ni vídeo ni arxiu)', r.other_files, '', (o) => o.name),
    reviewList('Tots els arxius reconeguts', r.releases.filter((x) => x.label), 'pill-info', (rel) => h('span', {},
      h('span', { class: 'mono', text: rel.label }), rel.tvdb_title ? ` — ${rel.tvdb_title}` : '', ' ', h('span', { class: 'muted', text: rel.name }),
      rel.in_library ? h('span', {}, ' ', pill('a la biblioteca', 'pill-ok')) : null,
      !rel.keep ? h('span', {}, ' ', pill('repetit', 'pill-warn')) : null)));
}

async function reconcileLibrary(id) {
  const res = await api(`sources/${id}/reconcile`, { method: 'POST' });
  if (res.source) state.sources.set(id, res.source);
  toast(`${plural(res.episodes, 'episodi', 'episodis')} a la biblioteca; ${plural(res.marked, 'fitxer marcat', 'fitxers marcats')} perquè no es baixin.`, { kind: 'success' });
  renderSources();
  loadMessages();
}

async function importTdl(id, path) {
  if (!path) { toast('Escriu la carpeta on tdl ha deixat els fitxers.', { kind: 'error' }); return; }
  try { localStorage.setItem(tdlPathKey(id), path); } catch { /* private mode */ }
  const dry = await api(`sources/${id}/import`, { method: 'POST', body: { path, dry_run: true } });
  const lines = [`A ${dry.folder} hi ha ${plural(dry.found, 'fitxer', 'fitxers')} d'aquest canal.`,
    `${dry.imported} es poden importar; ${dry.already} ja hi eren.`];
  if (dry.size_mismatch.length) lines.push(`${dry.size_mismatch.length} tenen una mida diferent de la de Telegram (baixades a mitges): no s'importen i es tornaran a baixar.`);
  if (dry.unknown.length) lines.push(`${dry.unknown.length} no són a l'índex (missatges esborrats o encara no indexats).`);
  if (dry.other_chat) lines.push(`${dry.other_chat} són d'altres canals i no es toquen.`);
  if (!dry.imported) { toast(lines.join(' '), { kind: 'info', title: 'Res per importar' }); return; }
  const ok = await confirmDialog({ title: 'Importar els fitxers de tdl?', message: lines.join(' ') + " Els fitxers no es mouen: només es marca que ja estan baixats.", okText: 'Importa' });
  if (!ok) return;
  const res = await api(`sources/${id}/import`, { method: 'POST', body: { path } });
  if (res.source) state.sources.set(id, res.source);
  toast(`${plural(res.imported, 'fitxer importat', 'fitxers importats')}.`, { kind: 'success' });
  renderSources();
  loadMessages();
}

function messagesPanel() {
  const q = h('input', { type: 'search', id: 'msg-q', placeholder: 'Cerca pel nom del fitxer o el text', 'aria-label': 'Cerca fitxers' });
  const st = h('select', { id: 'msg-state', 'aria-label': 'Estat' }, h('option', { value: '', text: 'Tots els estats' }),
    ...Object.entries(MESSAGE_STATES).map(([k, [label]]) => h('option', { value: k, text: label })));
  let timer = 0;
  q.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(() => { state.msgQuery = { ...state.msgQuery, q: q.value.trim(), offset: 0 }; loadMessages(); }, 250); });
  st.addEventListener('change', () => { state.msgQuery = { ...state.msgQuery, state: st.value, offset: 0 }; loadMessages(); });
  return h('section', { class: 'card' },
    h('h2', { text: 'Fitxers' }),
    h('div', { class: 'filters' }, q, st),
    h('div', { id: 'msg-table' }, spinner('Carregant…')),
    h('div', { id: 'msg-pager', class: 'pager' }));
}

let messagesTimer = 0;
/** Reload the file list at most every second and a half (downloads change states often). */
function loadMessagesSoon() {
  if (messagesTimer) return;
  messagesTimer = setTimeout(() => { messagesTimer = 0; loadMessages(); }, 1500);
}

async function loadMessages() {
  const id = state.openSource;
  if (!id) return;
  const { q, state: st, offset } = state.msgQuery;
  const params = new URLSearchParams({ q, state: st, offset: String(offset), limit: String(MESSAGE_PAGE) });
  try {
    const data = await api(`sources/${id}/messages?${params}`);
    if (state.openSource !== id) return;
    renderMessages(data);
  } catch (e) { showError(e); }
}

function messageAction(m) {
  const id = state.openSource;
  if (m.state === 'new' || m.state === 'error') {
    return h('button', { type: 'button', class: 'btn btn-small', text: m.state === 'error' ? 'Torna-ho a provar' : 'Baixa', onclick: () => queueFiles(id, [m.msg_id]) });
  }
  if (m.state === 'queued' || m.state === 'downloading') {
    return h('button', { type: 'button', class: 'btn btn-small btn-danger-ghost', text: 'Treu de la cua', title: "El que ja s'ha baixat es guarda per a més endavant", onclick: () => queueFiles(id, [m.msg_id], false) });
  }
  return null;
}

function renderMessages(data) {
  const table = $('#msg-table');
  if (!table) return;
  if (!data.items.length) {
    setChildren(table, h('p', { class: 'muted', text: data.total === 0 && !state.msgQuery.q && !state.msgQuery.state ? 'Encara no s\'ha trobat cap fitxer en aquesta font.' : 'Cap fitxer coincideix amb el filtre.' }));
  } else {
    setChildren(table, h('div', { class: 'table-wrap' }, h('table', { class: 'list' },
      h('thead', {}, h('tr', {}, h('th', { class: 'num', text: 'Missatge' }), h('th', { text: 'Fitxer' }), h('th', { text: 'Episodi' }), h('th', { class: 'num', text: 'Mida' }),
        h('th', { text: 'Data' }), h('th', { text: 'Estat' }), h('th', { text: '' }))),
      h('tbody', {}, data.items.map((m) => {
        const [label, kind] = MESSAGE_STATES[m.state] || [m.state, ''];
        return h('tr', {},
          h('td', { class: 'num mono', text: String(m.msg_id) }),
          h('td', { class: 'name' }, m.file_name, m.text ? h('div', { class: 'msg-text', text: m.text.slice(0, 300) }) : null),
          h('td', { class: 'mono nowrap' }, m.episode || h('span', { class: 'muted', text: '—' }), m.volume > 1 ? h('div', { class: 'msg-text', text: `volum ${m.volume}` }) : null),
          h('td', { class: 'num', text: formatBytes(m.size) }),
          h('td', { text: formatDate(m.date) }),
          h('td', {}, pill(label, kind),
            (m.state === 'downloading' || m.state === 'queued') && m.bytes_done > 0 && m.size ? h('div', { class: 'msg-text', text: `${Math.floor((100 * m.bytes_done) / m.size)} %` }) : null,
            m.error ? h('div', { class: 'msg-text', text: m.error }) : null),
          h('td', { class: 'nowrap' }, messageAction(m)));
      })))));
  }
  const { offset } = state.msgQuery;
  const pager = $('#msg-pager');
  const last = Math.min(offset + data.items.length, data.total);
  setChildren(pager,
    h('span', { class: 'muted', text: data.total ? `${offset + 1}–${last} de ${data.total}` : '' }),
    h('button', { type: 'button', class: 'btn btn-small', text: '← Anteriors', disabled: offset === 0, onclick: () => { state.msgQuery.offset = Math.max(0, offset - MESSAGE_PAGE); loadMessages(); } }),
    h('button', { type: 'button', class: 'btn btn-small', text: 'Següents →', disabled: last >= data.total, onclick: () => { state.msgQuery.offset = offset + MESSAGE_PAGE; loadMessages(); } }));
}

// ---------------------------------------------------------------------------
// Downloads
// ---------------------------------------------------------------------------

const JOB_STATUS = {
  starting: 'Preparant…',
  downloading: 'Baixant',
  waiting: 'Esperant (Telegram)',
  retrying: 'Tornant-ho a provar',
  finishing: 'Acabant…',
};

async function setPaused(paused) {
  try {
    state.downloads = await api(`downloads/${paused ? 'pause' : 'resume'}`, { method: 'POST' });
    renderDownloads();
  } catch (e) { showError(e); }
}

function jobCard(job) {
  const pct = job.size ? Math.min(100, (100 * job.bytes_done) / job.size) : 0;
  const bar = h('div', { class: 'progress-bar' });
  bar.style.width = `${pct.toFixed(1)}%`;   // CSSOM: allowed by the CSP (inline style attributes are not)
  const facts = [`${formatBytes(job.bytes_done)} de ${formatBytes(job.size)} (${Math.floor(pct)} %)`];
  if (job.speed) facts.push(`${formatBytes(job.speed)}/s`);
  if (job.eta != null) facts.push(formatEta(job.eta));
  if (job.connections > 1) facts.push(`${job.connections} connexions`);
  const warn = job.status === 'retrying' || job.status === 'waiting';
  return h('li', { class: 'dl-job card', dataset: { status: job.status } },
    h('div', { class: 'row-between' },
      h('div', { class: 'grow' },
        h('strong', { class: 'name', text: job.file_name }),
        h('div', { class: 'source-meta', text: job.source_title })),
      h('div', { class: 'row' },
        pill(JOB_STATUS[job.status] || job.status, warn ? 'pill-warn' : 'pill-info'),
        h('button', { type: 'button', class: 'btn btn-small btn-danger-ghost', text: 'Treu de la cua', title: "El que ja s'ha baixat es guarda per a més endavant", onclick: () => queueFiles(job.source_id, [job.msg_id], false) }))),
    h('div', { class: 'progress', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': String(Math.floor(pct)), 'aria-label': job.file_name }, bar),
    h('div', { class: 'source-meta', text: facts.join(' · ') }),
    job.note ? h('div', { class: 'msg-text', text: job.note }) : null);
}

function renderDownloads() {
  const d = state.downloads;
  const active = d ? d.active.length : 0;
  const badge = $('#downloads-badge');
  badge.hidden = active === 0;
  badge.textContent = String(active);
  const box = $('#downloads-content');
  if (!d) { setChildren(box, spinner('Carregant…')); return; }
  const banners = [];
  if (d.paused) banners.push(h('div', { class: 'banner banner-warn', text: "Les baixades estan en pausa. El que ja s'ha baixat es guarda." }));
  else if (d.blocked) banners.push(h('div', { class: 'banner banner-error', text: d.blocked }));
  if (d.premium === false && active) banners.push(h('div', { class: 'banner banner-info', text: 'El compte no és Premium: Telegram limita la velocitat de baixada.' }));
  const total = d.queued + active;
  const facts = [`Queden ${formatBytes(d.remaining)}`];
  if (d.speed) facts.push(`${formatBytes(d.speed)}/s`);
  if (d.eta != null) facts.push(formatEta(d.eta));
  const summary = h('section', { class: 'card' },
    h('div', { class: 'row-between' },
      h('div', { class: 'grow' },
        h('h2', { text: total ? `${plural(active, 'fitxer baixant-se', 'fitxers baixant-se')} · ${d.queued} en cua` : 'No hi ha res a la cua' }),
        total ? h('div', { class: 'source-meta', text: facts.join(' · ') })
          : h('p', { class: 'muted', text: 'Posa fitxers a la cua des de «Fonts»: «Baixa el que falta», o fitxer a fitxer.' })),
      h('div', { class: 'row' },
        d.paused
          ? h('button', { type: 'button', class: 'btn btn-primary', text: 'Reprèn les baixades', onclick: () => setPaused(false) })
          : h('button', { type: 'button', class: 'btn', text: 'Pausa', disabled: !total, onclick: () => setPaused(true) }))));
  setChildren(box, ...banners, summary, active ? h('ul', { class: 'dl-jobs' }, d.active.map(jobCard)) : null);
}

// ---------------------------------------------------------------------------
// Views, storage banner, live updates
// ---------------------------------------------------------------------------

const VIEW_HASHES = { telegram: '#telegram', sources: '#fonts', downloads: '#baixades', settings: '#configuracio' };

function showView(view) {
  state.view = view;
  if (window.location.hash !== VIEW_HASHES[view]) history.replaceState(null, '', VIEW_HASHES[view]);
  for (const btn of document.querySelectorAll('.nav-btn')) {
    if (btn.dataset.view === view) btn.setAttribute('aria-current', 'page');
    else btn.removeAttribute('aria-current');
  }
  for (const name of Object.keys(VIEW_HASHES)) $(`#view-${name}`).hidden = name !== view;
  if (view === 'settings') loadSettingsForm();
}

function renderStorage(status) {
  const banner = $('#storage-banner');
  banner.hidden = !status || status.ok;
  banner.textContent = status && !status.ok ? status.problem : '';
  const info = $('#storage-info');
  if (!info || !status) return;
  setChildren(info,
    h('dt', { text: 'Estat' }), h('dd', {}, status.ok ? pill('Disponible', 'pill-ok') : pill('No disponible', 'pill-err')),
    h('dt', { text: 'Espai lliure' }), h('dd', { text: status.free_bytes == null ? '—' : formatBytes(status.free_bytes) }),
    h('dt', { text: 'Sistema de fitxers' }), h('dd', { text: status.filesystem || '—' }));
}

async function checkHealth() {
  try {
    const health = await api('health');
    $('#footer-info').textContent = `Telegram Downloader ${health.version}`;
    renderStorage(health.storage);
  } catch { /* the connection indicator says so */ }
}

let eventSource = null;
let reconnectDelay = 1000;
let reconnectTimer = 0;

function setConnection(name) {
  const el = $('#conn-status');
  el.dataset.state = name;
  $('.conn-text', el).textContent = { online: 'Connectat', connecting: 'Connectant…', offline: 'Sense connexió. Reintentant…' }[name] || '';
}

function disconnectEvents() {
  clearTimeout(reconnectTimer);
  if (eventSource) eventSource.close();
  eventSource = null;
}

function onEvent(name, handler) {
  eventSource.addEventListener(name, (ev) => {
    try { handler(JSON.parse(ev.data)); } catch (err) { console.warn(`Bad ${name} event`, err); }
  });
}

function connectEvents() {
  clearTimeout(reconnectTimer);
  if (state.locked) return;
  if (eventSource) eventSource.close();
  setConnection('connecting');
  const es = new EventSource('api/events');
  eventSource = es;
  es.addEventListener('open', () => {
    reconnectDelay = 1000;
    setConnection('online');
    loadSources().catch((e) => console.warn('loadSources failed', e));
    checkHealth();
  });
  onEvent('telegram', (tg) => {
    const was = state.telegram?.state;
    state.telegram = tg;
    if (was && was !== 'ready' && tg.state === 'ready') toast(`Sessió iniciada com a ${tg.me?.name || ''}.`, { kind: 'success' });
    renderTelegram();
  });
  onEvent('source', (src) => {
    const prev = state.sources.get(src.id);
    state.sources.set(src.id, src);
    renderSources();
    const countsChanged = JSON.stringify(prev?.counts || {}) !== JSON.stringify(src.counts || {});
    if (state.openSource === src.id && ((prev?.indexing && !src.indexing) || countsChanged)) loadMessagesSoon();
  });
  onEvent('source_deleted', ({ id }) => { state.sources.delete(id); renderSources(); });
  onEvent('downloads', (d) => { state.downloads = d; renderDownloads(); });
  es.addEventListener('error', () => {
    if (state.locked) { es.close(); return; }
    setConnection('offline');
    if (es.readyState === EventSource.CLOSED) {
      checkSession();
      es.close();
      if (eventSource === es) eventSource = null;
      reconnectTimer = setTimeout(connectEvents, reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 2, 15000);
    }
  });
}

// ---------------------------------------------------------------------------
// Folder browser
// ---------------------------------------------------------------------------

/**
 * Let the user pick a folder on the NAS. Resolves with the path, or null if cancelled.
 * @param {{ title?: string, start?: string }} opts
 * @returns {Promise<string | null>}
 */
function pickFolder({ title = 'Tria una carpeta', start = '' } = {}) {
  const pathInput = h('input', { type: 'text', spellcheck: 'false', 'aria-label': 'Camí de la carpeta', class: 'picker-path' });
  const filter = h('input', { type: 'search', placeholder: 'Filtra les carpetes', 'aria-label': 'Filtra les carpetes' });
  const list = h('ul', { class: 'picker-list' });
  const info = h('p', { class: 'muted picker-info' });
  const upBtn = h('button', { type: 'button', class: 'btn btn-small', text: '↑ Amunt' });
  const okBtn = h('button', { type: 'button', class: 'btn btn-primary', text: 'Tria aquesta carpeta' });
  const cancelBtn = h('button', { type: 'button', class: 'btn', text: 'Cancel·la' });
  const dialog = h('dialog', { class: 'dialog dialog-wide', 'aria-label': title },
    h('h2', { text: title }),
    h('div', { class: 'picker-bar' }, upBtn, pathInput,
      h('button', { type: 'button', class: 'btn btn-small', text: 'Ves-hi', onclick: () => go(pathInput.value.trim()) })),
    filter, list, info,
    h('div', { class: 'dialog-actions' }, cancelBtn, okBtn));
  document.body.append(dialog);

  let current = null;
  const fill = () => {
    const q = normalise(filter.value);
    const dirs = (current?.dirs || []).filter((d) => !q || normalise(d.name).includes(q));
    setChildren(list, dirs.length ? dirs.map((d) => h('li', {},
      h('button', { type: 'button', class: 'picker-dir', text: `📁 ${d.name}`, onclick: () => go(d.path) })))
      : h('li', { class: 'muted', text: current?.dirs?.length ? 'Cap carpeta coincideix amb el filtre.' : 'No hi ha subcarpetes.' }));
  };
  const go = async (path) => {
    setChildren(list, h('li', {}, spinner('Carregant…')));
    try {
      current = await api(`fs?${new URLSearchParams({ path })}`);
    } catch (e) {
      showError(e);
      if (!current) { setChildren(list); return; }
    }
    pathInput.value = current.path;
    filter.value = '';
    upBtn.disabled = !current.parent;
    const bits = [plural(current.dirs.length, 'carpeta', 'carpetes')];
    if (current.videos) bits.push(plural(current.videos, 'vídeo', 'vídeos'));
    if (current.archives) bits.push(plural(current.archives, 'arxiu comprimit', 'arxius comprimits'));
    if (current.tdl) bits.push(`${current.tdl} de tdl`);
    if (current.truncated) bits.push('(llista retallada)');
    info.textContent = bits.join(' · ') + (current.root ? ` — només es pot navegar dins de ${current.root}` : '');
    fill();
  };
  upBtn.addEventListener('click', () => current?.parent && go(current.parent));
  filter.addEventListener('input', fill);
  pathInput.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') { ev.preventDefault(); go(pathInput.value.trim()); } });

  return new Promise((resolve) => {
    let result = null;
    okBtn.addEventListener('click', () => { result = current?.path || null; dialog.close(); });
    cancelBtn.addEventListener('click', () => dialog.close());
    dialog.addEventListener('close', () => { dialog.remove(); resolve(result); }, { once: true });
    dialog.showModal();
    go(start);
  });
}

/** A "Tria…" button that fills ``input`` with the chosen folder and fires its events. */
function browseButton(input, title) {
  return h('button', {
    type: 'button', class: 'btn btn-small', text: 'Tria…', title: 'Navega per les carpetes',
    onclick: async () => {
      if (input.disabled) return;
      const path = await pickFolder({ title, start: input.value.trim() || (input.placeholder.startsWith('/') ? input.placeholder : '') });
      if (path == null) return;
      input.value = path;
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.dispatchEvent(new Event('change', { bubbles: true }));
    },
  });
}

/** Put a "Tria…" button after each path field of the settings form. */
function addSettingsBrowsers() {
  const titles = { download_dir: 'Carpeta de baixades', temp_dir: 'Carpeta temporal', library_tv_dir: 'Biblioteca de sèries', library_movies_dir: 'Biblioteca de pel·lícules' };
  for (const [field, title] of Object.entries(titles)) {
    const input = $(`#settings-form [data-field="${field}"]`);
    if (!input || input.parentElement.classList.contains('path-field')) continue;
    const wrap = h('div', { class: 'path-field' });
    input.replaceWith(wrap);
    wrap.append(input, browseButton(input, title));
  }
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

function settingsInputs() {
  return [...document.querySelectorAll('#settings-form [data-field]')];
}

async function loadSettingsForm() {
  try {
    state.settings = await api('settings');
  } catch (e) { showError(e); return; }
  fillSettingsForm(state.settings);
  $('#settings-loading').hidden = true;
  $('#settings-form').hidden = false;
  checkHealth();
}

function fillSettingsForm(s) {
  const locked = new Set(s.locked || []);
  for (const el of settingsInputs()) {
    if (el.dataset.kind === 'bool') el.checked = Boolean(s[el.dataset.field]);
    else el.value = s[el.dataset.field] ?? '';
    el.disabled = locked.has(el.dataset.field);
    el.title = el.disabled ? 'Definit per una variable d\'entorn' : '';
  }
  $('#set-temp-dir').placeholder = s.effective_temp_dir || '';
  $('#set-api-id').value = s.telegram_api_id || '';
  $('#set-api-id').disabled = locked.has('telegram_api_id');
  $('#set-api-hash').value = '';
  $('#set-api-hash').disabled = locked.has('telegram_api_hash');
  $('#set-api-hash').placeholder = s.telegram_api_hash_set ? '•••••••• (desat; escriu-ne un de nou per canviar-lo)' : '';
  $('#api-hash-state').textContent = s.telegram_api_hash_set ? 'Desat.' : 'Encara no està configurat.';
  $('#set-api-key').value = s.api_key || '';
  setChildren($('#api-help'), apiHelp());
  markDirty(false);
}

function readSettingsForm() {
  const out = {};
  for (const el of settingsInputs()) {
    if (el.disabled) continue;
    if (el.dataset.kind === 'bool') out[el.dataset.field] = el.checked;
    else out[el.dataset.field] = el.dataset.kind === 'int' ? Number(el.value) : el.value.trim();
  }
  if (!$('#set-api-id').disabled) out.telegram_api_id = $('#set-api-id').value.trim() || 0;
  if (!$('#set-api-hash').disabled && $('#set-api-hash').value.trim()) out.telegram_api_hash = $('#set-api-hash').value.trim();
  return out;
}

function markDirty(dirty) {
  $('#settings-dirty').hidden = !dirty;
  $('#settings-save').disabled = !dirty;
  $('#settings-discard').disabled = !dirty;
}

async function saveSettings(event) {
  event.preventDefault();
  const btn = $('#settings-save');
  btn.disabled = true;
  try {
    state.settings = await api('settings', { method: 'PUT', body: readSettingsForm() });
    fillSettingsForm(state.settings);
    toast("S'ha desat la configuració.", { kind: 'success' });
    checkHealth();
  } catch (e) {
    showError(e, "No s'ha pogut desar");
    btn.disabled = false;
  }
}

function wireStaticUi() {
  addSettingsBrowsers();
  for (const btn of document.querySelectorAll('.nav-btn')) btn.addEventListener('click', () => showView(btn.dataset.view));
  $('#settings-form').addEventListener('input', () => markDirty(true));
  $('#settings-form').addEventListener('submit', saveSettings);
  $('#settings-discard').addEventListener('click', () => fillSettingsForm(state.settings));
  $('#set-copy-key').addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText($('#set-api-key').value);
      toast("S'ha copiat la clau.", { kind: 'success', timeout: 2000 });
    } catch {
      $('#set-api-key').select();
    }
  });
}

async function startApp() {
  if (state.started) { connectEvents(); return; }
  state.started = true;
  try {
    state.settings = await api('settings');
    state.telegram = await api('telegram/status');
  } catch (e) { showError(e); }
  renderTelegram();
  const fromHash = Object.keys(VIEW_HASHES).find((v) => VIEW_HASHES[v] === window.location.hash);
  if (fromHash) showView(fromHash);
  connectEvents();
  setInterval(checkHealth, 60000);
}

// ---------------------------------------------------------------------------
// Authentication (from 3Cat Downloader)
// ---------------------------------------------------------------------------

async function fetchAuthStatus() {
  const status = await api('auth/status');
  state.auth = status;
  applyMode();
  return status;
}

/** Reflect the mode and session state in the page chrome. */
function applyMode() {
  const auth = state.auth;
  const server = Boolean(auth && auth.mode === 'server');
  $('#session-box').hidden = !(auth && auth.auth_required);
  $('#logout-btn').hidden = !(auth && auth.auth_required && auth.authenticated);
  $('#password-form').hidden = !(server && auth.auth_required && !auth.password_from_env);
}

function showAuthScreen() {
  const auth = state.auth || { configured: true };
  const firstRun = !auth.configured;
  state.locked = true;
  disconnectEvents();
  document.body.classList.add('is-locked');
  $('#auth-screen').hidden = false;
  $('#auth-title').textContent = firstRun ? 'Crea una contrasenya' : 'Inicia la sessió';
  $('#auth-text').textContent = firstRun
    ? `Primer cop que obres Telegram Downloader. Tria una contrasenya (mínim ${MIN_PASSWORD} caràcters) per protegir-lo.`
    : 'Escriu la contrasenya de Telegram Downloader.';
  $('#auth-confirm-row').hidden = !firstRun;
  $('#auth-password').setAttribute('autocomplete', firstRun ? 'new-password' : 'current-password');
  $('#auth-submit').textContent = firstRun ? 'Crea la contrasenya i entra' : 'Entra';
  setAuthError('');
  applyMode();
  setTimeout(() => $('#auth-password').focus(), 0);
}

function hideAuthScreen() {
  state.locked = false;
  document.body.classList.remove('is-locked');
  $('#auth-screen').hidden = true;
  $('#auth-password').value = '';
  $('#auth-confirm').value = '';
}

function setAuthError(message) {
  const el = $('#auth-error');
  el.textContent = message;
  el.hidden = !message;
}

let unauthorizedPending = false;

/** Any protected call returned 401: the session expired or was revoked. */
async function handleUnauthorized() {
  if (state.locked || unauthorizedPending) return;
  unauthorizedPending = true;
  try {
    const status = await fetchAuthStatus();
    if (status.auth_required && !status.authenticated) {
      showAuthScreen();
      toast('La sessió ha caducat. Torna a entrar.', { kind: 'warning' });
    }
  } catch { /* server unreachable: the connection indicator already says so */ }
  finally { unauthorizedPending = false; }
}

async function checkSession() {
  if (!state.auth || !state.auth.auth_required || state.locked) return;
  try {
    const status = await api('auth/status');
    if (!status.authenticated) handleUnauthorized();
  } catch { /* offline */ }
}

async function submitAuth(event) {
  event.preventDefault();
  const firstRun = state.auth && !state.auth.configured;
  const password = $('#auth-password').value;
  if (!password) { setAuthError('Escriu la contrasenya.'); return; }
  if (firstRun) {
    if (password.length < MIN_PASSWORD) {
      setAuthError(`La contrasenya ha de tenir com a mínim ${MIN_PASSWORD} caràcters.`);
      return;
    }
    if (password !== $('#auth-confirm').value) { setAuthError('Les contrasenyes no coincideixen.'); return; }
  }
  const btn = $('#auth-submit');
  btn.disabled = true;
  setAuthError('');
  try {
    state.auth = await api(firstRun ? 'auth/setup' : 'auth/login', { method: 'POST', body: { password } });
    hideAuthScreen();
    applyMode();
    await startApp();
  } catch (err) {
    if (err instanceof ApiError && err.status === 409) {
      // Someone else set the password meanwhile (or it comes from the environment).
      await fetchAuthStatus().catch(() => {});
      showAuthScreen();
    }
    setAuthError(errorMessage(err));
    $('#auth-password').select();
  } finally {
    btn.disabled = false;
  }
}

async function logout() {
  try { await api('auth/logout', { method: 'POST' }); } catch { /* ignore */ }
  state.settings = null;
  try { await fetchAuthStatus(); } catch { /* ignore */ }
  showAuthScreen();
}

async function changePassword(event) {
  event.preventDefault();
  const current = $('#pw-current').value;
  const next = $('#pw-new').value;
  const errorEl = $('#pw-error');
  const fail = (msg) => { errorEl.textContent = msg; errorEl.hidden = !msg; };
  if (next.length < MIN_PASSWORD) { fail(`La contrasenya nova ha de tenir com a mínim ${MIN_PASSWORD} caràcters.`); return; }
  if (next !== $('#pw-confirm').value) { fail('Les contrasenyes noves no coincideixen.'); return; }
  fail('');
  const btn = $('#pw-save');
  btn.disabled = true;
  try {
    await api('auth/password', { method: 'PUT', body: { current, new: next } });
    for (const id of ['#pw-current', '#pw-new', '#pw-confirm']) $(id).value = '';
    toast("S'ha canviat la contrasenya.", { kind: 'success' });
  } catch (err) {
    fail(errorMessage(err));
  } finally {
    btn.disabled = false;
  }
}

function wireAuthUi() {
  $('#auth-form').addEventListener('submit', submitAuth);
  $('#auth-show').addEventListener('change', (ev) => {
    const type = ev.target.checked ? 'text' : 'password';
    $('#auth-password').type = type;
    $('#auth-confirm').type = type;
  });
  $('#logout-btn').addEventListener('click', logout);
  $('#password-form').addEventListener('submit', changePassword);
}

async function boot() {
  wireStaticUi();
  wireAuthUi();
  let status = null;
  try {
    status = await fetchAuthStatus();
  } catch (err) {
    console.warn('Could not read auth status', err);
  }
  if (status && status.auth_required && !status.authenticated) {
    showAuthScreen();
    return;
  }
  await startApp();
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
else boot();
