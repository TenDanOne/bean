"""Views for the Unified Social Inbox (F-3.1)."""

import logging
from datetime import datetime

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.members.channels import can_on_channel, visible_accounts
from apps.members.decorators import require_channel_capability, require_permission
from apps.members.models import WorkspaceMembership
from apps.notifications.engine import notify
from apps.notifications.models import EventType
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace
from providers import get_provider
from providers.exceptions import APIError, RateLimitError

from .forms import (
    AssignForm,
    BulkActionForm,
    InternalNoteForm,
    ReplyForm,
    SavedReplyForm,
    SentimentForm,
    SLAConfigForm,
    StatusForm,
)
from .grouping import _published_at, extract_platform_post_id, group_by_post, label_for, post_url
from .models import (
    InboxMessage,
    InboxReply,
    InboxSLAConfig,
    InternalNote,
    SavedReply,
)

logger = logging.getLogger(__name__)

MESSAGES_PER_PAGE = 50

# How many messages to read when building the folder list. Folders are grouped
# in Python because the post id lives under a different key per platform
# (`video_id`, `media_id`), so there is no one JSON lookup that finds them all.
# A bound is still needed: without one, a workspace with 50,000 comments would
# load all of them to draw a sidebar. 2,000 covers any realistic inbox and the
# tail is older than anyone scrolls to.
FOLDER_SCAN_LIMIT = 2000


def _avatar_url(user):
    """A user's avatar URL, or "" when they have none.

    Not `getattr(user.avatar, "url", "")`: an empty FileField *raises*
    ValueError on `.url` rather than being absent, so the default never applies
    and the whole panel 500s for anyone who never uploaded a picture.
    """
    avatar = getattr(user, "avatar", None)
    if not avatar:
        return ""
    try:
        return avatar.url
    except ValueError:
        return ""


def _turn_from_message(message):
    """One synced message as a conversation turn.

    Used for the root as well as for every child, so the message being answered
    is drawn by exactly the same code as the replies under it. Rendering it
    separately is how it ended up as the one bubble in the panel with no face.
    """
    return {
        "side": "right" if message.is_outbound else "left",
        "kind": "message",
        "pending": False,
        "name": message.sender_name,
        "handle": message.sender_handle,
        "avatar_url": message.sender_avatar_url,
        "initial": (message.sender_name or "?")[0].upper(),
        "body": message.body,
        "at": message.received_at,
        "sent_by": message.sent_by,
        "platform": message.social_account.get_platform_display(),
        # The row itself, so a bubble can carry the same moderation controls the
        # panel header has. Replies we wrote only ever appear here — the feed
        # excludes outbound messages — so without this there is nowhere in the
        # product to take back something we posted.
        "message": message,
    }


def _author_name(user):
    """Display name of an author, tolerating a deleted account (FK is SET_NULL)."""
    return user.display_name if user is not None else "Usuario eliminado"


def _post_card(message):
    """The publication a comment is about, for the card above the thread.

    None for direct messages (no publication) and when we hold no data at all.
    """
    if message.message_type == InboxMessage.MessageType.DM:
        return None
    title = label_for(message)
    if not title:
        return None
    account = message.social_account
    return {
        "title": title,
        "platform": account.platform,
        "platform_display": account.get_platform_display(),
        "account": account.account_name or account.account_handle,
        "published_at": _published_at([message])
        or (message.related_post.published_at if message.related_post_id else None),
        "url": post_url(message),
    }


def _thread_turns(root, replies, notes, children):
    """The conversation as uniform turns, oldest first.

    Normalised here rather than in the template because the template was doing
    the deciding — three partials, each resolving "who is this from" its own way,
    which is how they drifted into three visual languages. The worst of it was
    invisible: a reply you had just sent rendered through one template and,
    once the platform confirmed it, through another, so the same message changed
    shape underneath you.

    Every turn is the same shape:

        side       "right" ours, "left" theirs, "none" internal notes
        pending    sent from here, not yet confirmed by the platform
        author     the public identity — the channel for us, the person for them
        sent_by    the team member behind an outbound reply, when we know
    """
    turns = []

    for reply in replies:
        # Sent from here and not yet echoed back by sync: ours, still in flight.
        turns.append(
            {
                "side": "right",
                "kind": "reply",
                "pending": True,
                "name": _author_name(reply.author),
                "handle": "",
                "avatar_url": _avatar_url(reply.author),
                "initial": (_author_name(reply.author) or "?")[0].upper(),
                "body": reply.body,
                "at": reply.sent_at,
                "sent_by": reply.author,
            }
        )

    for note in notes:
        turns.append(
            {
                # Not a turn in the conversation — never sent, never seen by
                # them. A side would imply it was.
                "side": "none",
                "kind": "note",
                "pending": False,
                "name": _author_name(note.author),
                "handle": "",
                "avatar_url": _avatar_url(note.author),
                "initial": (_author_name(note.author) or "?")[0].upper(),
                "body": note.body,
                "at": note.created_at,
                "sent_by": None,
            }
        )

    # Same builder as the root: one definition of what a message turn looks
    # like, so the two can never disagree.
    turns.extend(_turn_from_message(child) for child in children)

    return sorted(turns, key=lambda t: t["at"])


