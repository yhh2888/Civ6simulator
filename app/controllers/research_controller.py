from fastapi import APIRouter

from app.services.research_service import (
    get_research_status,
    select_research,
    set_research_availability,
)

router = APIRouter()

router.add_api_route("/api/research", get_research_status, methods=["GET"])
router.add_api_route("/api/research/{category}", select_research, methods=["PUT"])
router.add_api_route(
    "/api/research/{category}/{target_id}/availability",
    set_research_availability,
    methods=["PUT"],
)
