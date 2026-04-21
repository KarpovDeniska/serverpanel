"""Authentication routes — login, register, logout."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from serverpanel.infrastructure.auth.backend import hash_password, verify_password
from serverpanel.infrastructure.database.repositories.users import UserRepository
from serverpanel.presentation.dependencies import get_db
from serverpanel.presentation.ratelimit import login_limiter, register_limiter
from serverpanel.presentation.templates import templates

router = APIRouter(tags=["auth"])


def _client_key(request: Request, email: str | None = None) -> str:
    # Prefer forwarded-for for reverse proxy; fall back to peer addr.
    xff = request.headers.get("x-forwarded-for")
    ip = xff.split(",", 1)[0].strip() if xff else (request.client.host if request.client else "unknown")
    return f"{ip}|{email or ''}"


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "auth/login.html", {"error": None})


@router.post("/login")
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    if not await login_limiter.check(_client_key(request, email)):
        return templates.TemplateResponse(
            request, "auth/login.html",
            {"error": "Слишком много попыток входа. Повторите через несколько минут."},
            status_code=429,
        )

    repo = UserRepository(db)
    user = await repo.get_by_email(email)

    if not user or not user.password_hash or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request, "auth/login.html", {"error": "Неверный email или пароль"},
            status_code=400,
        )

    if not user.is_active:
        return templates.TemplateResponse(
            request, "auth/login.html", {"error": "Аккаунт деактивирован"},
            status_code=400,
        )

    # Rotate session id on privilege change to prevent session fixation.
    request.session.clear()
    request.session["user_id"] = user.id
    return RedirectResponse(url="/", status_code=302)


async def _registration_open(repo: UserRepository) -> bool:
    """Self-serve registration is allowed only for bootstrapping the first admin."""
    existing = await repo.get_all()
    return len(existing) == 0


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request, db: AsyncSession = Depends(get_db)):
    repo = UserRepository(db)
    if not await _registration_open(repo):
        raise HTTPException(
            403,
            "Регистрация закрыта. Попросите администратора создать учётку.",
        )
    return templates.TemplateResponse(request, "auth/register.html", {"error": None})


@router.post("/register")
async def register(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    display_name: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    if not await register_limiter.check(_client_key(request)):
        return templates.TemplateResponse(
            request, "auth/register.html",
            {"error": "Слишком много попыток регистрации. Повторите позже."},
            status_code=429,
        )

    repo = UserRepository(db)
    if not await _registration_open(repo):
        raise HTTPException(
            403,
            "Регистрация закрыта. Попросите администратора создать учётку.",
        )

    if await repo.email_exists(email):
        return templates.TemplateResponse(
            request, "auth/register.html", {"error": "Этот email уже зарегистрирован"},
            status_code=400,
        )

    if len(password) < 12:
        return templates.TemplateResponse(
            request, "auth/register.html",
            {"error": "Пароль должен быть не менее 12 символов"},
            status_code=400,
        )

    user = await repo.create_user(
        email=email,
        password_hash=hash_password(password),
        display_name=display_name or None,
        role="admin",  # first user is bootstrapping admin; further users created by admin
    )

    request.session.clear()
    request.session["user_id"] = user.id
    return RedirectResponse(url="/", status_code=302)


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)
