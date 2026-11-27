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

from app.services.game_data_service import (catalog_cost, effective_catalog_row, game_rules, game_state, index_rows, raw_table)
def get_entity_catalog(
    category: str | None = None,
    city_id: str | None = None,
    x: int | None = None,
    y: int | None = None,
):
    categories = [category] if category else list(ENTITY_SOURCES)
    unknown = [name for name in categories if name not in ENTITY_SOURCES]
    if unknown:
        raise ApplicationError(
            422, f"Unsupported entity category: {', '.join(unknown)}."
        )
    if (x is None) != (y is None):
        raise ApplicationError(422, "Tile placement requires both x and y.")
    if (x is not None or y is not None) and city_id is None:
        raise ApplicationError(422, "Tile placement requires a city_id.")

    catalog = game_data_repository.catalog()
    state = game_state()
    rules = game_rules()
    cities = city_repository.get_all()
    map_data = map_repository.get_map({})
    city = cities.get(city_id) if city_id else None
    if city_id and city is None:
        raise ApplicationError(404, "City not found.")
    tile = None
    if x is not None and y is not None:
        tile = next(
            (
                item for item in map_data.get("tiles", [])
                if item.get("x") == x and item.get("y") == y
            ),
            None,
        )
        if tile is None:
            raise ApplicationError(404, "Placement tile not found.")

    result = {}
    for entity_category in categories:
        items = []
        section = {
            "buildings": "buildings",
            "wonders": "buildings",
            "units": "units",
            "districts": "districts",
            "improvements": "improvements",
        }.get(entity_category)
        for entity_id, source_row in entity_rows(catalog, entity_category):
            row = (
                effective_catalog_row(source_row, section, entity_id, rules)
                if section
                else source_row
            )
            reasons = entity_availability(
                catalog,
                entity_category,
                entity_id,
                row,
                state,
                city=city,
                tile=tile,
                map_data=map_data,
                cities=cities,
                rules=rules,
            )
            items.append(
                {
                    "id": entity_id,
                    "name": row.get("Name", entity_id),
                    "available": not reasons,
                    "unavailable_reasons": reasons,
                    "prereq_tech": row.get("PrereqTech"),
                    "prereq_civic": row.get("PrereqCivic"),
                    "prereq_district": row.get("PrereqDistrict"),
                    "cost": catalog_cost(row),
                }
            )
        result[entity_category] = {"count": len(items), "items": items}
    return {
        "categories": result,
        "context": {
            "city_id": city_id,
            "tile": {"x": x, "y": y} if tile else None,
            "technologies": state["technologies"],
            "civics": state["civics"],
        },
    }


@synchronized_mutation
def force_add_entity(request: ForceEntityRequest):
    if request.category not in ("buildings", "wonders", "districts", "improvements"):
        raise ApplicationError(
            422,
            "Forced entity category must be buildings, wonders, districts, or improvements.",
        )
    needs_tile = request.category in ("wonders", "districts", "improvements")
    if needs_tile and (request.x is None or request.y is None):
        raise ApplicationError(422, "This entity requires a selected placement tile.")
    if not needs_tile and (request.x is not None or request.y is not None):
        raise ApplicationError(422, "Buildings do not accept a placement tile.")

    cities = city_repository.get_all()
    city = cities.get(request.city_id)
    if city is None:
        raise ApplicationError(404, "City not found.")
    catalog = game_data_repository.catalog()
    source_row = dict(entity_rows(catalog, request.category)).get(request.entity_id)
    if source_row is None:
        raise ApplicationError(422, "Unknown entity for the selected category.")
    rules = game_rules()
    section = "districts" if request.category == "districts" else (
        "improvements" if request.category == "improvements" else "buildings"
    )
    row = effective_catalog_row(
        source_row, section, request.entity_id, rules
    )
    map_data = map_repository.get_map({})
    tile = None
    if needs_tile:
        tile = next(
            (
                item for item in map_data.get("tiles", [])
                if item.get("x") == request.x and item.get("y") == request.y
            ),
            None,
        )
        if tile is None:
            raise ApplicationError(404, "Placement tile not found.")

    state = game_state()
    unavailable = entity_availability(
        catalog,
        request.category,
        request.entity_id,
        row,
        state,
        city=city,
        tile=tile,
        map_data=map_data,
        cities=cities,
        rules=rules,
    )
    if unavailable:
        raise ApplicationError(409, " ".join(unavailable))

    if request.category == "buildings":
        city.setdefault("buildings", []).append(request.entity_id)
        city_repository.save_all(cities, atomic=True)
        return {"category": request.category, "id": request.entity_id}
    if request.category == "wonders":
        tile["district"] = "DISTRICT_WONDER"
        tile["worked"] = False
        city.setdefault("wonders", []).append(request.entity_id)
    elif request.category == "districts":
        tile["district"] = request.entity_id
        tile["improvement"] = None
        tile["worked"] = False
        if request.entity_id not in city.setdefault("districts", []):
            city["districts"].append(request.entity_id)
    else:
        tile["improvement"] = request.entity_id
    write_json_transaction(
        {locations.cities_file(): cities, locations.map_file(): map_data}
    )
    return {
        "category": request.category,
        "id": request.entity_id,
        "x": tile["x"],
        "y": tile["y"],
    }
