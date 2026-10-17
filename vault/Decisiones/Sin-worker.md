# Decisión · Sin worker 24/7 (modo manual)

- Nada corre en segundo plano permanente. Un post programado sale **cuando el usuario procesa**.
- Botón «Procesar pendientes»: drena la cola de django-background-tasks (equivale a `process_tasks`), con límite de tiempo.
- `POST /cron/run` con `Authorization: Bearer <CRON_SECRET>`, **dormido**: 503 si no hay `CRON_SECRET` configurado, 401 con bearer incorrecto.
- Contexto: el motor de publicación es `apps/publisher/engine.py`; tareas recurrentes keyed por `verbose_name` (ver [[Arquitectura]]).
- Implementación: Fase 3.
