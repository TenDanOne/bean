release: python manage.py migrate
web: gunicorn config.wsgi:application --bind 0.0.0.0:$PORT --workers 2 --threads 2
# Opcional (BEAN corre sin worker; ver HANDOFF): worker: python manage.py process_tasks
