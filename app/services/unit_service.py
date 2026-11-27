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

from app.services.game_data_service import (catalog_flag, catalog_integer, city_prerequisite_error, configured_entity, effective_catalog_row, game_rules, game_state, hex_distance, index_rows, raw_table, write_json_transaction)
from app.services.food_service import refresh_city_food_state
def get_game_state():
    return game_state()


@synchronized_mutation
def force_create_unit(request: ForceUnitCreateRequest):
    if (request.x is None) != (request.y is None):
        raise ApplicationError(422, "Unit placement requires both x and y.")
    cities = city_repository.get_all()
    city = cities.get(request.owner_city_id)
    if city is None:
        raise ApplicationError(404, "Unit owner city not found.")
    x = city["x"] if request.x is None else request.x
    y = city["y"] if request.y is None else request.y
    map_data = map_repository.get_map({})
    placement_tile = next(
        (
            tile for tile in map_data.get("tiles", [])
            if tile.get("x") == x and tile.get("y") == y
        ),
        None,
    )
    if placement_tile is None:
        raise ApplicationError(404, "Unit placement tile not found.")
    if placement_tile.get("owner_city_id") != city["id"]:
        raise ApplicationError(409, "Units must be placed on their owner's territory.")

    catalog = game_data_repository.catalog()
    unit_row = index_rows(catalog, "Units::Units", "UnitType").get(
        request.unit_type
    )
    if unit_row is None:
        raise ApplicationError(422, "Unknown unit type.")
    rules = game_rules()
    unit_row = effective_catalog_row(unit_row, "units", request.unit_type, rules)
    state = game_state()
    unavailable = entity_availability(
        catalog,
        "units",
        request.unit_type,
        unit_row,
        state,
        city=city,
        map_data=map_data,
        cities=cities,
        rules=rules,
    )
    if unavailable:
        raise ApplicationError(409, " ".join(unavailable))
    unit = {
        "id": "unit-" + uuid4().hex[:12],
        "unit_type": request.unit_type,
        "owner_city_id": city["id"],
        "x": x,
        "y": y,
        "moves_remaining": catalog_integer(
            unit_row,
            "BaseMoves",
            rules["units"]["default_base_moves"],
            request.unit_type,
        ),
        "charges_remaining": catalog_integer(
            unit_row,
            "BuildCharges",
            rules["units"]["default_build_charges"],
            request.unit_type,
        ),
        "created_turn": state["turn"],
    }
    state["units"].append(unit)
    game_data_repository.save_game_state(state)
    return unit


@synchronized_mutation
def force_delete_unit(unit_id: str):
    state = game_state()
    unit = next(
        (item for item in state["units"] if item.get("id") == unit_id),
        None,
    )
    if unit is None:
        raise ApplicationError(404, "Unit not found.")
    state["units"] = [
        item for item in state["units"] if item.get("id") != unit_id
    ]
    game_data_repository.save_game_state(state)
    return {"deleted": unit_id}


@synchronized_mutation
def move_unit(unit_id: str, request: UnitMoveRequest):
    state = game_state()
    unit = next((value for value in state["units"] if value.get("id") == unit_id), None)
    if unit is None:
        raise ApplicationError(404, "Unit not found.")
    if unit.get("moves_remaining", 0) <= 0:
        raise ApplicationError(409, "This unit has no movement remaining this turn.")
    if hex_distance(unit["x"], unit["y"], request.x, request.y) != 1:
        raise ApplicationError(422, "Units can move one adjacent hex at a time.")
    if not map_repository.has_tile(request.x, request.y):
        raise ApplicationError(404, "Destination tile not found.")
    unit["x"], unit["y"] = request.x, request.y
    unit["moves_remaining"] -= 1
    game_data_repository.save_game_state(state)
    return unit


