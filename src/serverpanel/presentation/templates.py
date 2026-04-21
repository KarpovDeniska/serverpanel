"""Shared Jinja2 templates instance.

Uses the installed package path so templates work when running from a
checkout AND when installed as a wheel.
"""

from importlib.resources import files as _pkg_files

from fastapi.templating import Jinja2Templates

from serverpanel.presentation.csrf import get_csrf_token

_TEMPLATE_DIR = str(_pkg_files("serverpanel") / "templates")

templates = Jinja2Templates(directory=_TEMPLATE_DIR)

# Expose in every template: {{ get_csrf_token(request) }}
templates.env.globals["get_csrf_token"] = get_csrf_token
