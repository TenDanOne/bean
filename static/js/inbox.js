/* Bandeja social: foco y «Volver» en móvil, leyenda de estados, navegación con teclado y estados de carga/error.
 * Todo con eventos delegados en document: el servidor vuelve a dibujar el contenido en cada cambio. */
(function () {
  'use strict';
  var origin = null; /* elemento de la lista que abrió el detalle, para devolverle el foco */

  function $(s, r) { return (r || document).querySelector(s); }
  function $$(s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); }
  function visible(el) { return !!(el && el.offsetParent); }
  function detail() { return document.getElementById('inbox-detail-panel'); }

  /* ── Detalle: cargando, error, y foco al abrir ── */
  document.addEventListener('htmx:beforeRequest', function (e) {
    var item = e.detail.elt.closest && e.detail.elt.closest('[data-ib-open]');
    if (!item || e.detail.target !== detail()) return;
    detail().innerHTML =
      '<div class="ib-sk" role="status" aria-busy="true"><span class="sr-only">Cargando mensajes…</span><div></div><div></div><div></div></div>' +
      '<p class="ib-hint" style="margin-top:8px">Cargando…</p>';
  });

  document.addEventListener('htmx:afterSwap', function (e) {
    var elt = e.detail.requestConfig && e.detail.requestConfig.elt;
    var item = elt && elt.closest && elt.closest('[data-ib-open]');
    if (e.detail.target === detail() && item) {
      origin = item;
      $$('.ib-it[aria-current]').forEach(function (b) { b.removeAttribute('aria-current'); });
      item.setAttribute('aria-current', 'true');
      var nuevo = $('.ib-k-nuevo', item);
      if (nuevo) nuevo.remove();
      var split = document.getElementById('ib-split');
      if (split) split.classList.add('has-sel');
      var back = $('[data-ib-back]', detail());
      /* En móvil el foco va a «Volver»; en escritorio, al título del detalle. */
      var target = visible(back) ? back : $('[data-ib-title]', detail());
      if (target) target.focus();
    }
    if (e.detail.target && e.detail.target.id === 'inbox-message-list') { origin = null; updateMore(); }
  });

  function failed(e) {
    if (e.detail.target !== detail()) return;
    var item = e.detail.elt.closest && e.detail.elt.closest('[data-ib-open]');
    if (!item) return;
    detail().innerHTML =
      '<div class="ib-alert ib-al-err" role="alert"><div>No se pudo cargar. Revisa tu conexión e inténtalo de nuevo. ' +
      '<button type="button" class="ib-btn ib-btn-secondary" data-ib-retry>Reintentar</button></div></div>';
    origin = item;
  }
  /* htmx restaura el foco por id: si quedó en la pestaña o cuenta anterior, vuelve al elemento activo nuevo. */
  document.addEventListener('htmx:afterSettle', function (e) {
    if (!e.detail.target || e.detail.target.id !== 'inbox-message-list') return;
    var a = document.activeElement;
    var want = $('[role=tab][aria-selected=true]') && a && a.getAttribute('role') === 'tab' ? $('[role=tab][aria-selected=true]')
      : a && a.getAttribute('role') === 'radio' ? $('[role=radio][aria-checked=true]') : null;
    if (want && want !== a) want.focus();
  });
  document.addEventListener('htmx:responseError', failed);
  document.addEventListener('htmx:sendError', failed);

  /* ── Clics: Volver, Reintentar ── */
  document.addEventListener('click', function (e) {
    if (e.target.closest('[data-ib-back]')) {
      var split = document.getElementById('ib-split');
      if (split) split.classList.remove('has-sel');
      if (origin && document.body.contains(origin)) origin.focus();
      return;
    }
    var saved = e.target.closest('[data-ib-saved]');
    if (saved) {
      var form = saved.closest('form'), ta = form && form.querySelector('textarea');
      if (ta) { ta.value = saved.dataset.body; ta.focus(); }
      return;
    }
    if (e.target.closest('[data-ib-retry]') && origin) { origin.click(); return; }
    var help = $('[data-ib-help][data-open]');
    if (help && !e.target.closest('[data-ib-help]')) closeHelp(help, false);
    var btn = e.target.closest('.ib-helpb');
    if (btn) {
      var h = btn.closest('[data-ib-help]');
      h.removeAttribute('data-closed');
      if (h.hasAttribute('data-open')) closeHelp(h, false); else openHelp(h);
    }
  });

  /* ── Leyenda «?»: hover, foco y toque; Esc cierra y devuelve el foco ── */
  function openHelp(h) { h.setAttribute('data-open', ''); $('.ib-helpb', h).setAttribute('aria-expanded', 'true'); }
  function closeHelp(h, refocus) {
    h.removeAttribute('data-open');
    $('.ib-helpb', h).setAttribute('aria-expanded', 'false');
    if (refocus) $('.ib-helpb', h).focus();
  }
  function sync(h, on) { $('.ib-helpb', h).setAttribute('aria-expanded', on ? 'true' : 'false'); }
  document.addEventListener('mouseover', function (e) {
    var h = e.target.closest('[data-ib-help]');
    if (h) { h.removeAttribute('data-closed'); sync(h, true); }
  });
  document.addEventListener('mouseout', function (e) {
    var h = e.target.closest('[data-ib-help]');
    if (h && !h.contains(e.relatedTarget) && !h.hasAttribute('data-open') && !h.matches(':focus-within')) sync(h, false);
  });
  document.addEventListener('focusin', function (e) {
    var h = e.target.closest('[data-ib-help]');
    if (h) sync(h, true);
  });
  document.addEventListener('focusout', function (e) {
    var h = e.target.closest('[data-ib-help]');
    if (h && !h.contains(e.relatedTarget)) { h.removeAttribute('data-closed'); if (!h.hasAttribute('data-open')) sync(h, false); }
  });
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    var h = $('[data-ib-help]');
    if (h && $('.ib-helpb', h).getAttribute('aria-expanded') === 'true') {
      h.removeAttribute('data-open');
      h.setAttribute('data-closed', '');
      sync(h, false);
      $('.ib-helpb', h).focus();
      e.preventDefault();
    }
  });

  /* ── Teclado: flechas en listas, pestañas y selector de cuenta ── */
  document.addEventListener('keydown', function (e) {
    var list = e.target.closest('[data-ib-nav]');
    if (list && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
      var b = $$('button.ib-it', list), i = b.indexOf(e.target.closest('button'));
      if (i > -1) { b[Math.max(0, Math.min(b.length - 1, i + (e.key === 'ArrowDown' ? 1 : -1)))].focus(); e.preventDefault(); }
      return;
    }
    var tl = e.target.closest('[data-ib-tablist]');
    if (tl && (e.key === 'ArrowRight' || e.key === 'ArrowLeft')) {
      var t = $$('[role=tab]', tl), j = t.indexOf(e.target);
      if (j > -1) { var nt = t[(j + (e.key === 'ArrowRight' ? 1 : t.length - 1)) % t.length]; nt.focus(); nt.click(); e.preventDefault(); }
      return;
    }
    var rg = e.target.closest('[role=radiogroup]');
    var step = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: 2, ArrowUp: -2 }[e.key];
    if (rg && step) {
      var r = $$('[role=radio]', rg), k = r.indexOf(e.target);
      if (k > -1) { var nr = r[Math.max(0, Math.min(r.length - 1, k + step))]; nr.focus(); nr.click(); e.preventDefault(); }
    }
  });

  /* ── Degradado al pie del selector de cuenta cuando hay más de 6 ── */
  function updateMore() {
    $$('[data-ib-more]').forEach(function (w) {
      var bar = w.firstElementChild;
      function u() { w.classList.toggle('more', bar.scrollHeight > bar.clientHeight + 1 && bar.scrollTop + bar.clientHeight < bar.scrollHeight - 2); }
      var cur = $('[aria-checked=true]', bar);
      if (cur && bar.scrollHeight > bar.clientHeight) cur.scrollIntoView({ block: 'nearest' });
      bar.onscroll = u;
      u();
    });
  }
  document.addEventListener('DOMContentLoaded', updateMore);
  window.addEventListener('resize', updateMore);
  updateMore();
})();
