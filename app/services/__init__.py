"""Domain services for the Civ VI city simulator."""

from app.services import (
    city_service,
    entity_service,
    food_service,
    game_data_service,
    map_service,
    production_service,
    research_service,
    snapshot_service,
    unit_service,
    yield_service,
)

COMPATIBILITY_MODULES = (
    game_data_service,
    food_service,
    snapshot_service,
    yield_service,
    production_service,
    research_service,
    entity_service,
    unit_service,
    map_service,
    city_service,
)
