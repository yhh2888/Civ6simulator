from fastapi import APIRouter

from app.services.yield_service import calculate_city, calculate_tile_yields

router = APIRouter()

router.add_api_route("/api/calculate/{city_id}", calculate_city, methods=["POST"])
router.add_api_route(
    "/api/tiles/{x}/{y}/yields", calculate_tile_yields, methods=["GET"]
)
