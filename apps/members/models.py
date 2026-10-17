import secrets
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.common.managers import OrgScopedManager


class OrgMembership(models.Model):
    class OrgRole(models.TextChoices):
        OWNER = "owner", "Owner"
        ADMIN = "admin", "Admin"
        MEMBER = "member", "Member"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="org_memberships",
    )
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    org_role = models.CharField(max_length=20, choices=OrgRole.choices, default=OrgRole.MEMBER)
    invited_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(blank=True, null=True)

    objects = OrgScopedManager()

    class Meta:
        db_table = "members_org_membership"
        unique_together = [("user", "organization")]

    def __str__(self):
        return f"{self.user.email} - {self.organization.name} ({self.org_role})"


# Roles that stay in the database but are no longer offered in the UI.
#
# `client` backs the client portal and the approval flow, both of which are kept
# but switched off; `contributor` predates the current hierarchy and has no
# distinct meaning next to `editor`. Leaving them assignable meant a six-option
# picker where only four choices mean anything, and a role picked by mistake is
# hard to notice. They stay in the model so existing rows and the dormant portal
# keep working — this is about what can be *granted*, not what exists.
RETIRED_WORKSPACE_ROLES = frozenset({"client", "contributor"})

# Workspace roles, weakest to strongest. Lived inline in members/decorators.py;
# moved here because channel assignment needs it too — a manager may only hand
# out channels they hold themselves, which is a comparison between two roles.
#
# Only the order matters, not the numbers. `reviewer` sits above `viewer`
# because it can approve, and below `editor` because it cannot create.
WORKSPACE_ROLE_ORDER = {
    "owner": 70,
    "manager": 60,
    "editor": 50,
    "reviewer": 40,
    "contributor": 30,
    "client": 20,
    "viewer": 10,
}


def role_rank(role: str) -> int:
    """Strength of a workspace role. Unknown roles rank below everything."""
    return WORKSPACE_ROLE_ORDER.get(role, 0)


class WorkspaceMembership(models.Model):
    class WorkspaceRole(models.TextChoices):
        OWNER = "owner", "Owner"
        MANAGER = "manager", "Manager"
        EDITOR = "editor", "Editor"
        # Approves, and reads what it was given. Exists so the person who owns a
        # personal account can sign off on their own posts without being handed
        # the run of the workspace: reviewer plus a single assigned channel sees
        # and approves only their own.
        REVIEWER = "reviewer", "Reviewer"
        CONTRIBUTOR = "contributor", "Contributor"
        CLIENT = "client", "Client"
        VIEWER = "viewer", "Viewer"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="workspace_memberships",
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    workspace_role = models.CharField(
        max_length=20,
        choices=WorkspaceRole.choices,
        default=WorkspaceRole.VIEWER,
    )
    custom_role = models.ForeignKey(
        "CustomRole",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="memberships",
    )
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "members_workspace_membership"
        unique_together = [("user", "workspace")]

    def __str__(self):
        role = self.custom_role.name if self.custom_role else self.workspace_role
        return f"{self.user.email} - {self.workspace.name} ({role})"

    @property
    def effective_permissions(self):
        """Return the effective permission dict for this membership."""
        if self.custom_role:
            return self.custom_role.permissions
        return BUILTIN_ROLE_PERMISSIONS.get(self.workspace_role, {})


class CustomRole(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="custom_roles",
    )
    name = models.CharField(max_length=100)
    permissions = models.JSONField(
        default=dict,
        help_text="Permission keys mapped to booleans",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = OrgScopedManager()

    class Meta:
        db_table = "members_custom_role"
        unique_together = [("organization", "name")]

    def __str__(self):
        return f"{self.name} ({self.organization.name})"


def _generate_invitation_token():
    return secrets.token_urlsafe(32)


class Invitation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="invitations",
    )
    email = models.EmailField()
    org_role = models.CharField(
        max_length=20,
        choices=OrgMembership.OrgRole.choices,
        default=OrgMembership.OrgRole.MEMBER,
    )
    workspace_assignments = models.JSONField(
        default=list,
        help_text='List of {"workspace_id": "...", "role": "..."}',
    )
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="sent_invitations",
    )
    token = models.CharField(max_length=255, unique=True, default=_generate_invitation_token)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = OrgScopedManager()

    class Meta:
        db_table = "members_invitation"

    def __str__(self):
        return f"Invitation to {self.email} for {self.organization.name}"

    @property
    def is_expired(self):
        return timezone.now() > self.expires_at

    @property
    def is_accepted(self):
        return self.accepted_at is not None