def _detail_context(workspace, message):
    """Build the full context needed for the message detail panel."""
    sla_config = InboxSLAConfig.objects.filter(workspace=workspace, is_active=True).first()
    saved_replies = SavedReply.objects.for_workspace(workspace.id)
    team_members = WorkspaceMembership.objects.filter(
        workspace=workspace,
    ).select_related("user")
    # One chronological thread. Child messages used to render in a separate
    # block from replies, which split a single conversation in two and showed
    # the same reply twice — once as the local InboxReply, once as the comment
    # the API handed back. Sync now folds those together, so anything still in
    # `replies` is a send the platform hasn't confirmed yet.
    replies = list(message.replies.select_related("author"))
    notes = list(message.internal_notes.select_related("author"))
    children = list(InboxMessage.objects.filter(parent_message=message).select_related("social_account", "sent_by"))
    thread = _thread_turns(message, replies, notes, children)
    root_turn = _turn_from_message(message)
    return {
        "workspace": workspace,
        "message": message,
        "thread": thread,
        "root_turn": root_turn,
        "post_card": _post_card(message),
        "sla_config": sla_config,
        "saved_replies": saved_replies,
        "team_members": team_members,
        "reply_form": ReplyForm(),
        "note_form": InternalNoteForm(),
        "status_choices": InboxMessage.Status.choices,
        "sentiment_choices": InboxMessage.Sentiment.choices,
    }


def _get_workspace(request, workspace_id):
    """Resolve workspace and enforce membership check."""
    workspace = get_object_or_404(Workspace, id=workspace_id)
    if not request.user.is_authenticated:
        raise PermissionDenied("Se requiere autenticación.")
    has_membership = WorkspaceMembership.objects.filter(
        user=request.user,
        workspace=workspace,
    ).exists()
    if not has_membership:
        raise PermissionDenied("No eres miembro de este espacio de trabajo.")
    return workspace


# --- Main Feed ---

# Las tres vistas de la bandeja: todo lo pendiente junto, comentarios por publicación y chats por conversación.
FEED_TABS = ("todo", "com", "chat")
# Chip de plataforma -> plataformas que agrupa (Instagram también conecta por el login propio de Instagram).
PLATFORM_FILTERS = {
    "facebook": ("facebook",),
    "instagram": ("instagram", "instagram_login"),
    "youtube": ("youtube",),
}
PLATFORM_LABELS = {"facebook": "Facebook", "instagram": "Instagram", "youtube": "YouTube"}
# YouTube no tiene mensajes directos.
NO_CHAT_PLATFORMS = ("youtube",)


def _feed_state(request, accounts):
    """Parámetros de la bandeja ya validados: lo que no existe o no es visible vuelve al valor por defecto."""
    ids = {str(a.id) for a in accounts}
    tab = request.GET.get("tab")
    platform = request.GET.get("platform", "")
    account = request.GET.get("account", "")
    return {
        "tab": tab if tab in FEED_TABS else "todo",
        "platform": platform if platform in PLATFORM_FILTERS else "",
        "account": account if account in ids else "",
        "only": "all" if request.GET.get("only") == "all" else "unans",
        "mine": request.GET.get("mine") == "1",
        "q": request.GET.get("q", "").strip()[:100],
        # «Ver más» en No contestados: cuántos comentarios leer (de 100 en 100, tope 1000).
        "limit": min(
            max(int(request.GET["n"]) if request.GET.get("n", "").isdigit() else 0, MESSAGES_PER_PAGE * 2), 1000
        ),
        "acc": request.GET.get("acc", ""),
    }


def _feed_context(request, workspace):
    from collections import Counter

    from django.db.models import Count

    from .panel import conversations, legend, message_pills, pill, post_kind

    membership = request.workspace_membership
    # Cuentas que el miembro puede ver. Los mensajes se acotan a ellas, así que ningún filtro puede ampliar el alcance.
    visible = visible_accounts(membership)
    accounts = list(
        visible_accounts(
            membership,
            SocialAccount.objects.filter(
                workspace=workspace, connection_status=SocialAccount.ConnectionStatus.CONNECTED
            ),
        ).order_by("account_name", "platform")
    )
    st = _feed_state(request, accounts)
    show_all = st["only"] == "all"

    shown = [
        a
        for a in accounts
        if (not st["platform"] or a.platform in PLATFORM_FILTERS[st["platform"]])
        and (not st["account"] or str(a.id) == st["account"])
    ]

    base = InboxMessage.objects.for_workspace(workspace.id).filter(social_account__in=visible)
    if st["platform"]:
        base = base.filter(social_account__platform__in=PLATFORM_FILTERS[st["platform"]])
    if st["account"]:
        base = base.filter(social_account_id=st["account"])
    comments = base.filter(parent_message__isnull=True, is_outbound=False).exclude(
        message_type=InboxMessage.MessageType.DM
    )
    dms = base.filter(message_type=InboxMessage.MessageType.DM)
    if st["mine"]:
        comments = comments.filter(assigned_to=request.user)
    q = st["q"]
    if q:
        comments = comments.filter(Q(body__icontains=q) | Q(sender_name__icontains=q) | Q(sender_handle__icontains=q))

    pending_comments = comments.filter(InboxMessage.pending_q())
    pending_dm_ids = set(dms.filter(InboxMessage.pending_q()).values_list("id", flat=True))
    # shortcut: se leen a lo sumo 2000 mensajes directos por carga; paginar si una bandeja llega a acumular más.
    convs = conversations(list(dms.select_related("social_account", "assigned_to")[:FOLDER_SCAN_LIMIT]), pending_dm_ids)
    if st["mine"]:
        convs = [c for c in convs if request.user.id in c["mine"]]
    if q:
        needle = q.lower()
        convs = [
            c
            for c in convs
            if any(needle in f"{m.body} {m.sender_name} {m.sender_handle}".lower() for m in c["messages"])
        ]

    com_by_account = dict(pending_comments.values_list("social_account_id").annotate(n=Count("id")))
    chat_by_account = Counter(c["account"].id for c in convs if c["pending"])
    com_count, chat_count = sum(com_by_account.values()), sum(chat_by_account.values())

    kinds: dict = {}

    def kind_of(message):
        key = message.related_post_id
        if key not in kinds:
            kinds[key] = post_kind(message.related_post, message.platform)
        return kinds[key]

    items, tiles, posts, chats, selected, more = [], [], [], [], None, False
    if st["tab"] == "todo":
        rows = pending_comments if not show_all else comments.filter(deleted_on_platform__isnull=True)
        fetched = list(
            rows.select_related("social_account", "related_post__post").order_by("-received_at")[: st["limit"]]
        )
        more = len(fetched) == st["limit"]
        for m in fetched:
            items.append(
                {"type": "com", "m": m, "at": m.received_at, "kind": kind_of(m), "pills": message_pills(m, show_all)}
            )
        for c in convs:
            if not show_all and not c["pending"]:
                continue
            pills = [pill("nuevo")] if c["unread"] else []
            if show_all:
                pills.append(pill("pendiente" if c["pending"] else "contestado"))
            if c["window"]:
                pills.append(pill(c["window"]))
            items.append(
                {"type": "chat", "m": c["rep"], "c": c, "at": c["last"].received_at, "kind": "Chat", "pills": pills}
            )
        items.sort(key=lambda i: i["at"], reverse=True)
    else:
        pool = [a for a in shown if st["tab"] == "com" or a.platform not in NO_CHAT_PLATFORMS]
        counts = com_by_account if st["tab"] == "com" else chat_by_account
        selected = next((a for a in pool if str(a.id) == st["acc"]), pool[0] if pool else None)
        tiles = [{"account": a, "n": counts.get(a.id, 0), "selected": a == selected} for a in pool]
        if selected and st["tab"] == "com":
            rows = comments.filter(social_account=selected, deleted_on_platform__isnull=True)
            for g in group_by_post(
                list(rows.select_related("social_account", "related_post__post")[:FOLDER_SCAN_LIMIT])
            ):
                if show_all or any(m.is_pending for m in g["messages"]):
                    g["rep"] = g["messages"][0]
                    g["kind"] = kind_of(g["rep"])
                    g["count"] = len(g["messages"])
                    posts.append(g)
        elif selected:
            for c in convs:
                if c["account"].id != selected.id or not (show_all or c["pending"]):
                    continue
                pills = [pill("nuevo")] if c["unread"] else []
                if show_all:
                    pills.append(pill("pendiente" if c["pending"] else "contestado"))
                if c["window"]:
                    pills.append(pill(c["window"]))
                chats.append({"c": c, "pills": pills})

    return {
        "workspace": workspace,
        "st": st,
        "show_all": show_all,
        "social_accounts": accounts,
        "platform_chips": [(key, PLATFORM_LABELS[key]) for key in PLATFORM_FILTERS],
        "tabs": [
            ("todo", "No contestados", com_count + chat_count),
            ("com", "Comentarios", com_count),
            ("chat", "Chats", chat_count),
        ],
        "tiles": tiles,
        "selected": selected,
        "items": items,
        "more_n": st["limit"] + MESSAGES_PER_PAGE * 2 if more else 0,
        "posts": posts,
        "chats": chats,
        "legend": legend(),
        "sla_config": InboxSLAConfig.objects.filter(workspace=workspace, is_active=True).first(),
    }


