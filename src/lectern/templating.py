from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATES_DIR = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(default=True, default_for_string=True),
)
templates = Jinja2Templates(env=_env)


def render(request: Request, name: str, context: dict | None = None, status_code: int = 200):
    ctx = {"request": request}
    auth = getattr(request.state, "auth", None)
    ctx["current_user"] = auth.user if auth else None
    ctx["csrf_token"] = auth.session.csrf_token if auth else ""
    if context:
        ctx.update(context)
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)
