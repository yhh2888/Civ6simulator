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

from app.services.game_data_service import (catalog_cost, game_rules, game_state, index_rows, research_catalog, research_prerequisites, unlocked_research)
from app.services.yield_service import calculate_city_yields
def research_status(state: dict | None = None) -> dict:
    state = state or game_state()
    cities = city_repository.get_all()
    map_data = map_repository.get_map({})
    catalog = game_data_repository.catalog()
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


def get_research_status():
    return research_status()


@synchronized_mutation
def select_research(category: str, request: ResearchSelectRequest):
    if category not in ("technology", "civic"):
        raise ApplicationError(404, "Research category must be technology or civic.")
    state = game_state()
    catalog = game_data_repository.catalog()
    option = next(
        (
            item
            for item in research_catalog(catalog, state)[category]
            if item["id"] == request.target_id
        ),
        None,
    )
    if option is None:
        raise ApplicationError(422, "Unknown research target.")
    if not option["available"]:
        raise ApplicationError(409, option["unavailable_reason"])
    state["research"][category]["current_id"] = request.target_id
    state["research"][category]["progress"].setdefault(request.target_id, 0)
    game_data_repository.save_game_state(state)
    return research_status(state)


@synchronized_mutation
def set_research_availability(
    category: str,
    target_id: str,
    request: ResearchAvailabilityRequest,
):
    if category not in ("technology", "civic"):
        raise ApplicationError(404, "Research category must be technology or civic.")
    catalog = game_data_repository.catalog()
    table, key, state_key = (
        ("Technologies::Technologies", "TechnologyType", "technologies")
        if category == "technology"
        else ("Civics::Civics", "CivicType", "civics")
    )
    if target_id not in index_rows(catalog, table, key):
        raise ApplicationError(404, "Research target not found.")

    state = game_state()
    cities = city_repository.get_all()
    completed = set(state[state_key])
    track = state["research"][category]
    if request.unlocked:
        completed.add(target_id)
    else:
        completed.discard(target_id)
        if isinstance(cities, dict):
            for city in cities.values():
                if isinstance(city, dict):
                    city_research = city.get(state_key, [])
                    if not isinstance(city_research, list) or any(
                        not isinstance(item, str) or not item
                        for item in city_research
                    ):
                        raise ApplicationError(
                            500,
                            f"City {state_key} research data is invalid.",
                        )
                    city[state_key] = [
                        item
                        for item in city_research
                        if item != target_id
                    ]
        track["progress"].pop(target_id, None)
    state[state_key] = sorted(completed)
    if track["current_id"] == target_id:
        track["current_id"] = None
    updates = {locations.game_state_file(): state}
    if not request.unlocked:
        updates[locations.cities_file()] = cities
    write_json_transaction(updates)
    return research_status(state)
