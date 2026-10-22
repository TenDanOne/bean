"""Presentación de la bandeja: pastillas de estado, ventana de chat, tipo de publicación y conversaciones."""

from datetime import timedelta

from django.utils import timezone

from .models import InboxMessage

# clave -> (texto, clase de color de .st-*, descripción). Una palabra por estado, mismo alto y ancho mínimo.
PILLS = {
    "nuevo": ("Nuevo", "st-scheduled", "Aún no lo abres"),
    "pendiente": ("Pendiente", "st-review", "Falta contestarlo"),
    "contestado": ("Contestado", "st-published", "Ya tiene respuesta"),
    "oculto": ("Oculto", "st-paused", "Solo tú lo ves, tu audiencia no"),
    "borrado": ("Borrado", "st-paused", "Ya no aparece en la plataforma"),
    "enviando": ("Enviando", "st-scheduled", "Tu respuesta va en camino"),
    "fallido": ("Fallido", "st-failed", "La respuesta no se envió"),
    "abierto": ("Abierto", "st-published", "Menos de 24 h desde su último mensaje"),
    "agente": ("Agente", "st-review", "Pasaron 24 h: solo con «Agente humano», hasta 7 días"),
    "cerrado": ("Cerrado", "st-failed", "Más de 7 días: ya no se puede responder"),
}

# Agrupación de la leyenda del botón «?».
LEGEND_GROUPS = [
    ("Mensaje", ["nuevo", "pendiente", "contestado"]),
    ("Comentario", ["oculto", "borrado"]),
    ("Respuesta", ["enviando", "fallido"]),
    ("Ventana del chat", ["abierto", "agente", "cerrado"]),
]
ONLY_WITH_ALL = {"pendiente", "contestado"}


def pill(key):
    label, st, title = PILLS[key]
    return {"key": key, "label": label, "st": st, "title": title, "only_all": key in ONLY_WITH_ALL}


def legend():
    return [(title, [pill(k) for k in keys]) for title, keys in LEGEND_GROUPS]


def message_pills(message, show_all):
    """Pastillas de un mensaje: Nuevo/Oculto/Borrado siempre que apliquen; Pendiente/Contestado solo con «Todos»."""
    out = []
    deleted = message.deleted_on_platform is not None
    if message.status == InboxMessage.Status.UNREAD and not deleted:
        out.append(pill("nuevo"))
    if message.is_hidden:
        out.append(pill("oculto"))
    if deleted:
        out.append(pill("borrado"))
    elif show_all and not message.is_outbound:
        out.append(pill("pendiente" if message.is_pending else "contestado"))
    return out


def window_state(last_inbound_at, now=None):
    """Ventana de respuesta de un chat según su último mensaje entrante (mismos cortes que el provider de Meta)."""
    if last_inbound_at is None:
        return None
    age = (now or timezone.now()) - last_inbound_at
    if age < timedelta(hours=24):
        return "abierto"
    return "agente" if age < timedelta(days=7) else "cerrado"


def window_notice(state, last_inbound_at, name, platform, now=None):
    """(nivel, texto) del aviso bajo la conversación; el nivel es info, warn o err."""
    age = (now or timezone.now()) - last_inbound_at
    if state == "abierto":
        left = max(1, round((timedelta(hours=24) - age).total_seconds() / 3600))
        return "info", (
            f"Ventana de 24 h abierta: puedes responder durante {left} h más. Pasado ese plazo, "
            f"{platform} solo permite responder con «Agente humano» (hasta 7 días)."
        )
    if state == "agente":
        days = max(1, round((timedelta(days=7) - age).total_seconds() / 86400))
        return "warn", (
            f"Pasaron más de 24 h desde el último mensaje de {name}. {platform} solo permite responder "
            f"con la etiqueta «Agente humano», hasta 7 días después (te quedan {days} d)."
        )
    return "err", (
        f"Pasaron más de 7 días desde el último mensaje de {name}. {platform} ya no permite "
        "responder: escríbele por otro medio."
    )


KIND_ICONS = {
    "Video": "video",
    "Short": "video",
    "Reel": "video",
    "Story": "clock",
    "Carrusel": "layer-group",
    "Foto": "image",
    "Texto": "file-lines",
    "Publicación": "comment",
}
VERTICAL_KINDS = {"Short", "Reel", "Story"}


def post_kind(related_post, platform):
    """Tipo de la publicación: el elegido en el composer o, si no hay, el que sale de sus medios y plataforma."""
    if related_post is not None:
        if related_post.post_type_label:
            return related_post.post_type_label
        types = {a.media_asset.media_type for a in related_post.post.media_attachments.select_related("media_asset")}
        if "video" in types:
            return "Video"
        return "Foto" if types else "Texto"
    return "Video" if platform == "youtube" else "Publicación"


def conversations(dm_messages, pending_ids):
    """Agrupa mensajes directos por conversación, la más reciente primero.

    ``pending_ids`` son los ids que cumplen ``InboxMessage.pending_q()``; la conversación está pendiente si
    alguno de sus mensajes lo está. ``rep`` es el mensaje con el que se abre: el último entrante, o el último.
    """
    convs = {}
    for m in sorted(dm_messages, key=lambda m: m.received_at):
        c = convs.setdefault(
            (m.social_account_id, m.conversation_id or str(m.id)),
            {"account": m.social_account, "messages": [], "pending": False, "unread": False, "mine": set()},
        )
        c["messages"].append(m)
        c["pending"] = c["pending"] or m.id in pending_ids
        if not m.is_outbound and m.status == InboxMessage.Status.UNREAD:
            c["unread"] = True
        if m.assigned_to_id:
            c["mine"].add(m.assigned_to_id)
    out = []
    for c in convs.values():
        inbound = [m for m in c["messages"] if not m.is_outbound]
        c["last"] = c["messages"][-1]
        c["last_inbound"] = inbound[-1] if inbound else None
        c["rep"] = c["last_inbound"] or c["last"]
        c["name"] = c["rep"].sender_name
        c["window"] = window_state(c["last_inbound"].received_at) if inbound else None
        out.append(c)
    out.sort(key=lambda c: c["last"].received_at, reverse=True)
    return out


def pending_count(workspace_id, accounts):
    """Cuántos hay en «No contestados»: comentarios pendientes más conversaciones de chat con algo pendiente.

    Mismo criterio que las pestañas de la bandeja (``pending_q``, chats agrupados por conversación), sin filtros.
    """
    pending = InboxMessage.objects.for_workspace(workspace_id).filter(
        InboxMessage.pending_q(), social_account__in=accounts
    )
    dm = InboxMessage.MessageType.DM
    comments = pending.exclude(message_type=dm).count()
    chats = {
        (acc, conv or str(pk))
        for acc, conv, pk in pending.filter(message_type=dm).values_list("social_account_id", "conversation_id", "id")
    }
    return comments + len(chats)
