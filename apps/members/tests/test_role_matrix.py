"""What each role can do, asserted against the matrix itself.

These read like restatements of `BUILTIN_ROLE_PERMISSIONS`, and that is the
point: the matrix is a wall of booleans where a single wrong one is invisible on
review, and two of them were wrong. An editor could edit everyone's drafts, and a
viewer could not open the inbox — so the role named for reading could not read
the one surface people actually wanted them in.

The descriptions shown when picking a role are checked against the same source,
because a description that overstates a role is how someone ends up with less
access than the person granting it believed they were giving.
"""

import pytest

from apps.members.models import (
    BUILTIN_ROLE_PERMISSIONS,
    PERMISSION_KEYS,
    WORKSPACE_ROLE_DESCRIPTIONS,
    assignable_workspace_role_choices,
)

WRITING_KEYS = [
    "create_posts",
    "edit_others_posts",
    "approve_posts",
    "publish_directly",
    "manage_social_accounts",
    "manage_workspace_settings",
    "reply_from_inbox",
    "upload_media",
    "edit_media",
    "delete_media",
    "manage_media",
]


class TestEditor:
    def test_creates_but_does_not_touch_other_peoples_posts(self):
        """The role is named for editing, which made the True easy to miss."""
        editor = BUILTIN_ROLE_PERMISSIONS["editor"]
        assert editor["create_posts"] is True
        assert editor["edit_others_posts"] is False

    def test_does_not_approve_or_publish(self):
        editor = BUILTIN_ROLE_PERMISSIONS["editor"]
        assert editor["approve_posts"] is False
        assert editor["publish_directly"] is False

    def test_answers_the_inbox(self):
        assert BUILTIN_ROLE_PERMISSIONS["editor"]["reply_from_inbox"] is True

    def test_only_a_manager_may_edit_someone_elses_work(self):
        assert BUILTIN_ROLE_PERMISSIONS["manager"]["edit_others_posts"] is True
        for role in ("editor", "reviewer", "viewer"):
            assert BUILTIN_ROLE_PERMISSIONS[role]["edit_others_posts"] is False, role


class TestViewer:
    def test_can_read_the_inbox_and_the_numbers(self):
        viewer = BUILTIN_ROLE_PERMISSIONS["viewer"]
        assert viewer["use_inbox"] is True
        assert viewer["view_analytics"] is True

    def test_changes_nothing_at_all(self):
        """Every writing permission, not a sample: this is the read-only role,
        so a new key defaulting to True here is a silent grant."""
        viewer = BUILTIN_ROLE_PERMISSIONS["viewer"]
        for key in WRITING_KEYS:
            assert viewer[key] is False, key

    def test_does_not_reply(self):
        """Reading the inbox and being the voice of the channel are different."""
        assert BUILTIN_ROLE_PERMISSIONS["viewer"]["reply_from_inbox"] is False


class TestTheMatrixIsComplete:
    def test_every_grantable_role_answers_every_permission(self):
        """A missing key reads as False through `.get`, so a role could quietly
        lose a permission by omission rather than by decision."""
        for value, _label in assignable_workspace_role_choices():
            permissions = BUILTIN_ROLE_PERMISSIONS[value]
            missing = [key for key in PERMISSION_KEYS if key not in permissions]
            assert missing == [], f"{value} does not answer {missing}"

    def test_the_owner_can_do_everything(self):
        assert all(BUILTIN_ROLE_PERMISSIONS["owner"][key] for key in PERMISSION_KEYS)


class TestDescriptions:
    def test_every_offered_role_explains_itself(self):
        for value, label in assignable_workspace_role_choices():
            assert WORKSPACE_ROLE_DESCRIPTIONS.get(value), f"{label} has no description"

    @pytest.mark.parametrize(
        "role,key,expected_absent",
        [
            # The claims that would be lies if the matrix drifted back.
            ("editor", "approve_posts", "aprobar"),
            ("editor", "publish_directly", "publicar"),
            ("viewer", "reply_from_inbox", "responde"),
        ],
    )
    def test_the_description_matches_the_matrix(self, role, key, expected_absent):
        assert BUILTIN_ROLE_PERMISSIONS[role][key] is False
        assert expected_absent in WORKSPACE_ROLE_DESCRIPTIONS[role].lower()
