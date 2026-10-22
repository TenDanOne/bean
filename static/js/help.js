/* Botones «?»: popover no modal con la entrada de Ayuda (datos en #help-data, ver apps/common/help_content.py).
   Delegado en document: funciona también con botones que Alpine crea después de cargar la página. */
(function () {
  'use strict';
  var data = {};
  try { data = JSON.parse(document.getElementById('help-data').textContent) || {}; } catch (e) { /* sin datos: no hay popovers */ }

  function build(btn) {
    var wrap = btn.closest('[data-help-wrap]');
    var pop = wrap && wrap.querySelector('.ib-pop');
    var entry = data[btn.dataset.help];
    if (!pop || !entry || pop.firstChild) return;
    var card = document.createElement('div');
    card.setAttribute('role', 'dialog');
    card.setAttribute('aria-label', entry.title);
    var inner = document.createElement('div');
    var h = document.createElement('h4'); h.textContent = entry.title;
    var p = document.createElement('p'); p.className = 'cp-pop-txt'; p.textContent = entry.text;
    inner.append(h, p);
    var a = document.createElement('a');
    a.className = 'ib-btn ib-btn-secondary';
    a.href = '/ayuda/#g-' + btn.dataset.help;
    a.textContent = 'Abrir en Ayuda';
    card.append(inner, a);
    pop.append(card);
  }

  function closeAll(except) {
    document.querySelectorAll('[data-help-wrap][data-open]').forEach(function (w) {
      if (w === except) return;
      w.removeAttribute('data-open');
      w.querySelector('.ib-helpb').setAttribute('aria-expanded', 'false');
    });
  }

  document.addEventListener('mouseover', function (e) { var b = e.target.closest && e.target.closest('.ib-helpb'); if (b) build(b); });
  document.addEventListener('focusin', function (e) { var b = e.target.closest && e.target.closest('.ib-helpb'); if (b) build(b); });
  document.addEventListener('click', function (e) {
    var b = e.target.closest('.ib-helpb');
    if (b) {
      e.preventDefault();
      var wrap = b.closest('[data-help-wrap]');
      var was = wrap.hasAttribute('data-open');
      closeAll();
      build(b);
      if (!was) { wrap.setAttribute('data-open', ''); b.setAttribute('aria-expanded', 'true'); }
      return;
    }
    if (!e.target.closest('.ib-pop')) closeAll();
  });
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    var open = document.querySelector('[data-help-wrap][data-open] .ib-helpb');
    closeAll();
    if (open) {
      // data-closed anula también el :hover/:focus-within que mantenía abierto el popover.
      open.closest('[data-help-wrap]').setAttribute('data-closed', '');
      open.focus();
    }
  });
  function reopenable() { document.querySelectorAll('[data-help-wrap][data-closed]').forEach(function (w) { w.removeAttribute('data-closed'); }); }
  document.addEventListener('click', reopenable, true);
  document.addEventListener('focusout', function (e) {
    var w = e.target.closest && e.target.closest('[data-help-wrap]');
    if (w) w.removeAttribute('data-closed');
  });
  document.addEventListener('mouseout', function (e) {
    var w = e.target.closest && e.target.closest('[data-help-wrap]');
    if (w && !w.contains(e.relatedTarget)) w.removeAttribute('data-closed');
  });

  /* Página Ayuda: filtro del glosario y destello de la entrada enlazada. */
  var q = document.getElementById('gq');
  if (q) {
    q.addEventListener('input', function () {
      var t = q.value.toLowerCase().trim();
      document.querySelectorAll('.cp-ge').forEach(function (g) { g.hidden = !!t && g.dataset.q.indexOf(t) < 0; });
      document.querySelectorAll('#glist section').forEach(function (s) {
        s.hidden = !s.querySelector('.cp-ge:not([hidden])');
      });
      var none = document.getElementById('gnone');
      if (none) none.hidden = !t || !!document.querySelector('.cp-ge:not([hidden])');
    });
  }
  function flash() {
    var t = location.hash && document.getElementById(location.hash.slice(1));
    if (t && t.classList.contains('cp-ge')) { t.scrollIntoView({ block: 'center' }); t.classList.remove('flash'); void t.offsetWidth; t.classList.add('flash'); }
  }
  window.addEventListener('hashchange', flash);
  flash();
})();
