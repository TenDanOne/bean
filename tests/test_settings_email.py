"""Development settings must not silently swallow a configured SMTP host.

`development.py` used to force the console backend unconditionally, which
overrode base.py's EMAIL_BACKEND_TYPE resolution. The symptom was invite emails
printing to the runserver terminal and never being delivered, with nothing to
explain why. These reload the module under a patched environment to pin the
guard — they only read a module attribute, never touching django.conf.settings.
"""

import importlib
import os
from unittest.mock import patch

CONSOLE_BACKEND = "django.core.mail.backends.console.EmailBackend"


def _reload_development_settings():
    return importlib.reload(importlib.import_module("config.settings.development"))


def test_console_backend_when_no_smtp_host_configured():
    with patch.dict(os.environ, {"EMAIL_HOST": ""}):
        assert _reload_development_settings().EMAIL_BACKEND == CONSOLE_BACKEND


def test_configured_smtp_host_is_not_overridden():
    with patch.dict(os.environ, {"EMAIL_HOST": "smtp.example.com"}):
        assert _reload_development_settings().EMAIL_BACKEND != CONSOLE_BACKEND