@login_required
@require_permission("use_inbox")
def inbox_feed(request, workspace_id):
    """Bandeja: pestañas No contestados, Comentarios y Chats con sus filtros."""
    workspace = _get_workspace(request, workspace_id)
    context = _feed_context(request, workspace)
    # La página completa cuando el botón atrás/adelante del navegador llegó aquí: htmx restaura el historial
    # sustituyendo todo el <body>, y el fragmento solo dejaría la página rota.
    if request.htmx and request.headers.get("HX-History-Restore-Request") != "true":
        return render(request, "inbox/partials/_main.html", context)
    return render(request, "inbox/feed.html", context)


# --- Message Detail ---


@login_required
@require_permission("use_inbox")
def message_detail(request, workspace_id, message_id):
    """Message detail with thread, replies, notes, and reply composer.

    Narrowed to the channels this member was assigned, exactly as the feed is.
    A workspace filter alone was not enough: this is the deepest read in the app
    — full body, sender, every reply and every internal note — and an id outlives
    the assignment that produced it. Someone who worked a channel keeps ids in
    history and in assignment notifications, so revoking the channel hid it from
    their feed while this URL still answered in full.
    """
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(
        InboxMessage.objects.select_related("social_account", "assigned_to").filter(
            social_account__in=visible_accounts(request.workspace_membership)
        ),
        id=message_id,
        workspace=workspace,
    )

    # Mark as read → open
    if message.status == InboxMessage.Status.UNREAD:
        message.status = InboxMessage.Status.OPEN
        message.save(update_fields=["status"])

    context = _detail_context(workspace, message)

    if request.htmx:
        return render(request, "inbox/partials/_message_panel.html", context)
    return render(request, "inbox/message_detail.html", context)


# --- Hilo y chat de la bandeja ---

# Un hilo muestra a lo sumo estos comentarios de una publicación. shortcut: sin paginar; paginar si una
# publicación llega a acumular más.
THREAD_LIMIT = 100


def _show_all(request):
    return request.GET.get("only", request.POST.get("only")) == "all"


def _visible_message(request, workspace, message_id):
    """El mensaje, solo si su canal es visible para el miembro (si no, 404)."""
    return get_object_or_404(
        InboxMessage.objects.select_related("social_account", "assigned_to").filter(
            social_account__in=visible_accounts(request.workspace_membership)
        ),
        id=message_id,
        workspace=workspace,
    )


def _caps(request, account):
    """Qué puede hacer este miembro en este canal: rol y permiso del canal, igual que los endpoints."""
    membership = request.workspace_membership
    role = bool(membership.effective_permissions.get("reply_from_inbox"))
    return {
        "reply": role and can_on_channel(membership, account, "can_reply"),
        "moderate": role and can_on_channel(membership, account, "can_moderate"),
        "delete": role and can_on_channel(membership, account, "can_delete_comments"),
        "role": role,
    }


