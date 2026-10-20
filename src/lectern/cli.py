"""`lectern` console entry point: run the server."""

from __future__ import annotations

import uvicorn

from lectern.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "lectern.main:app_factory",
        factory=True,
        host=settings.host,
        port=settings.port,
        # request.client stays the socket peer; lectern.security.proxy applies forwarding
        # headers itself, and only for peers listed in LECTERN_TRUSTED_PROXIES.
        proxy_headers=False,
        log_level=settings.log_level.lower(),
        access_log=False,
    )


if __name__ == "__main__":
    main()
