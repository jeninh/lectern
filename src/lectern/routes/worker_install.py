"""``GET /worker/install.sh``: the one-command worker installer (docs/mac-worker.md).

Public and secret-free: the script knows only this server's address and where to install
Lectern from (pinned to the server's commit when the image was built with one). The
enrollment code is asked for when it runs, or read from ``LECTERN_WORKER_CODE``.
"""

from __future__ import annotations

import shlex
from importlib.resources import files

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from lectern.security.deps import SettingsDep

router = APIRouter()


def install_source(settings) -> str:
    """The requirement the installer installs: ``worker_install_source`` at this server's
    own commit (``worker_install_ref``, set from the image's build), so the worker speaks
    the same protocol as the server. Without a ref: the source as configured."""
    source, ref = settings.worker_install_source, settings.worker_install_ref
    if not ref or not source.startswith("git+") or "@" in source.rsplit("/", 1)[-1]:
        return source
    return f"{source}@{ref}"


@router.get("/worker/install.sh", include_in_schema=False)
def install_script(settings: SettingsDep) -> PlainTextResponse:
    script = files("lectern.worker").joinpath("install.sh").read_text()
    script = script.replace("__LECTERN_SERVER__", shlex.quote(settings.base_url)).replace(
        "__LECTERN_SOURCE__", shlex.quote(install_source(settings))
    )
    return PlainTextResponse(
        script, media_type="text/x-shellscript", headers={"Cache-Control": "no-store"}
    )