def _saved_for(templates, message):
    """Plantillas guardadas ya con sus variables sustituidas para responder a ``message``."""
    account = message.social_account
    variables = {
        "sender_name": message.sender_name,
        "account_name": account.account_name or account.account_handle,
        "post_url": post_url(message) or "",
    }
    return [{"title": r.title, "body": r.render(variables)} for r in templates]


def _team(workspace):
    return WorkspaceMembership.objects.filter(workspace=workspace).select_related("user")


def _thread_context(request, workspace, message, hl_id=None):
    """Todos los comentarios de la publicación de ``message``, con sus respuestas anidadas."""
    from collections import defaultdict

    from .panel import message_pills, post_kind

    account = message.social_account
    key = extract_platform_post_id(message)
    pool = (
        InboxMessage.objects.for_workspace(workspace.id)
        .filter(social_account=account, parent_message__isnull=True, is_outbound=False)
        .exclude(message_type=InboxMessage.MessageType.DM)
        .select_related("social_account", "related_post__post", "moderated_by", "assigned_to")
        .order_by("-received_at")
    )
    templates = list(SavedReply.objects.for_workspace(workspace.id))
    comments = [m for m in pool[:FOLDER_SCAN_LIMIT] if extract_platform_post_id(m) == key][:THREAD_LIMIT]
    ids = [m.id for m in comments]
    kids, sent = defaultdict(list), defaultdict(list)
    for child in InboxMessage.objects.filter(parent_message_id__in=ids).select_related("social_account", "sent_by"):
        kids[child.parent_message_id].append(child)
    for reply in InboxReply.objects.filter(inbox_message_id__in=ids).select_related("author"):
        sent[reply.inbox_message_id].append(reply)

    show_all = _show_all(request)
    notes = defaultdict(list)
    for note in InternalNote.objects.filter(inbox_message_id__in=ids).select_related("author").order_by("created_at"):
        notes[note.inbox_message_id].append(note)
    rows = [
        {
            "m": m,
            "notes": notes[m.id],
            "saved": _saved_for(templates, m),
            "pills": message_pills(m, show_all),
            "replies": _thread_turns(m, sent[m.id], [], kids[m.id]),
            "hl": str(m.id) == hl_id,
        }
        for m in comments
    ]
    # Se marcan como leídos al abrir el hilo, después de calcular las pastillas: esta vez aún se ven como Nuevo.
    InboxMessage.objects.filter(id__in=ids, status=InboxMessage.Status.UNREAD).update(status=InboxMessage.Status.OPEN)
    return {
        "workspace": workspace,
        "rows": rows,
        "account": account,
        "title": label_for(message) or "Publicación",
        "kind": post_kind(message.related_post, account.platform),
        "url": post_url(message),
        "caps": _caps(request, account),
        "team_members": _team(workspace),
        "only": "all" if show_all else "",
    }


def _chat_context(request, workspace, message):
    """La conversación de ``message`` en burbujas, con el estado de su ventana de respuesta."""
    from .panel import conversations, pill, window_notice

    account = message.social_account
    convo = InboxMessage.objects.for_workspace(workspace.id).filter(
        social_account=account, message_type=InboxMessage.MessageType.DM
    )
    if message.conversation_id:
        convo = convo.filter(conversation_id=message.conversation_id)
    else:
        convo = convo.filter(id=message.id)
    msgs = list(convo.select_related("sent_by", "social_account"))
    conv = conversations(msgs, set())[0]
    bubbles = [
        {"out": m.is_outbound, "text": m.body, "at": m.received_at, "sent_by": m.sent_by, "pending": False}
        for m in msgs
    ]
    # Respuestas enviadas desde aquí que la sincronización aún no devolvió.
    bubbles += [
        {"out": True, "text": r.body, "at": r.sent_at, "sent_by": r.author, "pending": True}
        for r in InboxReply.objects.filter(inbox_message__in=msgs).select_related("author")
    ]
    bubbles.sort(key=lambda b: b["at"])
    InboxMessage.objects.filter(
        id__in=[m.id for m in msgs if not m.is_outbound], status=InboxMessage.Status.UNREAD
    ).update(status=InboxMessage.Status.OPEN)

    state = conv["window"]
    notice = None
    if conv["last_inbound"]:
        level, text = window_notice(
            state, conv["last_inbound"].received_at, conv["name"], account.get_platform_display()
        )
        notice = {"level": level, "text": text, "pill": pill(state)}
    return {
        "workspace": workspace,
        "account": account,
        "rep": conv["rep"],
        "name": conv["name"],
        "bubbles": bubbles,
        "window": state,
        "notice": notice,
        "closed": state == "cerrado",
        "needs_agent": state == "agente",
        "caps": _caps(request, account),
        "saved": _saved_for(list(SavedReply.objects.for_workspace(workspace.id)), conv["rep"]),
        "notes": list(
            InternalNote.objects.filter(inbox_message__in=msgs).select_related("author").order_by("created_at")
        ),
        "team_members": _team(workspace),
        "only": "all" if _show_all(request) else "",
    }


def _panel_response(request, workspace, message):
    """El panel de detalle que corresponde a ``message``: chat si es un mensaje directo, hilo si es un comentario."""
    if message.message_type == InboxMessage.MessageType.DM:
        return render(request, "inbox/partials/_chat.html", _chat_context(request, workspace, message))
    return render(
        request,
        "inbox/partials/_thread.html",
        _thread_context(request, workspace, message, hl_id=str(message.id)),
    )


@login_required
@require_permission("use_inbox")
def thread_detail(request, workspace_id, message_id):
    """Hilo de comentarios de la publicación a la que pertenece el mensaje (resaltado con ``?hl=1``)."""
    workspace = _get_workspace(request, workspace_id)
    message = _visible_message(request, workspace, message_id)
    if message.message_type == InboxMessage.MessageType.DM:
        return _panel_response(request, workspace, message)
    hl = str(message.id) if request.GET.get("hl") else None
    return render(request, "inbox/partials/_thread.html", _thread_context(request, workspace, message, hl_id=hl))


