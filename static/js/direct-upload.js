/**
 * Direct browser -> R2/S3 upload using a presigned POST (see media_library.upload_presign).
 *
 * beanDirectUpload(file, {presignUrl, csrf, onProgress}) resolves with the upload_id to hand
 * to the server's finalize step. It rejects with an Error whose `.fallback` is true when direct
 * upload is not available (local storage, or the bucket CORS rule is missing), so the caller
 * can send the file through the server as before.
 */
(function () {
  'use strict';

  window.beanDirectUpload = function (file, opts) {
    return new Promise(function (resolve, reject) {
      function fail(message, fallback) {
        var err = new Error(message);
        err.fallback = !!fallback;
        reject(err);
      }

      fetch(opts.presignUrl, {
        method: 'POST',
        headers: { 'X-CSRFToken': opts.csrf, 'Content-Type': 'application/json' },
        body: JSON.stringify({ filename: file.name, content_type: file.type, size: file.size }),
      })
        .then(function (r) {
          return r.json().then(
            function (d) { return { r: r, d: d }; },
            function () { return { r: r, d: {} }; }
          );
        })
        .then(function (res) {
          var d = res.d;
          if (res.r.status === 409) return fail(d.error || 'Carga directa no disponible', true);
          if (!res.r.ok) return fail(d.error || 'No se pudo iniciar la carga.', false);

          var form = new FormData();
          Object.keys(d.fields).forEach(function (k) { form.append(k, d.fields[k]); });
          form.append('file', file); // the file field must come last
          var xhr = new XMLHttpRequest();
          xhr.upload.onprogress = function (e) {
            if (e.lengthComputable && opts.onProgress) opts.onProgress(e.loaded / e.total);
          };
          xhr.onload = function () {
            if (xhr.status >= 200 && xhr.status < 300) resolve(d.upload_id);
            else fail('El almacenamiento rechazó el archivo.', false);
          };
          xhr.onerror = function () { fail('No se pudo conectar con el almacenamiento.', true); };
          xhr.open('POST', d.url);
          xhr.send(form);
        })
        .catch(function () { fail('No se pudo iniciar la carga.', true); });
    });
  };
})();
