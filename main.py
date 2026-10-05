"""Entry point: ``uvicorn main:app`` serves the mirror."""

import os

from mirror.app import create_app
from mirror.config import Settings

app = create_app(
    Settings(
        upstream_url=os.environ.get("MIRROR_UPSTREAM_URL", "https://pypi.org"),
        cache_dir=os.environ.get("MIRROR_CACHE_DIR", ".mirror-cache"),
    )
)
