"""Regenera los PNG/ICO de static/favicon/ desde favicon.svg usando Edge headless + Pillow.

Uso: python scripts/marca/generar_favicon.py
"""

import subprocess
import tempfile
from pathlib import Path

from PIL import Image

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
DEST = Path(__file__).resolve().parents[2] / "static" / "favicon"
SVG = DEST / "favicon.svg"
# Maskable: los 192/512 llevan el icono completo (el SVG ya tiene su margen de seguridad).
PNGS = {
    "favicon-96x96.png": 96,
    "apple-touch-icon.png": 180,
    "web-app-manifest-192x192.png": 192,
    "web-app-manifest-512x512.png": 512,
}


def render(size: int, out: Path) -> None:
    html = Path(tempfile.mkdtemp()) / "i.html"
    html.write_text(
        f'<style>html,body{{margin:0;background:transparent}}</style>'
        f'<img src="{SVG.as_uri()}" width="{size}" height="{size}" style="display:block">'
    )
    subprocess.run(
        [EDGE, "--headless", "--disable-gpu", "--hide-scrollbars", "--default-background-color=00000000",
         f"--window-size={size},{size}", f"--screenshot={out}", html.as_uri()],
        check=True, capture_output=True,
    )


for name, size in PNGS.items():
    render(size, DEST / name)
    assert Image.open(DEST / name).size == (size, size), name
Image.open(DEST / "web-app-manifest-512x512.png").save(
    DEST / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64)]
)
print("favicons regenerados en", DEST)
