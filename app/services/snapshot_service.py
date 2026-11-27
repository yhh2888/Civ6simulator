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
from app.repositories.snapshot_repository import snapshot_repository
from app.repositories.json_repository import read, write, write_atomically, write_json_transaction
from app.config import RuleConfigurationError, YIELD_KEYS, load_game_rules, matching_tile_yield_modifiers
from app.entity_rules import ENTITY_SOURCES, entity_availability, entity_rows, missing_research_requirements, research_prerequisites as entity_research_prerequisites
from app.schemas.city_schema import CityCreate, TileOwnership
from app.schemas.snapshot_schema import SaveMapRequest
from app.schemas.game_schema import (BuilderActionRequest, FoundCityRequest, ForceEntityRequest, ForceUnitCreateRequest, ProductionQueueRequest, ProductionReorderRequest, ResearchAvailabilityRequest, ResearchSelectRequest, UnitMoveRequest)

from app.services.game_data_service import game_rules, game_state, raw_table
from app.services.food_service import ensure_city_food_state
def saved_map_path(name: str) -> Path:
    if "/" in name or "\\" in name:
        raise ApplicationError(422, "Map file name must not contain a path.")
    filename = Path(name).name
    stem = filename[:-5] if filename.lower().endswith(".json") else filename
    if (
        not stem
        or ".." in stem
        or re.fullmatch(r"[A-Za-z0-9가-힣][A-Za-z0-9가-힣 _.-]{0,79}", stem) is None
    ):
        raise ApplicationError(422, "Invalid map file name.")
    return snapshot_repository.path(f"{stem}.json")


def validate_map_snapshot(snapshot: Any) -> tuple[dict, dict]:
    rules = game_rules()
    if not isinstance(snapshot, dict):
        raise ApplicationError(422, "Saved map must contain a JSON object.")
    map_data = snapshot.get("map")
    cities = snapshot.get("cities")
    if not isinstance(map_data, dict) or not isinstance(cities, dict):
        raise ApplicationError(422, "Saved map must include map and cities objects.")

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
        raise ApplicationError(422, "Saved map dimensions or tile count are invalid.")
    map_data["count"] = count

    positions = set()
    for tile in tiles:
        if not isinstance(tile, dict):
            raise ApplicationError(422, "Saved map contains an invalid tile.")
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
            raise ApplicationError(422, "Saved map contains invalid or duplicate coordinates.")
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
            raise ApplicationError(422, "Saved map contains an invalid city.")
        snapshot_state = snapshot.get("game_state")
        speed = (
            snapshot_state.get("game_speed", rules["game_speed"])
            if isinstance(snapshot_state, dict)
            else rules["game_speed"]
        )
        try:
            ensure_city_food_state(city, speed, rules)
        except (ApplicationError, ValueError, TypeError) as error:
            raise ApplicationError(
                422, f"Saved city Food state is invalid: {error}"
            ) from error
        queue = city.get("production_queue", [])
        progress = city.get("production_progress", {})
        if not isinstance(queue, list) or not isinstance(progress, dict):
            raise ApplicationError(422, "Saved city production data is invalid.")
        queue_ids = set()
        for entry in queue:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("queue_id"), str)
                or entry["queue_id"] in queue_ids
                or entry.get("category") not in ("unit", "building", "district", "wonder", "project")
                or not isinstance(entry.get("target_id"), str)
            ):
                raise ApplicationError(422, "Saved production queue contains an invalid entry.")
            queue_ids.add(entry["queue_id"])
        if city.get("current_production") not in (None, *queue_ids):
            raise ApplicationError(422, "Saved current production does not reference its queue.")
        if not set(progress).issubset(queue_ids):
            raise ApplicationError(422, "Saved production progress references a missing queue item.")
        for amount in progress.values():
            if isinstance(amount, bool):
                raise ApplicationError(422, "Saved production progress is invalid.")
            try:
                numeric_amount = float(amount)
            except (TypeError, ValueError) as error:
                raise ApplicationError(422, "Saved production progress is invalid.") from error
            if not math.isfinite(numeric_amount) or numeric_amount < 0:
                raise ApplicationError(422, "Saved production progress must be finite and non-negative.")
    for tile in tiles:
        owner_city_id = tile.get("owner_city_id")
        if owner_city_id is not None and (
            not isinstance(owner_city_id, str) or owner_city_id not in cities
        ):
            raise ApplicationError(422, "Saved map refers to a missing city owner.")

    return map_data, cities


