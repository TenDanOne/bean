# Decisión · Plataformas activas

- Activas: YouTube, Instagram (vía Facebook/Meta Graph), Facebook, TikTok.
- Las demás (LinkedIn, Mastodon, Bluesky, Pinterest, Threads, Google Business, Dev.to…) se **ocultan** con `DEFAULT_ENABLED_PLATFORMS` de `apps/social_accounts/platforms.py` (override por env `BBS_ENABLED_PLATFORMS`); **nunca se borran**: choices, migraciones y `providers/` quedan intactos.
- Implementación: Fase 2 (ver `HANDOFF.md`). Invariante vigilada por `juez-bean`.
- Providers y registro: `providers/__init__.py` (`PROVIDER_REGISTRY`), credenciales en `config/settings/base.py` (`PLATFORM_CREDENTIALS_FROM_ENV`).
- Notas: [[Plataformas/YouTube]] · [[Plataformas/Instagram]] · [[Plataformas/Facebook]] · [[Plataformas/TikTok]]