@login_required
@require_permission("use_inbox")
def chat_detail(request, workspace_id, message_id):
    """Conversación a la que pertenece el mensaje directo."""
    workspace = _get_workspace(request, workspace_id)
    message = _visible_message(request, workspace, message_id)
    if message.message_type != InboxMessage.MessageType.DM:
        return _panel_response(request, workspace, message)
    return render(request, "inbox/partials/_chat.html", _chat_context(request, workspace, message))


# --- Reply ---


def _reply_error(request, message: str):
    """Render a send failure where the reply would have appeared.

    422 rather than 200 so HTMX doesn't swap this into the thread as if it were
    a delivered reply — the whole point is that nothing was sent.
    """
    if request.POST.get("ui"):
        # Interfaz nueva: htmx 2 no cambia nada con un 4xx, así que va un 200 que el encabezado
        # redirige al aviso «Fallido» de esa respuesta.
        message_id = request.resolver_match.kwargs["message_id"]
        response = render(request, "inbox/partials/_reply_failed.html", {"error_message": message})
        response["HX-Retarget"] = f"#reply-err-{message_id}"
        response["HX-Reswap"] = "innerHTML"
        return response
    return render(
        request,
        "inbox/partials/_reply_error.html",
        {"error_message": message},
        status=422,
    )


def _reply_extra(message):
    """What a provider needs to answer ``message``: its stored ``extra`` plus, for a DM, who to write to and when they last wrote."""
    extra = dict(message.extra or {})
    if message.message_type != InboxMessage.MessageType.DM:
        return extra
    # Rows stored before recipient_id existed: the other party is whoever wrote the message.
    extra.setdefault("recipient_id", "" if message.is_outbound else message.sender_handle)
    inbound = InboxMessage.objects.filter(social_account_id=message.social_account_id, is_outbound=False)
    if message.conversation_id:
        inbound = inbound.filter(conversation_id=message.conversation_id)
    else:
        inbound = inbound.filter(id=message.id)
    last = inbound.order_by("-received_at").values_list("received_at", flat=True).first()
    if last:
        extra["last_inbound_at"] = last.isoformat()
    return extra


def _account_for_message(request, workspace_id, message_id, *args, **kwargs):
    """The channel a message belongs to, for the per-channel capability checks.

    Scoped to the workspace the URL names, so a message id from another tenant
    resolves to None and the capability check refuses — rather than fetching an
    account the caller was never entitled to see.
    """
    message = (
        InboxMessage.objects.filter(id=message_id, workspace_id=workspace_id).select_related("social_account").first()
    )
    return message.social_account if message else None


@login_required
@require_permission("reply_from_inbox")
@require_channel_capability("can_reply", _account_for_message)
@require_POST
def send_reply(request, workspace_id, message_id):
    """Send a reply via the platform API and record it.

    Two gates, and both are needed: `reply_from_inbox` says the role may answer
    at all, `can_reply` says they may answer *as this channel*. Only the first
    was enforced, so the per-channel box saved a value nothing ever read.
    """
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(InboxMessage, id=message_id, workspace=workspace)

    form = ReplyForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Respuesta no válida.", status=400)

    body = form.cleaned_data["body"]
    account = message.social_account
    reply_extra = _reply_extra(message)
    if message.message_type == InboxMessage.MessageType.DM and not reply_extra.get("recipient_id"):
        return _reply_error(
            request, "No se pudo identificar a quién responder en este chat; espera la próxima sincronización."
        )
    # Pasadas 24 h, Meta solo admite la etiqueta de agente humano: la interfaz nueva exige marcarla.
    if request.POST.get("ui") and message.message_type == InboxMessage.MessageType.DM:
        from .panel import window_state

        last = reply_extra.get("last_inbound_at")
        if last and window_state(datetime.fromisoformat(last)) == "agente" and not request.POST.get("human_agent"):
            return _reply_error(
                request, "Pasaron más de 24 h: marca «Responder como agente humano» para poder enviarlo."
            )

    # Only record a reply that actually left the building. This used to swallow
    # every failure and store the reply anyway with an empty platform_reply_id,
    # so the thread showed it as sent while the platform never received it —
    # the operator had no way to tell. To note something without publishing it,
    # use an internal note (`add_note`) instead.
    try:
        from apps.publisher.engine import _resolve_publish_credentials

        provider = get_provider(account.platform, _resolve_publish_credentials(account))
        result = provider.reply_to_message(
            access_token=account.get_fresh_access_token(provider),
            message_id=message.platform_message_id,
            text=body,
            extra=reply_extra,
        )
        platform_reply_id = result.platform_message_id
    except NotImplementedError:
        logger.info("Provider %s does not support reply_to_message.", account.platform)
        return _reply_error(request, f"{account.get_platform_display()} doesn't support replying from the inbox.")
    except (ConnectionError, TimeoutError, OSError) as exc:
        logger.exception("Network error sending reply for message %s: %s", message.id, exc)
        return _reply_error(
            request, "No se pudo contactar a la plataforma. Tu respuesta no se envió; inténtalo de nuevo."
        )
    except APIError as exc:
        logger.exception("Failed to send reply for message %s", message.id)
        # A non-retryable error is one we raised ourselves with a readable reason (e.g. the 7-day DM window).
        if not exc.retryable:
            return _reply_error(request, f"{exc} No se envió.")
        return _reply_error(request, "La plataforma rechazó la respuesta. No se envió.")
    except Exception:
        logger.exception("Failed to send reply for message %s", message.id)
        return _reply_error(request, "La plataforma rechazó la respuesta. No se envió.")

    InboxReply.objects.create(
        inbox_message=message,
        author=request.user,
        body=body,
        platform_reply_id=platform_reply_id,
    )

    message.mark_thread_answered()

    # Auto-resolve on reply if configured
    sla_config = InboxSLAConfig.objects.filter(workspace=workspace, is_active=True).first()
    if sla_config and sla_config.auto_resolve_on_reply:
        message.status = InboxMessage.Status.RESOLVED
        message.save(update_fields=["status"])
    elif message.status == InboxMessage.Status.UNREAD:
        message.status = InboxMessage.Status.OPEN
        message.save(update_fields=["status"])

    # The whole panel, not the single bubble.
    #
    # Appending one item to the end of the thread put it in the wrong place as
    # soon as anything else had arrived since, and it bypassed the ordering that
    # _thread_turns does. Re-rendering costs one extra query and cannot get the
    # order wrong.
    if request.POST.get("ui"):
        return _panel_response(request, workspace, message)
    context = _detail_context(workspace, message)
    return render(request, "inbox/partials/_message_panel.html", context)


