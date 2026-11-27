from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

from app.core import paths
from app.core.constants import APP_VERSION, HEALTH_APP_NAME

router = APIRouter()


@router.get("/")
def index():
    return FileResponse(
        paths.STATIC / "index.html",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@router.get("/health")
def health():
    return JSONResponse(
        {"status": "ok", "version": APP_VERSION, "app": HEALTH_APP_NAME},
        headers={"Cache-Control": "no-store"},
    )
