"""
interfaces.web — FastAPI-приложение.

Использование:
    from interfaces.web import create_app
    app = create_app()

Или через CLI:
    python -m interfaces.cli
"""

from interfaces.web.app import create_app

__all__ = ["create_app"]
