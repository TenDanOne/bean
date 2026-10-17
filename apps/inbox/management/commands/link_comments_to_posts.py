"""Fill ``related_post`` on inbox messages that predate the link existing.

The field was declared long before anything wrote to it, so every comment synced
until now points at nothing. New messages are linked at sync time; this is for
the ones already in the table.

Safe to re-run: it only touches rows where ``related_post`` is empty, and a
comment on a post published outside this app has nothing to match and stays
empty, which is the correct answer rather than a failure.

    python manage.py link_comments_to_posts --dry-run
    python manage.py link_comments_to_posts
"""

from django.core.management.base import BaseCommand

from apps.inbox.grouping import resolve_related_post
from apps.inbox.models import InboxMessage


class Command(BaseCommand):
    help = "Link existing inbox comments to the posts they were left on."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be linked without writing anything.",
        )
        parser.add_argument(
            "--workspace",
            help="Limit to one workspace id.",
        )

    def handle(self, *args, **options):
        queryset = InboxMessage.objects.filter(related_post__isnull=True)
        if options["workspace"]:
            queryset = queryset.filter(workspace_id=options["workspace"])

        total = queryset.count()
        cache: dict = {}
        matched, updated = 0, []

        for message in queryset.iterator(chunk_size=500):
            post = resolve_related_post(message, cache)
            if post is None:
                continue
            matched += 1
            message.related_post = post
            updated.append(message)
            if len(updated) >= 500 and not options["dry_run"]:
                InboxMessage.objects.bulk_update(updated, ["related_post"])
                updated = []

        if updated and not options["dry_run"]:
            InboxMessage.objects.bulk_update(updated, ["related_post"])

        verb = "would link" if options["dry_run"] else "linked"
        self.stdout.write(
            self.style.SUCCESS(
                f"{matched} of {total} unlinked messages {verb} to a post. "
                f"{total - matched} have no post published through this app to match."
            )
        )
