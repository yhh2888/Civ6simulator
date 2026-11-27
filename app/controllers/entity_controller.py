from fastapi import APIRouter

from app.services.entity_service import force_add_entity, get_entity_catalog

router = APIRouter()

router.add_api_route(
    "/api/entities/catalog", get_entity_catalog, methods=["GET"]
)
router.add_api_route("/api/entities/force", force_add_entity, methods=["POST"])