# Roles that exist but can never be *granted* through the interface.
#
# Owner is not a role you hand out: a workspace owner sees every channel without
# any assignment, so granting it is the single move that undoes the whole
# channel-access layer. It is set when the workspace is created and moved by
# ownership transfer, which demotes the previous owner rather than adding a
# second one. `services._reject_workspace_owner` is the enforcement; this only
# keeps the picker from offering it.
UNGRANTABLE_WORKSPACE_ROLES = frozenset({"owner"})


def assignable_workspace_role_choices(granter_role=None):
    """Workspace roles that may be granted, in hierarchy order.

    ``granter_role`` narrows the list to what that role may hand out: strictly
    below itself. An owner offers Manager down; a manager offers Editor down and
    never another manager, because two managers able to promote each other is a
    loop with nobody above it.

    Passing None returns everything grantable — for contexts that are about the
    set of roles rather than about one person handing out another's.
    """
    excluded = RETIRED_WORKSPACE_ROLES | UNGRANTABLE_WORKSPACE_ROLES
    choices = [(value, label) for value, label in WorkspaceMembership.WorkspaceRole.choices if value not in excluded]
    if granter_role is None:
        return choices
    ceiling = role_rank(granter_role)
    return [(value, label) for value, label in choices if role_rank(value) < ceiling]


# What each role means, in the words someone picking one would use.
#
# Kept honest against BUILTIN_ROLE_PERMISSIONS below: a description that
# overstates a role is worse than none, because it is how someone ends up handed
# less access than the person granting it believed they were giving.
WORKSPACE_ROLE_DESCRIPTIONS = {
    "owner": "Everything, including connecting channels and handing out access.",
    "manager": "Runs the brand: creates, approves, publishes and edits its settings. Cannot connect channels.",
    "editor": "Creates and schedules posts, and answers the inbox. Cannot approve or publish.",
    "reviewer": "Approves posts and reads the inbox. Creates nothing and does not reply.",
    "viewer": "Reads everything they are given, inbox included. Does not reply and changes nothing.",
}


def assignable_workspace_role_descriptions(granter_role=None):
    """``(value, label, description)`` for each role ``granter_role`` may grant."""
    return [
        (value, label, WORKSPACE_ROLE_DESCRIPTIONS.get(value, ""))
        for value, label in assignable_workspace_role_choices(granter_role)
    ]


# Built-in workspace role permission mappings
PERMISSION_KEYS = [
    "create_posts",
    "edit_others_posts",
    "approve_posts",
    "publish_directly",
    "manage_social_accounts",
    "view_analytics",
    "use_inbox",
    "reply_from_inbox",
    "manage_workspace_settings",
    "upload_media",
    "edit_media",
    "delete_media",
    "manage_media",
]

BUILTIN_ROLE_PERMISSIONS = {
    "owner": {k: True for k in PERMISSION_KEYS},
    "manager": {
        "create_posts": True,
        "edit_others_posts": True,
        "approve_posts": True,
        "publish_directly": True,
        "manage_social_accounts": True,
        "view_analytics": True,
        "use_inbox": True,
        "reply_from_inbox": True,
        # True as of the settings audit. It was False while the workspace views
        # let a manager archive and delete the workspace anyway via their own
        # role check — so the matrix said one thing and the code did another, and
        # renaming was open to everyone because no check ran at all. One key now
        # decides all of it, including the inbox's saved replies and SLA.
        "manage_workspace_settings": True,
        "upload_media": True,
        "edit_media": True,
        "delete_media": True,
        "manage_media": True,
    },
    # Creates and answers. `edit_others_posts` used to be True here, which made
    # the role's own name misleading: an editor edits their work, not everyone's.
    # Reaching into a colleague's draft is a manager's call.
    "editor": {
        "create_posts": True,
        "edit_others_posts": False,
        "approve_posts": False,
        "publish_directly": False,
        "manage_social_accounts": False,
        "view_analytics": True,
        "use_inbox": True,
        "reply_from_inbox": True,
        "manage_workspace_settings": False,
        "upload_media": True,
        "edit_media": True,
        "delete_media": True,
        "manage_media": False,
    },
    # Reads and approves; creates nothing. `use_inbox` is on because approving a
    # post without being able to see what people said about the last one is
    # reviewing blind — but replying is off, since a reviewer is not the voice
    # of the channel.
    "reviewer": {
        "create_posts": False,
        "edit_others_posts": False,
        "approve_posts": True,
        "publish_directly": False,
        "manage_social_accounts": False,
        "view_analytics": True,
        "use_inbox": True,
        "reply_from_inbox": False,
        "manage_workspace_settings": False,
        "upload_media": False,
        "edit_media": False,
        "delete_media": False,
        "manage_media": False,
    },
    "contributor": {
        "create_posts": True,
        "edit_others_posts": False,
        "approve_posts": False,
        "publish_directly": False,
        "manage_social_accounts": False,
        "view_analytics": False,
        "use_inbox": False,
        "reply_from_inbox": False,
        "manage_workspace_settings": False,
        "upload_media": True,
        "edit_media": True,
        "delete_media": False,
        "manage_media": False,
    },
    "client": {
        "create_posts": False,
        "edit_others_posts": False,
        "approve_posts": True,
        "publish_directly": False,
        "manage_social_accounts": False,
        "view_analytics": True,
        "use_inbox": False,
        "reply_from_inbox": False,
        "manage_workspace_settings": False,
        "upload_media": False,
        "edit_media": False,
        "delete_media": False,
        "manage_media": False,
    },
    # Reads everything they were given, changes nothing. `use_inbox` is on:
    # "viewer" that cannot open the inbox is not a viewer, it is a role with a
    # hole in it — and reading the inbox changes nothing, while `reply_from_inbox`
    # stays off so they are never the voice of the channel.
    #
    # This is the read-only role, so every writing key here must stay False. If
    # a new permission is added to PERMISSION_KEYS, its answer for viewer is no.
    "viewer": {
        "create_posts": False,
        "edit_others_posts": False,
        "approve_posts": False,
        "publish_directly": False,
        "manage_social_accounts": False,
        "view_analytics": True,
        "use_inbox": True,
        "reply_from_inbox": False,
        "manage_workspace_settings": False,
        "upload_media": False,
        "edit_media": False,
        "delete_media": False,
        "manage_media": False,
    },
}


