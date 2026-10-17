# HANDOFF — BEAN by ITHOS

## Estado (2026-10-17): Fase 0 hecha, Fase 1 en curso
- Fase 0: copia de BrightBean Studio como base, commit `6585f53`, `.gitattributes` con LF.
- Fase 1 «forma de trabajo» (estilo SIMU/PAAX): `CLAUDE.md` con sección BEAN arriba, `vault/`, este `HANDOFF.md`, `.claude/hooks/validate-bash.{sh,mjs}` (adaptado a Django: bloquea `flush`/`reset_db`/`migrate <app> zero`/DROP), `.claude/settings.json` (permisos + hook; `launch.json` conservado) y `.claude/agents/` (`lector-vault`, `juez-bean`, `design-system-bean`). Sin commit todavía.
- Línea base de pruebas: `docs/linea-base-2026-10-17.md` (la crea otro agente).

## Fases siguientes
2. Ocultar plataformas con `DEFAULT_ENABLED_PLATFORMS` (`apps/social_accounts/platforms.py`, override `BBS_ENABLED_PLATFORMS`) (YouTube, Instagram, Facebook, TikTok activas; el resto oculto, no borrado).
3. Sin worker: botón «Procesar pendientes» + `POST /cron/run` (Bearer `CRON_SECRET`, 503 sin secreto, 401 bearer malo).
4. Marca: estilo ITHOS (isotipo, Figtree, paleta combinada BrightBean + ITHOS).
5. Kit legal + App Review (Meta, TikTok, Google): privacidad, términos, borrado de datos, demos.
6. Sentry, correo (MailerSend SMTP), Railway, Supabase, R2, GitHub `TenDanOne/bean`, dominio `bean.ithos.mx`.

## Pendiente / dudas
- Paleta final: la elige el usuario.
- Verificar requisitos exactos de App Review por plataforma (marcados «pendiente verificar» en `vault/Plataformas/`).
- Los tests requieren PostgreSQL (`brightbean_test`).

## RETOMAR AQUÍ
- Pasar `juez-bean` sobre la Fase 1 (archivos de arriba), hacer commit y empezar la Fase 2.

## Verificación
```bash
git status --short
pytest && ruff check . && ruff format --check . && mypy apps/ config/ providers/ tests/ --ignore-missing-imports
```
