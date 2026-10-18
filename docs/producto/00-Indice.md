# Índice de los documentos de BEAN by ITHOS

- Fecha: 2026-10-18
- Estado: v1
- Base: mapa de `docs/`; no introduce decisiones nuevas.

## Documentos

| Documento | Qué es | Para quién |
|---|---|---|
| `producto/01-PRD.md` | Para qué es BEAN, a quién sirve, alcance, funciones reales, riesgos y fuera de alcance | Dueño, legal |
| `producto/03-Flujo-de-la-app.md` | Mapa de páginas, recorridos y estados de un post | Producto, QA |
| `GUIA-USO.md` | Manual paso a paso para quien usa BEAN | Quien publica |
| `APP-REVIEW.md` | Permisos, textos de justificación, URLs, guion de screencast y brechas para Meta y TikTok | Dueño, quien envía la revisión |
| `marca/manual.html` | Manual de marca mínimo (logos, paleta A, Figtree, usos prohibidos) | Diseño, quien toca UI |
| `marca/propuestas-paleta.html` | Las tres paletas comparadas; la A fue la elegida | Diseño |
| `linea-base-2026-10-17.md` | Resultado de pytest/ruff/mypy antes de tocar nada | Quien programa |

No existen (aún) `02-TRD`, `04-Design-Brief`, `05-Esquema-de-datos` ni `06-Plan`: el equivalente técnico es `CLAUDE.md`, `vault/` y `HANDOFF.md`.

## Orden de lectura sugerido

1. `01-PRD` (qué y por qué). 2. `03-Flujo` (cómo se recorre). 3. `GUIA-USO` (cómo se usa). 4. `APP-REVIEW` (antes de pedir permisos a Meta/TikTok). 5. `marca/manual.html` (si vas a tocar UI).

## Jerarquía ante conflicto

**Código > estos documentos > bóveda (`vault/`) y `HANDOFF.md`.** Si un documento contradice al código, se corrige el documento y se anota.

## Cómo se mantienen

Al cerrar una fase: actualizar el documento que cambie (flujo si hay página nueva, APP-REVIEW si cambia un permiso o se cierra una brecha, PRD si cambia una decisión) y subir la fecha de la cabecera.