# ---------------------------------------------------------------------------
# Org-level permission model
# ---------------------------------------------------------------------------
# The workspace permission system above is workspace-scoped via
# ``WorkspaceMembership.effective_permissions``. The Intelligence integration
# needs ORG-scoped permission checks (subscriptions and Stripe billing are
# tied to the Organization, not any one workspace). Rather than expand the
# workspace system to do double duty, we introduce a parallel, narrower
# org-permission table keyed on ``OrgMembership.org_role``.
#
# Add new keys here as future features need org-scoped gating.

ORG_PERMISSION_KEYS = (("manage_api_keys", "Issue and revoke Agent API keys for any workspace in the org"),)


BUILTIN_ORG_PERMISSIONS = {
    OrgMembership.OrgRole.OWNER: {"manage_api_keys"},
    OrgMembership.OrgRole.ADMIN: {"manage_api_keys"},
    OrgMembership.OrgRole.MEMBER: set(),
}


def has_org_permission(membership, key):
    """Return True if ``membership`` grants the given org-permission key.

    ``membership`` is an ``OrgMembership`` or None (e.g., the user has no
    membership in the org being checked). None always returns False.
    """
    if membership is None:
        return False
    return key in BUILTIN_ORG_PERMISSIONS.get(membership.org_role, set())


# ── Channel assignment ────────────────────────────────────────────────
#
# Which member may do what on which connected channel. Orthogonal to the
# permission keys above, and both must pass: replying to a comment needs
# `reply_from_inbox` from the role *and* `can_reply` on that channel.
#
# Seven independent flags rather than a ladder, because the real cases do not
# nest. Someone who answers comments all day should not thereby be able to
# delete them, and the person who reads the numbers often should not post.

CHANNEL_CAPABILITIES = (
    "can_view_metrics",
    "can_reply",
    "can_publish",
    "can_moderate",
    "can_delete_comments",
)

# `can_approve` is deliberately absent, and the column stays on the table.
#
# Approving comes from the role — owner, manager and reviewer — and since
# visibility already goes by assignment, a reviewer only ever approves the
# channels they were given. The checkbox added nothing and its label ("Approve
# posts here even if their role would not") was false in both directions:
# ticking it granted nothing, and unticking it took nothing away from someone
# whose role already approved. Retired rather than dropped so existing rows and
# any future decision to revive it survive; no migration either way.
RETIRED_CHANNEL_CAPABILITIES = frozenset({"can_approve"})

# What each flag means to the person ticking the box. Written for whoever is
# handing out access, not for whoever wrote the model — "can_delete_comments"
# says nothing about the fact that it cannot be undone.
CHANNEL_CAPABILITY_LABELS = (
    ("can_view_metrics", "See metrics", "Analytics for this channel."),
    ("can_reply", "Reply", "Answer comments and messages as this channel."),
    ("can_publish", "Publish", "Create and schedule posts on this channel."),
    ("can_moderate", "Moderate", "Hide comments so nobody else sees them. Reversible."),
    ("can_delete_comments", "Delete comments", "Permanent. Cannot be undone."),
)

