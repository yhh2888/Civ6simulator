from fastapi import APIRouter

from app.services.map_service import update_tile
from app.services.yield_service import tile_details

router = APIRouter()

router.add_api_route("/api/map/tile/{x}/{y}", update_tile, methods=["PUT"])
router.add_api_route("/api/tiles/{x}/{y}/details", tile_details, methods=["GET"])
