from __future__ import annotations
import json
import math
import re
import os
import tempfile
from functools import wraps
from threading import RLock
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from typing import Any
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from app.config import (
    RuleConfigurationError,
    YIELD_KEYS,
    load_game_rules,
    matching_tile_yield_modifiers,
)

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
STATIC = ROOT / "static"
SAVED_MAPS = DATA / "saved_maps"
GAME_STATE = DATA / "game_state.json"
STATE_LOCK = RLock()
app = FastAPI(title="Civ VI City Studio Map Editor", version="0.3.0")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def game_rules() -> dict:
    return load_game_rules(DATA / "game_rules.json")


@app.exception_handler(RuleConfigurationError)
async def handle_rule_configuration_error(request, error: RuleConfigurationError):
    return JSONResponse(status_code=500, content={"detail": str(error)})


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


def synchronized_mutation(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with STATE_LOCK:
            return function(*args, **kwargs)

    return wrapped

def read(path: Path, default: Any):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default

def write(path: Path, data: Any):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def write_atomically(path: Path, data: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
        ) as temp_file:
            temp_path = Path(temp_file.name)
            json.dump(data, temp_file, ensure_ascii=False, indent=2)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()

def hex_distance(x1: int, y1: int, x2: int, y2: int) -> int:
    q1 = x1 - (y1 - (y1 & 1)) // 2
    q2 = x2 - (y2 - (y2 & 1)) // 2
    r1, r2 = y1, y2
    return max(abs(q1 - q2), abs(r1 - r2), abs((q1 + r1) - (q2 + r2)))

class CityCreate(BaseModel):
    name: str = "New City"
    x: int
    y: int
    population: int | None = Field(default=None, ge=1)
    civilization: str | None = None
    leader: str | None = None

class TileOwnership(BaseModel):
    owned: bool

class SaveMapRequest(BaseModel):
    name: str = Field(min_length=1, max_length=84)

class ProductionQueueRequest(BaseModel):
    category: str
    target_id: str
    x: int | None = None
    y: int | None = None

class ProductionReorderRequest(BaseModel):
    queue_id: str
    position: int = Field(ge=0)

class UnitMoveRequest(BaseModel):
    x: int
    y: int

class BuilderActionRequest(BaseModel):
    improvement_id: str | None = None

class FoundCityRequest(BaseModel):
    name: str = Field(default="New City", min_length=1, max_length=80)

class ResearchSelectRequest(BaseModel):
    target_id: str

def game_state() -> dict:
    value = read(GAME_STATE, {"turn": 1, "units": []})
    if not isinstance(value, dict):
        raise HTTPException(500, "Game state file is invalid.")
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
        raise HTTPException(500, "Game state research data is invalid.")
    cities = read(DATA / "cities.json", {})
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
    ):
        raise HTTPException(500, "Game state turn or unit collection is invalid.")
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
            raise HTTPException(500, f"Game state {category} research is invalid.")
        for target_id, amount in track["progress"].items():
            if (
                not isinstance(target_id, str)
                or not target_id
                or isinstance(amount, bool)
                or not isinstance(amount, (int, float))
                or not math.isfinite(amount)
                or amount < 0
            ):
                raise HTTPException(500, f"Game state {category} progress is invalid.")
    unit_ids = set()
    for unit in value["units"]:
        if (
            not isinstance(unit, dict)
            or not isinstance(unit.get("id"), str)
            or unit["id"] in unit_ids
            or unit.get("unit_type") not in ("UNIT_BUILDER", "UNIT_SETTLER")
            or not isinstance(unit.get("owner_city_id"), str)
            or any(
                not isinstance(unit.get(key), int) or isinstance(unit.get(key), bool)
                for key in ("x", "y", "moves_remaining", "charges_remaining")
            )
            or unit["moves_remaining"] < 0
            or unit["charges_remaining"] < 0
        ):
            raise HTTPException(500, "Game state contains an invalid unit.")
        unit_ids.add(unit["id"])
    return value

