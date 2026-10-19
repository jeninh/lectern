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
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_level=settings.log_level.lower(),
        access_log=False,
    )


if __name__ == "__main__":
    main()
