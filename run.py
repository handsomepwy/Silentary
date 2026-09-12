"""Development entrypoint: python run.py

Production uses uvicorn via systemd — see README and deployment/.
"""

import uvicorn

from app.config import load_settings


def main() -> None:
    settings = load_settings()
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
