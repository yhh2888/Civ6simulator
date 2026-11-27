"""First-run game data setup (see app/services/game_data_setup_service.py)."""

from fastapi import APIRouter
from pydantic import BaseModel

from app.services import game_data_setup_service

router = APIRouter(prefix="/api/setup", tags=["setup"])


class SetupRequest(BaseModel):
    path: str


@router.get("/status")
def setup_status():
    return game_data_setup_service.status()


@router.post("/build")
def setup_build(request: SetupRequest):
    return game_data_setup_service.start(request.path)
