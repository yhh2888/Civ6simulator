from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.exceptions import ApplicationError

from app.core.dependencies import synchronized_mutation
from app.repositories import locations
from app.repositories.map_repository import map_repository
from app.repositories.city_repository import city_repository
from app.repositories.tile_repository import tile_repository
from app.repositories.json_repository import read, write, write_atomically, write_json_transaction
from app.config import RuleConfigurationError, YIELD_KEYS, load_game_rules, matching_tile_yield_modifiers
from app.entity_rules import ENTITY_SOURCES, entity_availability, entity_rows, missing_research_requirements, research_prerequisites as entity_research_prerequisites
from app.schemas.city_schema import CityCreate, TileOwnership
from app.schemas.snapshot_schema import SaveMapRequest
from app.schemas.game_schema import (BuilderActionRequest, FoundCityRequest, ForceEntityRequest, ForceUnitCreateRequest, ProductionQueueRequest, ProductionReorderRequest, ResearchAvailabilityRequest, ResearchSelectRequest, UnitMoveRequest)

from app.services.game_data_service import game_rules
def get_map():
    return map_repository.get_map()


@synchronized_mutation
def update_tile(x: int, y: int, payload: dict):
    mp = map_repository.get_map({})
    tile = tile_repository.find(mp, x, y)
    if tile is None:
        raise ApplicationError(404, "Tile not found")
    if payload.get("worked") is True:
        owner_city_id = tile.get("owner_city_id")
        cities = city_repository.get_all()
        city = cities.get(owner_city_id)
        if city is None:
            raise ApplicationError(409, "A tile must be owned by a city before citizens can work it.")
        rules = game_rules()
        if (x, y) != (city["x"], city["y"]):
            worked_count = sum(
                1
                for owned_tile in mp.get("tiles", [])
                if owned_tile.get("owner_city_id") == owner_city_id
                and owned_tile.get("worked")
                and (owned_tile["x"], owned_tile["y"]) != (city["x"], city["y"])
            )
            if not tile.get("worked") and worked_count >= max(
                0,
                city.get("population", rules["city"]["initial_population"])
                * rules["city"]["worked_tiles_per_population"],
            ):
                raise ApplicationError(409, "The city's population cannot work any more tiles.")
    allowed = {"terrain","feature","resource","river","river_wires","improvement","district","worked","hill_override"}
    for key, value in payload.items():
        if key in allowed:
            tile[key] = value
    map_repository.save_map(mp, atomic=False)
    return tile
