/* Diálogo «Importar publicación (.zip)»: sube el paquete por POST (con progreso) y sigue la respuesta JSON de
   composer:import_package: error | elegir (varias piezas) | existe (reimportación) | redirect al composer. */
(function () {
  'use strict';
  var dlg = document.getElementById('imp-dialog');
  if (!dlg) return;
  var $ = function (id) { return document.getElementById(id); };
  var file = null, pieza = '';
  var show = function (id, on) { $(id).hidden = !on; };

  function reset() {
    file = null; pieza = '';
    ['imp-progress', 'imp-pick-list', 'imp-exists', 'imp-error'].forEach(function (i) { show(i, false); });
    show('imp-drop', true);
    $('imp-status').textContent = '';
    $('imp-file').value = '';
  }
  function fail(t, m) {
    show('imp-progress', false); show('imp-pick-list', false); show('imp-exists', false); show('imp-drop', false);
    $('imp-err-t').textContent = t; $('imp-err-m').textContent = m;
    show('imp-error', true); $('imp-again').focus();
  }
  function csrf() {
    var m = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : '';
  }
  function send(extra) {
    extra = extra || {};
    var fd = new FormData();
    fd.append('file', file);
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
    if (e.target.closest('[data-import-open]')) { e.preventDefault(); reset(); dlg.showModal(); $('imp-pick').focus(); }
    if (e.target.closest('[data-import-close]')) dlg.close();
    var m = e.target.closest('[data-modo]');
    if (m) send({ pieza: pieza, modo: m.dataset.modo });
  });
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