@synchronized_mutation
def builder_build(unit_id: str, request: BuilderActionRequest):
    if not request.improvement_id:
        raise ApplicationError(422, "An improvement ID is required.")
    state = game_state()
    unit = next((value for value in state["units"] if value.get("id") == unit_id), None)
    if unit is None:
        raise ApplicationError(404, "Unit not found.")
    if unit.get("unit_type") != "UNIT_BUILDER":
        raise ApplicationError(409, "Only Builders can construct improvements.")
    cities = city_repository.get_all()
    city = cities.get(unit["owner_city_id"])
    map_data = map_repository.get_map({})
    tile = next(
        (value for value in map_data.get("tiles", []) if (value["x"], value["y"]) == (unit["x"], unit["y"])),
        None,
    )
    if city is None or tile is None:
        raise ApplicationError(409, "Builder ownership or current tile is invalid.")
    if tile.get("owner_city_id") != city["id"] or (city["x"], city["y"]) == (unit["x"], unit["y"]):
        raise ApplicationError(409, "Improvements require a non-center tile owned by the Builder's city.")
    catalog = game_data_repository.catalog()
    rules = game_rules()
    improvement_rows = index_rows(catalog, "Improvements::Improvements", "ImprovementType")
    row = improvement_rows.get(request.improvement_id)
    if row is None or str(row.get("Buildable", "")).lower() != "true":
        raise ApplicationError(422, "This improvement is not buildable in the extracted game data.")
    row = effective_catalog_row(row, "improvements", request.improvement_id, rules)
    improvement_override = configured_entity(
        rules, "improvements", request.improvement_id
    )
    valid_builders = {
        value.get("UnitType")
        for value in raw_table(catalog, "Improvements::Improvement_ValidBuildUnits")
        if value.get("ImprovementType") == request.improvement_id
    }
    if unit["unit_type"] not in valid_builders:
        raise ApplicationError(409, "This Builder is not listed as a valid unit for the improvement.")
    xml_valid_terrains = [
        value.get("TerrainType")
        for value in raw_table(catalog, "Improvements::Improvement_ValidTerrains")
        if value.get("ImprovementType") == request.improvement_id
    ]
    valid_terrains = improvement_override.get(
        "allowed_terrains", xml_valid_terrains
    )
    terrain_valid = tile.get("terrain") in valid_terrains
    feature_rules = [
        value
        for value in raw_table(catalog, "Improvements::Improvement_ValidFeatures")
        if value.get("ImprovementType") == request.improvement_id
    ]
    valid_features = improvement_override.get(
        "allowed_features",
        [value.get("FeatureType") for value in feature_rules],
    )
    feature_valid = tile.get("feature") in valid_features
    xml_resource_rules = [
        value
        for value in raw_table(catalog, "Improvements::Improvement_ValidResources")
        if value.get("ImprovementType") == request.improvement_id
        and value.get("ResourceType") == tile.get("resource")
    ]
    valid_resources = improvement_override.get("allowed_resources")
    if valid_resources is not None:
        resource_rules = (
            [{"ResourceType": resource} for resource in valid_resources]
            if tile.get("resource") in valid_resources
            else []
        )
    else:
        resource_rules = xml_resource_rules
    if not (terrain_valid or feature_valid or resource_rules):
        raise ApplicationError(409, "The selected tile does not meet the improvement's configured or extracted terrain, feature or resource conditions.")
    if any(str(rule.get("MustRemoveFeature", "")).lower() == "true" for rule in resource_rules) and tile.get("feature"):
        raise ApplicationError(409, "Remove the required feature before building this improvement.")
    if tile.get("feature") and not feature_valid and not resource_rules:
        raise ApplicationError(409, "Remove the incompatible feature before building this improvement.")
    prerequisite_error = city_prerequisite_error(city, row)
    if prerequisite_error:
        raise ApplicationError(409, prerequisite_error)
    if tile.get("district"):
        raise ApplicationError(409, "An improvement cannot be built on a district tile.")
    if tile.get("improvement") == request.improvement_id:
        raise ApplicationError(409, "This improvement is already present.")
    action_charge_cost = improvement_override.get(
        "build_charges", rules["improvements"]["default_build_charges"]
    )
    if unit["charges_remaining"] < action_charge_cost:
        raise ApplicationError(409, "This Builder has insufficient charges for the improvement.")
    tile["improvement"] = request.improvement_id
    unit["charges_remaining"] -= action_charge_cost
    write_json_transaction(
        {locations.map_file(): map_data, locations.game_state_file(): state}
    )
    return {"unit": unit, "tile": tile}


@synchronized_mutation
def builder_remove_feature(unit_id: str):
    rules = game_rules()
    state = game_state()
    unit = next((value for value in state["units"] if value.get("id") == unit_id), None)
    if unit is None:
        raise ApplicationError(404, "Unit not found.")
    if unit.get("unit_type") != "UNIT_BUILDER":
        raise ApplicationError(409, "Only Builders can remove features.")
    catalog = game_data_repository.catalog()
    cities = city_repository.get_all()
    city = cities.get(unit["owner_city_id"])
    map_data = map_repository.get_map({})
    tile = next(
        (value for value in map_data.get("tiles", []) if (value["x"], value["y"]) == (unit["x"], unit["y"])),
        None,
    )
    if city is None or tile is None or tile.get("owner_city_id") != city["id"]:
        raise ApplicationError(409, "Feature removal requires a tile owned by the Builder's city.")
    feature = tile.get("feature")
    feature_row = index_rows(catalog, "Features::Features", "FeatureType").get(feature)
    if not feature_row or str(feature_row.get("Removable", "")).lower() != "true":
        raise ApplicationError(409, "The current feature is not marked removable in the extracted game data.")
    prerequisite_error = city_prerequisite_error(
        city,
        {
            "PrereqTech": feature_row.get("RemoveTech"),
            "PrereqCivic": feature_row.get("AddCivic"),
        },
    )
    if prerequisite_error:
        raise ApplicationError(409, prerequisite_error)
    charge_cost = rules["improvements"]["feature_removal_charge_cost"]
    if unit["charges_remaining"] < charge_cost:
        raise ApplicationError(409, "This Builder has insufficient charges to remove the feature.")
    tile["feature"] = None
    unit["charges_remaining"] -= charge_cost
    write_json_transaction({locations.map_file(): map_data, locations.game_state_file(): state})
    return {"unit": unit, "tile": tile}


