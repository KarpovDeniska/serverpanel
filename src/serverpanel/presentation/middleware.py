"""Middleware for auth redirect and error handling."""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse

# Routes that don't require authentication
PUBLIC_PATHS = {"/login", "/register", "/static", "/favicon.ico"}


class AuthRedirectMiddleware(BaseHTTPMiddleware):
    """Redirect unauthenticated users to login page (for HTML requests only)."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Skip auth check for public paths and API calls
        if any(path.startswith(p) for p in PUBLIC_PATHS):
            return await call_next(request)

        # Only redirect HTML requests, not API/HTMX
        is_html = "text/html" in request.headers.get("accept", "")
        is_htmx = request.headers.get("hx-request") == "true"

        if not request.session.get("user_id") and is_html and not is_htmx:
            return RedirectResponse(url="/login", status_code=302)

        return await call_next(request)