def validate_game_state_snapshot(state: Any, cities: dict, map_data: dict) -> dict:
    game_speed = (
        state.get("game_speed", game_rules()["game_speed"])
        if isinstance(state, dict)
        else None
    )
    if (
        not isinstance(state, dict)
        or not isinstance(game_speed, str)
        or game_speed not in GAME_SPEEDS
        or not isinstance(state.get("turn", 1), int)
        or isinstance(state.get("turn", 1), bool)
        or state.get("turn", 1) < 1
        or not isinstance(state.get("units", []), list)
    ):
        raise ApplicationError(422, "Saved game state is invalid.")
    unit_ids = set()
    positions = {(tile["x"], tile["y"]) for tile in map_data["tiles"]}
    known_unit_types = {
        row.get("UnitType")
        for row in raw_table(game_data_repository.catalog(), "Units::Units")
        if isinstance(row.get("UnitType"), str)
    } | {"UNIT_BUILDER", "UNIT_SETTLER"}
    for unit in state.get("units", []):
        if not isinstance(unit, dict):
            raise ApplicationError(422, "Saved game state contains an invalid unit.")
        numeric_fields = ("x", "y", "moves_remaining", "charges_remaining")
        if (
            not isinstance(unit.get("id"), str)
            or unit["id"] in unit_ids
            or unit.get("owner_city_id") not in cities
            or unit.get("unit_type") not in known_unit_types
            or any(
                not isinstance(unit.get(key), int) or isinstance(unit.get(key), bool)
                for key in numeric_fields
            )
            or unit["moves_remaining"] < 0
            or unit["charges_remaining"] < 0
            or (unit["x"], unit["y"]) not in positions
        ):
            raise ApplicationError(422, "Saved game state contains an invalid unit.")
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
        raise ApplicationError(422, "Saved game research state is invalid.")
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
            raise ApplicationError(422, f"Saved {category} research is invalid.")
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
                raise ApplicationError(422, f"Saved {category} research progress is invalid.")
        validated_research[category] = {
            "current_id": track.get("current_id"),
            "progress": progress,
        }
        overflow = track.get("overflow")
        if isinstance(overflow, (int, float)) and not isinstance(overflow, bool) and math.isfinite(overflow) and overflow >= 0:
            validated_research[category]["overflow"] = overflow
        boost_applied = track.get("boost_applied")
        if isinstance(boost_applied, list) and all(isinstance(item, str) for item in boost_applied):
            validated_research[category]["boost_applied"] = list(boost_applied)
    validated = {
        "turn": state.get("turn", 1),
        "units": state.get("units", []),
        "game_speed": game_speed,
        "technologies": sorted(technologies),
        "civics": sorted(civics),
        "research": validated_research,
    }
    # Simulation extensions (empire settings and turn history) are preserved.
    from app.services.empire_service import sanitize_empire

    if isinstance(state.get("empire"), dict):
        validated["empire"] = sanitize_empire(state["empire"])
    if isinstance(state.get("difficulties"), dict):
        from app.services.difficulty_service import settings

        kept = settings(state)
        validated["difficulties"] = {
            "default": kept["default"],
            "players": {k: {"difficulty": v.get("difficulty"), "human": v.get("human") is True}
                        for k, v in list(kept["players"].items())[:64] if isinstance(v.get("difficulty"), str)},
        }
    if isinstance(state.get("sim_history"), list):
        validated["sim_history"] = [item for item in state["sim_history"] if isinstance(item, dict)][-60:]
    from app.repositories.locations import RULESET_FILES

    if state.get("ruleset") in RULESET_FILES:
        validated["ruleset"] = state["ruleset"]
    from app.services.sim_context import GAME_SETTING_OVERRIDES

    overrides = state.get("simulation_overrides")
    if isinstance(overrides, dict):
        kept = {k: overrides[k] for k in GAME_SETTING_OVERRIDES if isinstance(overrides.get(k), bool)}
        if kept:
            validated["simulation_overrides"] = kept
    return validated


@synchronized_mutation
def save_map():
    map_data = map_repository.get_map({})
    cities = city_repository.get_all()
    state = validate_game_state_snapshot(game_state(), cities, map_data)
    write_json_transaction(
        {
            locations.map_file(): map_data,
            locations.cities_file(): cities,
            locations.game_state_file(): state,
        },
        persist=True,
    )
    return {"saved": True, "tile_count": len(map_data.get("tiles", []))}


def list_saved_maps():
    return {"maps": snapshot_repository.list_names()}


@synchronized_mutation
def save_map_as(payload: SaveMapRequest):
    path = saved_map_path(payload.name)
    snapshot_repository.ensure_directory()
    snapshot = {
        "format": "civ6-city-studio-map",
        "format_version": 1,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "map": map_repository.get_map({}),
        "cities": city_repository.get_all(),
        "game_state": game_state(),
    }
    validate_map_snapshot(snapshot)
    snapshot["game_state"] = validate_game_state_snapshot(
        snapshot["game_state"], snapshot["cities"], snapshot["map"]
    )
    try:
        snapshot_repository.save_new(path, snapshot)
    except FileExistsError as error:
        raise ApplicationError(409, "A saved map with this name already exists.") from error
    return {"name": path.stem, "tile_count": len(snapshot["map"].get("tiles", []))}


@synchronized_mutation
def load_saved_map(payload: SaveMapRequest):
    path = saved_map_path(payload.name)
    if not snapshot_repository.exists(path):
        raise ApplicationError(404, "Saved map not found.")
    try:
        snapshot = snapshot_repository.load(path)
    except (OSError, json.JSONDecodeError) as error:
        raise ApplicationError(422, "Saved map file is unreadable or invalid JSON.") from error
    map_data, cities = validate_map_snapshot(snapshot)
    state = validate_game_state_snapshot(
        snapshot.get("game_state", {"turn": 1, "units": []}), cities, map_data
    )
    for city in cities.values():
        ensure_city_food_state(city, state["game_speed"], game_rules())
    write_json_transaction(
        {
            locations.map_file(): map_data,
            locations.cities_file(): cities,
            locations.game_state_file(): state,
        }
    )
    return {
        "map": map_data,
        "cities": list(cities.values()),
        "game_state": state,
    }
