from fastapi import APIRouter

from app.schemas.city_schema import CityCreate, TileOwnership
from app.services.city_service import (
    create_city,
    delete_city,
    get_cities,
    update_city,
    update_city_tile_ownership,
)

router = APIRouter()

router.add_api_route("/api/cities", get_cities, methods=["GET"])
router.add_api_route("/api/cities", create_city, methods=["POST"])
router.add_api_route(
    "/api/cities/{city_id}/tiles/{x}/{y}",
    update_city_tile_ownership,
    methods=["PUT"],
)
router.add_api_route("/api/cities/{city_id}", update_city, methods=["PUT"])
router.add_api_route("/api/cities/{city_id}", delete_city, methods=["DELETE"])