# --- Marcar contestado ---


@login_required
@require_permission("reply_from_inbox")
@require_channel_capability("can_reply", _account_for_message)
@require_POST
def mark_answered(request, workspace_id, message_id):
    """Marca el hilo como contestado sin enviar nada (se respondió en la plataforma, o no requiere respuesta)."""
    workspace = _get_workspace(request, workspace_id)
    message = _visible_message(request, workspace, message_id)
    message.mark_thread_answered()
    return _panel_response(request, workspace, message)


# --- Internal Note ---


@login_required
@require_permission("reply_from_inbox")
@require_channel_capability("can_reply", _account_for_message)
@require_POST
def add_note(request, workspace_id, message_id):
    """Add an internal note to a message.

    Same two gates as `send_reply`. A note is not published, but it is written
    into a thread on a channel — and the panel it renders back carries that
    thread, so an ungated note was also a read of everything around it.
    """
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(InboxMessage, id=message_id, workspace=workspace)

    form = InternalNoteForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Nota no válida.", status=400)

    InternalNote.objects.create(
        inbox_message=message,
        author=request.user,
        body=form.cleaned_data["body"],
    )

    if request.POST.get("ui"):
        return _panel_response(request, workspace, message)
    context = _detail_context(workspace, message)
    return render(request, "inbox/partials/_message_panel.html", context)


# --- Moderation ---
#
# Two actions, deliberately named for what they do rather than lumped together
# as "remove". Hiding is reversible and the platform keeps the comment; deleting
# is permanent. Calling both "delete" is how someone reaches for the wrong one.
#
# In both cases our row survives. A hidden comment has to be restorable, and a
# deleted one still carries the thread we answered and who did what — dropping
# the record too would erase the evidence along with the comment.


def _moderation_provider(account):
    """A provider and a fresh token for one channel, or a reason it cannot be."""
    from apps.publisher.engine import _resolve_publish_credentials

    provider = get_provider(account.platform, _resolve_publish_credentials(account))
    return provider, account.get_fresh_access_token(provider)


def _moderation_error(request, message_text):
    return render(
        request,
        "inbox/partials/_reply_error.html",
        {"error_message": message_text},
        status=422,
    )


@login_required
@require_permission("reply_from_inbox")
@require_channel_capability("can_moderate", _account_for_message)
@require_POST
def toggle_hidden(request, workspace_id, message_id):
    """Hide a comment from the public, or bring it back.

    On YouTube this is `setModerationStatus`, not deletion: the comment stops
    being publicly visible and the platform keeps it, which is why this is
    offered to whoever works the inbox rather than reserved for a manager.
    """
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(InboxMessage, id=message_id, workspace=workspace)

    if message.deleted_on_platform:
        return _moderation_error(
            request, "Este comentario fue eliminado, por lo que ya no se puede ocultar ni restaurar."
        )
    if not message.is_moderatable:
        return _moderation_error(request, "Esto no es un comentario, así que no hay nada que ocultar en la plataforma.")

    account = message.social_account
    hide = not message.is_hidden

    try:
        provider, token = _moderation_provider(account)
        provider.set_comment_visibility(
            access_token=token,
            comment_id=message.platform_message_id,
            visible=not hide,
        )
    except NotImplementedError:
        return _moderation_error(request, f"{account.get_platform_display()} doesn't support hiding comments.")
    except RateLimitError:
        return _moderation_error(request, "La cuota de la plataforma está agotada por ahora. Inténtalo más tarde.")
    except (ConnectionError, TimeoutError, OSError):
        logger.exception("Network error hiding comment %s", message.id)
        return _moderation_error(
            request, "No se pudo contactar a la plataforma. No se cambió nada; inténtalo de nuevo."
        )
    except APIError as exc:
        logger.exception("Platform refused the moderation change for %s: %s", message.id, exc)
        # The hardest failure to explain is unhiding a comment that had been
        # *rejected* rather than held for review. YouTube will not let an app
        # restore a rejected comment (it answers 400), while a held one comes
        # back fine — so say which of the two is going on instead of dumping
        # the API's cryptic reason.
        if not hide and message.platform == "youtube":
            return _moderation_error(
                request,
                "Este comentario no se puede restaurar desde la app. Si fue rechazado "
                "(y no solo retenido para revisión), YouTube no permite que las apps lo "
                "recuperen; restáuralo en youtube.com/comments, dentro de YouTube "
                "Studio.",
            )
        return _moderation_error(request, f"{account.get_platform_display()} lo rechazó: {exc}")

    # Written only after the platform agreed. Storing it first would show the
    # comment as hidden here while it stayed visible to everyone else, which is
    # the one thing this feature must never do.
    message.is_hidden = hide
    message.moderated_by = request.user
    message.save(update_fields=["is_hidden", "moderated_by"])

    if request.POST.get("ui"):
        return _panel_response(request, workspace, message)
    return render(request, "inbox/partials/_message_panel.html", _detail_context(workspace, message))


