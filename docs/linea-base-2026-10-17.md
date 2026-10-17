# Linea base de calidad 2026-10-17

Entorno: Python 3.13.7 (venv `.venv`, deps de requirements.txt), Windows 11, PostgreSQL 17 local.
Nota PG: con postgres/postgres falla la autenticacion; se uso la credencial local del proyecto simu via DB_USER/DB_PASSWORD (sin guardarla).

## pytest (`pytest -q -p no:cacheprovider`)
1489 tests: 1486 passed, 2 failed, 0 errors, 1 skipped (153 s).

Fallos preexistentes:
- `apps/composer/tests/test_preview.py::ComposerPreviewTests::test_compose_page_posts_live_preview_form_data` - UnicodeDecodeError cp1252 (byte 0x90): `Path(...compose.html).read_text()` sin encoding (test_preview.py:33), falla en Windows.
- `apps/inbox/tests/test_channel_scoping.py::TestBulkAssignment::test_bulk_assign_skips_channels_the_assignee_lacks` - `assert None == <User c@example.com>` (test_channel_scoping.py:268).

## ruff
- `ruff check .`: 0 errores.
- `ruff format --check .`: 4 archivos requieren reformateo (398 ya conformes).

## mypy (`apps/ config/ providers/ tests/ --ignore-missing-imports`, con SECRET_KEY/ENCRYPTION_KEY_SALT definidos)
4 errores en 2 archivos:
- apps/members/channels.py:180 assignment (list vs QuerySet)
- apps/analytics/views.py:99, 119, 243 `HttpRequest` sin atributo `workspace_membership`
(Sin SECRET_KEY el plugin django-stubs falla al construir.)

## manage.py check (config.settings.test)
System check identified no issues (0 silenced).
