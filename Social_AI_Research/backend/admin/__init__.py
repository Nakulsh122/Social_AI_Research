from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .router import router


def setup(app: FastAPI) -> None:
    """Wire the admin module into a FastAPI app.

    Static files must be mounted on the app itself (not on the APIRouter) for
    the StaticFiles request path resolution to work correctly under FastAPI.
    Mounting on an APIRouter silently returns 404 for asset requests, which
    breaks every CSS / JS / image link in the templates.
    """
    here = Path(__file__).resolve().parent
    app.mount(
        "/admin/static",
        StaticFiles(directory=str(here / "static")),
        name="admin-static",
    )
    app.include_router(router)
