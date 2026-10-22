/* Pendientes por contestar: título «(N) BEAN…» y un aviso de voz solo cuando el número sube. */
(function () {
    var last = null;
    function sync() {
        var box = document.getElementById('inbox-pending');
        var mark = box && box.querySelector('[data-count]');
        if (!mark) return;
        var n = parseInt(mark.getAttribute('data-count'), 10) || 0;
        document.title = (n ? '(' + n + ') ' : '') + document.title.replace(/^\(\d+\)\s*/, '');
        var live = document.getElementById('inbox-live');
        if (live && last !== null && n > last) {
            var up = n - last;
            live.textContent = up === 1 ? '1 mensaje nuevo por contestar' : up + ' mensajes nuevos por contestar';
        }
        last = n;
    }
    document.addEventListener('htmx:afterSettle', sync);
    document.addEventListener('DOMContentLoaded', sync);
})();
