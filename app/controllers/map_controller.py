from fastapi import APIRouter

from app.services.map_service import get_map

router = APIRouter()

router.add_api_route("/api/map", get_map, methods=["GET"])
