from fastapi import APIRouter

from app.services.game_data_service import get_catalog, get_game_rules

router = APIRouter()

router.add_api_route("/api/game-rules", get_game_rules, methods=["GET"])
router.add_api_route("/api/catalog", get_catalog, methods=["GET"])
