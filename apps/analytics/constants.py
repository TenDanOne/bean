"""Cross-module constants for the analytics app."""

from __future__ import annotations

# Platforms whose APIs don't expose aggregate analytics. The analytics
# page renders a per-platform "not available" variant instead of zeroed-
# out KPI cards and charts.
#
# Each entry MUST also have ``BACKFILL_DAYS_PER_PLATFORM[<platform>] = 0``
# in ``apps/analytics/tasks.py`` — otherwise the background cron will
# still try to fetch metrics that don't exist.
NO_ANALYTICS_PLATFORMS: dict[str, str] = {
    "linkedin_personal": (
        "LinkedIn no ofrece analíticas de perfiles personales. Solo las páginas de empresa las tienen."
    ),
    "bluesky": ("El protocolo AT de Bluesky no ofrece analíticas agregadas de publicaciones."),
    "mastodon": ("La API de Mastodon no ofrece analíticas agregadas de publicaciones."),
}
