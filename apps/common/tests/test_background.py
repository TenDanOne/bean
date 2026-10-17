"""register_recurring_task must reconcile a cadence changed in code.

Registration is keyed by ``verbose_name`` and ``create_repetition`` copies
``repeat`` from the existing row on every run, so without reconciliation an
edited interval constant would never take effect on an installed deployment.
"""

from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from background_task.models import Task
from django.utils import timezone

from apps.common.background import register_recurring_task


def _make_task(verbose_name, *, repeat, run_at):
    return Task.objects.create(
        task_name="apps.inbox.tasks.run_inbox_sync_cycle",
        task_params="[[], {}]",
        task_hash="hash-1",
        verbose_name=verbose_name,
        repeat=repeat,
        run_at=run_at,
    )


@pytest.mark.django_db
class TestRegisterRecurringTask:
    def test_creates_when_missing(self):
        task_func = MagicMock()

        register_recurring_task(task_func, repeat=60, verbose_name="brand_new")

        task_func.assert_called_once_with(repeat=60, verbose_name="brand_new")

    def test_does_not_recreate_when_present(self):
        _make_task("already_there", repeat=60, run_at=timezone.now() + timedelta(seconds=30))
        task_func = MagicMock()

        register_recurring_task(task_func, repeat=60, verbose_name="already_there")

        task_func.assert_not_called()
        assert Task.objects.filter(verbose_name="already_there").count() == 1

    def test_updates_repeat_when_cadence_changed(self):
        task = _make_task("cadence_change", repeat=300, run_at=timezone.now() + timedelta(seconds=30))
        task_func = MagicMock()

        register_recurring_task(task_func, repeat=60, verbose_name="cadence_change")

        task.refresh_from_db()
        assert task.repeat == 60
        # No duplicate row — the existing one is reconciled in place.
        task_func.assert_not_called()
        assert Task.objects.filter(verbose_name="cadence_change").count() == 1

    def test_pulls_run_at_in_when_cadence_shrinks(self):
        """A far-future run_at would otherwise wait out the old long interval once."""
        far_future = timezone.now() + timedelta(hours=6)
        task = _make_task("shrinking", repeat=86400, run_at=far_future)

        register_recurring_task(MagicMock(), repeat=60, verbose_name="shrinking")

        task.refresh_from_db()
        assert task.repeat == 60
        assert task.run_at < far_future
        assert task.run_at <= timezone.now() + timedelta(seconds=60)

    def test_leaves_an_imminent_run_at_alone(self):
        soon = timezone.now() + timedelta(seconds=5)
        task = _make_task("imminent", repeat=300, run_at=soon)

        register_recurring_task(MagicMock(), repeat=60, verbose_name="imminent")

        task.refresh_from_db()
        assert task.repeat == 60
        # Already sooner than now+repeat, so don't push it back.
        assert task.run_at == soon

    def test_same_cadence_touches_nothing(self):
        run_at = timezone.now() + timedelta(hours=6)
        task = _make_task("unchanged", repeat=3600, run_at=run_at)

        register_recurring_task(MagicMock(), repeat=3600, verbose_name="unchanged")

        task.refresh_from_db()
        assert task.repeat == 3600
        assert task.run_at == run_at