def write_json_transaction(updates: dict[Path, Any]):
    staged: dict[Path, Path] = {}
    originals = {
        path: path.read_bytes() if path.exists() else None for path in updates
    }
    try:
        for path, data in updates.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
            ) as temp_file:
                json.dump(data, temp_file, ensure_ascii=False, indent=2)
                staged[path] = Path(temp_file.name)
        for path, temp_path in staged.items():
            os.replace(temp_path, path)
    except Exception:
        for path, original in originals.items():
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(original)
        raise
    finally:
        for temp_path in staged.values():
            temp_path.unlink(missing_ok=True)

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
    if category == "technology":
        table, target_key, prereq_key = (
            "Technologies::TechnologyPrereqs",
            "Technology",
            "PrereqTech",
        )
    else:
        table, target_key, prereq_key = (
            "Civics::CivicPrereqs",
            "Civic",
            "PrereqCivic",
        )
    return sorted(
        {
            row[prereq_key]
            for row in raw_table(catalog, table)
            if row.get(target_key) == target_id
            and isinstance(row.get(prereq_key), str)
        }
    )

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
        raise HTTPException(422, f"Invalid {field} data for {entity_id}.")
    if isinstance(value, float) and not value.is_integer():
        raise HTTPException(422, f"Invalid {field} data for {entity_id}.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise HTTPException(422, f"Invalid {field} data for {entity_id}.") from error
    if parsed < 0:
        raise HTTPException(422, f"Invalid {field} data for {entity_id}.")
    return parsed

def city_prerequisite_error(city: dict, row: dict) -> str | None:
    tech = row.get("PrereqTech")
    civic = row.get("PrereqCivic")
    if tech and tech not in unlocked_research("technology"):
        return f"Requires technology {tech}."
    if civic and civic not in unlocked_research("civic"):
        return f"Requires civic {civic}."
    return None

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
        raise HTTPException(422, "Unsupported production target.")
    if not item["cost_supported"]:
        raise HTTPException(422, item["unsupported_reason"])
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
        raise HTTPException(409, prerequisite_error)
    cost = item["cost"]
    if request.category == "unit":
        required_population = catalog_integer(
            row, "PrereqPopulation", 0, request.target_id
        )
        if city.get("population", rules["city"]["initial_population"]) < required_population:
            raise HTTPException(
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
            raise HTTPException(409, "This district is already built or queued in this city.")
        if request.x is None or request.y is None:
            raise HTTPException(422, "District production requires a placement tile.")
        tile = next(
            (t for t in map_data.get("tiles", []) if (t["x"], t["y"]) == (request.x, request.y)),
            None,
        )
        if tile is None:
            raise HTTPException(404, "District placement tile not found.")
        if tile.get("owner_city_id") != city["id"]:
            raise HTTPException(409, "Districts must be placed on this city's owned tiles.")
        if request.x == city["x"] and request.y == city["y"]:
            raise HTTPException(409, "A district cannot replace the city center.")
        if tile.get("district") or "MOUNTAIN" in tile.get("terrain", ""):
            raise HTTPException(409, "This tile is already occupied or is not a valid district site.")
        configured_terrains = district_override.get("allowed_terrains")
        valid_terrains = configured_terrains if configured_terrains is not None else [
            r.get("TerrainType")
            for r in raw_table(catalog, "Districts::District_ValidTerrains")
            if r.get("DistrictType") == request.target_id
        ]
        if valid_terrains and tile.get("terrain") not in valid_terrains:
            raise HTTPException(409, "This district cannot be placed on the selected terrain.")
        occupied_placements = {
            (entry.get("x"), entry.get("y"))
            for entry in city.get("production_queue", [])
            if entry.get("category") in ("district", "wonder")
            and entry.get("queue_id") != ignore_queue_id
        }
        if (request.x, request.y) in occupied_placements:
            raise HTTPException(409, "Another queued construction already reserves this tile.")
        return item, cost

    districts = completed_districts(city, map_data)
    building_override = configured_entity(rules, "buildings", request.target_id)
    required_district = building_override.get(
        "required_district", row.get("PrereqDistrict")
    )
    if required_district and required_district not in districts:
        raise HTTPException(409, f"Requires completed district {required_district}.")
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
        raise HTTPException(409, f"Requires building {missing_buildings[0]}.")
    if request.target_id in owned_buildings or any(
        entry.get("target_id") == request.target_id
        and entry.get("queue_id") != ignore_queue_id
        for entry in city.get("production_queue", [])
    ):
        raise HTTPException(409, "This building or wonder is already built or queued.")
    try:
        max_player_instances = catalog_integer(
            row, "MaxPlayerInstances", 0, request.target_id
        )
    except HTTPException as error:
        raise HTTPException(
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
            raise HTTPException(409, "The XML player-wide instance limit has been reached.")
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
            raise HTTPException(409, "This world wonder has already been built.")
        if request.x is None or request.y is None:
            raise HTTPException(422, "Wonder production requires a placement tile.")
        if request.x == city["x"] and request.y == city["y"]:
            raise HTTPException(409, "A wonder cannot replace the city center.")
        tile = next(
            (t for t in map_data.get("tiles", []) if (t["x"], t["y"]) == (request.x, request.y)),
            None,
        )
        if tile is None:
            raise HTTPException(404, "Wonder placement tile not found.")
        if tile.get("owner_city_id") != city["id"] or tile.get("district"):
            raise HTTPException(409, "A wonder requires an unoccupied tile owned by this city.")
        if any(
            entry.get("queue_id") != ignore_queue_id
            and entry.get("category") in ("district", "wonder")
            and (entry.get("x"), entry.get("y")) == (request.x, request.y)
            for entry in city.get("production_queue", [])
        ):
            raise HTTPException(409, "Another queued construction already reserves this tile.")
        valid_terrains = [
            r.get("TerrainType")
            for r in raw_table(catalog, "Buildings::Building_ValidTerrains")
            if r.get("BuildingType") == request.target_id
        ]
        if valid_terrains and tile.get("terrain") not in valid_terrains:
            raise HTTPException(409, "This wonder cannot be placed on the selected terrain.")
        required_features = [
            r.get("FeatureType")
            for r in raw_table(catalog, "Buildings::Building_RequiredFeatures")
            if r.get("BuildingType") == request.target_id
        ]
        if required_features and tile.get("feature") not in required_features:
            raise HTTPException(409, "This wonder requires a specific feature on its placement tile.")
    return item, cost
def saved_map_path(name: str) -> Path:
    if "/" in name or "\\" in name:
        raise HTTPException(422, "Map file name must not contain a path.")
    filename = Path(name).name
    stem = filename[:-5] if filename.lower().endswith(".json") else filename
    if (
        not stem
        or ".." in stem
        or re.fullmatch(r"[A-Za-z0-9가-힣][A-Za-z0-9가-힣 _.-]{0,79}", stem) is None
    ):
        raise HTTPException(422, "Invalid map file name.")
    return SAVED_MAPS / f"{stem}.json"

def validate_map_snapshot(snapshot: Any) -> tuple[dict, dict]:
    rules = game_rules()
    if not isinstance(snapshot, dict):
        raise HTTPException(422, "Saved map must contain a JSON object.")
    map_data = snapshot.get("map")
    cities = snapshot.get("cities")
    if not isinstance(map_data, dict) or not isinstance(cities, dict):
        raise HTTPException(422, "Saved map must include map and cities objects.")

    width, height = map_data.get("width"), map_data.get("height")
    tiles = map_data.get("tiles")
    count = map_data.get("count", len(tiles) if isinstance(tiles, list) else None)
    if (
        not isinstance(width, int)
        or isinstance(width, bool)
        or not isinstance(height, int)
        or isinstance(height, bool)
        or width < 1
        or height < 1
        or not isinstance(tiles, list)
        or len(tiles) != width * height
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count != len(tiles)
    ):
        raise HTTPException(422, "Saved map dimensions or tile count are invalid.")
    map_data["count"] = count

    positions = set()
    for tile in tiles:
        if not isinstance(tile, dict):
            raise HTTPException(422, "Saved map contains an invalid tile.")
        x, y = tile.get("x"), tile.get("y")
        if (
            not isinstance(x, int)
            or isinstance(x, bool)
            or not isinstance(y, int)
            or isinstance(y, bool)
            or not (0 <= x < width and 0 <= y < height)
            or (x, y) in positions
            or not isinstance(tile.get("terrain"), str)
        ):
            raise HTTPException(422, "Saved map contains invalid or duplicate coordinates.")
        positions.add((x, y))

    for city_id, city in cities.items():
        if (
            not isinstance(city_id, str)
            or not isinstance(city, dict)
            or city.get("id") != city_id
            or not isinstance(city.get("name"), str)
            or not isinstance(city.get("x"), int)
            or isinstance(city.get("x"), bool)
            or not isinstance(city.get("y"), int)
            or isinstance(city.get("y"), bool)
            or (city["x"], city["y"]) not in positions
            or not isinstance(city.get("population"), int)
            or isinstance(city.get("population"), bool)
            or not 1 <= city["population"] <= rules["city"]["maximum_population"]
        ):
            raise HTTPException(422, "Saved map contains an invalid city.")
        queue = city.get("production_queue", [])
        progress = city.get("production_progress", {})
        if not isinstance(queue, list) or not isinstance(progress, dict):
            raise HTTPException(422, "Saved city production data is invalid.")
        queue_ids = set()
        for entry in queue:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("queue_id"), str)
                or entry["queue_id"] in queue_ids
                or entry.get("category") not in ("unit", "building", "district", "wonder")
                or not isinstance(entry.get("target_id"), str)
            ):
                raise HTTPException(422, "Saved production queue contains an invalid entry.")
            queue_ids.add(entry["queue_id"])
        if city.get("current_production") not in (None, *queue_ids):
            raise HTTPException(422, "Saved current production does not reference its queue.")
        if not set(progress).issubset(queue_ids):
            raise HTTPException(422, "Saved production progress references a missing queue item.")
        for amount in progress.values():
            if isinstance(amount, bool):
                raise HTTPException(422, "Saved production progress is invalid.")
            try:
                numeric_amount = float(amount)
            except (TypeError, ValueError) as error:
                raise HTTPException(422, "Saved production progress is invalid.") from error
            if not math.isfinite(numeric_amount) or numeric_amount < 0:
                raise HTTPException(422, "Saved production progress must be finite and non-negative.")
    for tile in tiles:
        owner_city_id = tile.get("owner_city_id")
        if owner_city_id is not None and (
            not isinstance(owner_city_id, str) or owner_city_id not in cities
        ):
            raise HTTPException(422, "Saved map refers to a missing city owner.")

    return map_data, cities

def validate_game_state_snapshot(state: Any, cities: dict, map_data: dict) -> dict:
    if (
        not isinstance(state, dict)
        or not isinstance(state.get("turn", 1), int)
        or isinstance(state.get("turn", 1), bool)
        or state.get("turn", 1) < 1
        or not isinstance(state.get("units", []), list)
    ):
        raise HTTPException(422, "Saved game state is invalid.")
    unit_ids = set()
    positions = {(tile["x"], tile["y"]) for tile in map_data["tiles"]}
    for unit in state.get("units", []):
        if not isinstance(unit, dict):
            raise HTTPException(422, "Saved game state contains an invalid unit.")
        numeric_fields = ("x", "y", "moves_remaining", "charges_remaining")
        if (
            not isinstance(unit.get("id"), str)
            or unit["id"] in unit_ids
            or unit.get("owner_city_id") not in cities
            or unit.get("unit_type") not in ("UNIT_BUILDER", "UNIT_SETTLER")
            or any(
                not isinstance(unit.get(key), int) or isinstance(unit.get(key), bool)
                for key in numeric_fields
            )
            or unit["moves_remaining"] < 0
            or unit["charges_remaining"] < 0
            or (unit["x"], unit["y"]) not in positions
        ):
            raise HTTPException(422, "Saved game state contains an invalid unit.")
        unit_ids.add(unit["id"])
    technologies = state.get("technologies", [])
    civics = state.get("civics", [])
    research = state.get("research", {})
    if (
        not isinstance(technologies, list)
        or any(not isinstance(item, str) or not item for item in technologies)
        or not isinstance(civics, list)
        or any(not isinstance(item, str) or not item for item in civics)
        or not isinstance(research, dict)
    ):
        raise HTTPException(422, "Saved game research state is invalid.")
    technologies = set(technologies)
    civics = set(civics)
    for city in cities.values():
        if isinstance(city.get("technologies", []), list):
            technologies.update(
                item for item in city.get("technologies", []) if isinstance(item, str)
            )
        if isinstance(city.get("civics", []), list):
            civics.update(
                item for item in city.get("civics", []) if isinstance(item, str)
            )
    validated_research = {}
    for category in ("technology", "civic"):
        track = research.get(category, {"current_id": None, "progress": {}})
        if (
            not isinstance(track, dict)
            or (
                track.get("current_id") is not None
                and (
                    not isinstance(track["current_id"], str)
                    or not track["current_id"]
                )
            )
            or not isinstance(track.get("progress", {}), dict)
        ):
            raise HTTPException(422, f"Saved {category} research is invalid.")
        progress = track.get("progress", {})
        for target_id, amount in progress.items():
            if (
                not isinstance(target_id, str)
                or not target_id
                or isinstance(amount, bool)
                or not isinstance(amount, (int, float))
                or not math.isfinite(amount)
                or amount < 0
            ):
                raise HTTPException(422, f"Saved {category} research progress is invalid.")
        validated_research[category] = {
            "current_id": track.get("current_id"),
            "progress": progress,
        }
    return {
        "turn": state.get("turn", 1),
        "units": state.get("units", []),
        "technologies": sorted(technologies),
        "civics": sorted(civics),
        "research": validated_research,
    }

def yield_values(values: Any) -> dict[str, float]:
    result = {key: 0.0 for key in YIELD_KEYS}
    if not isinstance(values, dict):
        return result
    for key in YIELD_KEYS:
        try:
            result[key] = float(values.get(key, 0) or 0)
        except (TypeError, ValueError):
            continue
    return result

def add_yield_values(total: dict[str, float], values: dict[str, float]):
    for key in YIELD_KEYS:
        total[key] += values.get(key, 0.0)

def extracted_yield_map(catalog: dict, catalog_key: str, list_key: str) -> dict:
    values = catalog.get(catalog_key)
    if isinstance(values, dict):
        return values
    return {
        item["id"]: item.get("yields", {})
        for item in catalog.get(list_key, [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }

def raw_yield_map(catalog: dict, table_name: str, item_column: str) -> dict:
    result = {}
    rows = catalog.get("raw_tables", {}).get(table_name, [])
    if not isinstance(rows, list):
        return result
    for row in rows:
        if not isinstance(row, dict):
            continue
        item_id = row.get(item_column)
        yield_id = row.get("YieldType")
        if not isinstance(item_id, str) or not isinstance(yield_id, str):
            continue
        key = yield_id.removeprefix("YIELD_").title()
        if key not in YIELD_KEYS:
            continue
        try:
            result.setdefault(item_id, {})[key] = (
                result.setdefault(item_id, {}).get(key, 0.0)
                + float(row.get("YieldChange", 0) or 0)
            )
        except (TypeError, ValueError):
            continue
    return result

def calculate_city_yields(
    city: dict, map_data: dict, catalog: dict, rules: dict | None = None
) -> dict:
    rules = rules or game_rules()
    yield_keys = tuple(YIELD_KEYS)
    terrain_yields = extracted_yield_map(catalog, "terrain_yields", "terrains")
    feature_yields = extracted_yield_map(catalog, "feature_yields", "features")
    improvement_yields = extracted_yield_map(
        catalog, "improvement_yields", "improvements"
    )
    resource_yields = raw_yield_map(
        catalog, "Resources::Resource_YieldChanges", "ResourceType"
    )
    district_citizen_yields = raw_yield_map(
        catalog, "Districts::District_CitizenYieldChanges", "DistrictType"
    )
    building_yields = extracted_yield_map(catalog, "building_yields", "buildings")
    resources = {
        item["id"]: item.get("raw", {}).get("ResourceClassType", "UNKNOWN")
        for item in catalog.get("resources", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    tiles = map_data.get("tiles", [])
    city_id = city["id"]
    center = (city["x"], city["y"])
    city_tiles = [
        tile
        for tile in tiles
        if tile.get("owner_city_id") == city_id
        or (tile["x"], tile["y"]) == center
    ]
    other_worked = [
        tile
        for tile in city_tiles
        if (tile["x"], tile["y"]) != center and bool(tile.get("worked"))
    ]
    available_worker_slots = (
        max(0, int(city.get("population", rules["city"]["initial_population"])))
        * rules["city"]["worked_tiles_per_population"]
    )
    active_workers = {
        (tile["x"], tile["y"])
        for tile in other_worked[:available_worker_slots]
    }
    totals = {key: 0.0 for key in yield_keys}
    base_totals = {key: 0.0 for key in yield_keys}
    tile_bonus_totals = {key: 0.0 for key in yield_keys}
    tile_results = []

    for tile in city_tiles:
        terrain_id = tile.get("terrain", "")
        feature_id = tile.get("feature")
        resource_id = tile.get("resource")
        improvement_id = tile.get("improvement")
        district_id = tile.get("district")
        is_center = (tile["x"], tile["y"]) == center
        selected_to_work = bool(tile.get("worked")) and not is_center
        worked = is_center or (
            selected_to_work and (tile["x"], tile["y"]) in active_workers
        )
        base = yield_values(terrain_yields.get(terrain_id))
        if terrain_id not in terrain_yields:
            base_terrain = next(
                (
                    name
                    for name in ("GRASS", "PLAINS", "DESERT", "TUNDRA", "SNOW", "COAST", "OCEAN", "LAKE")
                    if f"TERRAIN_{name}" in terrain_id
                ),
                None,
            )
            fallback = rules["tile_yields"]["fallback_terrain_yields"]
            base = yield_values(fallback.get(base_terrain, {}))
            if "HILLS" in terrain_id:
                base["Production"] += rules["tile_yields"]["hill_production_delta"]
        terrain_is_hills = "HILLS" in terrain_id
        hill_override = tile.get("hill_override")
        is_hills = (
            bool(hill_override)
            if hill_override is not None
            else terrain_is_hills
        )
        if hill_override is not None and bool(hill_override) != terrain_is_hills:
            delta = rules["tile_yields"]["hill_production_delta"]
            base["Production"] += delta if hill_override else -delta
            base["Production"] = max(0.0, base["Production"])
        bonus = {key: 0.0 for key in yield_keys}
        components = []
        for kind, item_id, source_map in (
            ("feature", feature_id, feature_yields),
            ("resource", resource_id, resource_yields),
            ("improvement", improvement_id, improvement_yields),
        ):
            if item_id and item_id in source_map:
                values = yield_values(source_map[item_id])
                add_yield_values(bonus, values)
                components.append({"type": kind, "id": item_id, "yields": values})
        modifier_context = {
            "terrain": terrain_id,
            "feature": feature_id,
            "resource": resource_id,
            "improvement": improvement_id,
            "district": district_id,
            "worked": worked,
            "city_center": is_center,
            "is_hills": is_hills,
            "river_adjacent": bool(tile.get("river") or tile.get("river_wires")),
        }
        if worked and district_id in district_citizen_yields:
            values = yield_values(district_citizen_yields[district_id])
            add_yield_values(bonus, values)
            components.append(
                {"type": "district_citizen", "id": district_id, "yields": values}
            )
        modifier_results = []
        for modifier in matching_tile_yield_modifiers(
            modifier_context, rules["tile_yield_modifiers"]
        ):
            before = {
                key: base[key] + bonus[key]
                for key in yield_keys
            }
            values = yield_values(modifier["yield_changes"])
            add_yield_values(bonus, values)
            after = {
                key: base[key] + bonus[key]
                for key in yield_keys
            }
            detail = {
                "type": "modifier",
                "id": modifier["id"],
                "description": modifier.get("description", ""),
                "before_yields": before,
                "changes": values,
                "after_yields": after,
            }
            modifier_results.append(detail)
            components.append(detail)
        if is_center:
            center_minimums = rules["city"]["city_center_min_yields"]
            for yield_name, minimum in center_minimums.items():
                bonus[yield_name] += max(
                    0.0, minimum - base[yield_name] - bonus[yield_name]
                )
        total = {key: base[key] + bonus[key] for key in yield_keys}
        if worked:
            add_yield_values(totals, total)
            add_yield_values(base_totals, base)
            add_yield_values(tile_bonus_totals, bonus)
        tile_results.append(
            {
                "x": tile["x"],
                "y": tile["y"],
                "worked": worked,
                "selected_to_work": selected_to_work,
                "city_center": is_center,
                "owner_city_id": tile.get("owner_city_id"),
                "terrain": terrain_id,
                "is_hills": is_hills,
                "hill_source": (
                    "manual_override"
                    if hill_override is not None
                    else "terrain_id"
                ),
                "feature": feature_id,
                "resource": resource_id,
                "resource_class": resources.get(resource_id),
                "improvement": improvement_id,
                "district": district_id,
                "river_adjacent": bool(tile.get("river") or tile.get("river_wires")),
                "base_yields": base,
                "bonus_yields": bonus,
                "yields": total,
                "components": components,
                "applied_modifiers": modifier_results,
            }
        )

    population = max(0, int(city.get("population", rules["city"]["initial_population"])))
    population_yields = {
        key: amount * population
        for key, amount in yield_values(rules["city"]["population_yields"]).items()
    }
    add_yield_values(totals, population_yields)

    district_links = catalog.get("raw_tables", {}).get(
        "Districts::District_Adjacencies", []
    )
    adjacency_rules = {
        row.get("ID"): row
        for row in catalog.get("raw_tables", {}).get(
            "Districts::Adjacency_YieldChanges", []
        )
        if isinstance(row, dict) and isinstance(row.get("ID"), str)
    }
    feature_rows = {
        item.get("id"): item
        for item in catalog.get("features", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    natural_wonders = {
        feature_id
        for feature_id, item in feature_rows.items()
        if str(item.get("raw", {}).get("NaturalWonder", "")).lower() == "true"
    }
    supported_conditions = {
        "AdjacentTerrain",
        "AdjacentFeature",
        "AdjacentRiver",
        "AdjacentNaturalWonder",
        "AdjacentWonder",
        "AdjacentResource",
        "AdjacentSeaResource",
        "AdjacentImprovement",
        "AdjacentDistrict",
        "OtherDistrictAdjacent",
    }
    districts = []
    district_totals = {key: 0.0 for key in YIELD_KEYS}
    for district_tile in city_tiles:
        district_id = district_tile.get("district")
        if not district_id:
            continue
        neighbors = [
            other
            for other in tiles
            if hex_distance(
                district_tile["x"], district_tile["y"], other["x"], other["y"]
            ) == 1
        ]
        district_yields = {key: 0.0 for key in YIELD_KEYS}
        district_components = []
        links = [
            row
            for row in district_links
            if isinstance(row, dict) and row.get("DistrictType") == district_id
        ]
        for link in links:
            rule = adjacency_rules.get(link.get("YieldChangeId"), {})
            conditions = {
                key: value
                for key, value in rule.items()
                if key in supported_conditions and value not in (None, "", "false", "0")
            }
            if len(conditions) != 1:
                continue
            condition, expected = next(iter(conditions.items()))
            matching = 0
            for neighbor in neighbors:
                neighbor_terrain = neighbor.get("terrain")
                neighbor_feature = neighbor.get("feature")
                neighbor_resource = neighbor.get("resource")
                if condition == "AdjacentTerrain":
                    matching += neighbor_terrain == expected
                elif condition == "AdjacentFeature":
                    matching += neighbor_feature == expected
                elif condition == "AdjacentRiver":
                    matching += bool(
                        neighbor.get("river") or neighbor.get("river_wires")
                    )
                elif condition in ("AdjacentNaturalWonder", "AdjacentWonder"):
                    matching += (
                        neighbor_feature in natural_wonders
                        or neighbor.get("district") == "DISTRICT_WONDER"
                    )
                elif condition == "AdjacentResource":
                    matching += bool(neighbor_resource)
                elif condition == "AdjacentSeaResource":
                    matching += bool(neighbor_resource) and str(
                        neighbor_terrain
                    ).endswith(("COAST", "OCEAN"))
                elif condition == "AdjacentImprovement":
                    matching += neighbor.get("improvement") == expected
                elif condition == "AdjacentDistrict":
                    matching += neighbor.get("district") == expected
                elif condition == "OtherDistrictAdjacent":
                    matching += bool(neighbor.get("district"))
            try:
                tiles_required = max(1, int(rule.get("TilesRequired", 1)))
                change = float(rule.get("YieldChange", 0) or 0)
            except (TypeError, ValueError):
                continue
            amount = (matching // tiles_required) * change
            yield_key = str(rule.get("YieldType", "")).removeprefix("YIELD_").title()
            if amount and yield_key in YIELD_KEYS:
                district_yields[yield_key] += amount
                district_components.append(
                    {
                        "rule": link.get("YieldChangeId"),
                        "yield": yield_key,
                        "matching_tiles": matching,
                        "amount": amount,
                    }
                )
        add_yield_values(totals, district_yields)
        add_yield_values(district_totals, district_yields)
        districts.append(
            {
                "x": district_tile["x"],
                "y": district_tile["y"],
                "id": district_id,
                "yields": district_yields,
                "components": district_components,
            }
        )

    building_results = []
    building_totals = {key: 0.0 for key in YIELD_KEYS}
    building_ids = dict.fromkeys(city.get("buildings", []) + city.get("wonders", []))
    for building_id in building_ids:
        values = building_yields.get(building_id)
        if values is None:
            continue
        yields = yield_values(values)
        add_yield_values(totals, yields)
        add_yield_values(building_totals, yields)
        building_results.append({"id": building_id, "yields": yields})

    return {
        "city": city,
        "totals": totals,
        "breakdown": {
            "worked_terrain": base_totals,
            "worked_tile_bonuses": tile_bonus_totals,
            "population": population_yields,
            "district_adjacency": district_totals,
            "buildings": building_totals,
        },
        "tiles": tile_results,
        "districts": districts,
        "buildings": building_results,
        "worker_limit": available_worker_slots,
        "selected_worked_tile_count": len(other_worked),
        "ignored_worked_tile_count": max(
            0, len(other_worked) - available_worker_slots
        ),
        "coverage": {
            "implemented": [
                "XML terrain, feature, resource and improvement yield changes where extracted",
                "city-center minimum yields configured in game_rules.json",
                "population-based yields configured in game_rules.json",
                "owned and worked tile filtering; city center is always worked and citizen work capacity is configured in game_rules.json",
                "declared tile yield modifiers with per-modifier before/change/after details",
                "selected XML district adjacency rules for neighboring terrain, features, rivers, resources, improvements, wonders and districts",
                "XML district citizen yield changes when that district tile is worked",
                "direct building and wonder yield changes from extracted XML",
            ],
            "not_implemented": [
                "technology, civic, era, game-speed and conditional yield modifiers",
                "policy, governor, civilization, leader, religion, city-state and trade-route effects",
                "feature/resource-specific dynamic rules and improvement unlock requirements",
                "district adjacency rules requiring modifier arguments or unsupported conditions",
                "complete DLC coverage and Korean localization",
            ],
        },
        "caveats": [
            "River edges are approximated from the tile river flag or RiverWires; their housing effects are not yields.",
            "Population Science and Culture are configurable flat yields per citizen; food consumption, growth and starvation are not simulated.",
            "The city center is always worked; citizens can work additional owned tiles according to game_rules.json. If more are selected, the first tiles in map data order are counted and excess selections are reported.",
            "Only yields explicitly present in the extracted XML are used; absent entries are not inferred as zero-valued game rules.",
        ],
    }

@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "Pragma": "no-cache", "Expires": "0"})

@app.get("/health")
def health():
    return JSONResponse({"status":"ok", "version":"0.3.0", "app":"Civ VI City Studio MAP EDITOR"}, headers={"Cache-Control":"no-store"})

@app.get("/api/game-rules")
def get_game_rules():
    return game_rules()

@app.get("/api/map")
def get_map():
    return read(DATA/"map.json", {"width":0,"height":0,"count":0,"tiles":[]})

@app.post("/api/map/save")
@synchronized_mutation
def save_map():
    map_data = read(DATA / "map.json", {})
    cities = read(DATA / "cities.json", {})
    state = validate_game_state_snapshot(game_state(), cities, map_data)
    write_json_transaction(
        {
            DATA / "map.json": map_data,
            DATA / "cities.json": cities,
            GAME_STATE: state,
        }
    )
    return {"saved": True, "tile_count": len(map_data.get("tiles", []))}

@app.get("/api/maps/saved")
def list_saved_maps():
    if not SAVED_MAPS.exists():
        return {"maps": []}
    return {
        "maps": sorted(
            path.stem for path in SAVED_MAPS.glob("*.json") if path.is_file()
        )
    }

@app.post("/api/maps/save-as")
@synchronized_mutation
def save_map_as(payload: SaveMapRequest):
    path = saved_map_path(payload.name)
    SAVED_MAPS.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "format": "civ6-city-studio-map",
        "format_version": 1,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "map": read(DATA / "map.json", {}),
        "cities": read(DATA / "cities.json", {}),
        "game_state": game_state(),
    }
    validate_map_snapshot(snapshot)
    snapshot["game_state"] = validate_game_state_snapshot(
        snapshot["game_state"], snapshot["cities"], snapshot["map"]
    )
    try:
        with path.open("x", encoding="utf-8") as saved_file:
            json.dump(snapshot, saved_file, ensure_ascii=False, indent=2)
    except FileExistsError as error:
        raise HTTPException(409, "A saved map with this name already exists.") from error
    return {"name": path.stem, "tile_count": len(snapshot["map"].get("tiles", []))}

@app.post("/api/maps/load")
@synchronized_mutation
def load_saved_map(payload: SaveMapRequest):
    path = saved_map_path(payload.name)
    if not path.is_file():
        raise HTTPException(404, "Saved map not found.")
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HTTPException(422, "Saved map file is unreadable or invalid JSON.") from error
    map_data, cities = validate_map_snapshot(snapshot)
    state = validate_game_state_snapshot(
        snapshot.get("game_state", {"turn": 1, "units": []}), cities, map_data
    )
    write_json_transaction(
        {
            DATA / "map.json": map_data,
            DATA / "cities.json": cities,
            GAME_STATE: state,
        }
    )
    return {
        "map": map_data,
        "cities": list(cities.values()),
        "game_state": state,
    }

@app.put("/api/map/tile/{x}/{y}")
@synchronized_mutation
def update_tile(x: int, y: int, payload: dict):
    mp = read(DATA/"map.json", {})
    tile = next((t for t in mp.get("tiles",[]) if t["x"]==x and t["y"]==y), None)
    if tile is None:
        raise HTTPException(404, "Tile not found")
    if payload.get("worked") is True:
        owner_city_id = tile.get("owner_city_id")
        cities = read(DATA/"cities.json", {})
        city = cities.get(owner_city_id)
        if city is None:
            raise HTTPException(409, "A tile must be owned by a city before citizens can work it.")
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
                raise HTTPException(409, "The city's population cannot work any more tiles.")
    allowed = {"terrain","feature","resource","river","river_wires","improvement","district","worked","hill_override"}
    for key, value in payload.items():
        if key in allowed:
            tile[key] = value
    write(DATA/"map.json", mp)
    return tile

@app.get("/api/catalog")
def get_catalog():
    cat = read(DATA/"catalog.json", {})
    return {k:v for k,v in cat.items() if k != "raw_tables"}

@app.get("/api/cities")
def get_cities():
    return list(read(DATA/"cities.json", {}).values())

def prepare_city_production(city: dict):
    city.setdefault("production_queue", [])
    city.setdefault("production_progress", {})
    city.setdefault("current_production", None)
    city.setdefault("production_history", [])
    if not isinstance(city["production_queue"], list) or not isinstance(
        city["production_progress"], dict
    ):
        raise HTTPException(422, "City production data is invalid.")
    queue_ids = set()
    for entry in city["production_queue"]:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("queue_id"), str)
            or entry["queue_id"] in queue_ids
            or entry.get("category") not in ("unit", "building", "district", "wonder")
            or not isinstance(entry.get("target_id"), str)
        ):
            raise HTTPException(422, "City production queue contains an invalid entry.")
        queue_ids.add(entry["queue_id"])
    if not set(city["production_progress"]).issubset(queue_ids):
        raise HTTPException(422, "City production progress references a missing queue item.")
    if city["production_queue"] and city["current_production"] not in {
        entry.get("queue_id") for entry in city["production_queue"]
    }:
        city["current_production"] = city["production_queue"][0]["queue_id"]
    if not city["production_queue"]:
        city["current_production"] = None

def production_progress_amount(city: dict, queue_id: str) -> float:
    value = city["production_progress"].get(queue_id, 0)
    if isinstance(value, bool):
        raise HTTPException(422, "Stored production progress is invalid.")
    try:
        amount = float(value)
    except (TypeError, ValueError) as error:
        raise HTTPException(422, "Stored production progress is invalid.") from error
    if not math.isfinite(amount) or amount < 0:
        raise HTTPException(422, "Stored production progress must be finite and non-negative.")
    return amount

def production_status(city: dict, map_data: dict, catalog: dict) -> dict:
    rules = game_rules()
    prepare_city_production(city)
    current_yields = calculate_city_yields(city, map_data, catalog, rules)
    production_per_turn = max(0.0, current_yields["totals"]["Production"])
    progress = city["production_progress"]
    total_ahead = 0.0
    queue = []
    for entry in city["production_queue"]:
        item = next(
            (
                candidate
                for candidate in production_catalog(catalog, rules)
                if candidate["category"] == entry["category"]
                and candidate["id"] == entry["target_id"]
            ),
            None,
        )
        if item is None or not item.get("cost_supported"):
            cost = None
            remaining = None
            eta = None
        else:
            cost = item["cost"]
            amount = production_progress_amount(city, entry["queue_id"])
            remaining = max(0.0, cost - amount)
            total_ahead += remaining
            eta = (
                math.ceil(total_ahead / production_per_turn)
                if production_per_turn > 0
                else None
            )
        queue.append(
            {
                **entry,
                "cost": cost,
                "progress": progress.get(entry["queue_id"], 0),
                "remaining": remaining,
                "estimated_turns": eta,
                "cost_basis": (
                    item.get("cost_source", "Unknown")
                    if item
                    else "No valid cost"
                ),
            }
        )
    return {
        "city_id": city["id"],
        "turn": game_state()["turn"],
        "production_per_turn": production_per_turn,
        "current_production": city["current_production"],
        "queue": queue,
        "completed": city["production_history"][-20:],
        "cost_notes": [
            "Costs use extracted XML Cost unless explicitly overridden in game_rules.json.",
            "CostProgressionModel, game speed, technologies, policies and civilization modifiers are not applied.",
            f"Production overflow policy: {rules['production']['overflow_policy']}.",
            "Estimated turns assume current Production remains unchanged.",
        ],
    }

@app.get("/api/game")
def get_game_state():
    return game_state()

def research_status(state: dict | None = None) -> dict:
    state = state or game_state()
    cities = read(DATA / "cities.json", {})
    map_data = read(DATA / "map.json", {})
    catalog = read(DATA / "catalog.json", {})
    rules = game_rules()
    per_turn = {"technology": 0.0, "civic": 0.0}
    yield_key = {"technology": "Science", "civic": "Culture"}
    for city in cities.values():
        totals = calculate_city_yields(city, map_data, catalog, rules)["totals"]
        for category, key in yield_key.items():
            per_turn[category] += float(totals[key])
    options = research_catalog(catalog, state)
    tracks = {}
    for category in ("technology", "civic"):
        track = state["research"][category]
        current = next(
            (
                item
                for item in options[category]
                if item["id"] == track["current_id"]
            ),
            None,
        )
        progress = (
            track["progress"].get(track["current_id"], 0)
            if track["current_id"]
            else 0
        )
        cost = current["cost"] if current else None
        remaining = max(0, cost - progress) if cost is not None else None
        yield_amount = per_turn[category]
        tracks[category] = {
            "current_id": track["current_id"],
            "progress": progress,
            "cost": cost,
            "remaining": remaining,
            "yield_per_turn": yield_amount,
            "estimated_turns": (
                math.ceil(remaining / yield_amount)
                if remaining is not None and yield_amount > 0
                else None
            ),
            "completed": list(
                state["technologies" if category == "technology" else "civics"]
            ),
        }
    return {
        "turn": state["turn"],
        "tracks": tracks,
        "options": options,
        "technologies": state["technologies"],
        "civics": state["civics"],
        "science_per_turn": per_turn["technology"],
        "culture_per_turn": per_turn["civic"],
    }

@app.get("/api/research")
def get_research_status():
    return research_status()

@app.put("/api/research/{category}")
@synchronized_mutation
def select_research(category: str, request: ResearchSelectRequest):
    if category not in ("technology", "civic"):
        raise HTTPException(404, "Research category must be technology or civic.")
    state = game_state()
    catalog = read(DATA / "catalog.json", {})
    option = next(
        (
            item
            for item in research_catalog(catalog, state)[category]
            if item["id"] == request.target_id
        ),
        None,
    )
    if option is None:
        raise HTTPException(422, "Unknown research target.")
    if not option["available"]:
        raise HTTPException(409, option["unavailable_reason"])
    state["research"][category]["current_id"] = request.target_id
    state["research"][category]["progress"].setdefault(request.target_id, 0)
    write_atomically(GAME_STATE, state)
    return research_status(state)

@app.get("/api/production/catalog")
def get_production_catalog(city_id: str | None = None):
    catalog = read(DATA / "catalog.json", {})
    options = production_catalog(catalog)
    cities = read(DATA / "cities.json", {})
    map_data = read(DATA / "map.json", {})
    if city_id is not None and city_id not in cities:
        raise HTTPException(404, "City not found.")
    for item in options:
        item["available"] = item["cost_supported"]
        item["unavailable_reason"] = item.get("unsupported_reason")
        if city_id and city_id in cities:
            city = cities[city_id]
            try:
                validate_production_target(
                    city,
                    ProductionQueueRequest(
                        category=item["category"],
                        target_id=item["id"],
                    ),
                    cities,
                    map_data,
                    catalog,
                )
            except HTTPException as error:
                reason = str(error.detail)
                if (
                    item["category"] == "district"
                    and reason == "District production requires a placement tile."
                ) or (
                    item["category"] == "wonder"
                    and reason == "Wonder production requires a placement tile."
                ):
                    pass
                else:
                    item["available"] = False
                    item["unavailable_reason"] = reason
    return {"items": options}

@app.get("/api/cities/{city_id}/production")
def get_city_production(city_id: str):
    cities = read(DATA / "cities.json", {})
    city = cities.get(city_id)
    if city is None:
        raise HTTPException(404, "City not found.")
    return production_status(
        city, read(DATA / "map.json", {}), read(DATA / "catalog.json", {})
    )

@app.post("/api/cities/{city_id}/production/queue")
@synchronized_mutation
def add_production(city_id: str, request: ProductionQueueRequest):
    cities = read(DATA / "cities.json", {})
    city = cities.get(city_id)
    if city is None:
        raise HTTPException(404, "City not found.")
    prepare_city_production(city)
    map_data = read(DATA / "map.json", {})
    catalog = read(DATA / "catalog.json", {})
    validate_production_target(city, request, cities, map_data, catalog)
    entry = {
        "queue_id": "prod-" + uuid4().hex[:12],
        "category": request.category,
        "target_id": request.target_id,
        "x": request.x,
        "y": request.y,
    }
    city["production_queue"].append(entry)
    city["production_progress"].setdefault(
        entry["queue_id"], game_rules()["production"]["initial_progress"]
    )
    if city["current_production"] is None:
        city["current_production"] = entry["queue_id"]
    write_atomically(DATA / "cities.json", cities)
    return production_status(city, map_data, catalog)

@app.put("/api/cities/{city_id}/production/order")
@synchronized_mutation
def reorder_production(city_id: str, request: ProductionReorderRequest):
    cities = read(DATA / "cities.json", {})
    city = cities.get(city_id)
    if city is None:
        raise HTTPException(404, "City not found.")
    prepare_city_production(city)
    index = next(
        (i for i, entry in enumerate(city["production_queue"]) if entry["queue_id"] == request.queue_id),
        None,
    )
    if index is None:
        raise HTTPException(404, "Production queue item not found.")
    if request.position >= len(city["production_queue"]):
        raise HTTPException(422, "Production queue position is outside the queue.")
    entry = city["production_queue"].pop(index)
    city["production_queue"].insert(request.position, entry)
    city["current_production"] = city["production_queue"][0]["queue_id"]
    write_atomically(DATA / "cities.json", cities)
    return production_status(
        city, read(DATA / "map.json", {}), read(DATA / "catalog.json", {})
    )

@app.delete("/api/cities/{city_id}/production/queue/{queue_id}")
@synchronized_mutation
def remove_production(city_id: str, queue_id: str):
    cities = read(DATA / "cities.json", {})
    city = cities.get(city_id)
    if city is None:
        raise HTTPException(404, "City not found.")
    prepare_city_production(city)
    index = next(
        (i for i, entry in enumerate(city["production_queue"]) if entry["queue_id"] == queue_id),
        None,
    )
    if index is None:
        raise HTTPException(404, "Production queue item not found.")
    city["production_queue"].pop(index)
    city["production_progress"].pop(queue_id, None)
    city["current_production"] = (
        city["production_queue"][0]["queue_id"] if city["production_queue"] else None
    )
    write_atomically(DATA / "cities.json", cities)
    return production_status(
        city, read(DATA / "map.json", {}), read(DATA / "catalog.json", {})
    )

def complete_production(
    city: dict,
    entry: dict,
    map_data: dict,
    catalog: dict,
    state: dict,
    rules: dict,
) -> dict:
    item_id = entry["target_id"]
    units = index_rows(catalog, "Units::Units", "UnitType")
    if entry["category"] == "unit":
        row = effective_catalog_row(units[item_id], "units", item_id, rules)
        charges = catalog_integer(
            row, "BuildCharges", rules["units"]["default_build_charges"], item_id
        )
        moves = catalog_integer(
            row, "BaseMoves", rules["units"]["default_base_moves"], item_id
        )
        if moves == 0:
            raise HTTPException(422, f"BaseMoves must be positive for {item_id}.")
        unit = {
            "id": "unit-" + uuid4().hex[:12],
            "unit_type": item_id,
            "owner_city_id": city["id"],
            "x": city["x"],
            "y": city["y"],
            "moves_remaining": moves,
            "charges_remaining": charges,
            "created_turn": state["turn"],
        }
        state["units"].append(unit)
        population_cost = catalog_integer(row, "PopulationCost", 0, item_id)
        if population_cost:
            city["population"] = max(
                1, city.get("population", rules["city"]["initial_population"]) - population_cost
            )
        return {"type": "unit", "id": item_id, "unit": unit}
    if entry["category"] == "district":
        tile = next(
            tile
            for tile in map_data["tiles"]
            if tile["x"] == entry["x"] and tile["y"] == entry["y"]
        )
        tile["district"] = item_id
        tile["improvement"] = None
        tile["worked"] = False
        if item_id not in city.setdefault("districts", []):
            city["districts"].append(item_id)
        return {"type": "district", "id": item_id, "x": entry["x"], "y": entry["y"]}
    if entry["category"] == "wonder":
        tile = next(
            tile
            for tile in map_data["tiles"]
            if tile["x"] == entry["x"] and tile["y"] == entry["y"]
        )
        tile["district"] = "DISTRICT_WONDER"
        tile["worked"] = False
        city.setdefault("wonders", []).append(item_id)
        return {"type": "wonder", "id": item_id, "x": entry["x"], "y": entry["y"]}
    city.setdefault("buildings", []).append(item_id)
    return {"type": "building", "id": item_id}

@app.post("/api/turn/end")
@synchronized_mutation
def end_turn():
    rules = game_rules()
    cities = read(DATA / "cities.json", {})
    map_data = read(DATA / "map.json", {})
    catalog = read(DATA / "catalog.json", {})
    state = game_state()
    completed = []
    completed_research = []
    production_by_city = {}
    research_yield_by_category = {"technology": 0.0, "civic": 0.0}
    for city_id, city in cities.items():
        totals = calculate_city_yields(city, map_data, catalog, rules)["totals"]
        production_by_city[city_id] = totals["Production"]
        research_yield_by_category["technology"] += float(totals["Science"])
        research_yield_by_category["civic"] += float(totals["Culture"])
    for city in cities.values():
        prepare_city_production(city)
        production = production_by_city[city["id"]]
        if not math.isfinite(float(production)) or production < 0:
            raise HTTPException(422, f"City {city['name']} has invalid Production.")
        remaining_production = float(production)
        while city["production_queue"] and remaining_production > 0:
            entry = city["production_queue"][0]
            validate_production_target(
                city,
                ProductionQueueRequest(
                    category=entry["category"],
                    target_id=entry["target_id"],
                    x=entry.get("x"),
                    y=entry.get("y"),
                ),
                cities,
                map_data,
                catalog,
                ignore_queue_id=entry["queue_id"],
            )
            item = next(
                (
                    candidate
                    for candidate in production_catalog(catalog, rules)
                    if candidate["category"] == entry["category"]
                    and candidate["id"] == entry["target_id"]
                ),
                None,
            )
            if item is None or not item.get("cost_supported"):
                raise HTTPException(
                    422,
                    f"Production cost is unavailable for queued item {entry.get('target_id')}.",
                )
            cost = item["cost"]
            current_progress = production_progress_amount(city, entry["queue_id"])
            needed = max(0.0, cost - current_progress)
            applied = min(needed, remaining_production)
            current_progress += applied
            remaining_production -= applied
            city["production_progress"][entry["queue_id"]] = current_progress
            if current_progress < cost:
                break
            completion = complete_production(
                city, entry, map_data, catalog, state, rules
            )
            completed.append({"city_id": city["id"], **completion})
            city["production_history"].append(
                {"turn": state["turn"], "category": entry["category"], "target_id": entry["target_id"]}
            )
            city["production_queue"].pop(0)
            city["production_progress"].pop(entry["queue_id"], None)
            city["current_production"] = (
                city["production_queue"][0]["queue_id"]
                if city["production_queue"]
                else None
            )
            if rules["production"]["overflow_policy"] == "stop_at_item":
                break
        # Unused production is discarded when the configured item policy stops.
    research_tables = {
        "technology": ("Technologies::Technologies", "TechnologyType", "technologies"),
        "civic": ("Civics::Civics", "CivicType", "civics"),
    }
    for category, (table, key, completed_key) in research_tables.items():
        track = state["research"][category]
        target_id = track["current_id"]
        if not target_id:
            continue
        row = index_rows(catalog, table, key).get(target_id)
        cost = catalog_cost(row) if row else None
        if cost is None:
            raise HTTPException(
                422, f"Research cost is unavailable for selected target {target_id}."
            )
        earned = research_yield_by_category[category]
        if not math.isfinite(earned) or earned < 0:
            raise HTTPException(422, f"Invalid {category} research yield.")
        progress = track["progress"].get(target_id, 0) + earned
        if progress >= cost:
            progress = float(cost)
            if target_id not in state[completed_key]:
                state[completed_key].append(target_id)
            track["current_id"] = None
            completed_research.append({"category": category, "id": target_id})
        track["progress"][target_id] = progress
    state["turn"] += 1
    for unit in state["units"]:
        row = index_rows(catalog, "Units::Units", "UnitType").get(unit["unit_type"], {})
        row = effective_catalog_row(row, "units", unit["unit_type"], rules)
        unit["moves_remaining"] = catalog_integer(
            row,
            "BaseMoves",
            rules["units"]["default_base_moves"],
            unit["unit_type"],
        )
    write_json_transaction(
        {
            DATA / "cities.json": cities,
            DATA / "map.json": map_data,
            GAME_STATE: state,
        }
    )
    return {
        "turn": state["turn"],
        "completed": completed,
        "completed_research": completed_research,
        "game_state": state,
    }

@app.post("/api/units/{unit_id}/move")
@synchronized_mutation
def move_unit(unit_id: str, request: UnitMoveRequest):
    state = game_state()
    unit = next((value for value in state["units"] if value.get("id") == unit_id), None)
    if unit is None:
        raise HTTPException(404, "Unit not found.")
    if unit.get("moves_remaining", 0) <= 0:
        raise HTTPException(409, "This unit has no movement remaining this turn.")
    if hex_distance(unit["x"], unit["y"], request.x, request.y) != 1:
        raise HTTPException(422, "Units can move one adjacent hex at a time.")
    map_data = read(DATA / "map.json", {})
    if not any((tile["x"], tile["y"]) == (request.x, request.y) for tile in map_data.get("tiles", [])):
        raise HTTPException(404, "Destination tile not found.")
    unit["x"], unit["y"] = request.x, request.y
    unit["moves_remaining"] -= 1
    write_atomically(GAME_STATE, state)
    return unit

@app.post("/api/units/{unit_id}/build")
@synchronized_mutation
def builder_build(unit_id: str, request: BuilderActionRequest):
    if not request.improvement_id:
        raise HTTPException(422, "An improvement ID is required.")
    state = game_state()
    unit = next((value for value in state["units"] if value.get("id") == unit_id), None)
    if unit is None:
        raise HTTPException(404, "Unit not found.")
    if unit.get("unit_type") != "UNIT_BUILDER":
        raise HTTPException(409, "Only Builders can construct improvements.")
    cities = read(DATA / "cities.json", {})
    city = cities.get(unit["owner_city_id"])
    map_data = read(DATA / "map.json", {})
    tile = next(
        (value for value in map_data.get("tiles", []) if (value["x"], value["y"]) == (unit["x"], unit["y"])),
        None,
    )
    if city is None or tile is None:
        raise HTTPException(409, "Builder ownership or current tile is invalid.")
    if tile.get("owner_city_id") != city["id"] or (city["x"], city["y"]) == (unit["x"], unit["y"]):
        raise HTTPException(409, "Improvements require a non-center tile owned by the Builder's city.")
    catalog = read(DATA / "catalog.json", {})
    rules = game_rules()
    improvement_rows = index_rows(catalog, "Improvements::Improvements", "ImprovementType")
    row = improvement_rows.get(request.improvement_id)
    if row is None or str(row.get("Buildable", "")).lower() != "true":
        raise HTTPException(422, "This improvement is not buildable in the extracted game data.")
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
        raise HTTPException(409, "This Builder is not listed as a valid unit for the improvement.")
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
        raise HTTPException(409, "The selected tile does not meet the improvement's configured or extracted terrain, feature or resource conditions.")
    if any(str(rule.get("MustRemoveFeature", "")).lower() == "true" for rule in resource_rules) and tile.get("feature"):
        raise HTTPException(409, "Remove the required feature before building this improvement.")
    if tile.get("feature") and not feature_valid and not resource_rules:
        raise HTTPException(409, "Remove the incompatible feature before building this improvement.")
    prerequisite_error = city_prerequisite_error(city, row)
    if prerequisite_error:
        raise HTTPException(409, prerequisite_error)
    if tile.get("district"):
        raise HTTPException(409, "An improvement cannot be built on a district tile.")
    if tile.get("improvement") == request.improvement_id:
        raise HTTPException(409, "This improvement is already present.")
    action_charge_cost = improvement_override.get(
        "build_charges", rules["improvements"]["default_build_charges"]
    )
    if unit["charges_remaining"] < action_charge_cost:
        raise HTTPException(409, "This Builder has insufficient charges for the improvement.")
    tile["improvement"] = request.improvement_id
    unit["charges_remaining"] -= action_charge_cost
    write_json_transaction(
        {DATA / "map.json": map_data, GAME_STATE: state}
    )
    return {"unit": unit, "tile": tile}

@app.post("/api/units/{unit_id}/remove-feature")
@synchronized_mutation
def builder_remove_feature(unit_id: str):
    rules = game_rules()
    state = game_state()
    unit = next((value for value in state["units"] if value.get("id") == unit_id), None)
    if unit is None:
        raise HTTPException(404, "Unit not found.")
    if unit.get("unit_type") != "UNIT_BUILDER":
        raise HTTPException(409, "Only Builders can remove features.")
    catalog = read(DATA / "catalog.json", {})
    cities = read(DATA / "cities.json", {})
    city = cities.get(unit["owner_city_id"])
    map_data = read(DATA / "map.json", {})
    tile = next(
        (value for value in map_data.get("tiles", []) if (value["x"], value["y"]) == (unit["x"], unit["y"])),
        None,
    )
    if city is None or tile is None or tile.get("owner_city_id") != city["id"]:
        raise HTTPException(409, "Feature removal requires a tile owned by the Builder's city.")
    feature = tile.get("feature")
    feature_row = index_rows(catalog, "Features::Features", "FeatureType").get(feature)
    if not feature_row or str(feature_row.get("Removable", "")).lower() != "true":
        raise HTTPException(409, "The current feature is not marked removable in the extracted game data.")
    prerequisite_error = city_prerequisite_error(
        city,
        {
            "PrereqTech": feature_row.get("RemoveTech"),
            "PrereqCivic": feature_row.get("AddCivic"),
        },
    )
    if prerequisite_error:
        raise HTTPException(409, prerequisite_error)
    charge_cost = rules["improvements"]["feature_removal_charge_cost"]
    if unit["charges_remaining"] < charge_cost:
        raise HTTPException(409, "This Builder has insufficient charges to remove the feature.")
    tile["feature"] = None
    unit["charges_remaining"] -= charge_cost
    write_json_transaction({DATA / "map.json": map_data, GAME_STATE: state})
    return {"unit": unit, "tile": tile}

@app.post("/api/units/{unit_id}/found-city")
@synchronized_mutation
def settler_found_city(unit_id: str, request: FoundCityRequest):
    rules = game_rules()
    state = game_state()
    unit = next((value for value in state["units"] if value.get("id") == unit_id), None)
    if unit is None:
        raise HTTPException(404, "Unit not found.")
    if unit.get("unit_type") != "UNIT_SETTLER":
        raise HTTPException(409, "Only Settlers can found cities.")
    catalog = read(DATA / "catalog.json", {})
    settler_data = index_rows(catalog, "Units::Units", "UnitType").get(
        "UNIT_SETTLER"
    )
    if settler_data is None or not catalog_flag(settler_data.get("FoundCity")):
        raise HTTPException(
            422, "Settler city-founding capability is not present in the extracted game data."
        )
    cities = read(DATA / "cities.json", {})
    owner_city = cities.get(unit.get("owner_city_id"))
    if owner_city is None:
        raise HTTPException(409, "Settler owner city no longer exists.")
    map_data = read(DATA / "map.json", {})
    center_tile = next(
        (tile for tile in map_data.get("tiles", []) if (tile["x"], tile["y"]) == (unit["x"], unit["y"])),
        None,
    )
    if center_tile is None:
        raise HTTPException(404, "Settler is not on a valid map tile.")
    for city in cities.values():
        if (
            hex_distance(unit["x"], unit["y"], city["x"], city["y"])
            < rules["city"]["minimum_distance"]
        ):
            raise HTTPException(
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
        raise HTTPException(409, "The city center or initial territory is owned by another city.")
    if center_tile.get("district") or "MOUNTAIN" in center_tile.get("terrain", ""):
        raise HTTPException(409, "This tile cannot host a city center.")
    terrain_row = index_rows(catalog, "Terrains::Terrains", "TerrainType").get(
        center_tile.get("terrain")
    )
    if terrain_row is not None and catalog_flag(terrain_row.get("Water")):
        raise HTTPException(409, "A city cannot be founded on a water tile.")
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
    state["units"] = [value for value in state["units"] if value["id"] != unit_id]
    write_json_transaction(
        {
            DATA / "cities.json": cities,
            DATA / "map.json": map_data,
            GAME_STATE: state,
        }
    )
    return {"city": city, "consumed_unit_id": unit_id}

@app.post("/api/cities")
@synchronized_mutation
def create_city(payload: CityCreate):
    rules = game_rules()
    mp = read(DATA/"map.json", {})
    tile = next((t for t in mp.get("tiles",[]) if t["x"]==payload.x and t["y"]==payload.y), None)
    if tile is None:
        raise HTTPException(404, "Cannot create a city outside the loaded map.")
    cities = read(DATA/"cities.json", {})
    for city in cities.values():
        if (
            hex_distance(payload.x, payload.y, city["x"], city["y"])
            < rules["city"]["minimum_distance"]
        ):
            raise HTTPException(
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
        raise HTTPException(
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
        raise HTTPException(409, "Initial city territory overlaps another city.")
    for owned_tile in initial_tiles:
        owned_tile["owner_city_id"] = city_id
        owned_tile["worked"] = owned_tile is tile
    write(DATA/"cities.json", cities)
    write(DATA/"map.json", mp)
    return city

@app.put("/api/cities/{city_id}/tiles/{x}/{y}")
@synchronized_mutation
def update_city_tile_ownership(
    city_id: str, x: int, y: int, payload: TileOwnership
):
    cities = read(DATA/"cities.json", {})
    city = cities.get(city_id)
    if city is None:
        raise HTTPException(404, "City not found")

    mp = read(DATA/"map.json", {})
    tile = next(
        (item for item in mp.get("tiles", []) if item["x"] == x and item["y"] == y),
        None,
    )
    if tile is None:
        raise HTTPException(404, "Tile not found")
    if (x, y) == (city["x"], city["y"]) and not payload.owned:
        raise HTTPException(409, "A city cannot give up its center tile.")
    if payload.owned:
        owner_city_id = tile.get("owner_city_id")
        if owner_city_id not in (None, city_id):
            raise HTTPException(409, "This tile is already owned by another city.")
        if owner_city_id is None and not any(
            owned_tile.get("owner_city_id") == city_id
            and hex_distance(x, y, owned_tile["x"], owned_tile["y"]) == 1
            for owned_tile in mp.get("tiles", [])
        ):
            raise HTTPException(409, "A city can only claim a tile adjacent to its territory.")
        if owner_city_id is None:
            tile["owner_city_id"] = city_id
            tile["worked"] = False
    else:
        if tile.get("owner_city_id") != city_id:
            raise HTTPException(409, "This city does not own the selected tile.")
        tile["owner_city_id"] = None
        tile["worked"] = False

    write(DATA/"map.json", mp)
    return tile

@app.put("/api/cities/{city_id}")
@synchronized_mutation
def update_city(city_id: str, payload: dict):
    cities = read(DATA/"cities.json", {})
    if city_id not in cities:
        raise HTTPException(404, "City not found")
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
                    raise HTTPException(422, "Population must be an integer.") from error
                if not 1 <= value <= rules["city"]["maximum_population"]:
                    raise HTTPException(
                        422,
                        "Population must be between 1 and "
                        f"{rules['city']['maximum_population']}.",
                    )
            cities[city_id][key] = value
    if cities[city_id].get(
        "population", rules["city"]["initial_population"]
    ) != old_population:
        map_data = read(DATA / "map.json", {})
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
        write_json_transaction(
            {DATA / "cities.json": cities, DATA / "map.json": map_data}
        )
    else:
        write_atomically(DATA / "cities.json", cities)
    return cities[city_id]

@app.delete("/api/cities/{city_id}")
@synchronized_mutation
def delete_city(city_id: str):
    cities = read(DATA/"cities.json", {})
    if city_id not in cities:
        raise HTTPException(404, "City not found")
    cities.pop(city_id)
    mp = read(DATA/"map.json", {})
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
            DATA / "cities.json": cities,
            DATA / "map.json": mp,
            GAME_STATE: state,
        }
    )
    return {"deleted":city_id}

def add_yields(total, values):
    for key, val in (values or {}).items():
        try:
            total[key] = total.get(key, 0) + float(val)
        except (TypeError, ValueError):
            pass

@app.post("/api/calculate/{city_id}")
def calculate_city(city_id: str):
    cities = read(DATA/"cities.json", {})
    if city_id not in cities:
        raise HTTPException(404, "City not found")
    return calculate_city_yields(
        cities[city_id],
        read(DATA / "map.json", {}),
        read(DATA / "catalog.json", {}),
    )

@app.get("/api/tiles/{x}/{y}/yields")
def calculate_tile_yields(x: int, y: int):
    map_data = read(DATA / "map.json", {})
    tile = next(
        (
            item
            for item in map_data.get("tiles", [])
            if item.get("x") == x and item.get("y") == y
        ),
        None,
    )
    if tile is None:
        raise HTTPException(404, "Tile not found")

    cities = read(DATA / "cities.json", {})
    city = cities.get(tile.get("owner_city_id"))
    if city:
        result = calculate_city_yields(
            city, map_data, read(DATA / "catalog.json", {})
        )
        tile_result = next(
            (item for item in result["tiles"] if item["x"] == x and item["y"] == y),
            None,
        )
        if tile_result is not None:
            return {**tile_result, "preview": False}

    preview_id = f"__tile_preview__{x}_{y}"
    preview_map = {
        **map_data,
        "tiles": [
            {
                **item,
                **(
                    {"owner_city_id": preview_id, "worked": True}
                    if item.get("x") == x and item.get("y") == y
                    else {}
                ),
            }
            for item in map_data.get("tiles", [])
        ],
    }
    preview_city = {
        "id": preview_id,
        "name": "Tile yield preview",
        "x": -1,
        "y": -1,
        "population": 1,
        "buildings": [],
        "wonders": [],
    }
    result = calculate_city_yields(
        preview_city, preview_map, read(DATA / "catalog.json", {})
    )
    tile_result = next(
        (item for item in result["tiles"] if item["x"] == x and item["y"] == y),
        None,
    )
    if tile_result is None:
        raise HTTPException(500, "Unable to calculate tile yields")
    return {**tile_result, "preview": True}
