"""Genera static/marca/bean-by-ithos{,-oscuro}.svg a partir del molde simu-by-ithos.svg
(isotipo + bloque "by ITHOS ™" intactos) y los glifos BEAN de Figtree ExtraBold.
Uso: python scripts/marca/generar_logo.py
"""

import re
from pathlib import Path

from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

MOLDE = Path(r"C:\Users\ultra\Proyectos\simu\public\marca\nuevo\simu-by-ithos.svg")
FUENTE = Path(r"C:\Users\ultra\Proyectos\paax\data\fuentes\Figtree-ExtraBold.ttf")
OUT = Path(__file__).resolve().parents[2] / "static" / "marca"
CAP_MOLDE = 101  # alto de mayuscula de SIMU en unidades del molde (461..562)
X_TEXTO, ESCALA, Y_TOP = 134.78, 0.565, 5  # mismo bloque que SIMU
VB_H = 114.3

svg = MOLDE.read_text(encoding="utf-8")
font = TTFont(FUENTE)
gs, cmap, hmtx = font.getGlyphSet(), font.getBestCmap(), font["hmtx"]
k = CAP_MOLDE / font["OS/2"].sCapHeight

x, paths, xmax = 0.0, [], 0.0
lsb0 = hmtx[cmap[ord("B")]][1]
for ch in "BEAN":
    g = cmap[ord(ch)]
    pen = SVGPathPen(gs, ntos=lambda v: f"{v:.2f}".rstrip("0").rstrip("."))
    # y de fuente -> y del molde (base de mayuscula en 562)
    gs[g].draw(TransformPen(pen, (k, 0, 0, -k, x - lsb0 * k, 562)))
    paths.append(f'<path d="{pen.getCommands()}"/>')
    bp = BoundsPen(gs)
    gs[g].draw(TransformPen(bp, (k, 0, 0, -k, x - lsb0 * k, 562)))
    xmax = bp.bounds[2]
    x += hmtx[g][0] * k
texto = (
    f'<g transform="translate({X_TEXTO} {Y_TOP}) scale({ESCALA}) translate(0 -461)" fill="#0f2a30">\n'
    + "\n".join(paths)
    + "\n</g>"
)

# reemplaza el primer grupo de tinta (glifos SIMU)
svg, n = re.subn(
    r'<g transform="translate\(134\.78 5\) scale\(0\.565\) translate\(0 -461\)" fill="#0f2a30">.*?</g>',
    lambda _m: texto,
    svg,
    count=1,
    flags=re.S,
)
assert n == 1, "no se encontro el bloque SIMU"
# el bloque "by ITHOS ™" termina en ~309.2; el viewBox cubre lo mayor entre eso y BEAN
ancho = max(309.2, X_TEXTO + xmax * ESCALA) + 1.0
svg = svg.replace('viewBox="0 0 310.2 114.3"', f'viewBox="0 0 {ancho:.1f} {VB_H}"').replace(
    "SIMU by ITHOS", "BEAN by ITHOS"
)
assert "SIMU" not in svg
OUT.mkdir(parents=True, exist_ok=True)
(OUT / "bean-by-ithos.svg").write_text(svg, encoding="utf-8")
(OUT / "bean-by-ithos-oscuro.svg").write_text(svg.replace("#0f2a30", "#f4f1ea"), encoding="utf-8")
print("ok", f"viewBox ancho {ancho:.1f}", "BEAN termina en", round(X_TEXTO + xmax * ESCALA, 1))
