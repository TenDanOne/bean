// Páginas legales: el selector del menú (móvil) navega al documento elegido.
(function () {
  var sel = document.getElementById("lg-select");
  if (sel) sel.addEventListener("change", function () { location.href = sel.value; });
})();
