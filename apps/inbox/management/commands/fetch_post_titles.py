"""Fetch titles for publications already in the inbox.

New comments get their title at sync time. This is for everything synced before
that existed — without it those folders are labelled with a raw video id.

Cheap: YouTube's videos.list is 1 quota unit per call and takes 50 ids, so a
channel with 200 distinct videos costs 4 units of a daily 10,000.

Safe to re-run. Only touches messages with no title stored, so a second run over
the same data makes no API calls at all.

    python manage.py fetch_post_titles --dry-run
    python manage.py fetch_post_titles --workspace <uuid>
"""

import logging

from django.core.management.base import BaseCommand

from apps.inbox.grouping import extract_platform_post_id
from apps.inbox.models import InboxMessage
from apps.social_accounts.models import SocialAccount
from providers import get_provider

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Fetch and store titles for the publications already in the inbox."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report without writing.")
        parser.add_argument("--workspace", help="Limit to one workspace id.")

    def handle(self, *args, **options):
        accounts = SocialAccount.objects.filter(connection_status=SocialAccount.ConnectionStatus.CONNECTED)
        if options["workspace"]:
            accounts = accounts.filter(workspace_id=options["workspace"])

        total_named = 0
        for account in accounts:
            named = self._handle_account(account, options["dry_run"])
            total_named += named

        verb = "would be named" if options["dry_run"] else "named"
        self.stdout.write(self.style.SUCCESS(f"{total_named} messages {verb}."))

    def _handle_account(self, account, dry_run):
        # Missing *either* field, not just the title.
        #
        # The first version only looked for an absent title, which meant every
        # message named before the publish date existed was skipped forever —
        # re-running the command could never fill in the date it had just
        # learned how to fetch.
        from django.db.models import Q

        pending = list(
            InboxMessage.objects.filter(social_account=account).filter(
                Q(extra__post_title__isnull=True) | Q(extra__post_published_at__isnull=True)
            )
        )
        wanted = {pid for pid in (extract_platform_post_id(m) for m in pending) if pid}
        if not wanted:
            return 0

        # _resolve_publish_credentials, not account.credentials: the latter is
        # this account's OAuth token blob, while get_provider needs the app's
        # client_id/secret. Every other caller resolves it this way.
        from apps.publisher.engine import _resolve_publish_credentials

        try:
            provider = get_provider(account.platform, _resolve_publish_credentials(account))
            details = provider.get_post_details(
                access_token=account.get_fresh_access_token(provider),
                post_ids=sorted(wanted),
            )
        except NotImplementedError:
            self.stdout.write(f"  {account.platform} has no title lookup; skipping {account}.")
            return 0
        except Exception as exc:
            # Loud, with the traceback: a silent warning here is why this looked
            # like "the titles just don't work".
            self.stdout.write(self.style.ERROR(f"  {account}: {type(exc).__name__}: {exc}"))
            logger.exception("fetch_post_titles failed for %s", account)
            return 0

        if not details:
            self.stdout.write(f"  {account}: no titles resolved for {len(wanted)} publications.")
            return 0

        updated = []
        for message in pending:
            post_id = extract_platform_post_id(message)
            found = details.get(post_id) if post_id else None
            if found:
                message.extra["post_title"] = found.get("title", "")
                if found.get("published_at"):
                    message.extra["post_published_at"] = found["published_at"]
                updated.append(message)

        if updated and not dry_run:
            InboxMessage.objects.bulk_update(updated, ["extra"])

        self.stdout.write(f"  {account}: {len(details)} of {len(wanted)} publications named ({len(updated)} messages).")
        return len(updated)
