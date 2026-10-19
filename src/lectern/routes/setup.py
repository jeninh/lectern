"""First-run owner creation, gated by the one-time token printed to the logs."""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse

from lectern.models import Role, User
from lectern.security import audit
from lectern.security.deps import DbDep, SettingsDep
from lectern.security.passwords import hash_password, validate_password_strength
from lectern.security.sessions import client_ip, create_session, set_session_cookie
from lectern.setup_state import check_setup_token, complete_setup, setup_completed
from lectern.templating import render

router = APIRouter()

USERNAME_RE = r"^[a-z0-9][a-z0-9._-]{1,31}$"


def _limit(request: Request) -> None:
    ip = client_ip(request) or "?"
    if not request.app.state.limiters["setup"].allow(ip):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts")


@router.get("/setup", include_in_schema=False)
def setup_form(request: Request, db: DbDep, token: str = ""):
    _limit(request)
    if setup_completed(db):
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    if not token or not check_setup_token(db, token):
        return render(request, "setup_invalid.html", status_code=status.HTTP_403_FORBIDDEN)
    return render(request, "setup.html", {"token": token})


@router.post("/setup", include_in_schema=False)
def setup_submit(
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    token: Annotated[str, Form()],
    username: Annotated[str, Form(pattern=USERNAME_RE)],
    display_name: Annotated[str, Form(max_length=128)] = "",
    password: Annotated[str, Form()] = "",
    password_confirm: Annotated[str, Form()] = "",
):
    _limit(request)
    if setup_completed(db):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Setup already completed")
    if not check_setup_token(db, token):
        audit.record(db, "setup.invalid_token", ip=client_ip(request))
        db.commit()
        return render(request, "setup_invalid.html", status_code=status.HTTP_403_FORBIDDEN)
    error = validate_password_strength(password)
    if error is None and password != password_confirm:
        error = "Passwords do not match."
    if error:
        return render(
            request,
            "setup.html",
            {"token": token, "error": error, "username": username},
            status_code=400,
        )

    user = User(
        username=username,
        display_name=display_name.strip() or username,
        role=Role.OWNER,
        password_hash=hash_password(password),
        webauthn_user_handle=secrets.token_bytes(32),
    )
    db.add(user)
    db.flush()
    complete_setup(db)
    audit.record(
        db, "setup.owner_created", actor_user_id=user.id, target=username, ip=client_ip(request)
    )
    session_token = create_session(db, user, request, settings)
    db.commit()
    response = RedirectResponse("/account?welcome=1", status_code=status.HTTP_303_SEE_OTHER)
    set_session_cookie(response, session_token, settings)
    return response
