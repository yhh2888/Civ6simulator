from fastapi import APIRouter

from app.services.unit_service import (
    builder_build,
    builder_remove_feature,
    force_create_unit,
    force_delete_unit,
    get_game_state,
    move_unit,
    settler_found_city,
)

router = APIRouter()

router.add_api_route("/api/game", get_game_state, methods=["GET"])
router.add_api_route("/api/units/force", force_create_unit, methods=["POST"])
router.add_api_route("/api/units/{unit_id}", force_delete_unit, methods=["DELETE"])
router.add_api_route("/api/units/{unit_id}/move", move_unit, methods=["POST"])
router.add_api_route("/api/units/{unit_id}/build", builder_build, methods=["POST"])
router.add_api_route(
    "/api/units/{unit_id}/remove-feature",
    builder_remove_feature,
    methods=["POST"],
)
router.add_api_route(
    "/api/units/{unit_id}/found-city",
    settler_found_city,
    methods=["POST"],
)
