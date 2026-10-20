"""Contenido de la página Ayuda y de los botones «?» del composer (una sola fuente).

Cada entrada: ``key`` (ancla ``#g-<key>``), ``title``, ``text`` en español llano y, opcional, ``pill``
(clase ``st-*`` de la pastilla de estado que se muestra antes del texto). Sin jerga: si un término técnico
es inevitable (stitch, LUFS) tiene su propia entrada que lo explica.
"""

GLOSSARY = [
    (
        "Botones y secciones",
        [
            {
                "key": "nuevo",
                "title": "Nuevo",
                "text": "Único punto de entrada para crear. Elige Publicación (abre el composer) o Idea (guarda algo para después).",
            },
            {
                "key": "publicar",
                "title": "Publicar",
                "text": "Tu calendario: todo lo programado por día. El «+» de un día abre el composer con esa fecha ya puesta.",
            },
            {
                "key": "cola",
                "title": "Cola",
                "text": "Posts sin hora fija que salen en el siguiente espacio libre, en orden. Útil para tener siempre contenido en marcha.",
            },
            {
                "key": "borradores",
                "title": "Borradores",
                "text": "Guardados sin enviar. No salen hasta que los publiques o los programes.",
            },
            {
                "key": "aprobaciones",
                "title": "Aprobaciones",
                "text": "Posts que esperan el visto bueno de alguien con permiso antes de salir.",
            },
            {
                "key": "enviadas",
                "title": "Enviadas",
                "text": "Lo que ya se publicó y lo que falló, con el motivo y el botón Reintentar.",
            },
            {
                "key": "bandeja",
                "title": "Bandeja social",
                "text": "Comentarios y chats de tus cuentas, para responder, ocultar o borrar.",
            },
            {
                "key": "canales",
                "title": "Canales",
                "text": "Tus cuentas conectadas (YouTube, Instagram, Facebook, TikTok). Desde aquí se conectan y reconectan.",
            },
            {
                "key": "procesar",
                "title": "Procesar pendientes",
                "text": "BEAN publica solo cada 5 minutos; con «Procesar pendientes» lo fuerzas y sale ahora todo lo programado cuya hora ya pasó.",
            },
        ],
    ),
    (
        "Pasos del composer",
        [
            {
                "key": "formato",
                "title": "Formato",
                "text": "Lo que quieres hacer (Reel, video largo, feed…). BEAN elige el tipo correcto en cada plataforma y te ofrece la plantilla.",
            },
            {
                "key": "ninos",
                "title": "Hecho para niños",
                "text": "YouTube te obliga a declararlo. Si es «Sí», se desactivan comentarios y anuncios personalizados. Elige con cuidado.",
            },
            {
                "key": "organico",
                "title": "Solo orgánico",
                "text": "Márcalo si usas música o clips de terceros: así no se puede promocionar con anuncios.",
            },
            {
                "key": "primercom",
                "title": "Primer comentario",
                "text": "Un comentario que BEAN publica justo después de la publicación. Úsalo para el enlace, una pregunta o un recurso.",
            },
            {
                "key": "portada",
                "title": "Portada y miniatura",
                "text": "La imagen que se ve antes de reproducir. En YouTube se llama miniatura (hasta 2 MB, 1280×720); en Reels y TikTok es un fotograma del video.",
            },
            {
                "key": "capitulos",
                "title": "Capítulos",
                "text": "Lista de «mm:ss Título» que YouTube convierte en secciones del video. Deben empezar en 00:00, ser al menos 3 y durar 10 s o más cada uno.",
            },
            {
                "key": "zona",
                "title": "Zona segura",
                "text": "Parte del video 9:16 que la app tapa con botones y texto (abajo y a la derecha). Evita poner ahí texto o rostros.",
            },
            {
                "key": "tz",
                "title": "Zona horaria",
                "text": "La hora que eliges se interpreta en la zona del espacio de trabajo, mostrada junto a la fecha.",
            },
            {
                "key": "revisor",
                "title": "Revisión",
                "text": "Panel que revisa tu publicación mientras escribes. «Bloquea» impide publicar o programar hasta que lo arregles; «Aviso» solo te recomienda.",
            },
            {
                "key": "stitch",
                "title": "Stitch (TikTok)",
                "text": "Permite que otra persona use un trozo de tu video para empezar el suyo. Si no quieres que te recorten, déjalo apagado.",
            },
            {
                "key": "lufs",
                "title": "Volumen del audio (LUFS)",
                "text": "LUFS es la medida del volumen. YouTube, Instagram y TikTok normalizan a unos −14: si tu video suena más fuerte, la plataforma lo baja.",
            },
        ],
    ),
    (
        "Estados",
        [
            {"key": "e-borrador", "title": "Borrador", "text": "Guardado sin fecha ni envío.", "pill": "st-draft"},
            {"key": "e-revision", "title": "En revisión", "text": "Espera aprobación.", "pill": "st-review"},
            {"key": "e-aprobado", "title": "Aprobado", "text": "Listo para salir.", "pill": "st-approved"},
            {
                "key": "e-programado",
                "title": "Programado",
                "text": "Saldrá a su hora cuando BEAN procese los pendientes.",
                "pill": "st-scheduled",
            },
            {"key": "e-publicado", "title": "Publicado", "text": "Ya está en la plataforma.", "pill": "st-published"},
            {
                "key": "e-fallido",
                "title": "Fallido",
                "text": "La plataforma lo rechazó; abre el post para ver el motivo.",
                "pill": "st-failed",
            },
            {
                "key": "e-pausa",
                "title": "En pausa",
                "text": "Detenido a mano; no sale hasta reanudarlo.",
                "pill": "st-paused",
            },
        ],
    ),
    (
        "Tipos de publicación",
        [
            {"key": "t-video", "title": "Video", "text": "Video largo en YouTube o Facebook, horizontal 16:9."},
            {"key": "t-short", "title": "Short", "text": "Video vertical de hasta 3 minutos en YouTube."},
            {"key": "t-reel", "title": "Reel", "text": "Video vertical de 3 a 90 s en Instagram o Facebook."},
            {
                "key": "t-story",
                "title": "Story",
                "text": "Imagen o video vertical que dura 24 horas. Sin texto largo.",
            },
            {
                "key": "t-carrusel",
                "title": "Carrusel",
                "text": "Varias láminas deslizables en Instagram (3 a 8). En Facebook se muestra como cuadrícula de fotos.",
            },
            {"key": "t-feed", "title": "Feed / Foto", "text": "Una imagen 4:5 o 1:1 con descripción."},
            {"key": "t-enlace", "title": "Enlace", "text": "Texto con tarjeta del enlace en Facebook."},
            {"key": "t-texto", "title": "Texto", "text": "Solo palabras, en Facebook."},
        ],
    ),
]

STATE_WORDS = (
    "Estados de una palabra en listas y paneles: Bloquea, Aviso, Bien, Nuevo, Pendiente, Contestado, Oculto, "
    "Borrado, Subiendo, Listo, Rechazado. Todos son pastillas punteadas con la misma forma; el color solo "
    "acompaña a la palabra."
)


def help_entries() -> dict:
    """``{key: {title, text}}`` plano, para los botones «?» (el popover solo necesita título y texto)."""
    return {e["key"]: {"title": e["title"], "text": e["text"]} for _, entries in GLOSSARY for e in entries}
