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

from app.services.game_data_service import (catalog_cost, catalog_flag, catalog_integer, configured_production_cost, effective_catalog_row, game_rules, game_state, index_rows, production_catalog, raw_table, validate_production_target)
from app.services.yield_service import calculate_city_yields
from app.services.food_service import (
    city_food_status,
    process_city_food_turn,
    refresh_city_food_state,
)
def prepare_city_production(city: dict):
    city.setdefault("production_queue", [])
    city.setdefault("production_progress", {})
    city.setdefault("current_production", None)
    city.setdefault("production_history", [])
    if not isinstance(city["production_queue"], list) or not isinstance(
        city["production_progress"], dict
    ):
        raise ApplicationError(422, "City production data is invalid.")
    queue_ids = set()
    for entry in city["production_queue"]:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("queue_id"), str)
            or entry["queue_id"] in queue_ids
            or entry.get("category") not in ("unit", "building", "district", "wonder", "project")
            or not isinstance(entry.get("target_id"), str)
        ):
            raise ApplicationError(422, "City production queue contains an invalid entry.")
        queue_ids.add(entry["queue_id"])
    if not set(city["production_progress"]).issubset(queue_ids):
        raise ApplicationError(422, "City production progress references a missing queue item.")
    if city["production_queue"] and city["current_production"] not in {
        entry.get("queue_id") for entry in city["production_queue"]
    }:
        city["current_production"] = city["production_queue"][0]["queue_id"]
    if not city["production_queue"]:
        city["current_production"] = None


def production_progress_amount(city: dict, queue_id: str) -> float:
    value = city["production_progress"].get(queue_id, 0)
    if isinstance(value, bool):
        raise ApplicationError(422, "Stored production progress is invalid.")
    try:
        amount = float(value)
    except (TypeError, ValueError) as error:
        raise ApplicationError(422, "Stored production progress is invalid.") from error
    if not math.isfinite(amount) or amount < 0:
        raise ApplicationError(422, "Stored production progress must be finite and non-negative.")
    return amount


def production_status(
    city: dict,
    map_data: dict,
    catalog: dict,
    *,
    rules: dict | None = None,
    current_yields: dict | None = None,
    turn: int | None = None,
) -> dict:
    rules = rules or game_rules()
    prepare_city_production(city)
    if current_yields is None:
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
        "turn": game_state()["turn"] if turn is None else turn,
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


def get_production_catalog(city_id: str | None = None):
    catalog = game_data_repository.catalog()
    options = production_catalog(catalog)
    cities = city_repository.get_all()
    map_data = map_repository.get_map({})
    if city_id is not None and city_id not in cities:
        raise ApplicationError(404, "City not found.")
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
            except ApplicationError as error:
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


def get_city_production(city_id: str):
    cities = city_repository.get_all()
    city = cities.get(city_id)
    if city is None:
        raise ApplicationError(404, "City not found.")
    return production_status(
        city, map_repository.get_map({}), game_data_repository.catalog()
    )


@synchronized_mutation
def add_production(city_id: str, request: ProductionQueueRequest):
    cities = city_repository.get_all()
    city = cities.get(city_id)
    if city is None:
        raise ApplicationError(404, "City not found.")
    prepare_city_production(city)
    map_data = map_repository.get_map({})
    catalog = game_data_repository.catalog()
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
    city_repository.save_all(cities, atomic=True)
    return production_status(city, map_data, catalog)


@synchronized_mutation
def reorder_production(city_id: str, request: ProductionReorderRequest):
    cities = city_repository.get_all()
    city = cities.get(city_id)
    if city is None:
        raise ApplicationError(404, "City not found.")
    prepare_city_production(city)
    index = next(
        (i for i, entry in enumerate(city["production_queue"]) if entry["queue_id"] == request.queue_id),
        None,
    )
    if index is None:
        raise ApplicationError(404, "Production queue item not found.")
    if request.position >= len(city["production_queue"]):
        raise ApplicationError(422, "Production queue position is outside the queue.")
    entry = city["production_queue"].pop(index)
    city["production_queue"].insert(request.position, entry)
    city["current_production"] = city["production_queue"][0]["queue_id"]
    city_repository.save_all(cities, atomic=True)
    return production_status(
        city, map_repository.get_map({}), game_data_repository.catalog()
    )


