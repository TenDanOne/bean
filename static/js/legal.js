// Página /legal/: idioma ES/EN (ambos en el HTML), aside con aria-current y <details> según viewport.
(function () {
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var aside = $("#lg-aside"), cur = $("#lg-cur");
  var desktop = window.matchMedia("(min-width: 961px)");
  var lang = "es";

  function sync() {
    aside.open = desktop.matches;
    $$(".toc").forEach(function (d) { d.open = desktop.matches; });
  }

  function setLang(l) {
    lang = l;
    $$("[data-l]").forEach(function (e) { e.hidden = e.getAttribute("data-l") !== l; });
    $$("[data-lang]").forEach(function (b) { b.setAttribute("aria-pressed", b.getAttribute("data-lang") === l); });
    $$("#lg-aside nav a").forEach(function (a) { a.setAttribute("href", a.getAttribute("data-" + l)); });
    document.documentElement.lang = l;
    mark();
  }

  function mark() {
    var arts = $$('.lg-doc > [data-l="' + lang + '"] > article'), active = arts[0];
    arts.forEach(function (a) { if (a.getBoundingClientRect().top <= 140) active = a; });
    $$("#lg-aside nav a").forEach(function (a) {
      if (a.getAttribute("data-" + lang) === "#" + active.id) {
        a.setAttribute("aria-current", "true");
        cur.textContent = $("span:not([hidden])", a).textContent;
      } else a.removeAttribute("aria-current");
    });
  }

  // Un ancla explícita gana: si apunta al bloque del otro idioma, se activa ese idioma.
  function fromHash() {
    var id = decodeURIComponent(location.hash.slice(1)), t = id && document.getElementById(id);
    var block = t && t.closest(".lg-doc > [data-l]");
    if (!block) return;
    if (block.getAttribute("data-l") !== lang) setLang(block.getAttribute("data-l"));
    t.scrollIntoView();
  }

  $$("[data-lang]").forEach(function (b) { b.addEventListener("click", function () { setLang(b.getAttribute("data-lang")); }); });
  window.addEventListener("hashchange", fromHash);
  window.addEventListener("scroll", mark, { passive: true });
  desktop.addEventListener("change", sync);
  sync();
  setLang("es");
  fromHash();
})();