@login_required
@require_permission("reply_from_inbox")
@require_channel_capability("can_delete_comments", _account_for_message)
@require_POST
def delete_comment(request, workspace_id, message_id):
    """Delete a comment permanently on the platform.

    Manager and above, because it cannot be undone. Note the platform limit
    This used to refuse anything that was not our own comment, on the belief
    that platforms only let you delete your own. That belief was wrong — a
    channel owner may delete comments on their own videos — and the refusal was
    unreachable anyway: the feed excludes outbound messages, so the panel could
    never be opened on one. The check hid the whole feature.

    So we try, and if the platform does refuse we say so and name the reversible
    alternative rather than leaving an API error nobody can act on.
    """
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(InboxMessage, id=message_id, workspace=workspace)

    if message.deleted_on_platform:
        return _moderation_error(request, "Este comentario ya fue eliminado.")
    if not message.is_moderatable:
        return _moderation_error(
            request, "Esto no es un comentario, así que no hay nada que eliminar en la plataforma."
        )

    account = message.social_account

    try:
        provider, token = _moderation_provider(account)
        provider.delete_comment(access_token=token, comment_id=message.platform_message_id)
    except NotImplementedError:
        return _moderation_error(request, f"{account.get_platform_display()} doesn't support deleting comments.")
    except RateLimitError:
        return _moderation_error(request, "La cuota de la plataforma está agotada por ahora. Inténtalo más tarde.")
    except (ConnectionError, TimeoutError, OSError):
        logger.exception("Network error deleting comment %s", message.id)
        return _moderation_error(
            request, "No se pudo contactar a la plataforma. No se eliminó nada; inténtalo de nuevo."
        )
    except APIError as exc:
        logger.exception("Platform refused the deletion of %s: %s", message.id, exc)
        return _moderation_error(
            request,
            f"{account.get_platform_display()} no permitió eliminarlo: {exc} "
            "Intenta ocultarlo: deja de ser visible al público y se puede deshacer.",
        )

    message.deleted_on_platform = timezone.now()
    message.moderated_by = request.user
    message.save(update_fields=["deleted_on_platform", "moderated_by"])

    if request.POST.get("ui"):
        return _panel_response(request, workspace, message)
    return render(request, "inbox/partials/_message_panel.html", _detail_context(workspace, message))


# --- Assignment ---


@login_required
@require_permission("reply_from_inbox")
@require_channel_capability("can_reply", _account_for_message)
@require_POST
def assign_message(request, workspace_id, message_id):
    """Assign a message to a team member.

    Gated on the channel because this is the one action that hands someone else
    a live message id: the notification below carries it. Without the gate, a
    member could route work on a channel they were never given.
    """
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(InboxMessage, id=message_id, workspace=workspace)

    form = AssignForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Asignación no válida.", status=400)

    assigned_to_id = form.cleaned_data.get("assigned_to")
    if assigned_to_id:
        # Verify the user is a workspace member
        membership = (
            WorkspaceMembership.objects.filter(workspace=workspace, user_id=assigned_to_id)
            .select_related("user")
            .first()
        )
        if not membership:
            return HttpResponse("El usuario no es miembro del espacio de trabajo.", status=400)
        # And they must hold this channel. Being in the workspace is not enough:
        # assigning work to someone who cannot open it produces a notification
        # that leads to a 403, which reads as a broken app rather than as a
        # permission boundary. The channel dialog is where access is granted.
        if not can_on_channel(membership, message.social_account, "can_reply"):
            return HttpResponse("Ese miembro no está asignado a este canal.", status=400)
        message.assigned_to = membership.user
    else:
        message.assigned_to = None

    message.save(update_fields=["assigned_to"])

    # Notify the assignee
    if message.assigned_to and message.assigned_to != request.user:
        notify(
            user=message.assigned_to,
            event_type=EventType.NEW_INBOX_MESSAGE,
            title=f"Se te asignó un {message.get_message_type_display()}",
            body=f"De {message.sender_name}: {message.body[:100]}",
            data={
                "message_id": str(message.id),
                "workspace_id": str(workspace.id),
            },
        )

    if request.POST.get("ui"):
        return _panel_response(request, workspace, message)
    context = _detail_context(workspace, message)
    return render(request, "inbox/partials/_message_panel.html", context)


# --- Status ---


@login_required
@require_permission("reply_from_inbox")
@require_channel_capability("can_reply", _account_for_message)
@require_POST
def change_status(request, workspace_id, message_id):
    """Change message status.

    `bulk_action` refuses to archive a message on an unassigned channel; this is
    the same archive, one id at a time, and it used to allow it. Closing the bulk
    path and leaving the singular one open closes nothing.
    """
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(InboxMessage, id=message_id, workspace=workspace)

    form = StatusForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Estado no válido.", status=400)

    message.status = form.cleaned_data["status"]
    message.save(update_fields=["status"])

    context = _detail_context(workspace, message)
    return render(request, "inbox/partials/_message_panel.html", context)


# --- Sentiment Override ---


@login_required
@require_permission("reply_from_inbox")
@require_channel_capability("can_reply", _account_for_message)
@require_POST
def change_sentiment(request, workspace_id, message_id):
    """Override sentiment manually.

    Gated on `reply_from_inbox`, which is what editor, manager and owner hold
    and a plain viewer does not. Sentiment is not decoration: it is the record
    of how an audience talks to you, and it feeds the reporting — a viewer
    relabelling it quietly changes the numbers someone else reads.

    And gated on the channel for the same reason: the reporting it feeds is
    per-channel, so relabelling one you were never assigned edits numbers that
    belong to someone else's brand.
    """
    workspace = _get_workspace(request, workspace_id)
    message = get_object_or_404(InboxMessage, id=message_id, workspace=workspace)

    form = SentimentForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Sentimiento no válido.", status=400)

    message.sentiment = form.cleaned_data["sentiment"]
    message.sentiment_source = InboxMessage.SentimentSource.MANUAL
    message.save(update_fields=["sentiment", "sentiment_source"])

    # The full panel, like change_status — the label appears in three places
    # (the header control, the badge on the message, and the dropdown's own
    # current value), so returning just one of them leaves the other two lying.
    context = _detail_context(workspace, message)
    return render(request, "inbox/partials/_message_panel.html", context)


