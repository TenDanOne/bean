"""La bandeja nueva: pestañas con contadores, filtros, hilo, chat con su ventana y respuesta."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.inbox.models import InboxMessage, InboxReply
from apps.inbox.panel import window_state
from apps.members.models import ChannelAssignment, OrgMembership, WorkspaceMembership
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


@pytest.fixture
def workspace(db, organization):
    return Workspace.objects.create(organization=organization, name="UI WS")


@pytest.fixture
def operator(db, django_user_model, workspace):
    user = django_user_model.objects.create_user(
        email="op@example.com", password="pw12345!", tos_accepted_at=timezone.now()
    )
    OrgMembership.objects.create(user=user, organization=workspace.organization, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role="owner")
    return user


def make_account(workspace, platform, handle):
    return SocialAccount.objects.create(
        workspace=workspace,
        platform=platform,
        account_platform_id=handle,
        account_handle=handle,
        account_name=handle.title(),
        oauth_access_token="tok",
        connection_status=SocialAccount.ConnectionStatus.CONNECTED,
    )


@pytest.fixture
def fb(workspace):
    return make_account(workspace, "facebook", "fbpage")


@pytest.fixture
def ig(workspace):
    return make_account(workspace, "instagram", "igprof")


@pytest.fixture
def yt(workspace):
    return make_account(workspace, "youtube", "ytchan")


def msg(workspace, account, mid, *, kind="comment", minutes_ago=10, post=None, convo="", outbound=False, **kw):
    extra = {"post_id": post} if post and account.platform != "youtube" else ({"video_id": post} if post else {})
    return InboxMessage.objects.create(
        workspace=workspace,
        social_account=account,
        platform_message_id=mid,
        message_type=kind,
        sender_name=kw.pop("sender", "Persona " + mid),
        sender_handle=kw.pop("handle", "psid-" + mid),
        body=kw.pop("body", "hola " + mid),
        conversation_id=convo,
        is_outbound=outbound,
        awaiting_reply=kw.pop("awaiting", not outbound),
        extra=extra,
        received_at=timezone.now() - timedelta(minutes=minutes_ago),
        **kw,
    )


def feed(workspace, **params):
    return reverse("inbox:feed", kwargs={"workspace_id": workspace.id}) + (
        "?" + "&".join(f"{k}={v}" for k, v in params.items()) if params else ""
    )


def url(name, workspace, message):
    return reverse(f"inbox:{name}", kwargs={"workspace_id": workspace.id, "message_id": message.id})


def counts(response):
    return {key: n for key, _label, n in response.context["tabs"]}


@pytest.mark.django_db
class TestCounters:
    def test_tabs_count_comments_and_conversations(self, client, operator, workspace, fb, ig):
        msg(workspace, fb, "c1", post="p1")
        msg(workspace, ig, "c2", post="p2")
        # Dos mensajes de una misma conversación son un solo chat pendiente.
        msg(workspace, fb, "d1", kind="dm", convo="t1", minutes_ago=30)
        msg(workspace, fb, "d2", kind="dm", convo="t1", minutes_ago=20)
        client.force_login(operator)
        response = client.get(feed(workspace))
        assert counts(response) == {"todo": 3, "com": 2, "chat": 1}

    def test_answered_things_do_not_count(self, client, operator, workspace, fb):
        msg(workspace, fb, "c1", post="p1", awaiting=False)
        msg(workspace, fb, "c2", post="p1", outbound=True)
        client.force_login(operator)
        assert counts(client.get(feed(workspace))) == {"todo": 0, "com": 0, "chat": 0}

    def test_account_filter_narrows_the_counters(self, client, operator, workspace, fb, ig):
        msg(workspace, fb, "c1", post="p1")
        msg(workspace, ig, "c2", post="p2")
        client.force_login(operator)
        assert counts(client.get(feed(workspace, account=fb.id)))["com"] == 1

    def test_garbage_filters_fall_back_instead_of_failing(self, client, operator, workspace, fb):
        msg(workspace, fb, "c1", post="p1")
        client.force_login(operator)
        response = client.get(feed(workspace, account="not-a-uuid", platform="myspace", tab="x", only="zz"))
        assert response.status_code == 200
        assert counts(response)["todo"] == 1


@pytest.mark.django_db
class TestNoContestados:
    def test_pending_comments_and_chats_without_outbound(self, client, operator, workspace, fb):
        inbound = msg(workspace, fb, "c1", post="p1")
        msg(workspace, fb, "c2", post="p1", outbound=True)
        msg(workspace, fb, "c3", post="p1", parent_message=inbound)
        # El chat que ya contestamos (la sincronización apagó el aviso) no está pendiente.
        msg(workspace, fb, "d1", kind="dm", convo="t1", minutes_ago=30, awaiting=False)
        msg(workspace, fb, "d2", kind="dm", convo="t1", minutes_ago=20, outbound=True)
        client.force_login(operator)
        items = client.get(feed(workspace)).context["items"]
        assert [i["type"] for i in items].count("chat") == 0
        assert [i["m"].platform_message_id for i in items] == ["c1"]
        assert not any(i["m"].is_outbound for i in items)

    def test_a_chat_is_one_item_per_conversation(self, client, operator, workspace, fb):
        msg(workspace, fb, "d1", kind="dm", convo="t1", minutes_ago=30)
        msg(workspace, fb, "d2", kind="dm", convo="t1", minutes_ago=20)
        msg(workspace, fb, "d3", kind="dm", convo="t2", minutes_ago=5)
        client.force_login(operator)
        items = client.get(feed(workspace)).context["items"]
        assert len(items) == 2
        assert all(i["type"] == "chat" for i in items)

    def test_new_pill_only_until_opened_and_window_on_chats(self, client, operator, workspace, fb):
        msg(workspace, fb, "d1", kind="dm", convo="t1", minutes_ago=30)
        client.force_login(operator)
        (item,) = client.get(feed(workspace)).context["items"]
        assert [p["key"] for p in item["pills"]] == ["nuevo", "abierto"]
        client.get(url("chat_detail", workspace, item["m"]))
        (item,) = client.get(feed(workspace)).context["items"]
        assert [p["key"] for p in item["pills"]] == ["abierto"]

    def test_pending_and_answered_pills_only_with_todos(self, client, operator, workspace, fb):
        msg(workspace, fb, "c1", post="p1", status="open")
        msg(workspace, fb, "c2", post="p1", status="open", awaiting=False)
        client.force_login(operator)
        assert client.get(feed(workspace)).context["items"][0]["pills"] == []
        everything = client.get(feed(workspace, only="all")).context["items"]
        assert sorted(p["key"] for i in everything for p in i["pills"]) == ["contestado", "pendiente"]

    def test_assigned_to_me(self, client, operator, workspace, fb):
        msg(workspace, fb, "c1", post="p1", assigned_to=operator)
        msg(workspace, fb, "c2", post="p1")
        client.force_login(operator)
        assert counts(client.get(feed(workspace, mine=1)))["todo"] == 1

    def test_an_htmx_request_returns_only_the_fragment(self, client, operator, workspace, fb):
        msg(workspace, fb, "c1", post="p1")
        client.force_login(operator)
        body = client.get(feed(workspace), HTTP_HX_REQUEST="true").content.decode()
        assert "<html" not in body and 'role="tablist"' in body

    def test_no_accounts_shows_the_empty_state(self, client, operator, workspace):
        client.force_login(operator)
        assert "Aún no hay cuentas conectadas" in client.get(feed(workspace)).content.decode()


@pytest.mark.django_db
class TestComentariosPorCuenta:
    def test_posts_of_the_selected_account_never_mix_accounts(self, client, operator, workspace, fb, ig):
        msg(workspace, fb, "c1", post="pf")
        msg(workspace, fb, "c2", post="pf")
        msg(workspace, ig, "c3", post="pi")
        client.force_login(operator)
        response = client.get(feed(workspace, tab="com", acc=fb.id))
        assert response.context["selected"] == fb
        assert [(g["key"], g["count"]) for g in response.context["posts"]] == [("pf", 2)]
        other = client.get(feed(workspace, tab="com", acc=ig.id))
        assert [g["key"] for g in other.context["posts"]] == ["pi"]

    def test_tiles_carry_each_accounts_pending_count(self, client, operator, workspace, fb, ig):
        msg(workspace, fb, "c1", post="pf")
        msg(workspace, fb, "c2", post="pf")
        msg(workspace, ig, "c3", post="pi")
        client.force_login(operator)
        tiles = client.get(feed(workspace, tab="com")).context["tiles"]
        assert {t["account"].account_handle: t["n"] for t in tiles} == {"fbpage": 2, "igprof": 1}

    def test_chats_tab_skips_youtube(self, client, operator, workspace, fb, yt):
        client.force_login(operator)
        tiles = client.get(feed(workspace, tab="chat")).context["tiles"]
        assert [t["account"] for t in tiles] == [fb]

    def test_chat_list_groups_by_conversation_per_account(self, client, operator, workspace, fb, ig):
        msg(workspace, fb, "d1", kind="dm", convo="t1", minutes_ago=30)
        msg(workspace, fb, "d2", kind="dm", convo="t1", minutes_ago=20)
        msg(workspace, ig, "d3", kind="dm", convo="t9", minutes_ago=5)
        client.force_login(operator)
        chats = client.get(feed(workspace, tab="chat", acc=fb.id)).context["chats"]
        assert len(chats) == 1
        assert len(chats[0]["c"]["messages"]) == 2

    def test_answered_posts_only_appear_with_todos(self, client, operator, workspace, fb):
        msg(workspace, fb, "c1", post="pf", awaiting=False)
        client.force_login(operator)
        assert client.get(feed(workspace, tab="com")).context["posts"] == []
        assert len(client.get(feed(workspace, tab="com", only="all")).context["posts"]) == 1


@pytest.mark.django_db
class TestVisibleAccounts:
    def test_a_member_only_sees_their_channels(self, client, django_user_model, workspace, fb, ig):
        user = django_user_model.objects.create_user(
            email="ed@example.com", password="pw12345!", tos_accepted_at=timezone.now()
        )
        OrgMembership.objects.update_or_create(
            user=user, organization=workspace.organization, defaults={"org_role": OrgMembership.OrgRole.MEMBER}
        )
        membership = WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role="editor")
        ChannelAssignment.objects.create(membership=membership, social_account=fb, can_reply=True)
        msg(workspace, fb, "c1", post="pf")
        hidden = msg(workspace, ig, "c2", post="pi")
        msg(workspace, ig, "d1", kind="dm", convo="t1")
        client.force_login(user)
        response = client.get(feed(workspace))
        assert counts(response) == {"todo": 1, "com": 1, "chat": 0}
        assert [t["account"] for t in client.get(feed(workspace, tab="com")).context["tiles"]] == [fb]
        assert client.get(url("thread_detail", workspace, hidden)).status_code == 404
        assert client.get(url("chat_detail", workspace, hidden)).status_code == 404


@pytest.mark.django_db
class TestWindow:
    @pytest.mark.parametrize(
        ("age", "state"),
        [
            (timedelta(hours=1), "abierto"),
            (timedelta(hours=23, minutes=59), "abierto"),
            (timedelta(hours=25), "agente"),
            (timedelta(days=6, hours=23), "agente"),
            (timedelta(days=8), "cerrado"),
        ],
    )
    def test_states(self, age, state):
        assert window_state(timezone.now() - age) == state

    def test_it_counts_from_the_last_inbound_not_from_our_reply(self, client, operator, workspace, fb):
        msg(workspace, fb, "d1", kind="dm", convo="t1", minutes_ago=60 * 30, awaiting=False, status="open")
        msg(workspace, fb, "d2", kind="dm", convo="t1", minutes_ago=5, outbound=True)
        client.force_login(operator)
        chats = client.get(feed(workspace, tab="chat", only="all")).context["chats"]
        assert [p["key"] for p in chats[0]["pills"]] == ["contestado", "agente"]

    def test_chat_panel_notice_and_agent_option(self, client, operator, workspace, fb):
        row = msg(workspace, fb, "d1", kind="dm", convo="t1", minutes_ago=60 * 30)
        client.force_login(operator)
        html = client.get(url("chat_detail", workspace, row)).content.decode()
        assert 'role="status"' in html and "Agente humano" in html
        assert "Responder como agente humano" in html
        assert "aria-describedby" in html

    def test_open_window_has_no_agent_option(self, client, operator, workspace, fb):
        row = msg(workspace, fb, "d1", kind="dm", convo="t1", minutes_ago=30)
        client.force_login(operator)
        assert "Responder como agente humano" not in client.get(url("chat_detail", workspace, row)).content.decode()

    def test_closed_chat_disables_sending(self, client, operator, workspace, fb):
        row = msg(workspace, fb, "d1", kind="dm", convo="t1", minutes_ago=60 * 24 * 9)
        client.force_login(operator)
        html = client.get(url("chat_detail", workspace, row)).content.decode()
        assert "No puedes responder a esta conversación" in html
        assert "Responder como agente humano" not in html
        assert "disabled" in html


@pytest.mark.django_db
class TestThread:
    def test_the_thread_lists_the_posts_comments_nested_replies_and_actions(self, client, operator, workspace, ig):
        root = msg(workspace, ig, "c1", post="p1", body="Qué buen reel")
        msg(workspace, ig, "c2", post="p1", body="Otro comentario")
        msg(workspace, ig, "c3", post="p9", body="De otra publicación")
        msg(workspace, ig, "r1", post="p1", outbound=True, parent_message=root, body="Gracias")
        client.force_login(operator)
        html = client.get(url("thread_detail", workspace, root) + "?hl=1").content.decode()
        assert "Qué buen reel" in html and "Otro comentario" in html and "Gracias" in html
        assert "De otra publicación" not in html
        for label in ("Responder", "Ocultar", "Borrar", "Marcar contestado"):
            assert label in html

    def test_youtube_keeps_the_hide_button(self, client, operator, workspace, yt):
        root = msg(workspace, yt, "c1", post="v1")
        client.force_login(operator)
        html = client.get(url("thread_detail", workspace, root)).content.decode()
        assert "Ocultar" in html and "Responder" in html

    def test_hidden_and_deleted_comments_show_their_pills(self, client, operator, workspace, ig):
        root = msg(workspace, ig, "c1", post="p1", is_hidden=True)
        msg(workspace, ig, "c2", post="p1", deleted_on_platform=timezone.now())
        client.force_login(operator)
        html = client.get(url("thread_detail", workspace, root)).content.decode()
        assert "ib-k-oculto" in html and "ib-k-borrado" in html

    def test_opening_a_thread_marks_its_comments_read(self, client, operator, workspace, ig):
        root = msg(workspace, ig, "c1", post="p1")
        client.force_login(operator)
        client.get(url("thread_detail", workspace, root))
        root.refresh_from_db()
        assert root.status == InboxMessage.Status.OPEN


@pytest.mark.django_db
class TestReplying:
    def _post(self, client, workspace, row, **data):
        data = {"body": "gracias", "ui": "1", **data}
        with patch("apps.inbox.views.get_provider") as get_provider:
            get_provider.return_value.reply_to_message.return_value.platform_message_id = "r1"
            response = client.post(url("send_reply", workspace, row), data)
        return response, get_provider

    def test_a_reply_removes_the_item_from_no_contestados(self, client, operator, workspace, ig):
        row = msg(workspace, ig, "c1", post="p1")
        client.force_login(operator)
        assert counts(client.get(feed(workspace)))["todo"] == 1
        response, _ = self._post(client, workspace, row)
        assert response.status_code == 200
        assert "ib-cm" in response.content.decode()  # vuelve el hilo, no el panel antiguo
        after = client.get(feed(workspace))
        assert counts(after)["todo"] == 0 and after.context["items"] == []
        assert InboxReply.objects.filter(inbox_message=row).exists()

    def test_a_failure_is_a_200_that_targets_the_failed_notice(self, client, operator, workspace, ig):
        row = msg(workspace, ig, "c1", post="p1")
        client.force_login(operator)
        with patch("apps.inbox.views.get_provider") as get_provider:
            get_provider.return_value.reply_to_message.side_effect = RuntimeError("nope")
            response = client.post(url("send_reply", workspace, row), {"body": "x", "ui": "1"})
        assert response.status_code == 200
        assert response["HX-Retarget"] == f"#reply-err-{row.id}"
        assert 'role="alert"' in response.content.decode() and "Fallido" in response.content.decode()
        row.refresh_from_db()
        assert row.awaiting_reply is True

    def test_after_24_hours_the_agent_box_is_required(self, client, operator, workspace, fb):
        row = msg(workspace, fb, "d1", kind="dm", convo="t1", minutes_ago=60 * 30)
        client.force_login(operator)
        response, provider = self._post(client, workspace, row)
        assert "agente humano" in response.content.decode()
        provider.return_value.reply_to_message.assert_not_called()
        response, provider = self._post(client, workspace, row, human_agent="1")
        provider.return_value.reply_to_message.assert_called_once()
        assert "ib-chat" in response.content.decode()
        assert InboxMessage.objects.filter(conversation_id="t1", awaiting_reply=True).count() == 0

    def test_the_old_panel_contract_is_untouched_without_ui(self, client, operator, workspace, ig):
        row = msg(workspace, ig, "c1", post="p1")
        client.force_login(operator)
        with patch("apps.inbox.views.get_provider") as get_provider:
            get_provider.return_value.reply_to_message.side_effect = RuntimeError("nope")
            response = client.post(url("send_reply", workspace, row), {"body": "x"})
        assert response.status_code == 422

    def test_mark_answered_clears_the_thread_without_sending(self, client, operator, workspace, ig):
        row = msg(workspace, ig, "c1", post="p1")
        client.force_login(operator)
        response = client.post(url("mark_answered", workspace, row), {"ui": "1"})
        assert response.status_code == 200
        row.refresh_from_db()
        assert row.awaiting_reply is False
        assert not InboxReply.objects.exists()

    def test_hiding_with_ui_returns_the_thread(self, client, operator, workspace, ig):
        row = msg(workspace, ig, "c1", post="p1")
        client.force_login(operator)
        with (
            patch("apps.inbox.views._moderation_provider", return_value=(patch_provider(), "tok")),
        ):
            response = client.post(url("toggle_hidden", workspace, row), {"ui": "1"})
        assert response.status_code == 200
        row.refresh_from_db()
        assert row.is_hidden is True
        assert "ib-k-oculto" in response.content.decode()


def patch_provider():
    from unittest.mock import MagicMock

    return MagicMock()


@pytest.mark.django_db
class TestIsPending:
    def test_it_mirrors_pending_q(self, workspace, ig):
        rows = [
            msg(workspace, ig, "a"),
            msg(workspace, ig, "b", awaiting=False),
            msg(workspace, ig, "c", status="resolved"),
            msg(workspace, ig, "d", deleted_on_platform=timezone.now()),
            msg(workspace, ig, "e", outbound=True),
        ]
        in_db = set(InboxMessage.objects.filter(InboxMessage.pending_q()).values_list("id", flat=True))
        assert {r.id for r in rows if r.is_pending} == in_db


@pytest.mark.django_db
class TestBlankFilters:
    def test_blank_sentinels_do_not_empty_the_inbox(self, client, operator, workspace, fb):
        msg(workspace, fb, "c1", post="p1")
        client.force_login(operator)
        response = client.get(feed(workspace), {"platform": "", "account": "", "only": "", "mine": ""})
        assert counts(response)["todo"] == 1 and len(response.context["items"]) == 1


@pytest.mark.django_db
class TestSearch:
    def test_matches_body_sender_and_handle_across_tabs(self, client, operator, workspace, fb):
        msg(workspace, fb, "c1", post="p1", body="Quiero una cotización", sender="Ana", handle="ana_x")
        msg(workspace, fb, "c2", post="p1", body="otra cosa", sender="Beto", handle="beto_y")
        msg(workspace, fb, "d1", kind="dm", convo="t1", body="hola precio", sender="Carla", handle="carla_z")
        client.force_login(operator)
        for q, todo in [("cotiza", 1), ("BETO", 1), ("beto_y", 1), ("carla_z", 1), ("precio", 1), ("zzzz", 0)]:
            assert counts(client.get(feed(workspace, q=q)))["todo"] == todo, q
        assert len(client.get(feed(workspace, tab="com", q="ana_x")).context["posts"]) == 1
        assert client.get(feed(workspace, tab="chat", q="nada")).context["chats"] == []
        assert len(client.get(feed(workspace, tab="chat", q="Carla")).context["chats"]) == 1

    def test_the_query_is_kept_in_the_form(self, client, operator, workspace, fb):
        msg(workspace, fb, "c1", post="p1")
        client.force_login(operator)
        assert 'value="hola"' in client.get(feed(workspace, q="hola")).content.decode()


@pytest.mark.django_db
class TestSavedReplies:
    def test_templates_are_rendered_with_their_variables(self, client, operator, workspace, ig):
        from apps.inbox.models import SavedReply

        SavedReply.objects.create(
            workspace=workspace, title="Hola", body="Hola {sender_name}, gracias desde {account_name}"
        )
        root = msg(workspace, ig, "c1", post="p1", sender="Ana")
        client.force_login(operator)
        html = client.get(url("thread_detail", workspace, root)).content.decode()
        assert "data-ib-saved" in html and "Hola Ana, gracias desde Igprof" in html
        dm = msg(workspace, ig, "d1", kind="dm", convo="t1", sender="Luz")
        chat = client.get(url("chat_detail", workspace, dm)).content.decode()
        assert "Hola Luz, gracias desde Igprof" in chat

    def test_no_templates_no_button(self, client, operator, workspace, ig):
        root = msg(workspace, ig, "c1", post="p1")
        client.force_login(operator)
        assert "data-ib-saved" not in client.get(url("thread_detail", workspace, root)).content.decode()


@pytest.mark.django_db
class TestNotesAndAssign:
    def test_notes_are_listed_and_added_from_the_thread(self, client, operator, workspace, ig):
        from apps.inbox.models import InternalNote

        root = msg(workspace, ig, "c1", post="p1")
        InternalNote.objects.create(inbox_message=root, author=operator, body="Cliente VIP")
        client.force_login(operator)
        html = client.get(url("thread_detail", workspace, root)).content.decode()
        assert "Cliente VIP" in html and "Solo el equipo" in html
        response = client.post(url("add_note", workspace, root), {"body": "segunda", "ui": "1"})
        assert response.status_code == 200 and "segunda" in response.content.decode()
        assert InternalNote.objects.filter(inbox_message=root).count() == 2

    def test_a_note_is_never_sent_to_the_platform(self, client, operator, workspace, ig):
        root = msg(workspace, ig, "c1", post="p1")
        client.force_login(operator)
        with patch("apps.inbox.views.get_provider") as get_provider:
            client.post(url("add_note", workspace, root), {"body": "x", "ui": "1"})
        get_provider.assert_not_called()

    def test_notes_show_in_the_chat(self, client, operator, workspace, fb):
        from apps.inbox.models import InternalNote

        dm = msg(workspace, fb, "d1", kind="dm", convo="t1")
        InternalNote.objects.create(inbox_message=dm, author=operator, body="Llamar mañana")
        client.force_login(operator)
        assert "Llamar mañana" in client.get(url("chat_detail", workspace, dm)).content.decode()

    def test_assigning_from_the_new_ui_returns_the_thread(self, client, operator, workspace, ig):
        root = msg(workspace, ig, "c1", post="p1")
        client.force_login(operator)
        html = client.get(url("thread_detail", workspace, root)).content.decode()
        assert "Asignar" in html
        response = client.post(url("assign", workspace, root), {"assigned_to": str(operator.id), "ui": "1"})
        assert response.status_code == 200 and "ib-cm" in response.content.decode()
        root.refresh_from_db()
        assert root.assigned_to == operator
        assert counts(client.get(feed(workspace, mine=1)))["todo"] == 1

    def test_a_reader_gets_no_note_form(self, client, django_user_model, workspace, ig):
        user = django_user_model.objects.create_user(
            email="v@example.com", password="pw12345!", tos_accepted_at=timezone.now()
        )
        OrgMembership.objects.update_or_create(
            user=user, organization=workspace.organization, defaults={"org_role": OrgMembership.OrgRole.MEMBER}
        )
        WorkspaceMembership.objects.create(user=user, workspace=workspace, workspace_role="viewer")
        root = msg(workspace, ig, "c1", post="p1")
        client.force_login(user)
        assert "Agregar nota" not in client.get(url("thread_detail", workspace, root)).content.decode()


@pytest.mark.django_db
class TestChatNotesSpanTheConversation:
    def test_a_note_stays_visible_after_a_newer_inbound_message(self, client, operator, workspace, fb, ig):
        from apps.inbox.models import InternalNote

        first = msg(workspace, fb, "d1", kind="dm", convo="t1", minutes_ago=60)
        InternalNote.objects.create(inbox_message=first, author=operator, body="Nota vieja")
        latest = msg(workspace, fb, "d2", kind="dm", convo="t1", minutes_ago=5)
        InternalNote.objects.create(inbox_message=latest, author=operator, body="Nota nueva")
        other = msg(workspace, ig, "d3", kind="dm", convo="t1", minutes_ago=5)
        InternalNote.objects.create(inbox_message=other, author=operator, body="Otra cuenta")
        client.force_login(operator)
        html = client.get(url("chat_detail", workspace, latest)).content.decode()
        assert html.index("Nota vieja") < html.index("Nota nueva")
        assert "Otra cuenta" not in html


@pytest.mark.django_db
class TestVerMas:
    def test_no_contestados_pages_by_hundreds(self, client, operator, workspace, fb):
        for i in range(105):
            msg(workspace, fb, f"c{i}", post="p1", minutes_ago=i)
        client.force_login(operator)
        first = client.get(feed(workspace))
        assert len(first.context["items"]) == 100 and first.context["more_n"] == 200
        more = client.get(feed(workspace, n=200))
        assert len(more.context["items"]) == 105 and more.context["more_n"] == 0
        assert len(client.get(feed(workspace, n="zz")).context["items"]) == 100
