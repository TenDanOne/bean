"""«¿Cómo lo mandas?»: modo de entrega por red, guardado en los campos que ya existen de ``platform_extra``.

* TikTok -> ``tiktok_mode`` = draft | direct (bandeja de la app).
* YouTube / Facebook borrador -> ``entrega`` = "borrador" (YouTube sube «privado»; Facebook sin publicar).
* «A mano» -> ``manual_publish`` (BEAN no publica esa red).
"""

DIRECTO, BORRADOR, A_MANO = "directo", "borrador", "a_mano"
MODES = (DIRECTO, BORRADOR, A_MANO)
_MANUAL_ONLY = ("instagram", "instagram_login")  # Instagram no tiene borrador por API


def mode_of(platform: str, extra: dict | None) -> str:
    extra = extra or {}
    if platform == "tiktok" and extra.get("tiktok_mode") == "draft":
        return BORRADOR
    if extra.get("manual_publish"):
        return A_MANO
    return BORRADOR if extra.get("entrega") == "borrador" else DIRECTO


def effective_mode(platform: str, mode: str, *, video: bool = True) -> str:
    """Modo que realmente admite la red: IG y FB sin video caen en «A mano»; TikTok no tiene «A mano»."""
    if platform == "tiktok":
        return BORRADOR if mode in (BORRADOR, A_MANO) else DIRECTO
    if mode == BORRADOR and (platform in _MANUAL_ONLY or (platform == "facebook" and not video)):
        return A_MANO
    return mode


def set_mode(platform: str, extra: dict, mode: str, *, video: bool = True) -> str:
    """Escribe el modo en ``extra`` (se muta) y devuelve el modo efectivo."""
    mode = effective_mode(platform, mode if mode in MODES else DIRECTO, video=video)
    extra.pop("entrega", None)
    extra.pop("manual_publish", None)
    if platform == "tiktok":
        extra["tiktok_mode"] = "draft" if mode == BORRADOR else "direct"
    elif mode == A_MANO:
        extra["manual_publish"] = True
    elif mode == BORRADOR:
        extra["entrega"] = "borrador"
    return mode
