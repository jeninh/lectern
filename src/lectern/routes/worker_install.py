"""``GET /worker/install.sh``: the one-command worker installer (docs/mac-worker.md).

Public and secret-free: the script knows only this server's address and where to install
Lectern from. The enrollment code is passed on the command line by whoever runs it.
"""

from __future__ import annotations

import shlex
from importlib.resources import files

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from lectern.security.deps import SettingsDep

router = APIRouter()


@router.get("/worker/install.sh", include_in_schema=False)
def install_script(settings: SettingsDep) -> PlainTextResponse:
    script = files("lectern.worker").joinpath("install.sh").read_text()
    script = script.replace("__LECTERN_SERVER__", shlex.quote(settings.base_url)).replace(
        "__LECTERN_SOURCE__", shlex.quote(settings.worker_install_source)
    )
    return PlainTextResponse(
        script, media_type="text/x-shellscript", headers={"Cache-Control": "no-store"}
    )
