# BEAN by ITHOS · Bóveda

Fork personal/no comercial de BrightBean Studio (Django 5 + HTMX + Alpine + Tailwind 4, AGPL-3.0). Creada a mano el **2026-10-17** para no releer el código. `lector-vault` (Haiku) la lee **primero**. Estado vivo en `HANDOFF.md`.

## Arquitectura
- [[Arquitectura]] · resumen del CLAUDE.md original con enlaces a archivos.

## Decisiones
- [[Decisiones/Plataformas]]
- [[Decisiones/Sin-worker]]
- [[Decisiones/Marca]]
- [[Decisiones/Despliegue]]
- [[Decisiones/AGPL]]

## Plataformas activas
- [[Plataformas/YouTube]]
- [[Plataformas/Instagram]]
- [[Plataformas/Facebook]]
- [[Plataformas/TikTok]]

## Reglas rápidas
- Regla de oro: no romper lo existente; cada fase cierra con pytest + ruff + mypy vs `docs/linea-base-2026-10-17.md`.
- Hermanos de referencia (solo leer): `C:\Users\ultra\Proyectos\simu`, `C:\Users\ultra\Proyectos\paax`.
- Agentes: `.claude/agents/` (lector-vault, juez-bean, design-system-bean) + implementador/revisor globales.

## Notas manuales
