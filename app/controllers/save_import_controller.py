"""Civ VI save (.Civ6Save) import endpoints."""

from urllib.parse import unquote

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool

from app.core.exceptions import ApplicationError
from app.services import save_import_service
from app.services.civ6save.container import MAX_SAVE_BYTES

router = APIRouter()


@router.post("/api/import/civ6save/preview")
async def preview_civ6save(request: Request):
    """Body: the raw .Civ6Save bytes (application/octet-stream).

    Optional header ``X-Filename`` (URL-encoded) is kept for display.
    """
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_SAVE_BYTES:
        raise ApplicationError(413, "세이브 파일이 너무 큽니다.")
    raw = await request.body()
    if len(raw) > MAX_SAVE_BYTES:
        raise ApplicationError(413, "세이브 파일이 너무 큽니다.")
    filename = unquote(request.headers.get("x-filename") or "") or None
    return await run_in_threadpool(save_import_service.preview_save, raw, filename)


@router.post("/api/import/civ6save/apply")
async def apply_civ6save(payload: dict):
    return await run_in_threadpool(save_import_service.apply_save_import, payload)
