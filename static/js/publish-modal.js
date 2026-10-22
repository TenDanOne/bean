// Modal «Publicando» del composer: bloquea el envío doble y muestra el avance por cuenta.
// Lee la URL de estado y los destinos del <dialog id="publish-dialog"> (data-*); sin dependencias.
(function () {
  'use strict';
  const ZERO = '00000000-0000-0000-0000-000000000000'; // marcador de la URL; se cambia por el id del post
  const POLL_MS = 2000;
  const MAX_POLLS = 90; // ~3 min; después se invita a revisar en Enviadas
  const WORD = { wait: 'Esperando', run: 'Publicando', ok: 'Listo', proc: 'Procesando', draft: 'Enviado como borrador', private: 'Subido como privado', manual: 'A mano', fail: 'Falló' };
  const CLS = { wait: 'st-draft', run: 'st-publishing', ok: 'st-published', proc: 'st-review', draft: 'st-published', private: 'st-draft', manual: 'st-published', fail: 'st-failed' };
  const PLAT = { youtube: 'YouTube', instagram: 'Instagram', instagram_login: 'Instagram', facebook: 'Facebook', tiktok: 'TikTok' };
  const BUSY = { publish_now: 'Publicando…', schedule: 'Programando…', save_draft: 'Guardando…' };
  const isFinal = (s) => s === 'ok' || s === 'fail' || s === 'proc' || s === 'draft' || s === 'private' || s === 'manual';

  let sending = false, locked = [], dlg = null, live = null, body = null;
  let rows = [], kind = null, postId = null, requestDone = false, timer = null, polls = 0, finished = false, kinds = {};

  const form = () => document.getElementById('composer-form');
  const el = (tag, cls, txt) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (txt != null) n.textContent = txt;
    return n;
  };
  const app = () => {
    const root = document.querySelector('[x-data="composerApp()"]');
    return root && window.Alpine ? window.Alpine.$data(root) : null;
  };
  const csrf = () => document.querySelector('[name=csrfmiddlewaretoken]')?.value || '';
  const title = () => form()?.querySelector('[name=title]')?.value.trim() || 'tu publicación';
  const say = (t) => { if (live) live.textContent = t; };

  function actionOf(detail) {
    let a = detail.requestConfig?.parameters?.action;
    if (Array.isArray(a)) a = a[a.length - 1];
    return a || 'save_draft';
  }

  // ---- bloqueo de los botones (clic, Enter y Ctrl+S convergen en el submit del formulario)
  function lock(action) {
    sending = true;
    const f = form();
    const submit = f.querySelector(`button[name=action][value="${action}"]`) || document.getElementById('publish-action-btn');
    locked = [...f.querySelectorAll('button[type=submit]')].map((b) => ({ b, was: b.disabled, kids: [...b.childNodes] }));
    locked.forEach(({ b }) => {
      b.disabled = true;
      if (b === submit) {
        b.setAttribute('aria-busy', 'true');
        b.replaceChildren(el('span', 'pm-spin'), document.createTextNode(' ' + (BUSY[action] || 'Enviando…')));
      }
    });
  }
  function unlock() {
    locked.forEach(({ b, kids }) => {
      b.disabled = false; // el clic ocurrió con el botón habilitado
      if (b.hasAttribute('aria-busy')) { b.removeAttribute('aria-busy'); b.replaceChildren(...kids); }
    });
    locked = [];
    sending = false;
  }

  // ---- modal
  function initialRows() {
    const d = app();
    if (!d) return [];
    return d.selectedAccounts.map((id) => ({
      account: d.accName(id), platform: PLAT[d.plat(id)] || d.plat(id), kind: d.kindLabel(id) || '', state: 'wait', error: '',
    }));
  }
  function initials(name) {
    const w = name.replace(/[@.]/g, ' ').trim().split(/\s+/);
    return (w.length > 1 ? w[0][0] + w[1][0] : name.replace('@', '').slice(0, 2)).toUpperCase();
  }
  function pill(state) {
    const p = el('span', 'pm-pill st-pill ' + CLS[state]);
    if (state === 'wait') p.textContent = WORD.wait;
    else p.append(WORD[state]);
    return p;
  }
  function button(label, cls, fn) {
    const b = el('button', 'ib-btn ' + cls, label);
    b.type = 'button';
    b.addEventListener('click', fn);
    return b;
  }
  function render() {
    const t = title(), n = rows.length;
    const allFinal = rows.length > 0 && rows.every((r) => isFinal(r.state));
    const stalled = requestDone && !allFinal && polls >= MAX_POLLS;
    const done = (allFinal && requestDone) || stalled;
    const ok = rows.filter((r) => r.state === 'ok' || r.state === 'proc' || r.state === 'draft' || r.state === 'private' || r.state === 'manual').length;
    const bad = rows.filter((r) => r.state === 'fail').length;
    const proc = rows.filter((r) => r.state === 'proc').length;
    const fin = rows.filter((r) => isFinal(r.state)).length;
    const head = el('div', 'pm-hd');
    const h = el('h2', null, !done ? `Publicando "${t}"` : bad ? `"${t}": ${bad} de ${n} falló` : proc ? `Enviada "${t}"` : `Publicada "${t}"`);
    h.id = 'pm-title';
    h.tabIndex = -1;
    head.append(h);
    if (done) { const x = el('button', 'pm-x', '×'); x.type = 'button'; x.setAttribute('aria-label', 'Cerrar'); x.addEventListener('click', closeDialog); head.append(x); }

    const bd = el('div', 'pm-bd');
    const prog = el('div', 'pm-prog');
    const tx = el('div', 'pm-tx');
    tx.append(el('span', null, `${ok} de ${n} ${n === 1 ? 'lista' : 'listas'}`));
    if (bad) tx.append(el('span', 'pm-bad', `${bad} ${bad === 1 ? 'falló' : 'fallaron'}`));
    const bar = el('div', 'pm-bar');
    bar.setAttribute('role', 'progressbar');
    bar.setAttribute('aria-label', 'Avance');
    bar.setAttribute('aria-valuemin', '0');
    bar.setAttribute('aria-valuemax', String(n));
    bar.setAttribute('aria-valuenow', String(fin));
    const fill = el('i');
    fill.style.width = (n ? (fin / n) * 100 : 0) + '%';
    bar.append(fill);
    prog.append(tx, bar);
    const ul = el('ul', 'pm-rows');
    ul.setAttribute('aria-label', 'Avance por cuenta');
    rows.forEach((r) => {
      const li = el('li', 'pm-row');
      const av = el('span', 'pm-av', initials(r.account));
      av.setAttribute('aria-hidden', 'true');
      const who = el('span', 'pm-who');
      who.append(el('b', null, r.account), el('small', null, r.platform + (r.kind ? ' · ' + r.kind : '')));
      li.append(av, who, pill(r.state));
      if (r.state === 'fail') li.append(el('div', 'pm-more pm-why', r.error || 'No se pudo publicar.'));
      else if (r.state === 'proc') li.append(el('div', 'pm-more', `${r.platform} confirma al terminar de procesar el contenido.`));
      else if (r.state === 'draft') li.append(el('div', 'pm-more', 'Termínalo en la app de la red.'));
      else if (r.state === 'private') li.append(el('div', 'pm-more', 'Abre YouTube Studio, ponle música y cámbialo a Público.'));
      else if (r.state === 'manual') li.append(el('div', 'pm-more', 'Publícalo tú desde la app.'));
      ul.append(li);
    });
    bd.append(prog, ul);

    const ft = el('div', 'pm-ft');
    if (!done) {
      ft.append(el('span', 'pm-hint', 'No cierres esta pestaña mientras se envía.'));
    } else if (stalled) {
      const al = el('div', 'ib-alert ib-al-info', 'Tarda más de lo normal. Puedes cerrar y revisar el resultado en Enviadas.');
      al.setAttribute('role', 'status');
      bd.append(al);
      ft.append(button('Cerrar', 'ib-btn-secondary', closeDialog), button('Ver en Publicar → Enviadas', 'ib-btn-primary', () => go('sent')));
    } else {
      const al = el('div', 'ib-alert ' + (bad ? (ok ? 'ib-al-warn' : 'ib-al-err') : proc ? 'ib-al-info' : 'ib-al-ok'));
      al.setAttribute('role', 'status');
      al.textContent = bad
        ? ok ? `${bad === 1 ? 'Una red falló' : bad + ' redes fallaron'}; las demás ya salieron. Clona la publicación para reintentar.` : 'No se publicó en ninguna cuenta. Clona la publicación para reintentar.'
        : proc ? 'Una red sigue procesando el contenido y se confirma al procesar pendientes. Puedes cerrar.'
        : n === 1 ? 'Quedó publicada.' : `Las ${n} quedaron publicadas.`;
      bd.append(al);
      if (bad) ft.append(button('Cerrar', 'ib-btn-secondary', closeDialog), button('Clonar para reintentar los fallidos', 'ib-btn-primary', cloneIt));
      else if (proc) ft.append(button('Cerrar', 'ib-btn-secondary', closeDialog), button('Ver en Publicar → Enviadas', 'ib-btn-primary', () => go('sent')));
      else ft.append(button('Crear otra', 'ib-btn-secondary', () => go('new')), button('Ver en Publicar → Enviadas', 'ib-btn-primary', () => go('sent')));
    }
    swap(head, bd, ft, done);
  }
  function renderSchedule(state, whenText) {
    const t = title(), done = state === 'ok';
    const head = el('div', 'pm-hd');
    const h = el('h2', null, done ? 'Programada' : 'Programando…');
    h.id = 'pm-title';
    h.tabIndex = -1;
    head.append(h);
    if (done) { const x = el('button', 'pm-x', '×'); x.type = 'button'; x.setAttribute('aria-label', 'Cerrar'); x.addEventListener('click', closeDialog); head.append(x); }
    const bd = el('div', 'pm-bd');
    bd.append(el('p', 'pm-big', done ? `"${t}" se programó para ${whenText}.` : `Programando "${t}"…`));
    if (done) { const al = el('div', 'ib-alert ib-al-info', 'Sale cuando pulses «Procesar pendientes» pasada esa hora.'); al.setAttribute('role', 'status'); bd.append(al); }
    const ft = el('div', 'pm-ft');
    if (done) ft.append(button('Crear otra', 'ib-btn-secondary', () => go('new')), button('Ver en el calendario', 'ib-btn-primary', () => go('calendar')));
    else ft.append(el('span', 'pm-hint', 'Un momento…'));
    swap(head, bd, ft, done);
    if (done) say(`Programada para ${whenText}.`);
  }
  function swap(head, bd, ft, done) {
    const hadFocus = body.contains(document.activeElement);
    body.replaceChildren(head, bd, ft);
    if (done && !finished) { finished = true; (ft.querySelector('.ib-btn-primary') || head.querySelector('h2')).focus(); }
    else if (!done && !hadFocus) head.querySelector('h2').focus();
  }
  function openDialog() {
    if (!dlg.open) dlg.showModal();
  }
  function closeDialog() {
    if (dlg.open) dlg.close(); // el evento «close» libera el formulario
  }
  function go(where) {
    const url = { sent: dlg.dataset.sentUrl, calendar: dlg.dataset.calendarUrl, new: dlg.dataset.newUrl }[where];
    if (url) window.location.href = url;
  }
  async function cloneIt() {
    if (!postId) return;
    try {
      const r = await fetch(dlg.dataset.cloneUrl.replace(ZERO, postId), {
        method: 'POST', headers: { 'X-CSRFToken': csrf(), 'HX-Request': 'true' }, credentials: 'same-origin',
      });
      const to = r.headers.get('HX-Redirect');
      if (to) window.location.href = to;
      else say('No se pudo clonar. Inténtalo de nuevo.');
    } catch (e) {
      say('No se pudo clonar. Inténtalo de nuevo.');
    }
  }

  // ---- avance real
  async function poll() {
    clearTimeout(timer);
    if (!postId || kind !== 'publish_now') return;
    polls += 1;
    try {
      const r = await fetch(dlg.dataset.statusUrl.replace(ZERO, postId), { headers: { Accept: 'application/json' }, credentials: 'same-origin' });
      if (r.ok) {
        const data = await r.json();
        const next = (data.rows || []).map((x) => ({
          account: x.account, platform: x.platform, kind: x.kind || kinds[x.account] || '', state: x.state, error: x.error || '',
        }));
        if (next.length) {
          next.forEach((x, i) => { if (rows[i] && rows[i].state !== x.state) say(`${x.platform}: ${WORD[x.state]}.`); });
          rows = next;
        }
      }
    } catch (e) { /* red caída: se reintenta en el siguiente ciclo */ }
    render();
    const done = requestDone && rows.length > 0 && rows.every((x) => isFinal(x.state));
    if (done) { say(`${rows.filter((x) => x.state !== 'fail').length} de ${rows.length} listas.`); return; }
    if (polls < MAX_POLLS || !requestDone) timer = setTimeout(poll, POLL_MS);
    else render();
  }

  // ---- ciclo de vida de un envío
  function begin(action) {
    lock(action);
    kind = action;
    finished = false; requestDone = false; polls = 0; postId = null;
    const m = (form().getAttribute('hx-post') || '').match(/compose\/([0-9a-f-]{36})\/save/);
    if (action === 'publish_now') {
      rows = initialRows();
      kinds = Object.fromEntries(rows.map((r) => [r.account, r.kind]));
      openDialog();
      render();
      say('Publicando.');
      if (m) { postId = m[1]; timer = setTimeout(poll, POLL_MS); }
    } else if (action === 'schedule') {
      openDialog();
      renderSchedule('run');
      say('Programando.');
    }
  }
  function after(detail) {
    const xhr = detail.xhr;
    if (!detail.successful) {
      closeDialog();
      return;
    }
    if (kind === 'publish_now' || kind === 'schedule') {
      let trig = {};
      try { trig = JSON.parse(xhr.getResponseHeader('HX-Trigger') || '{}'); } catch (e) { /* sin cabecera */ }
      postId = trig.postSaved?.postId || postId;
      requestDone = true;
      if (kind === 'schedule') {
        const d = app();
        renderSchedule('ok', d && d.scheduledDate ? `${d.fmtDate(d.scheduledDate)}, ${d.scheduledTime} (${d.tz})` : 'la hora elegida');
      } else poll();
    } else if (!/^add_to_queue/.test(kind || '')) unlock(); // borrador: solo toast; la cola redirige
  }

  function init() {
    dlg = document.getElementById('publish-dialog');
    if (!dlg || !form()) return;
    body = dlg.querySelector('.pm-body');
    live = dlg.querySelector('.pm-live');
    // Un solo envío a la vez, venga del botón, de Enter o de Ctrl+S.
    document.addEventListener('submit', (e) => {
      if (sending && e.target === form()) { e.preventDefault(); e.stopImmediatePropagation(); }
    }, true);
    document.addEventListener('htmx:beforeRequest', (e) => { if (e.target === form()) begin(actionOf(e.detail)); });
    document.addEventListener('htmx:afterRequest', (e) => { if (e.target === form()) after(e.detail); });
    // Esc no cierra mientras envía.
    const busy = () => sending && !finished;
    dlg.addEventListener('cancel', (e) => { if (busy()) e.preventDefault(); });
    dlg.addEventListener('close', () => { clearTimeout(timer); unlock(); });
    dlg.addEventListener('keydown', (e) => { if (e.key === 'Escape' && busy()) e.preventDefault(); });
  }
  window.PublishModal = { handles: (a) => a === 'publish_now' || a === 'schedule' };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
