"""Revisor de calidad del composer: funciones puras que devuelven avisos en español.

Cada aviso es ``{code, level: "block"|"warn", message_es, platform}``. ``block`` impide programar;
``warn`` solo se muestra. La vista previa lo integra (``preview``) y ``_scheduling_guard`` reutiliza ``has_blocks``.
"""

import re

from apps.composer.services import media_error_for_platform

BANNED_PHRASES_KEY = "composer.banned_phrases"
THUMBNAIL_MAX_BYTES = 2 * 1024 * 1024
MAX_UPPERCASE_WORDS = 2
IG_MAX_HASHTAGS = 5
_PLACEHOLDER_RE = re.compile(r"\{\{[^{}]*\}\}")
_LINK_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_HASHTAG_RE = re.compile(r"(?<!\w)#\w+")
_WORD_RE = re.compile(r"[^\W\d_]{3,}")

# Caracteres visibles antes de que la plataforma corte el texto con «ver más».
_VISIBLE_LIMITS = {
    ("youtube", "short"): (40, "título"),
    ("youtube", ""): (70, "título"),
    "instagram": (125, "texto"),
    "instagram_login": (125, "texto"),
    "facebook": (125, "texto"),
}
# Proporción ancho/alto esperada por tipo (con 5 % de tolerancia).
_ASPECTS = {"short": 9 / 16, "reel": 9 / 16, "video": 16 / 9}


def _warn(code, message_es, platform, level="warn"):
    return {"code": code, "level": level, "message_es": message_es, "platform": platform}


def visible_counter(platform: str, post_type: str, title: str, caption: str) -> dict | None:
    """Contador de caracteres visibles: ``{limit, used, over, what}`` o ``None`` si la plataforma no lo usa."""
    spec = _VISIBLE_LIMITS.get((platform, post_type or "")) or _VISIBLE_LIMITS.get((platform, ""))
    spec = spec or _VISIBLE_LIMITS.get(platform)
    if not spec:
        return None
    limit, what = spec
    used = len(title if what == "título" else caption)
    return {"limit": limit, "used": used, "over": used > limit, "what": what}


def has_blocks(warnings) -> bool:
    return any(w["level"] == "block" for w in warnings)


def can_promote(extra: dict | None) -> bool:
    """«Solo orgánico» impide cualquier acción de promocionar/impulsar."""
    return not (extra or {}).get("solo_organico")


def quality_warnings(
    *,
    platform: str,
    post_type: str = "",
    title: str = "",
    caption: str = "",
    first_comment: str = "",
    cover_text: str = "",
    duration: float | None = None,
    width: int | None = None,
    height: int | None = None,
    thumbnail_bytes: int | None = None,
    banned_phrases=(),
    promoting: bool = False,
    extra: dict | None = None,
) -> list[dict]:
    out: list[dict] = []
    text = f"{title}\n{caption}\n{first_comment}"

    if _PLACEHOLDER_RE.search(text):
        out.append(
            _warn(
                "placeholders", "Hay marcadores {{…}} sin rellenar. Complétalos antes de programar.", platform, "block"
            )
        )

    title_words = {w.lower() for w in _WORD_RE.findall(title)}
    repeated = sorted(title_words & {w.lower() for w in _WORD_RE.findall(cover_text)})
    if repeated:
        out.append(
            _warn("cover_repeats_title", f"La portada repite palabras del título: {', '.join(repeated)}.", platform)
        )

    plain = _HASHTAG_RE.sub("", _LINK_RE.sub("", f"{title} {caption}"))
    caps = [w for w in _WORD_RE.findall(plain) if w.isupper()]
    if len(caps) > MAX_UPPERCASE_WORDS:
        out.append(
            _warn(
                "too_many_caps", f"Hay {len(caps)} palabras en MAYÚSCULAS; usa máximo {MAX_UPPERCASE_WORDS}.", platform
            )
        )

    if platform in ("instagram", "instagram_login"):
        n = len(_HASHTAG_RE.findall(caption))
        if n > IG_MAX_HASHTAGS:
            out.append(
                _warn(
                    "ig_hashtags", f"Instagram rinde mejor con {IG_MAX_HASHTAGS} hashtags o menos (hay {n}).", platform
                )
            )

    if platform == "facebook" and _LINK_RE.search(caption) and not _LINK_RE.search(first_comment):
        out.append(
            _warn(
                "fb_link_in_body",
                "El enlace en el cuerpo resta alcance en Facebook: ponlo en el primer comentario.",
                platform,
            )
        )

    if duration:
        err = media_error_for_platform(platform, ["video"], post_type, [duration])
        if err:
            out.append(_warn("duration", err, platform, "block"))
    ratio = _ASPECTS.get(post_type or "")
    if ratio and width and height and abs(width / height - ratio) / ratio > 0.05:
        want = "9:16 (vertical)" if ratio < 1 else "16:9 (horizontal)"
        out.append(_warn("aspect", f"La proporción del video no es {want}.", platform))

    if thumbnail_bytes and thumbnail_bytes > THUMBNAIL_MAX_BYTES:
        out.append(_warn("thumbnail_size", "La miniatura pesa más de 2 MB; YouTube la rechazará.", platform, "block"))

    lowered = text.lower()
    hits = [p for p in banned_phrases if str(p).strip() and str(p).strip().lower() in lowered]
    if hits:
        out.append(_warn("banned_phrase", f"Frases prohibidas por la marca: {', '.join(hits)}.", platform))

    if promoting and not can_promote(extra):
        out.append(
            _warn(
                "solo_organico",
                "Esta publicación está marcada «Solo orgánico»: no se puede promocionar.",
                platform,
                "block",
            )
        )

    return out
