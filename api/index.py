"""Vercel serverless entrypoint.

Vercel's Python runtime expects a module-level WSGI callable named `app`.
"""

from __future__ import annotations

# Import the Flask WSGI app from the project.
# `app.py` exports a module-level `app = create_app()`.
from app import app  # noqa: F401
