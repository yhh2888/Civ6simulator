"""HTTP controllers grouped by simulator feature."""

from app.controllers import (
    city_controller,
    entity_controller,
    food_controller,
    game_data_controller,
    map_controller,
    production_controller,
    research_controller,
    save_import_controller,
    setup_controller,
    simulation_controller,
    snapshot_controller,
    tile_controller,
    unit_controller,
    web_controller,
    yield_controller,
)

ROUTERS = (
    web_controller.router,
    game_data_controller.router,
    map_controller.router,
    snapshot_controller.router,
    tile_controller.router,
    city_controller.router,
    yield_controller.router,
    research_controller.router,
    production_controller.router,
    entity_controller.router,
    unit_controller.router,
    food_controller.router,
    simulation_controller.router,
    save_import_controller.router,
    setup_controller.router,
)
