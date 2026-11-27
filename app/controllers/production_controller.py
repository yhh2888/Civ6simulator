from fastapi import APIRouter

from app.services.production_service import (
    add_production,
    end_turn,
    get_city_production,
    get_production_catalog,
    remove_production,
    reorder_production,
)

router = APIRouter()

router.add_api_route(
    "/api/production/catalog", get_production_catalog, methods=["GET"]
)
router.add_api_route(
    "/api/cities/{city_id}/production", get_city_production, methods=["GET"]
)
router.add_api_route(
    "/api/cities/{city_id}/production/queue",
    add_production,
    methods=["POST"],
)
router.add_api_route(
    "/api/cities/{city_id}/production/order",
    reorder_production,
    methods=["PUT"],
)
router.add_api_route(
    "/api/cities/{city_id}/production/queue/{queue_id}",
    remove_production,
    methods=["DELETE"],
)
router.add_api_route("/api/turn/end", end_turn, methods=["POST"])
