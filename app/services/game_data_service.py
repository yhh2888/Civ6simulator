from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.exceptions import ApplicationError
from app.core.game_speed import GAME_SPEEDS

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
def game_rules() -> dict:
    return load_game_rules(locations.rules_file())


def configured_entity(
    rules: dict, section: str, entity_id: str
) -> dict:
    return rules.get(section, {}).get("overrides", {}).get(entity_id, {})


def effective_catalog_row(
    row: dict, section: str, entity_id: str, rules: dict
) -> dict:
    effective = dict(row)
    override_fields = {
        "required_tech": "PrereqTech",
        "required_civic": "PrereqCivic",
        "required_district": "PrereqDistrict",
        "min_population": "PrereqPopulation",
        "build_charges": "BuildCharges",
        "base_moves": "BaseMoves",
        "population_cost": "PopulationCost",
        "allowed_terrains": "_configured_allowed_terrains",
        "allowed_features": "_configured_allowed_features",
        "allowed_resources": "_configured_allowed_resources",
    }
    for config_key, row_key in override_fields.items():
        if config_key in configured_entity(rules, section, entity_id):
            effective[row_key] = configured_entity(rules, section, entity_id)[
                config_key
            ]
    return effective


def configured_production_cost(
    category: str, entity_id: str, row: dict, rules: dict
) -> tuple[int | None, str]:
    override = rules["production"]["cost_overrides"].get(category, {}).get(
        entity_id
    )
    if override is not None:
        return override, "game_rules.json override"
    return catalog_cost(row), "extracted XML Cost"


def hex_distance(x1: int, y1: int, x2: int, y2: int) -> int:
    q1 = x1 - (y1 - (y1 & 1)) // 2
    q2 = x2 - (y2 - (y2 & 1)) // 2
    r1, r2 = y1, y2
    return max(abs(q1 - q2), abs(r1 - r2), abs((q1 + r1) - (q2 + r2)))


def game_state() -> dict:
    value = game_data_repository.game_state({"turn": 1, "units": []})
    if not isinstance(value, dict):
        raise ApplicationError(500, "Game state file is invalid.")
    value.setdefault("game_speed", game_rules()["game_speed"])
    value.setdefault("turn", 1)
    value.setdefault("units", [])
    value.setdefault("research", {})
    value.setdefault("technologies", [])
    value.setdefault("civics", [])
    if (
        not isinstance(value["technologies"], list)
        or any(not isinstance(item, str) or not item for item in value["technologies"])
        or not isinstance(value["civics"], list)
        or any(not isinstance(item, str) or not item for item in value["civics"])
        or not isinstance(value["research"], dict)
    ):
        raise ApplicationError(500, "Game state research data is invalid.")
    cities = city_repository.get_all()
    legacy_technologies = set()
    legacy_civics = set()
    if isinstance(cities, dict):
        for city in cities.values():
            if not isinstance(city, dict):
                continue
            technologies = city.get("technologies", [])
            civics = city.get("civics", [])
            if isinstance(technologies, list):
                legacy_technologies.update(
                    item for item in technologies if isinstance(item, str)
                )
            if isinstance(civics, list):
                legacy_civics.update(
                    item for item in civics if isinstance(item, str)
                )
    value["technologies"] = sorted(set(value["technologies"]) | legacy_technologies)
    value["civics"] = sorted(set(value["civics"]) | legacy_civics)
    value["research"].setdefault(
        "technology", {"current_id": None, "progress": {}}
    )
    value["research"].setdefault("civic", {"current_id": None, "progress": {}})
    if (
        not isinstance(value["turn"], int)
        or isinstance(value["turn"], bool)
        or value["turn"] < 1
        or not isinstance(value["units"], list)
        or not isinstance(value["game_speed"], str)
        or value["game_speed"] not in GAME_SPEEDS
    ):
        raise ApplicationError(500, "Game state turn, speed or unit collection is invalid.")
    catalog = game_data_repository.catalog()
    known_unit_types = {
        row.get("UnitType")
        for row in raw_table(catalog, "Units::Units")
        if isinstance(row.get("UnitType"), str)
    } | {"UNIT_BUILDER", "UNIT_SETTLER"}
    for category in ("technology", "civic"):
        track = value["research"].get(category)
        if (
            not isinstance(track, dict)
            or (
                track.get("current_id") is not None
                and (
                    not isinstance(track.get("current_id"), str)
                    or not track["current_id"]
                )
            )
            or not isinstance(track.get("progress"), dict)
        ):
            raise ApplicationError(500, f"Game state {category} research is invalid.")
        for target_id, amount in track["progress"].items():
            if (
                not isinstance(target_id, str)
                or not target_id
                or isinstance(amount, bool)
                or not isinstance(amount, (int, float))
                or not math.isfinite(amount)
                or amount < 0
            ):
                raise ApplicationError(500, f"Game state {category} progress is invalid.")
    unit_ids = set()
    for unit in value["units"]:
        if (
            not isinstance(unit, dict)
            or not isinstance(unit.get("id"), str)
            or unit["id"] in unit_ids
            or unit.get("unit_type") not in known_unit_types
            or not isinstance(unit.get("owner_city_id"), str)
            or any(
                not isinstance(unit.get(key), int) or isinstance(unit.get(key), bool)
                for key in ("x", "y", "moves_remaining", "charges_remaining")
            )
            or unit["moves_remaining"] < 0
            or unit["charges_remaining"] < 0
        ):
            raise ApplicationError(500, "Game state contains an invalid unit.")
        unit_ids.add(unit["id"])
    return value


