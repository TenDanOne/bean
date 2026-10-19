/* Recorrido guiado de BEAN (vanilla, sin librerías; estilos .tour-* en styles.css).
 * Los pasos apuntan a elementos con data-tour="..."; un paso cuyo elemento no está en la
 * página (p. ej. «Procesar pendientes» fuera de Ajustes, Analíticas desactivado) se omite.
 * Se inicia con cualquier [data-tour-start]. «Ya visto» se guarda en localStorage (por navegador). */
(function () {
  'use strict';

  var SEEN_KEY = 'bean.tour.seen';
  var STEPS = [
    { sel: 'nuevo', side: true, t: 'Nuevo', d: 'Aquí empieza todo: crea una publicación para programarla o una idea para guardarla y retomarla después.' },
    { sel: 'publicar', side: true, t: 'Publicar (calendario)', d: 'Ves todas tus publicaciones en el calendario, con su estado, y programas nuevas con un clic sobre el día.' },
    { sel: 'canales', side: true, t: 'Canales', d: 'Tus cuentas conectadas. Usa «+ Conectar» o los iconos de abajo para añadir YouTube, Facebook, Instagram o TikTok.' },
    { sel: 'procesar', side: false, t: 'Procesar pendientes', d: 'Como BEAN no corre en un servidor 24/7, aquí salen las publicaciones programadas: pulsa el botón y se envían las que ya toca publicar.' },
    { sel: 'bandeja', side: true, t: 'Bandeja social', d: 'Comentarios y mensajes de tus canales en un solo lugar, para responder sin cambiar de app.' },
    { sel: 'analiticas', side: true, t: 'Analíticas', d: 'Cómo rinden tus publicaciones: alcance, interacciones y evolución por canal.' },
    { sel: 'primeros-pasos', side: true, last: true, t: '¡Listo!', d: 'Puedes repetir el recorrido desde Primeros pasos, en cualquier momento.', d2: 'Eso es todo. ¡Disfruta BEAN!' }
  ];

  var on = false, steps = [], idx = 0, prevFocus = null, openedDrawer = false;
  var ov, spot, bub, arr, live, bar, offer;

  function store(set) {
    try {
      if (set) { localStorage.setItem(SEEN_KEY, '1'); return true; }
      return localStorage.getItem(SEEN_KEY) === '1';
    } catch (e) { return !set; }
  }

  function targets(st) {
    return Array.prototype.slice.call(document.querySelectorAll('[data-tour="' + st.sel + '"]'))
      .filter(function (el) { return el.getClientRects().length > 0; });
  }

  function el(tag, cls, html) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html) n.innerHTML = html;
    return n;
  }

  function build() {
    if (ov) return;
    ov = el('div', 'tour-ov');
    ov.hidden = true;
    spot = el('div', 'tour-spot');
    bub = el('div', 'tour-bub');
    bub.setAttribute('role', 'dialog');
    bub.setAttribute('aria-modal', 'true');
    bub.setAttribute('aria-label', 'Recorrido guiado');
    bub.tabIndex = -1;
    arr = el('div', 'tour-arr');
    arr.setAttribute('aria-hidden', 'true');
    live = el('div');
    live.setAttribute('aria-live', 'polite');
    live.setAttribute('aria-atomic', 'true');
    bar = el('div', 'tour-bar');
    bar.setAttribute('aria-hidden', 'true');
    var btns = el('div', 'tour-btns',
      '<button type="button" class="tour-btn" data-t="skip">Saltar</button><span class="tour-sp"></span>' +
      '<button type="button" class="tour-btn tour-btn-secondary" data-t="prev">Anterior</button>' +
      '<button type="button" class="tour-btn tour-btn-primary" data-t="next">Siguiente</button>');
    bub.appendChild(arr); bub.appendChild(live); bub.appendChild(bar); bub.appendChild(btns);
    ov.appendChild(spot); ov.appendChild(bub);
    document.body.appendChild(ov);
  }

  function btn(k) { return bub.querySelector('[data-t=' + k + ']'); }

  // En móvil los pasos del menú necesitan el cajón abierto (estado Alpine de base.html).
  function drawer(open) {
    var root = document.querySelector('[x-data*="mobileMenuOpen"]');
    if (!root || !window.Alpine) return;
    var data = window.Alpine.$data(root);
    if (data.mobileMenuOpen === open) return;
    if (open) openedDrawer = true;
    data.mobileMenuOpen = open;
  }

  function show() {
    var st = steps[idx], n = steps.length - 1, mobile = window.innerWidth < 1024;
    if (mobile) drawer(st.side);
    var present = targets(st).length > 0;
    live.innerHTML = '<div class="tour-stp">' + (st.last ? 'Fin del recorrido' : 'Paso ' + (idx + 1) + ' de ' + n) +
      '</div><h4>' + st.t + '</h4><p>' + (st.last && !present ? st.d2 : st.d) + '</p>';
    bar.innerHTML = steps.map(function (_, i) { return '<i class="' + (i <= idx ? 'on' : '') + '"></i>'; }).join('');
    btn('prev').hidden = idx === 0;
    btn('skip').hidden = !!st.last;
    btn('next').textContent = st.last ? 'Terminar' : 'Siguiente';
    setTimeout(place, mobile ? 260 : 30); // 200 ms de transición del cajón
  }

  function place() {
    if (!on) return;
    var st = steps[idx], list = targets(st), W = window.innerWidth, H = window.innerHeight;
    ['left', 'right', 'top', 'bottom', 'width'].forEach(function (k) { bub.style[k] = ''; });
    ['left', 'right', 'top', 'bottom'].forEach(function (k) { arr.style[k] = ''; });
    var sheet = W <= 640;
    if (!list.length) { // sin elemento: burbuja centrada y sin foco de luz
      spot.style.display = 'none'; arr.style.display = 'none';
      bub.style.left = Math.max(8, (W - bub.offsetWidth) / 2) + 'px';
      bub.style.top = Math.max(8, (H - bub.offsetHeight) / 2) + 'px';
      return;
    }
    list[0].scrollIntoView({ block: 'nearest' });
    var rs = list.map(function (x) { return x.getBoundingClientRect(); });
    var l = Math.min.apply(null, rs.map(function (x) { return x.left; })),
        t = Math.min.apply(null, rs.map(function (x) { return x.top; })),
        r = Math.max.apply(null, rs.map(function (x) { return x.right; })),
        b = Math.max.apply(null, rs.map(function (x) { return x.bottom; }));
    var pad = 6, w = r - l, h = b - t, cx = l + w / 2, cy = t + h / 2;
    spot.style.display = '';
    spot.style.left = l - pad + 'px'; spot.style.top = t - pad + 'px';
    spot.style.width = w + pad * 2 + 'px'; spot.style.height = h + pad * 2 + 'px';
    arr.style.display = '';
    var bh = bub.offsetHeight, bw = 320;
    if (sheet) {
      arr.style.display = 'none';
      bub.style.left = bub.style.right = '8px'; bub.style.width = 'auto';
      if (cy > H / 2) bub.style.top = '8px'; else bub.style.bottom = '8px';
    } else if (r + pad + 16 + bw <= W) { // a la derecha
      var top = Math.max(8, Math.min(H - bh - 8, cy - bh / 2));
      bub.style.left = r + pad + 14 + 'px'; bub.style.top = top + 'px';
      arr.style.left = '-7px'; arr.style.top = Math.max(12, Math.min(bh - 26, cy - top - 7)) + 'px';
    } else { // debajo o encima
      var left = Math.max(8, Math.min(W - bw - 8, cx - bw / 2)), below = b + pad + 14 + bh <= H - 8;
      bub.style.left = left + 'px';
      bub.style.top = (below ? b + pad + 14 : Math.max(8, t - pad - 14 - bh)) + 'px';
      arr.style.left = Math.max(12, Math.min(bw - 26, cx - left - 7)) + 'px';
      arr.style[below ? 'top' : 'bottom'] = '-7px';
    }
  }

  function start() {
    if (on) return;
    hideOffer();
    build();
    steps = STEPS.filter(function (s) { return s.last || targets(s).length > 0; });
    if (steps.length < 2) return;
    store(true);
    on = true; idx = 0; openedDrawer = false;
    prevFocus = document.activeElement;
    ov.hidden = false;
    show();
    btn('next').focus({ preventScroll: true });
  }

  function end() {
    if (!on) return;
    on = false; ov.hidden = true;
    if (openedDrawer) drawer(false);
    if (prevFocus && prevFocus.isConnected && prevFocus !== document.body) prevFocus.focus({ preventScroll: true });
  }

  function go(d) {
    idx = Math.max(0, Math.min(steps.length - 1, idx + d));
    show();
    btn('next').focus({ preventScroll: true });
  }

  function hideOffer() { if (offer) { offer.remove(); offer = null; } }

  function showOffer() {
    if (store(false) || on || offer || !document.querySelector('[data-tour="nuevo"]')) return;
    store(true);
    offer = el('div', 'tour-offer',
      '<b>¿Quieres un recorrido rápido? (1 min)</b><p>Te mostramos dónde está cada cosa en BEAN.</p>' +
      '<div class="tour-btns"><button type="button" class="tour-btn tour-btn-primary" data-tour-start>Empezar</button>' +
      '<button type="button" class="tour-btn" data-t="later">Ahora no</button></div>');
    offer.setAttribute('role', 'region');
    offer.setAttribute('aria-label', 'Invitación al recorrido');
    document.body.appendChild(offer);
  }

  document.addEventListener('click', function (e) {
    if (e.target.closest('[data-tour-start]')) { e.preventDefault(); start(); return; }
    var k = e.target.closest('.tour-bub [data-t], .tour-offer [data-t]');
    if (!k) return;
    var a = k.dataset.t;
    if (a === 'later') hideOffer();
    else if (a === 'skip') end();
    else if (a === 'prev') go(-1);
    else if (a === 'next') { if (idx === steps.length - 1) end(); else go(1); }
  });

  document.addEventListener('keydown', function (e) {
    if (!on) return;
    if (e.key === 'Escape') { e.preventDefault(); end(); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); if (idx === steps.length - 1) end(); else go(1); }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); go(-1); }
    else if (e.key === 'Tab') {
      var f = Array.prototype.slice.call(bub.querySelectorAll('button')).filter(function (x) { return !x.hidden; });
      if (!f.length) return;
      var first = f[0], last = f[f.length - 1];
      if (!bub.contains(document.activeElement)) { e.preventDefault(); first.focus(); }
      else if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  }, true);

  window.addEventListener('resize', function () { if (on) show(); });

  // Alpine arranca con defer; la invitación espera a la primera pintura.
  window.addEventListener('load', function () { setTimeout(showOffer, 1200); });
})();
