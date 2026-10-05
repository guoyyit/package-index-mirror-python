"""Entry point: ``uvicorn main:app``."""

from pkgmirror.app import create_app

app = create_app()
