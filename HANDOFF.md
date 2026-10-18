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

---

## RETOMAR AQUÍ — 2026-10-17 (corte por límite de uso)

**Commits:** `6585f53` original BrightBean · `4d121bf` fase 1 (forma de trabajo, línea base, propuestas de marca). Juez Fase 1: PASA.

**Decisiones cerradas:** paleta **A · Oliva y petróleo** (hex en `docs/marca/propuestas-paleta.html` §A) · logo `static/marca/bean-by-ithos.svg` (+ `-oscuro`) · Instagram = `instagram.py` (vía Facebook) · plataformas por `DEFAULT_ENABLED_PLATFORMS` en `apps/social_accounts/platforms.py` (+ env `BBS_ENABLED_PLATFORMS`) · modo manual + `/cron/run` dormido.

**En curso SIN commit al cortar (2 implementadores en paralelo):**
- Fases 2+3: `platforms.py` (quitar instagram_login), `config/settings/base.py` (BBS_ENABLED_PLATFORMS, CRON_SECRET), `apps/common/background.py` (`drain_pending_tasks`), vista "Procesar pendientes" + `/cron/run`, publicar-ahora sin worker, `railway.toml`/`Procfile`/`.env.example`.
- Fase 4.3: paleta A en `theme/static_src/src/styles.css`, Figtree local en `static/fonts/figtree/`, logo/favicon, "BrightBean"→"BEAN" en templates, atribución AGPL en pies + `NOTICE` + README.

**Siguiente paso:**
1. `git status` / `git diff --stat` para ver qué quedó a medias de esos dos agentes.
2. Correr suite completa (credenciales PG: DATABASE_URL de `simu/.env` vía env DB_USER/DB_PASSWORD, no escribirlas): `pytest -q -p no:cacheprovider` → esperado solo los 2 fallos preexistentes; `ruff check .`; `mypy ...` (≤4 errores preexistentes).
3. Juez (Opus) sobre fases 2, 3 y 4.3 → commit por fase.
4. Luego Fase 5 (legal + `/legal/data-deletion/` + `docs/APP-REVIEW.md`) y Fase 6 (Sentry, MailerSend SMTP, Supabase, R2, Railway, GitHub público — push solo con confirmación).

**Ojo:** no correr dos pytest en paralelo (comparten `test_brightbean_test`). El original sigue en `OneDrive\Escritorio\bbs_unxio-main` hasta confirmación del usuario.