# --- Bulk Actions ---


@login_required
@require_permission("reply_from_inbox")
@require_POST
def bulk_action(request, workspace_id):
    """Perform bulk actions on multiple messages."""
    workspace = _get_workspace(request, workspace_id)

    form = BulkActionForm(request.POST)
    if not form.is_valid():
        return HttpResponse("Acción masiva no válida.", status=400)

    message_ids = form.cleaned_data["message_ids"]
    action = form.cleaned_data["action"]
    value = form.cleaned_data.get("value", "")

    # Scoped to the channels this member may see, like the feed itself.
    #
    # The re-render below goes through `inbox_feed` precisely so the two lists
    # cannot drift — but the *write* was still a bare workspace filter, so a
    # crafted set of ids could archive or reassign messages on channels the
    # sender was never given. Reading was fixed and writing was not.
    qs = InboxMessage.objects.filter(
        id__in=message_ids,
        workspace=workspace,
        social_account__in=visible_accounts(
            request.workspace_membership,
            SocialAccount.objects.for_workspace(workspace.id),
        ),
    )

    if action == "mark_read":
        qs.filter(status=InboxMessage.Status.UNREAD).update(status=InboxMessage.Status.OPEN)
    elif action == "resolve":
        qs.exclude(status=InboxMessage.Status.ARCHIVED).update(status=InboxMessage.Status.RESOLVED)
    elif action == "archive":
        qs.update(status=InboxMessage.Status.ARCHIVED)
    elif action == "assign" and value:
        membership = WorkspaceMembership.objects.filter(workspace=workspace, user_id=value).first()
        if membership:
            # Only the messages the assignee can actually open, matching the
            # single-message path. A bulk selection spans channels, so assigning
            # the whole set to one person would otherwise hand them work on
            # channels they were never given — silently, and in bulk.
            qs.filter(social_account__in=visible_accounts(membership, capability="can_reply")).update(
                assigned_to=membership.user
            )

    # Re-render through inbox_feed rather than building a second list here.
    #
    # This used to be its own query, and it had drifted from the real one in
    # three ways at once: no parent/outbound filter, so replies and our own
    # words came back as separate items; no folder grouping, so the layout
    # changed under you after a bulk action; and no visible_accounts, which
    # meant a member briefly saw channels they were never assigned.
    #
    # Two places building "the inbox list" will always disagree eventually. Now
    # there is one, and this borrows it.
    #
    # The filters travel in the POST because a bulk action submits the form, not
    # the URL — without this the list would snap back to unfiltered and lose the
    # view the user was working in.
    request.GET = request.POST
    return inbox_feed(request, workspace_id)


# --- Saved Replies ---


@login_required
@require_permission("manage_workspace_settings")
def saved_replies_list(request, workspace_id):
    """List saved replies for a workspace."""
    workspace = _get_workspace(request, workspace_id)
    replies = SavedReply.objects.for_workspace(workspace.id)

    context = {"workspace": workspace, "saved_replies": replies}
    return render(request, "inbox/saved_replies.html", context)


@login_required
@require_permission("manage_workspace_settings")
def saved_reply_create(request, workspace_id):
    """Create a new saved reply."""
    workspace = _get_workspace(request, workspace_id)

    if request.method == "POST":
        form = SavedReplyForm(request.POST)
        if form.is_valid():
            reply = form.save(commit=False)
            reply.workspace = workspace
            reply.created_by = request.user
            reply.save()
            return redirect("inbox:saved_replies", workspace_id=workspace.id)
    else:
        form = SavedReplyForm()

    context = {"workspace": workspace, "form": form}

    if request.htmx:
        return render(request, "inbox/partials/_saved_reply_form.html", context)
    return render(request, "inbox/saved_replies.html", context)


@login_required
@require_permission("manage_workspace_settings")
def saved_reply_edit(request, workspace_id, reply_id):
    """Edit an existing saved reply."""
    workspace = _get_workspace(request, workspace_id)
    reply = get_object_or_404(SavedReply, id=reply_id, workspace=workspace)

    if request.method == "POST":
        form = SavedReplyForm(request.POST, instance=reply)
        if form.is_valid():
            form.save()
            return redirect("inbox:saved_replies", workspace_id=workspace.id)
    else:
        form = SavedReplyForm(instance=reply)

    context = {"workspace": workspace, "form": form, "saved_reply": reply}

    if request.htmx:
        return render(request, "inbox/partials/_saved_reply_form.html", context)
    return render(request, "inbox/saved_replies.html", context)


@login_required
@require_permission("manage_workspace_settings")
@require_POST
def saved_reply_delete(request, workspace_id, reply_id):
    """Delete a saved reply."""
    workspace = _get_workspace(request, workspace_id)
    reply = get_object_or_404(SavedReply, id=reply_id, workspace=workspace)
    reply.delete()
    return redirect("inbox:saved_replies", workspace_id=workspace.id)


# --- SLA Config ---


@login_required
@require_permission("manage_workspace_settings")
def sla_config(request, workspace_id):
    """Configure SLA settings for inbox."""
    workspace = _get_workspace(request, workspace_id)
    config, _created = InboxSLAConfig.objects.get_or_create(
        workspace=workspace,
        defaults={"target_response_minutes": 120, "is_active": False},
    )

    if request.method == "POST":
        form = SLAConfigForm(request.POST, instance=config)
        if form.is_valid():
            form.save()
            return redirect("inbox:feed", workspace_id=workspace.id)
    else:
        form = SLAConfigForm(instance=config)

    context = {"workspace": workspace, "form": form, "config": config}
    return render(request, "inbox/sla_config.html", context)
