"""Context processors for sidebar and global template data."""

from django.conf import settings
from django.db.models import Count, Q


def sidebar_context(request):
    """Inject sidebar data into every template context.

    Provides:
        sidebar_workspaces: list of workspace objects the user belongs to
        sidebar_channels: connected social accounts for the current workspace
        sidebar_connectable_platforms: platforms available to connect
    """
    if not hasattr(request, "user") or not request.user.is_authenticated:
        return {}

    from apps.members.models import WorkspaceMembership
    from apps.social_accounts.models import AnalyticsPlatformConfig, PlatformVisibility, SocialAccount
    from apps.social_accounts.platforms import enabled_platform_keys

    analytics_enabled_platforms = AnalyticsPlatformConfig.enabled_platforms()

    # User's workspaces (non-archived)
    workspace_memberships = (
        WorkspaceMembership.objects.filter(
            user=request.user,
            workspace__is_archived=False,
        )
        .select_related("workspace")
        .order_by("workspace__name")
    )
    sidebar_workspaces = [wm.workspace for wm in workspace_memberships]

    # Connected channels for the current workspace
    sidebar_channels = []
    sidebar_unhealthy_channels = []
    sidebar_connectable_platforms = []

    workspace = getattr(request, "workspace", None)

    # Who to ask when something is out of reach. "Contact an administrator" is
    # useless in a five-person agency; a name turns a dead end into one message.
    # Falls back to None, and the empty states then simply omit the sentence.
    workspace_owner = None
    if workspace:
        owner_membership = (
            WorkspaceMembership.objects.filter(workspace=workspace, workspace_role="owner")
            .select_related("user")
            .order_by("added_at")
            .first()
        )
        workspace_owner = owner_membership.user if owner_membership else None

    if workspace:
        from apps.composer.models import PlatformPost
        from apps.members.channels import visible_accounts

        # Scoped to what this member was actually assigned. It used to list every
        # channel in the workspace, so someone with no assignments saw the whole
        # roster in the sidebar — names, platforms and queued counts — while the
        # inbox and analytics showed them nothing. The sidebar contradicted the
        # page, and it was the sidebar that was leaking.
        membership = getattr(request, "workspace_membership", None)
        assigned = visible_accounts(membership, SocialAccount.objects.for_workspace(workspace.id))

        sidebar_channels = list(
            assigned.filter(connection_status=SocialAccount.ConnectionStatus.CONNECTED)
            .annotate(
                queued_post_count=Count(
                    "platform_posts",
                    filter=Q(platform_posts__status=PlatformPost.Status.SCHEDULED),
                )
            )
            .order_by("platform", "account_name")
        )

        sidebar_unhealthy_channels = list(
            assigned.filter(
                connection_status__in=[
                    SocialAccount.ConnectionStatus.DISCONNECTED,
                    SocialAccount.ConnectionStatus.ERROR,
                    SocialAccount.ConnectionStatus.TOKEN_EXPIRING,
                ]
            ).order_by("platform", "account_name")
        )

        # Connectable platforms: not yet connected in this workspace.
        #
        # Only for whoever may actually connect one. Every link here goes to
        # `social_accounts:connect`, which requires `manage_social_accounts` —
        # so for everyone else this was a menu of dead ends ending on a bare 403
        # page with no sidebar to get back from.
        #
        # "Not yet connected" is asked of the whole workspace, not of the
        # filtered sidebar list: with assignments applied, a channel someone was
        # never given would otherwise show up here as connectable.
        may_connect = bool(membership and membership.effective_permissions.get("manage_social_accounts", False))
        if may_connect:
            connected_platforms = set(
                SocialAccount.objects.for_workspace(workspace.id)
                .filter(connection_status=SocialAccount.ConnectionStatus.CONNECTED)
                .values_list("platform", flat=True)
            )
            hidden_platforms = set(
                PlatformVisibility.objects.filter(is_visible=False).values_list("platform", flat=True)
            )
            sidebar_connectable_platforms = [
                (p, label)
                for p, label in _platform_display_names()
                if p not in connected_platforms and p not in hidden_platforms and p in enabled_platform_keys()
            ]

    # Unread inbox count for sidebar badge
    sidebar_unread_inbox_count = 0
    if workspace:
        from apps.inbox.models import InboxMessage

        # Only channels this member can see, and only what still needs an answer
        # (not our own messages, not thread replies, not resolved ones).
        sidebar_unread_inbox_count = (
            InboxMessage.objects.for_workspace(workspace.id)
            .filter(InboxMessage.pending_q(), social_account__in=assigned)
            .count()
        )

    # Pending approval count for badge
    sidebar_pending_approvals = 0
    if workspace:
        from apps.composer.models import PlatformPost

        sidebar_pending_approvals = (
            PlatformPost.objects.filter(
                post__workspace_id=workspace.id,
                status__in=["pending_review", "pending_client"],
            )
            .values("post_id")
            .distinct()
            .count()
        )

    # Idea columns and tags for the quick-create modal in the sidebar
    sidebar_idea_columns = []
    sidebar_idea_tags = []
    if workspace:
        from apps.composer.models import IdeaGroup, Tag

        groups = IdeaGroup.objects.for_workspace(workspace.id).order_by("position", "created_at")
        sidebar_idea_columns = [{"id": str(g.id), "label": g.name} for g in groups] if groups.exists() else []
        sidebar_idea_tags = list(Tag.objects.for_workspace(workspace.id).values_list("name", flat=True))

    # Workspace creation permission (org owners and admins only)
    can_create_workspace = False
    org_membership = getattr(request, "org_membership", None)
    if org_membership and org_membership.org_role in ("owner", "admin"):
        can_create_workspace = True

    # Filter the sidebar channel list to those whose platform is analytics-enabled,
    # so the per-channel deep-links under the Analytics nav item never point at a
    # disabled platform. The full sidebar channel list above is unrelated.
    analytics_sidebar_channels = [ch for ch in sidebar_channels if ch.platform in analytics_enabled_platforms]

    return {
        "sidebar_workspaces": sidebar_workspaces,
        "can_create_workspace": can_create_workspace,
        "sidebar_channels": sidebar_channels,
        "sidebar_unhealthy_channels": sidebar_unhealthy_channels,
        "sidebar_connectable_platforms": sidebar_connectable_platforms,
        "sidebar_unread_inbox_count": sidebar_unread_inbox_count,
        "sidebar_pending_approvals": sidebar_pending_approvals,
        "sidebar_idea_columns": sidebar_idea_columns,
        "sidebar_idea_tags": sidebar_idea_tags,
        "analytics_enabled_platforms": analytics_enabled_platforms,
        "analytics_sidebar_channels": analytics_sidebar_channels,
        # Who to ask when a screen is out of reach — see templates/common/_no_access.html.
        "workspace_owner": workspace_owner,
        # Advanced scheduling is built but unverified; templates gate on this so
        # nobody meets a half-wired feature. See BBS_SCHEDULING_ENABLED.
        "scheduling_enabled": getattr(settings, "BBS_SCHEDULING_ENABLED", False),
    }


def _platform_display_names():
    """Return list of (platform_key, display_name) tuples."""
    return [
        ("instagram", "Instagram"),
        ("facebook", "Facebook"),
        ("linkedin_personal", "LinkedIn (Personal)"),
        ("linkedin_company", "LinkedIn (Company)"),
        ("tiktok", "TikTok Business"),
        ("youtube", "YouTube"),
        ("pinterest", "Pinterest"),
        ("threads", "Threads"),
        ("bluesky", "Bluesky"),
        ("mastodon", "Mastodon"),
        ("twitter", "X (Twitter)"),
        ("google_business", "Google Business"),
    ]
