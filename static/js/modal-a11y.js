/* Accesibilidad común de modales Alpine (CSP-safe, sin plugins).
 * Uso: añadir el atributo `data-modal` al contenedor del modal (el que lleva x-show).
 * Opcional: `data-modal-label="Texto"` si no tiene encabezado h1-h3; `data-modal-focus` en el campo que recibe el foco.
 * Pone role="dialog" aria-modal="true", mueve el foco dentro al abrir, lo atrapa con Tab y lo devuelve al cerrar. */
(function () {
  var open = new WeakMap();
  var FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]):not([type=hidden]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';
  var uid = 0;

  function visible(el) { return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length); }
  function items(el) { return Array.prototype.filter.call(el.querySelectorAll(FOCUSABLE), visible); }

  function setup(el) {
    el.setAttribute('role', 'dialog');
    el.setAttribute('aria-modal', 'true');
    if (el.getAttribute('aria-label') || el.getAttribute('aria-labelledby')) return;
    var label = el.getAttribute('data-modal-label');
    if (label) { el.setAttribute('aria-label', label); return; }
    var h = el.querySelector('h1,h2,h3');
    if (h) { if (!h.id) h.id = 'modal-h-' + (++uid); el.setAttribute('aria-labelledby', h.id); }
  }

  function onKey(e) {
    if (e.key !== 'Tab') return;
    var list = items(this);
    if (!list.length) { e.preventDefault(); return; }
    var first = list[0], last = list[list.length - 1];
    if (e.shiftKey && (document.activeElement === first || !this.contains(document.activeElement))) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && (document.activeElement === last || !this.contains(document.activeElement))) { e.preventDefault(); first.focus(); }
  }

  function scan() {
    document.querySelectorAll('[data-modal]').forEach(function (el) {
      var was = open.get(el), is = visible(el);
      if (is && !was) {
        setup(el);
        open.set(el, { from: document.activeElement });
        el.addEventListener('keydown', onKey);
        setTimeout(function () {
          if (el.contains(document.activeElement)) return;
          var t = el.querySelector('[data-modal-focus]') || items(el)[0];
          if (t) t.focus();
        }, 30);
      } else if (!is && was) {
        el.removeEventListener('keydown', onKey);
        open.delete(el);
        if (was.from && document.contains(was.from)) was.from.focus();
      }
    });
  }

  var queued = false;
  function queue() { if (queued) return; queued = true; requestAnimationFrame(function () { queued = false; scan(); }); }
  new MutationObserver(queue).observe(document.documentElement, { subtree: true, childList: true, attributes: true, attributeFilter: ['style', 'class', 'hidden'] });
  document.addEventListener('DOMContentLoaded', queue);
})();