def raw_table(catalog: dict, name: str) -> list[dict]:
    rows = catalog.get("raw_tables", {}).get(name, [])
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def index_rows(catalog: dict, table: str, key: str) -> dict[str, dict]:
    return {
        row[key]: row
        for row in raw_table(catalog, table)
        if isinstance(row.get(key), str)
    }


def catalog_cost(row: dict) -> int | None:
    if isinstance(row.get("Cost"), bool):
        return None
    try:
        cost = int(row.get("Cost"))
    except (TypeError, ValueError):
        return None
    return cost if cost > 0 else None


def unlocked_research(category: str) -> set[str]:
    state = game_state()
    key = "technologies" if category == "technology" else "civics"
    return set(state[key])


def research_prerequisites(catalog: dict, category: str, target_id: str) -> list[str]:
    entity_category = "technologies" if category == "technology" else "civics"
    return entity_research_prerequisites(catalog, entity_category, target_id)


def research_catalog(catalog: dict, state: dict) -> dict[str, list[dict]]:
    result = {}
    for category, table, key in (
        ("technology", "Technologies::Technologies", "TechnologyType"),
        ("civic", "Civics::Civics", "CivicType"),
    ):
        completed = set(state["technologies" if category == "technology" else "civics"])
        track = state["research"][category]
        options = []
        for target_id, row in index_rows(catalog, table, key).items():
            prerequisites = research_prerequisites(catalog, category, target_id)
            cost = catalog_cost(row)
            missing_prerequisites = [
                prerequisite
                for prerequisite in prerequisites
                if prerequisite not in completed
            ]
            unavailable_reason = None
            if target_id in completed:
                unavailable_reason = "이미 완료한 연구입니다."
            elif cost is None:
                unavailable_reason = "카탈로그의 유효한 Cost 값이 없습니다."
            elif missing_prerequisites:
                unavailable_reason = "선행 연구 필요: " + ", ".join(missing_prerequisites)
            options.append(
                {
                    "id": target_id,
                    "name": row.get("Name", target_id),
                    "cost": cost,
                    "cost_supported": cost is not None,
                    "prerequisites": prerequisites,
                    "completed": target_id in completed,
                    "available": unavailable_reason is None,
                    "unavailable_reason": unavailable_reason,
                    "progress": track["progress"].get(target_id, 0),
                    "current": track["current_id"] == target_id,
                }
            )
        result[category] = sorted(options, key=lambda item: item["id"])
    return result


