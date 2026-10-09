"""Web dashboard (FastAPI + Jinja2) and JSON API. Entry point: `create_app()`."""

from .app import create_app

__all__ = ["create_app"]