@synchronized_mutation
def settler_found_city(unit_id: str, request: FoundCityRequest):
    rules = game_rules()
    state = game_state()
    unit = next((value for value in state["units"] if value.get("id") == unit_id), None)
    if unit is None:
        raise ApplicationError(404, "Unit not found.")
    if unit.get("unit_type") != "UNIT_SETTLER":
        raise ApplicationError(409, "Only Settlers can found cities.")
    catalog = game_data_repository.catalog()
    settler_data = index_rows(catalog, "Units::Units", "UnitType").get(
        "UNIT_SETTLER"
    )
    if settler_data is None or not catalog_flag(settler_data.get("FoundCity")):
        raise ApplicationError(
            422, "Settler city-founding capability is not present in the extracted game data."
        )
    cities = city_repository.get_all()
    owner_city = cities.get(unit.get("owner_city_id"))
    if owner_city is None:
        raise ApplicationError(409, "Settler owner city no longer exists.")
    map_data = map_repository.get_map({})
    center_tile = next(
        (tile for tile in map_data.get("tiles", []) if (tile["x"], tile["y"]) == (unit["x"], unit["y"])),
        None,
    )
    if center_tile is None:
        raise ApplicationError(404, "Settler is not on a valid map tile.")
    for city in cities.values():
        if (
            hex_distance(unit["x"], unit["y"], city["x"], city["y"])
            < rules["city"]["minimum_distance"]
        ):
            raise ApplicationError(
                409,
                f"Cities must be at least {rules['city']['minimum_distance']} tiles apart. "
                f"Nearby city: {city['name']}.",
            )
    initial_tiles = [
        tile
        for tile in map_data.get("tiles", [])
        if (
            hex_distance(unit["x"], unit["y"], tile["x"], tile["y"])
            <= rules["city"]["initial_territory_radius"]
        )
    ]
    if any(tile.get("owner_city_id") is not None for tile in initial_tiles):
        raise ApplicationError(409, "The city center or initial territory is owned by another city.")
    if center_tile.get("district") or "MOUNTAIN" in center_tile.get("terrain", ""):
        raise ApplicationError(409, "This tile cannot host a city center.")
    terrain_row = index_rows(catalog, "Terrains::Terrains", "TerrainType").get(
        center_tile.get("terrain")
    )
    if terrain_row is not None and catalog_flag(terrain_row.get("Water")):
        raise ApplicationError(409, "A city cannot be founded on a water tile.")
    city_id = "city-" + uuid4().hex[:8]
    city = {
        "id": city_id,
        "name": request.name,
        "x": unit["x"],
        "y": unit["y"],
        "population": rules["city"]["initial_population"],
        "civilization": owner_city.get("civilization"),
        "leader": owner_city.get("leader"),
        "buildings": [],
        "districts": [],
        "wonders": [],
        "governor": None,
        "governor_promotions": [],
        "policies": [],
        "technologies": list(owner_city.get("technologies", [])),
        "civics": list(owner_city.get("civics", [])),
        "religion": None,
        "housing": 2,
        "amenities": 0,
        "notes": "",
        "worked_tiles": [],
        "created_from_settler": True,
        "production_queue": [],
        "production_progress": {},
        "current_production": None,
        "production_history": [],
    }
    cities[city_id] = city
    for tile in initial_tiles:
        tile["owner_city_id"] = city_id
        tile["worked"] = tile is center_tile
    refresh_city_food_state(
        city,
        map_data,
        catalog,
        state["game_speed"],
        rules,
    )
    state["units"] = [value for value in state["units"] if value["id"] != unit_id]
    write_json_transaction(
        {
            locations.cities_file(): cities,
            locations.map_file(): map_data,
            locations.game_state_file(): state,
        }
    )
    return {"city": city, "consumed_unit_id": unit_id}
