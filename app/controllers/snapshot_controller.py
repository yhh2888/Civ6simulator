from fastapi import APIRouter

from app.schemas.snapshot_schema import SaveMapRequest
from app.services.snapshot_service import (
    list_saved_maps,
    load_saved_map,
    save_map,
    save_map_as,
)

router = APIRouter()

router.add_api_route("/api/map/save", save_map, methods=["POST"])
router.add_api_route("/api/maps/saved", list_saved_maps, methods=["GET"])
router.add_api_route("/api/maps/save-as", save_map_as, methods=["POST"])
router.add_api_route("/api/maps/load", load_saved_map, methods=["POST"])