# What the *role* must already allow for a capability to mean anything.
#
# Both layers have to pass, so granting "Delete comments" to a Viewer stores a
# flag the server will refuse anyway — the box is offered, saved, and does
# nothing, which is worse than not offering it. The dialog reads this to decide
# which boxes to show for the person being edited.
#
# A permission key means "the role must hold this key"; MANAGER_AND_ABOVE means
# the role must rank at manager or higher, for the destructive one that no
# permission key covers.
MANAGER_AND_ABOVE = "__manager_and_above__"

CHANNEL_CAPABILITY_REQUIRES = {
    "can_view_metrics": "view_analytics",
    "can_reply": "reply_from_inbox",
    "can_publish": "create_posts",
    # Hiding is reversible and belongs to whoever works the inbox — an editor
    # looking at spam should not have to find a manager to hide it. Deleting is
    # permanent and stays with manager and above. The line is drawn at
    # "can this be undone", not at seniority for its own sake.
    "can_moderate": "reply_from_inbox",
    "can_delete_comments": MANAGER_AND_ABOVE,
}


def capabilities_available_to(membership):
    """``(key, label, help)`` for the capabilities this member's role can use.

    Everything else is not merely unticked — it is not offered, because ticking
    it would store a grant that the role-level check refuses regardless.
    """
    if membership is None:
        return []
    permissions = membership.effective_permissions
    rank = role_rank(membership.workspace_role)
    available = []
    for key, label, help_text in CHANNEL_CAPABILITY_LABELS:
        requirement = CHANNEL_CAPABILITY_REQUIRES.get(key)
        if requirement == MANAGER_AND_ABOVE:
            allowed = rank >= role_rank("manager")
        else:
            allowed = bool(permissions.get(requirement, False))
        if allowed:
            available.append((key, label, help_text))
    return available


class ChannelAssignment(models.Model):
    """Grants one workspace member access to one connected channel.

    The row existing *is* the view grant — there is no `can_view` flag, because
    an assignment that grants nothing would be a row with no meaning. The six
    flags above are what the member may do beyond looking.

    Visibility rule: only an owner sees every channel without an assignment.
    Manager, editor, reviewer and viewer see exactly what they were given. That
    is what lets a personal-account owner be a reviewer with one channel and see
    nothing else in the workspace.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    membership = models.ForeignKey(
        "WorkspaceMembership",
        on_delete=models.CASCADE,
        related_name="channel_assignments",
    )
    social_account = models.ForeignKey(
        "social_accounts.SocialAccount",
        on_delete=models.CASCADE,
        related_name="assignments",
    )

    can_view_metrics = models.BooleanField(
        default=False,
        help_text="Analytics for this channel. Separate from viewing the channel on purpose.",
    )
    can_reply = models.BooleanField(default=False, help_text="Reply to comments and messages.")
    can_publish = models.BooleanField(default=False, help_text="Create and schedule on this channel.")
    can_moderate = models.BooleanField(default=False, help_text="Hide comments or mark them as spam.")
    can_delete_comments = models.BooleanField(
        default=False,
        help_text="Delete comments outright. The most destructive of the six.",
    )
    can_approve = models.BooleanField(
        default=False,
        help_text="Approve posts for this channel even when the member's role would not.",
    )

    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="channel_assignments_made",
        help_text="Kept for the activity log: who handed this out.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "members_channel_assignment"
        unique_together = [("membership", "social_account")]
        indexes = [
            # The hot path: "which channels can this member see", run on every
            # inbox, composer and analytics page load.
            models.Index(fields=["membership"], name="chassign_membership_idx"),
        ]

    def __str__(self):
        return f"{self.membership.user.email} → {self.social_account}"

    @property
    def granted(self):
        """The capabilities this assignment actually grants, for display."""
        return [c for c in CHANNEL_CAPABILITIES if getattr(self, c)]

    def clean(self):
        """A member can only be assigned channels from their own workspace.

        Nothing in the schema prevents pairing a membership in workspace A with
        an account in workspace B, and that pairing would quietly hand someone
        another brand's inbox.
        """
        from django.core.exceptions import ValidationError

        if (
            self.membership_id
            and self.social_account_id
            and self.membership.workspace_id != self.social_account.workspace_id
        ):
            raise ValidationError("That channel belongs to a different workspace than this member.")
