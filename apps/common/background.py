"""Shared helper for registering recurring django-background-tasks.

Centralizes the idempotent ``post_migrate`` registration pattern so each app
calls one function instead of copy-pasting the exists-check + error handling.
"""

import logging
from datetime import timedelta

from django.db import OperationalError, ProgrammingError
from django.utils import timezone

logger = logging.getLogger(__name__)


def register_recurring_task(task_func, *, repeat, verbose_name):
    """Idempotently schedule a ``@background`` task to repeat every ``repeat`` seconds.

    Registration is keyed by ``verbose_name``. When a row already exists but its
    cadence differs from the code, the row is reconciled rather than left alone —
    otherwise editing the interval constant would be silently inert forever, since
    ``create_repetition`` copies ``repeat`` from the existing row on every run.
    The code is the source of truth, so a ``repeat`` hand-edited in the Django
    admin is reset on the next ``migrate``.

    Safe to call from a ``post_migrate`` handler. A fresh DB without the
    background-task tables raises a DB error, which we swallow quietly (the next
    ``migrate`` re-runs registration). Any OTHER failure is logged at
    ``exception`` rather than swallowed silently — the worker is the only thing
    that runs these, so a missed registration means the task never runs, and
    that must be visible (production logs the ``apps`` logger at INFO).
    """
    from background_task.models import Task

    try:
        existing = Task.objects.filter(verbose_name=verbose_name).first()
        if existing is None:
            task_func(repeat=repeat, verbose_name=verbose_name)
            logger.info("Registered recurring task %s (every %ds)", verbose_name, repeat)
        elif existing.repeat != repeat:
            # Pull ``run_at`` in as well: a cadence that shrinks a lot would
            # otherwise wait out the old long interval once before speeding up.
            updates: dict = {"repeat": repeat}
            soonest = timezone.now() + timedelta(seconds=repeat)
            if existing.run_at > soonest:
                updates["run_at"] = soonest
            Task.objects.filter(pk=existing.pk).update(**updates)
            logger.info(
                "Updated recurring task %s cadence: %ds -> %ds",
                verbose_name,
                existing.repeat,
                repeat,
            )
    except (OperationalError, ProgrammingError):
        # Fresh DB: the background-task tables don't exist yet. The next migrate
        # re-runs this registration, so skip quietly.
        logger.debug("Skipping %s registration (database not ready)", verbose_name)
    except Exception:
        logger.exception("Failed to register recurring task %s", verbose_name)