def catalog_integer(row: dict, field: str, default: int, entity_id: str) -> int:
    value = row[field] if field in row else default
    if value is None or isinstance(value, bool):
        raise ApplicationError(422, f"Invalid {field} data for {entity_id}.")
    if isinstance(value, float) and not value.is_integer():
        raise ApplicationError(422, f"Invalid {field} data for {entity_id}.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ApplicationError(422, f"Invalid {field} data for {entity_id}.") from error
    if parsed < 0:
        raise ApplicationError(422, f"Invalid {field} data for {entity_id}.")
    return parsed


def city_prerequisite_error(city: dict, row: dict) -> str | None:
    missing = missing_research_requirements(
        row,
        unlocked_research("technology"),
        unlocked_research("civic"),
    )
    return missing[0] if missing else None


def catalog_flag(value: Any) -> bool:
    return str(value).strip().lower() in ("true", "1")


def completed_districts(city: dict, map_data: dict) -> set[str]:
    completed = {
        tile.get("district")
        for tile in map_data.get("tiles", [])
        if tile.get("owner_city_id") == city["id"] and tile.get("district")
    }
    completed.update(
        district
        for district in city.get("districts", [])
        if isinstance(district, str)
    )
    completed.add("DISTRICT_CITY_CENTER")
    return completed


def production_catalog(catalog: dict, rules: dict | None = None) -> list[dict]:
    rules = rules or game_rules()
    units = index_rows(catalog, "Units::Units", "UnitType")
    buildings = index_rows(catalog, "Buildings::Buildings", "BuildingType")
    districts = index_rows(catalog, "Districts::Districts", "DistrictType")
    building_kinds = {
        row.get("Type"): row.get("Kind")
        for row in raw_table(catalog, "Buildings::Types")
        if isinstance(row.get("Type"), str)
    }
    result = []
    for unit_id in ("UNIT_BUILDER", "UNIT_SETTLER"):
        row = units.get(unit_id)
        if row:
            cost, cost_source = configured_production_cost(
                "unit", unit_id, row, rules
            )
            row = effective_catalog_row(row, "units", unit_id, rules)
            result.append(
                {
                    "category": "unit",
                    "id": unit_id,
                    "cost": cost,
                    "cost_source": cost_source if cost is not None else None,
                    "cost_supported": cost is not None,
                    "unsupported_reason": None if cost is not None else "Missing or invalid XML Cost and no JSON override.",
                    "name": row.get("Name", unit_id),
                    "cost_progression_model": row.get("CostProgressionModel"),
                    "cost_progression_param": row.get("CostProgressionParam1"),
                    "prereq_population": catalog_integer(
                        row, "PrereqPopulation", 0, unit_id
                    ),
                }
            )
    for district_id, row in districts.items():
        if district_id == "DISTRICT_CITY_CENTER" or str(row.get("InternalOnly", "")).lower() == "true":
            continue
        cost, cost_source = configured_production_cost(
            "district", district_id, row, rules
        )
        row = effective_catalog_row(row, "districts", district_id, rules)
        result.append(
            {
                "category": "district",
                "id": district_id,
                "cost": cost,
                "cost_source": cost_source if cost is not None else None,
                "cost_supported": cost is not None,
                "unsupported_reason": None if cost is not None else "Missing or invalid XML Cost and no JSON override.",
                "name": row.get("Name", district_id),
                "prereq_tech": row.get("PrereqTech"),
                "prereq_civic": row.get("PrereqCivic"),
                "cost_progression_model": row.get("CostProgressionModel"),
            }
        )
    for building_id, row in buildings.items():
        if building_kinds.get(building_id) not in ("KIND_BUILDING", "KIND_WONDER"):
            continue
        category = (
            "wonder" if building_kinds[building_id] == "KIND_WONDER" else "building"
        )
        cost, cost_source = configured_production_cost(
            category, building_id, row, rules
        )
        row = effective_catalog_row(row, "buildings", building_id, rules)
        result.append(
            {
                "category": category,
                "id": building_id,
                "cost": cost,
                "cost_source": cost_source if cost is not None else None,
                "cost_supported": cost is not None,
                "unsupported_reason": None if cost is not None else "Missing or invalid XML Cost and no JSON override.",
                "name": row.get("Name", building_id),
                "prereq_district": row.get("PrereqDistrict"),
                "prereq_tech": row.get("PrereqTech"),
                "prereq_civic": row.get("PrereqCivic"),
                "cost_progression_model": row.get("CostProgressionModel"),
            }
        )
    return result


def validate_production_target(
    city: dict,
    request: ProductionQueueRequest,
    cities: dict,
    map_data: dict,
    catalog: dict,
    ignore_queue_id: str | None = None,
) -> tuple[dict, int]:
    rules = game_rules()
    options = {
        (item["category"], item["id"]): item
        for item in production_catalog(catalog, rules)
    }
    item = options.get((request.category, request.target_id))
    if item is None:
        raise ApplicationError(422, "Unsupported production target.")
    if not item["cost_supported"]:
        raise ApplicationError(422, item["unsupported_reason"])
    rows_by_type = {
        "unit": ("Units::Units", "UnitType"),
        "building": ("Buildings::Buildings", "BuildingType"),
        "wonder": ("Buildings::Buildings", "BuildingType"),
        "district": ("Districts::Districts", "DistrictType"),
    }
    table, id_key = rows_by_type[request.category]
    section = {
        "unit": "units",
        "building": "buildings",
        "wonder": "buildings",
        "district": "districts",
    }[request.category]
    row = effective_catalog_row(
        index_rows(catalog, table, id_key)[request.target_id],
        section,
        request.target_id,
        rules,
    )
    prerequisite_error = city_prerequisite_error(city, row)
    if prerequisite_error:
        raise ApplicationError(409, prerequisite_error)
    cost = item["cost"]
    if request.category == "unit":
        required_population = catalog_integer(
            row, "PrereqPopulation", 0, request.target_id
        )
        if city.get("population", rules["city"]["initial_population"]) < required_population:
            raise ApplicationError(
                409,
                f"This city needs population {required_population} to produce {request.target_id}.",
            )
        return item, cost

    if request.category == "district":
        district_override = configured_entity(rules, "districts", request.target_id)
        count_built = max(
            sum(
                1
                for tile in map_data.get("tiles", [])
                if tile.get("owner_city_id") == city["id"]
                and tile.get("district") == request.target_id
            ),
            city.get("districts", []).count(request.target_id),
        )
        count_queued = sum(
            1
            for entry in city.get("production_queue", [])
            if entry.get("category") == "district"
            and entry.get("target_id") == request.target_id
            and entry.get("queue_id") != ignore_queue_id
        )
        max_per_city = district_override.get(
            "max_per_city", rules["districts"]["default_max_per_city"]
        )
        if count_built + count_queued >= max_per_city:
            raise ApplicationError(409, "This district is already built or queued in this city.")
        if request.x is None or request.y is None:
            raise ApplicationError(422, "District production requires a placement tile.")
        tile = next(
            (t for t in map_data.get("tiles", []) if (t["x"], t["y"]) == (request.x, request.y)),
            None,
        )
        if tile is None:
            raise ApplicationError(404, "District placement tile not found.")
        if tile.get("owner_city_id") != city["id"]:
            raise ApplicationError(409, "Districts must be placed on this city's owned tiles.")
        if request.x == city["x"] and request.y == city["y"]:
            raise ApplicationError(409, "A district cannot replace the city center.")
        if tile.get("district") or "MOUNTAIN" in tile.get("terrain", ""):
            raise ApplicationError(409, "This tile is already occupied or is not a valid district site.")
        configured_terrains = district_override.get("allowed_terrains")
        valid_terrains = configured_terrains if configured_terrains is not None else [
            r.get("TerrainType")
            for r in raw_table(catalog, "Districts::District_ValidTerrains")
            if r.get("DistrictType") == request.target_id
        ]
        if valid_terrains and tile.get("terrain") not in valid_terrains:
            raise ApplicationError(409, "This district cannot be placed on the selected terrain.")
        occupied_placements = {
            (entry.get("x"), entry.get("y"))
            for entry in city.get("production_queue", [])
            if entry.get("category") in ("district", "wonder")
            and entry.get("queue_id") != ignore_queue_id
        }
        if (request.x, request.y) in occupied_placements:
            raise ApplicationError(409, "Another queued construction already reserves this tile.")
        return item, cost

    districts = completed_districts(city, map_data)
    building_override = configured_entity(rules, "buildings", request.target_id)
    required_district = building_override.get(
        "required_district", row.get("PrereqDistrict")
    )
    if required_district and required_district not in districts:
        raise ApplicationError(409, f"Requires completed district {required_district}.")
    owned_buildings = set(city.get("buildings", [])) | set(city.get("wonders", []))
    building_prereqs: dict[str, list[str]] = {}
    for prerequisite in raw_table(catalog, "Buildings::BuildingPrereqs"):
        building_prereqs.setdefault(prerequisite.get("Building"), []).append(
            prerequisite.get("PrereqBuilding")
        )
    building_prereqs.setdefault(request.target_id, []).extend(
        building_override.get("required_buildings", [])
    )
    missing_buildings = [
        prerequisite
        for prerequisite in building_prereqs.get(request.target_id, [])
        if prerequisite and prerequisite not in owned_buildings
    ]
    if missing_buildings:
        raise ApplicationError(409, f"Requires building {missing_buildings[0]}.")
    if request.target_id in owned_buildings or any(
        entry.get("target_id") == request.target_id
        and entry.get("queue_id") != ignore_queue_id
        for entry in city.get("production_queue", [])
    ):
        raise ApplicationError(409, "This building or wonder is already built or queued.")
    try:
        max_player_instances = catalog_integer(
            row, "MaxPlayerInstances", 0, request.target_id
        )
    except ApplicationError as error:
        raise ApplicationError(
            422, f"Instance limit data is invalid for {request.target_id}."
        ) from error
    if max_player_instances > 0:
        instances = sum(
            other_city.get("buildings", []).count(request.target_id)
            + other_city.get("wonders", []).count(request.target_id)
            + sum(
                1
                for queued in other_city.get("production_queue", [])
                if queued.get("target_id") == request.target_id
                and queued.get("queue_id") != ignore_queue_id
            )
            for other_city in cities.values()
        )
        if instances >= max_player_instances:
            raise ApplicationError(409, "The XML player-wide instance limit has been reached.")
    if request.category == "wonder":
        if any(
            request.target_id in other_city.get("wonders", [])
            or any(
                entry.get("category") == "wonder"
                and entry.get("target_id") == request.target_id
                and entry.get("queue_id") != ignore_queue_id
                for entry in other_city.get("production_queue", [])
            )
            for other_city in cities.values()
        ):
            raise ApplicationError(409, "This world wonder has already been built.")
        if request.x is None or request.y is None:
            raise ApplicationError(422, "Wonder production requires a placement tile.")
        if request.x == city["x"] and request.y == city["y"]:
            raise ApplicationError(409, "A wonder cannot replace the city center.")
        tile = next(
            (t for t in map_data.get("tiles", []) if (t["x"], t["y"]) == (request.x, request.y)),
            None,
        )
        if tile is None:
            raise ApplicationError(404, "Wonder placement tile not found.")
        if tile.get("owner_city_id") != city["id"] or tile.get("district"):
            raise ApplicationError(409, "A wonder requires an unoccupied tile owned by this city.")
        if any(
            entry.get("queue_id") != ignore_queue_id
            and entry.get("category") in ("district", "wonder")
            and (entry.get("x"), entry.get("y")) == (request.x, request.y)
            for entry in city.get("production_queue", [])
        ):
            raise ApplicationError(409, "Another queued construction already reserves this tile.")
        valid_terrains = [
            r.get("TerrainType")
            for r in raw_table(catalog, "Buildings::Building_ValidTerrains")
            if r.get("BuildingType") == request.target_id
        ]
        if valid_terrains and tile.get("terrain") not in valid_terrains:
            raise ApplicationError(409, "This wonder cannot be placed on the selected terrain.")
        required_features = [
            r.get("FeatureType")
            for r in raw_table(catalog, "Buildings::Building_RequiredFeatures")
            if r.get("BuildingType") == request.target_id
        ]
        if required_features and tile.get("feature") not in required_features:
            raise ApplicationError(409, "This wonder requires a specific feature on its placement tile.")
    return item, cost


def get_game_rules():
    return game_rules()


def get_catalog():
    cat = game_data_repository.catalog()
    return {k:v for k,v in cat.items() if k != "raw_tables"}
