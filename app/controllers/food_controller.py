from fastapi import APIRouter

from app.services.food_service import get_city_food

router = APIRouter()

router.add_api_route(
    "/api/cities/{city_id}/food", get_city_food, methods=["GET"]
)
