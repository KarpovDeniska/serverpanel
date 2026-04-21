"""CSRF protection for state-changing form POSTs.

Double-submit token in the session:

- On first render with a session, mint a random token stored under
  `_csrf` in the session. Template uses `get_csrf_token(request)` to
  render it as a hidden input or meta tag.
- Every unsafe method (POST/PUT/PATCH/DELETE) with a browser form
  content-type must include the token in either the `X-CSRF-Token`
  header or the `csrf_token` form field.

JSON APIs and WebSockets are exempt — JSON CSRF requires cross-origin
credentials, which our cookie uses SameSite=Lax by default.
"""

from __future__ import annotations

import hmac
import logging
import secrets
from urllib.parse import parse_qs

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

log = logging.getLogger(__name__)

SESSION_KEY = "_csrf"
FORM_FIELD = "csrf_token"
HEADER_NAME = "X-CSRF-Token"

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
FORM_CONTENT_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data")


def _get_or_create_token(request: Request) -> str:
    token = request.session.get(SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[SESSION_KEY] = token
    return token


def get_csrf_token(request: Request) -> str:
    """Template helper: `<input name="csrf_token" value="{{ get_csrf_token(request) }}">`."""
    return _get_or_create_token(request)


async def _read_body(request: Request) -> bytes:
    """Buffer body and push it back to the ASGI receive queue for downstream."""
    chunks: list[bytes] = []
    more = True
    while more:
        message = await request.receive()
        if message["type"] == "http.request":
            body = message.get("body", b"")
            chunks.append(body)
            more = message.get("more_body", False)
        else:  # http.disconnect
            more = False

    body = b"".join(chunks)

    async def _receive():
        # Replay the single buffered message, then end-of-stream.
        return {"type": "http.request", "body": body, "more_body": False}

    request._receive = _receive  # type: ignore[attr-defined]
    return body


class CSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.scope.get("type") != "http":
            return await call_next(request)

        if request.method in UNSAFE_METHODS:
            content_type = (
                request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            )
            if any(content_type.startswith(ct) for ct in FORM_CONTENT_TYPES):
                expected = request.session.get(SESSION_KEY)
                submitted = request.headers.get(HEADER_NAME)

                if not submitted and content_type.startswith("application/x-www-form-urlencoded"):
                    body = await _read_body(request)
                    try:
                        form = parse_qs(body.decode("utf-8"), keep_blank_values=True)
                        values = form.get(FORM_FIELD, [])
                        if values:
                            submitted = values[0]
                    except Exception:
                        submitted = None

                # For multipart we rely on the header (clients we control post it).

                if (
                    not expected
                    or not submitted
                    or not hmac.compare_digest(str(expected), str(submitted))
                ):
                    log.warning(
                        "CSRF rejected: %s %s (session_token=%s, submitted=%s)",
                        request.method,
                        request.url.path,
                        bool(expected),
                        bool(submitted),
                    )
                    return JSONResponse(
                        {"detail": "CSRF token missing or invalid"},
                        status_code=403,
                    )

        response = await call_next(request)
        _get_or_create_token(request)
        return response