@synchronized_mutation
def remove_production(city_id: str, queue_id: str):
    cities = city_repository.get_all()
    city = cities.get(city_id)
    if city is None:
        raise ApplicationError(404, "City not found.")
    prepare_city_production(city)
    index = next(
        (i for i, entry in enumerate(city["production_queue"]) if entry["queue_id"] == queue_id),
        None,
    )
    if index is None:
        raise ApplicationError(404, "Production queue item not found.")
    city["production_queue"].pop(index)
    city["production_progress"].pop(queue_id, None)
    city["current_production"] = (
        city["production_queue"][0]["queue_id"] if city["production_queue"] else None
    )
    city_repository.save_all(cities, atomic=True)
    return production_status(
        city, map_repository.get_map({}), game_data_repository.catalog()
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
            raise ApplicationError(422, f"BaseMoves must be positive for {item_id}.")
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


@synchronized_mutation
def end_turn(city_id: str | None = None):
    rules = game_rules()
    cities = city_repository.get_all()
    if city_id is not None and city_id not in cities:
        raise ApplicationError(404, "City not found.")
    map_data = map_repository.get_map({})
    catalog = game_data_repository.catalog()
    state = game_state()
    completed = []
    completed_research = []
    map_changed = False
    map_changes = []
    production_by_city = {}
    food_yield_by_city = {}
    research_yield_by_category = {"technology": 0.0, "civic": 0.0}
    for current_city_id, city in cities.items():
        totals = calculate_city_yields(city, map_data, catalog, rules)["totals"]
        production_by_city[current_city_id] = totals["Production"]
        food_yield_by_city[current_city_id] = totals["Food"]
        research_yield_by_category["technology"] += float(totals["Science"])
        research_yield_by_category["civic"] += float(totals["Culture"])
    for city in cities.values():
        process_city_food_turn(
            city,
            food_yield_by_city[city["id"]],
            state["game_speed"],
            rules,
        )
        prepare_city_production(city)
        production = production_by_city[city["id"]]
        if not math.isfinite(float(production)) or production < 0:
            raise ApplicationError(422, f"City {city['name']} has invalid Production.")
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
                raise ApplicationError(
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
            map_changed = map_changed or completion["type"] in {
                "district",
                "wonder",
            }
            if completion["type"] in {"district", "wonder"}:
                map_change = {
                    "x": completion["x"],
                    "y": completion["y"],
                    "district": (
                        completion["id"]
                        if completion["type"] == "district"
                        else "DISTRICT_WONDER"
                    ),
                    "worked": False,
                }
                if completion["type"] == "district":
                    map_change["improvement"] = None
                map_changes.append(map_change)
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
            raise ApplicationError(
                422, f"Research cost is unavailable for selected target {target_id}."
            )
        earned = research_yield_by_category[category]
        if not math.isfinite(earned) or earned < 0:
            raise ApplicationError(422, f"Invalid {category} research yield.")
        progress = track["progress"].get(target_id, 0) + earned
        if progress >= cost:
            progress = float(cost)
            if target_id not in state[completed_key]:
                state[completed_key].append(target_id)
            track["current_id"] = None
            completed_research.append({"category": category, "id": target_id})
        track["progress"][target_id] = progress
    for city in cities.values():
        refresh_city_food_state(
            city,
            map_data,
            catalog,
            state["game_speed"],
            rules,
        )
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
    research_summary = {}
    research_tables = {
        "technology": ("Technologies::Technologies", "TechnologyType"),
        "civic": ("Civics::Civics", "CivicType"),
    }
    for category, (table, key) in research_tables.items():
        track = state["research"][category]
        target_id = track["current_id"]
        row = index_rows(catalog, table, key).get(target_id) if target_id else None
        cost = catalog_cost(row) if row else None
        research_summary[category] = {
            "current_id": target_id,
            "progress": track["progress"].get(target_id, 0) if target_id else 0,
            "cost": cost,
            "yield_per_turn": research_yield_by_category[category],
        }
    selected_city_data = None
    if city_id is not None:
        city = cities[city_id]
        city_yields = calculate_city_yields(city, map_data, catalog, rules)
        selected_city_data = {
            "selected_city_yields": city_yields,
            "selected_city_food": city_food_status(
                city, city_yields["totals"]["Food"], state["game_speed"], rules
            ),
            "selected_city_production": production_status(
                city,
                map_data,
                catalog,
                rules=rules,
                current_yields=city_yields,
                turn=state["turn"],
            ),
        }
    updates = {
        locations.cities_file(): cities,
        locations.game_state_file(): state,
    }
    if map_changed:
        updates[locations.map_file()] = map_data
    write_json_transaction(updates)
    result = {
        "turn": state["turn"],
        "completed": completed,
        "completed_research": completed_research,
        "game_state": state,
        "cities": list(cities.values()),
        "map_changes": map_changes,
        "research_summary": research_summary,
    }
    if selected_city_data is not None:
        result.update(selected_city_data)
    return result
