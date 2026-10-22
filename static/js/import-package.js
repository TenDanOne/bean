/* Diálogo «Importar publicación (.zip)»: sube el paquete por POST (con progreso) y sigue la respuesta JSON de
   composer:import_package: error | elegir (varias piezas) | existe (reimportación) | revision (hoja de revisión
   rápida: programar / publicar ahora con composer:import_review) | redirect al composer. */
(function () {
  'use strict';
  var dlg = document.getElementById('imp-dialog');
  if (!dlg) return;
  var $ = function (id) { return document.getElementById(id); };
  var file = null, pieza = '', rev = null, kidsLi = null, hasManual = false, timer = 0, urls = {};
  // mismo ritmo que static/js/publish-modal.js
  var POLL_MS = 2000, MAX_POLLS = 90;
  var show = function (id, on) { $(id).hidden = !on; };
  var ZERO = '00000000-0000-0000-0000-000000000000';
  var MODE_CLS = { 'Directo': 'st-approved', 'Borrador': 'st-draft', 'A mano': 'st-paused' };
  var STATE = {
    wait: ['st-draft', 'Esperando'], run: ['st-scheduled', 'Publicando'], proc: ['st-review', 'Procesando'],
    ok: ['st-published', 'Listo'], draft: ['st-draft', 'Borrador'], private: ['st-draft', 'Privado'],
    manual: ['st-paused', 'A mano'], fail: ['st-failed', 'Falló']
  };
  var NOTE = {
    draft: 'Termínalo en la app de la red.', private: 'Abre YouTube Studio, ponle música y cámbialo a Público.',
    manual: 'Publícalo tú desde la app.'
  };

  function reset() {
    clearTimeout(timer); file = null; pieza = ''; rev = null;
    ['imp-progress', 'imp-pick-list', 'imp-exists', 'imp-error', 'imp-review', 'imp-result'].forEach(function (i) { show(i, false); });
    show('imp-drop', true);
    dlg.classList.remove('wide');
    $('imp-status').textContent = '';
    $('imp-file').value = '';
  }
  function fail(t, m) {
    ['imp-progress', 'imp-pick-list', 'imp-exists', 'imp-drop', 'imp-review', 'imp-result'].forEach(function (i) { show(i, false); });
    $('imp-err-t').textContent = t; $('imp-err-m').textContent = m;
    show('imp-error', true); $('imp-again').focus();
  }
  function csrf() {
    var m = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : '';
  }
  function el(tag, cls, txt) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (txt != null) n.textContent = txt;
    return n;
  }
  function pill(cls, txt) { return el('span', 'st-pill ' + cls, txt); }

  // ---- hoja de revisión rápida
  var ninosOk = function () { return !rev.ninos || !!dlg.querySelector('input[name=imp-ninos]:checked'); };
  // «Hecho para niños» sin responder bloquea Programar y Publicar ahora, con la pista visible
  function sync() {
    var ok = ninosOk(), h = $('imp-rhint');
    if (kidsLi) kidsLi.className = ok ? 'ok' : 'block';
    dlg.querySelectorAll('[data-apply]').forEach(function (b) { b.disabled = !ok; });
    h.classList.toggle('blk', !ok);
    h.textContent = !ok ? 'Responde «Hecho para niños» para continuar.' : hasManual ? 'Los A mano quedan como tarea tuya en Publicar.' : '';
  }
  function covText() {
    var o = dlg.querySelector('input[name=imp-portada]:checked');
    $('imp-cov').textContent = o ? 'Portada: ' + rev.portadas.tipo + ' ' + o.dataset.l + '.' : 'Portada: ' + rev.portada + '.';
  }
  function review(r) {
    rev = r; kidsLi = null;
    var ws = dlg.dataset.workspace;
    // el paquete puede mandar la publicación a otro espacio: aplicar y consultar el estado allá
    var to = function (u) { return r.espacio && r.espacio !== ws ? u.split(ws).join(r.espacio) : u; };
    urls = { apply: to(dlg.dataset.applyUrl), status: to(dlg.dataset.statusUrl), sent: to(dlg.dataset.sentUrl) };
    dlg.classList.add('wide');
    $('imp-fname').textContent = file ? file.name : '';
    $('imp-fsize').textContent = file ? (file.size / 1048576).toFixed(1).replace('.', ',') + ' MB · paquete válido' : '';
    $('imp-pz').textContent = 'Pieza ' + r.pieza[0] + ' de ' + r.pieza[1];
    $('imp-th').style.backgroundImage = r.miniatura ? 'url("' + encodeURI(r.miniatura) + '")' : '';
    $('imp-ty').textContent = r.tipo;
    $('imp-rt').textContent = r.titulo;
    $('imp-rx').textContent = r.texto;
    $('imp-nc').textContent = r.cuentas.length;
    hasManual = r.cuentas.some(function (c) { return c.modo === 'A mano'; });
    var ul = $('imp-rows'); ul.textContent = '';
    r.cuentas.forEach(function (c) {
      var li = el('li'), who = el('span');
      who.append(el('b', null, c.nombre), el('small', null, c.red + ' · ' + c.tipo));
      li.append(who, pill(MODE_CLS[c.modo] || 'st-draft', c.modo));
      if (c.modo === 'A mano' && c.cancion) {
        li.append(el('div', 'imp-more', 'Canción sugerida: ' + c.cancion +
          (c.golpe != null ? ' · entra el ritmo en ' + String(c.golpe).replace('.', ',') + ' s.' : '')));
      }
      ul.append(li);
    });
    var av = $('imp-avisos'); av.textContent = '';
    if (r.ninos) {
      var k = el('li', 'block'), fs = el('fieldset'), opts = el('div', 'cp-opts');
      kidsLi = k;
      fs.append(el('legend', null, 'Hecho para niños (YouTube · ' + r.ninos.cuenta + '). ¿Es contenido hecho para niños?'));
      [['si', 'Sí'], ['no', 'No']].forEach(function (o) {
        var l = el('label', 'ib-chip cp-opt'), i = el('input');
        i.type = 'radio'; i.name = 'imp-ninos'; i.value = o[0]; i.checked = o[0] === r.ninos.valor;
        i.addEventListener('change', sync);
        l.append(i, ' ' + o[1]); opts.append(l);
      });
      fs.append(opts); k.append(fs); av.append(k);
    }
    r.avisos.forEach(function (a) {
      av.append(el('li', a.nivel === 'block' ? 'block' : a.nivel === 'warn' ? 'warn' : '', (a.red ? a.red + ': ' : '') + a.mensaje));
    });
    var cv = $('imp-covopts'); cv.textContent = '';
    cv.hidden = $('imp-cov-h').hidden = !r.portadas;
    if (r.portadas) {
      r.portadas.opciones.forEach(function (o, n) {
        var l = el('label', 'ib-chip cp-opt'), i = el('input');
        i.type = 'radio'; i.name = 'imp-portada'; i.value = o.v; i.dataset.l = o.l; i.defaultChecked = i.checked = n === r.portadas.actual;
        i.addEventListener('change', covText);
        l.append(i, el('span', 'sr-only', 'Portada: ' + r.portadas.tipo + ' '), o.l); cv.append(l);
      });
    }
    covText();
    $('imp-fecha').value = r.fecha; $('imp-hora').value = r.hora; $('imp-tz').textContent = r.zona;
    $('imp-editor').href = r.editor;
    sync();
    show('imp-review', true);
    $('imp-rt').focus();
  }
  function rhint(t) { $('imp-rhint').textContent = t; $('imp-rhint').classList.add('blk'); }
  function apply(accion) {
    var btns = dlg.querySelectorAll('[data-apply]');
    var k = dlg.querySelector('input[name=imp-ninos]:checked');
    if (!ninosOk()) return sync();
    if (accion === 'programar' && (!$('imp-fecha').value || !$('imp-hora').value)) return rhint('Elige la fecha y la hora.');
    btns.forEach(function (b) { b.disabled = true; });
    $('imp-rhint').classList.remove('blk');
    $('imp-rhint').textContent = accion === 'publicar' ? 'Publicando…' : 'Programando…';
    var fd = new FormData();
    fd.append('post_id', rev.post_id); fd.append('accion', accion);
    fd.append('fecha', $('imp-fecha').value); fd.append('hora', $('imp-hora').value);
    if (k) fd.append('ninos', k.value);
    var pc = dlg.querySelector('input[name=imp-portada]:checked');
    if (pc && !pc.defaultChecked) fd.append('portada', pc.value); // sin cambio, el servidor conserva la del paquete
    fetch(urls.apply, { method: 'POST', body: fd, credentials: 'same-origin', headers: { 'X-CSRFToken': csrf() } })
      .then(function (res) {
        return res.json().catch(function () { return {}; }).then(function (j) { return { ok: res.ok, j: j, status: res.status }; });
      })
      .then(function (o) {
        if (!o.ok) {
          sync();
          var e = o.j.errors ? Object.keys(o.j.errors).map(function (n) {
            var v = o.j.errors[n]; return Array.isArray(v) ? v.join(' ') : v;
          }).join(' ') : '';
          return rhint(e || (o.status === 403 ? 'No tienes permiso para esto.' : 'No se pudo completar (' + o.status + ').'));
        }
        done(accion);
      })
      .catch(function () { sync(); rhint('Revisa tu conexión y vuelve a intentarlo.'); });
  }
  function done(accion) {
    show('imp-review', false); show('imp-result', true);
    var ul = $('imp-res-rows'), t = $('imp-res-t'), q = '«' + rev.titulo + '»';
    ul.textContent = '';
    $('imp-res-go').href = urls.sent;
    if (accion === 'programar') {
      var p = $('imp-fecha').value.split('-').map(Number);
      t.textContent = q + ' quedó programada para ' +
        new Date(p[0], p[1] - 1, p[2]).toLocaleDateString('es-MX', { weekday: 'short', day: 'numeric', month: 'short' }) +
        ', ' + $('imp-hora').value + ' (' + rev.zona + ').';
      ul.append(el('li', null, 'Sale cuando pulses «Procesar pendientes» pasada esa hora.'));
      t.focus();
      return;
    }
    t.textContent = 'Publicando ' + q + '…';
    t.focus();
    var polls = 0;
    // repite la consulta (como publish-modal.js) hasta que ninguna fila siga esperando, publicando o procesando
    (function poll() {
      polls += 1;
      fetch(urls.status.replace(ZERO, rev.post_id), { credentials: 'same-origin', headers: { Accept: 'application/json' } })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          ul.textContent = '';
          d.rows.forEach(function (x) {
            var s = STATE[x.state] || STATE.wait, li = el('li'), who = el('span');
            who.append(el('b', null, x.account), el('small', null, x.platform + (x.kind ? ' · ' + x.kind : '')));
            li.append(who, pill(s[0], s[1]));
            var n = x.state === 'fail' ? (x.error || 'No se pudo publicar.') : NOTE[x.state];
            if (n) li.append(el('div', 'imp-more', n));
            ul.append(li);
          });
          var open = !d.rows.length || d.rows.some(function (x) { return x.state === 'wait' || x.state === 'run' || x.state === 'proc'; });
          if (open && polls < MAX_POLLS) { timer = setTimeout(poll, POLL_MS); return; }
          var bad = d.rows.filter(function (x) { return x.state === 'fail'; }).length;
          t.textContent = open ? q + ' sigue en curso. Revisa el resultado en Publicar → Enviadas.'
            : bad ? q + ': ' + bad + ' de ' + d.rows.length + ' falló' : q + ': listo';
        })
        .catch(function () { t.textContent = 'Enviada. Revisa el resultado en Publicar → Enviadas.'; });
    })();
  }

  function send(extra) {
    extra = extra || {};
    var fd = new FormData();
    fd.append('file', file);
    fd.append('revisar', '1');
    Object.keys(extra).forEach(function (k) { fd.append(k, extra[k]); });
    show('imp-drop', false); show('imp-error', false); show('imp-pick-list', false); show('imp-exists', false);
    show('imp-progress', true);
    $('imp-name').textContent = file.name;
    var bar = dlg.querySelector('.imp-bar');
    var setPct = function (n) {
      $('imp-pct').textContent = n + '%'; $('imp-fill').style.width = n + '%'; bar.setAttribute('aria-valuenow', n);
    };
    setPct(0);
    $('imp-status').textContent = 'Subiendo el paquete…';
    var x = new XMLHttpRequest();
    x.open('POST', dlg.dataset.url);
    x.setRequestHeader('X-CSRFToken', csrf());
    x.upload.onprogress = function (e) {
      if (e.lengthComputable) {
        setPct(Math.round(100 * e.loaded / e.total));
        if (e.loaded === e.total) $('imp-status').textContent = 'Revisando el paquete y creando el borrador…';
      }
    };
    x.onerror = function () { fail('No se pudo subir el paquete.', 'Revisa tu conexión y vuelve a intentarlo.'); };
    x.onload = function () {
      var r = null;
      try { r = JSON.parse(x.responseText); } catch (e) { /* respuesta no JSON */ }
      if (!r) return fail('No se pudo importar.', 'El servidor respondió con un error (' + x.status + '). Vuelve a intentarlo.');
      if (r.error) return fail(r.error.titulo, r.error.mensaje);
      if (r.redirect) { $('imp-status').textContent = 'Listo. Abriendo el borrador…'; location.href = r.redirect; return; }
      show('imp-progress', false); $('imp-status').textContent = '';
      if (r.revision) return review(r.revision);
      if (r.elegir) {
        var ul = dlg.querySelector('.imp-list'); ul.textContent = '';
        r.elegir.forEach(function (p) {
          var li = document.createElement('li'), b = document.createElement('button');
          b.type = 'button'; b.className = 'imp-item';
          var s = document.createElement('b'); s.textContent = p.id;
          var d = document.createElement('small'); d.textContent = p.tipo.replace('_', ' ') + ' · ' + p.redes.join(', ');
          b.append(s, d);
          b.addEventListener('click', function () { pieza = p.id; send({ pieza: p.id }); });
          li.append(b); ul.append(li);
        });
        show('imp-pick-list', true); ul.querySelector('button').focus();
      } else if (r.existe) {
        $('imp-exists-t').textContent = 'Ya hay un borrador de «' + r.existe.id + '» (creado el ' + r.existe.fecha + '). ¿Qué hacemos?';
        show('imp-exists', true); dlg.querySelector('[data-modo]').focus();
      }
    };
    x.send(fd);
  }
  function take(f) {
    if (!f) return;
    if (!/\.zip$/i.test(f.name)) return fail('Ese archivo no es un .zip.', 'Elige el paquete exportado (.zip).');
    file = f; send();
  }

  document.addEventListener('click', function (e) {
    var ap = e.target.closest('[data-apply]');
    if (ap) return apply(ap.dataset.apply);
    if (e.target.closest('[data-import-open]')) { e.preventDefault(); reset(); dlg.showModal(); $('imp-pick').focus(); }
    if (e.target.closest('[data-import-close]')) dlg.close();
    var m = e.target.closest('[data-modo]');
    if (m) send({ pieza: pieza, modo: m.dataset.modo });
  });
  dlg.addEventListener('close', function () { clearTimeout(timer); });
  $('imp-pick').addEventListener('click', function () { $('imp-file').click(); });
  $('imp-file').addEventListener('change', function () { take(this.files[0]); });
  $('imp-again').addEventListener('click', function () { reset(); $('imp-pick').focus(); });
  var drop = $('imp-drop');
  ['dragenter', 'dragover'].forEach(function (ev) {
    drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.add('over'); });
  });
  ['dragleave', 'drop'].forEach(function (ev) {
    drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.remove('over'); });
  });
  drop.addEventListener('drop', function (e) { take(e.dataTransfer.files[0]); });
})();
