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
from app.repositories.game_data_repository import game_data_repository
from app.repositories.json_repository import read, write, write_atomically, write_json_transaction
from app.config import RuleConfigurationError, YIELD_KEYS, load_game_rules, matching_tile_yield_modifiers
from app.entity_rules import ENTITY_SOURCES, entity_availability, entity_rows, missing_research_requirements, research_prerequisites as entity_research_prerequisites
from app.schemas.city_schema import CityCreate, TileOwnership
from app.schemas.snapshot_schema import SaveMapRequest
from app.schemas.game_schema import (BuilderActionRequest, FoundCityRequest, ForceEntityRequest, ForceUnitCreateRequest, ProductionQueueRequest, ProductionReorderRequest, ResearchAvailabilityRequest, ResearchSelectRequest, UnitMoveRequest)

from app.services.game_data_service import (game_rules, game_state, hex_distance, write_json_transaction)
from app.services.production_service import prepare_city_production
from app.services.food_service import refresh_city_food_state
def get_cities():
    return list(city_repository.get_all().values())


@synchronized_mutation
def create_city(payload: CityCreate):
    rules = game_rules()
    mp = map_repository.get_map({})
    tile = next((t for t in mp.get("tiles",[]) if t["x"]==payload.x and t["y"]==payload.y), None)
    if tile is None:
        raise ApplicationError(404, "Cannot create a city outside the loaded map.")
    cities = city_repository.get_all()
    for city in cities.values():
        if (
            hex_distance(payload.x, payload.y, city["x"], city["y"])
            < rules["city"]["minimum_distance"]
        ):
            raise ApplicationError(
                409,
                f"Cities must be at least {rules['city']['minimum_distance']} tiles apart. "
                f"Nearby city: {city['name']}",
            )
    population = (
        rules["city"]["initial_population"]
        if payload.population is None
        else payload.population
    )
    if population > rules["city"]["maximum_population"]:
        raise ApplicationError(
            422,
            "Population cannot exceed "
            f"{rules['city']['maximum_population']}.",
        )
    city_id = "city-" + uuid4().hex[:8]
    city = {
        "id":city_id, "name":payload.name, "x":payload.x, "y":payload.y,
        "population":population, "civilization":payload.civilization,
        "leader":payload.leader, "buildings":[], "districts":[],
        "wonders":[], "governor":None, "governor_promotions":[],
        "policies":[], "technologies":[], "civics":[], "religion":None,
        "housing":2, "amenities":0, "notes":"", "worked_tiles":[],
        "created_from_map":True
    }
    prepare_city_production(city)
    cities[city_id] = city
    initial_tiles = [
        owned_tile
        for owned_tile in mp.get("tiles", [])
        if (
            hex_distance(payload.x, payload.y, owned_tile["x"], owned_tile["y"])
            <= rules["city"]["initial_territory_radius"]
        )
    ]
    if any(
        owned_tile.get("owner_city_id") is not None
        for owned_tile in initial_tiles
    ):
        raise ApplicationError(409, "Initial city territory overlaps another city.")
    for owned_tile in initial_tiles:
        owned_tile["owner_city_id"] = city_id
        owned_tile["worked"] = owned_tile is tile
    refresh_city_food_state(
        city,
        mp,
        game_data_repository.catalog(),
        game_state()["game_speed"],
        rules,
    )
    city_repository.save_all(cities, atomic=False)
    map_repository.save_map(mp, atomic=False)
    return city


@synchronized_mutation
def update_city_tile_ownership(
    city_id: str, x: int, y: int, payload: TileOwnership
):
    cities = city_repository.get_all()
    city = cities.get(city_id)
    if city is None:
        raise ApplicationError(404, "City not found")

    mp = map_repository.get_map({})
    tile = next(
        (item for item in mp.get("tiles", []) if item["x"] == x and item["y"] == y),
        None,
    )
    if tile is None:
        raise ApplicationError(404, "Tile not found")
    if (x, y) == (city["x"], city["y"]) and not payload.owned:
        raise ApplicationError(409, "A city cannot give up its center tile.")
    if payload.owned:
        owner_city_id = tile.get("owner_city_id")
        if owner_city_id not in (None, city_id):
            raise ApplicationError(409, "This tile is already owned by another city.")
        if owner_city_id is None and not any(
            owned_tile.get("owner_city_id") == city_id
            and hex_distance(x, y, owned_tile["x"], owned_tile["y"]) == 1
            for owned_tile in mp.get("tiles", [])
        ):
            raise ApplicationError(409, "A city can only claim a tile adjacent to its territory.")
        if owner_city_id is None:
            tile["owner_city_id"] = city_id
            tile["worked"] = False
    else:
        if tile.get("owner_city_id") != city_id:
            raise ApplicationError(409, "This city does not own the selected tile.")
        tile["owner_city_id"] = None
        tile["worked"] = False

    map_repository.save_map(mp, atomic=False)
    return tile


@synchronized_mutation
def update_city(city_id: str, payload: dict):
    cities = city_repository.get_all()
    if city_id not in cities:
        raise ApplicationError(404, "City not found")
    rules = game_rules()
    allowed = {"name","population","civilization","leader","buildings","districts","wonders","governor","governor_promotions","policies","technologies","civics","religion","housing","amenities","notes","worked_tiles"}
    old_population = cities[city_id].get(
        "population", rules["city"]["initial_population"]
    )
    for key, value in payload.items():
        if key in allowed:
            if key == "population":
                try:
                    value = int(value)
                except (TypeError, ValueError) as error:
                    raise ApplicationError(422, "Population must be an integer.") from error
                if not 1 <= value <= rules["city"]["maximum_population"]:
                    raise ApplicationError(
                        422,
                        "Population must be between 1 and "
                        f"{rules['city']['maximum_population']}.",
                    )
            cities[city_id][key] = value
    if cities[city_id].get(
        "population", rules["city"]["initial_population"]
    ) != old_population:
        map_data = map_repository.get_map({})
        center = (cities[city_id]["x"], cities[city_id]["y"])
        eligible_slots = (
            max(0, cities[city_id]["population"])
            * rules["city"]["worked_tiles_per_population"]
        )
        worked_count = 0
        for tile in map_data.get("tiles", []):
            if (tile["x"], tile["y"]) == center:
                tile["worked"] = True
            elif tile.get("owner_city_id") == city_id and tile.get("worked"):
                if worked_count >= eligible_slots:
                    tile["worked"] = False
                else:
                    worked_count += 1
    else:
        map_data = map_repository.get_map({})
    refresh_city_food_state(
        cities[city_id],
        map_data,
        game_data_repository.catalog(),
        game_state()["game_speed"],
        rules,
    )
    if cities[city_id].get(
        "population", rules["city"]["initial_population"]
    ) != old_population:
        write_json_transaction(
            {locations.cities_file(): cities, locations.map_file(): map_data}
        )
    else:
        city_repository.save_all(cities, atomic=True)
    return cities[city_id]


@synchronized_mutation
def delete_city(city_id: str):
    cities = city_repository.get_all()
    if city_id not in cities:
        raise ApplicationError(404, "City not found")
    cities.pop(city_id)
    mp = map_repository.get_map({})
    state = game_state()
    state["units"] = [
        unit for unit in state["units"] if unit.get("owner_city_id") != city_id
    ]
    for tile in mp.get("tiles", []):
        if tile.get("owner_city_id") == city_id:
            tile["owner_city_id"] = None
            tile["worked"] = False
    write_json_transaction(
        {
            locations.cities_file(): cities,
            locations.map_file(): mp,
            locations.game_state_file(): state,
        }
    )
    return {"deleted":city_id}
