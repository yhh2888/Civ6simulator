"""Shared fixtures for simulation-engine tests.

Uses the real XML-derived rules_db.json and catalog.json from app/data so the
data-driven rules are exercised with genuine Civ VI values, on a small
synthetic map whose expected numbers can be derived by hand.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from app.config import load_game_rules
from app.repositories.rules_db_repository import RulesDB
from app.services.sim_context import build_context, hex_distance
from app.services.turn_simulation_service import Statics

APP_DATA = Path(__file__).resolve().parent.parent / "app" / "data"

_CACHE: dict[str, object] = {}


def real_catalog() -> dict:
    if "catalog" not in _CACHE:
        _CACHE["catalog"] = json.loads((APP_DATA / "catalog.json").read_text(encoding="utf-8"))
    return _CACHE["catalog"]


def real_db() -> RulesDB:
    if "db" not in _CACHE:
        _CACHE["db"] = RulesDB(json.loads((APP_DATA / "rules_db.json").read_text(encoding="utf-8")))
    return _CACHE["db"]


def real_db_payload() -> dict:
    if "db_payload" not in _CACHE:
        _CACHE["db_payload"] = json.loads((APP_DATA / "rules_db.json").read_text(encoding="utf-8"))
    return _CACHE["db_payload"]


def base_rules(**simulation) -> dict:
    rules = load_game_rules(APP_DATA / "game_rules.json")
    rules = deepcopy(rules)
    if simulation:
        rules["simulation"] = {**rules.get("simulation", {}), **simulation}
    return rules


def make_map(width: int = 11, height: int = 9, terrain: str = "TERRAIN_GRASS") -> dict:
    tiles = [
        {"x": x, "y": y, "terrain": terrain, "feature": None, "resource": None, "improvement": None,
         "district": None, "owner_city_id": None, "worked": False, "river": False, "river_wires": None,
         "hill_override": None}
        for y in range(height)
        for x in range(width)
    ]
    return {"width": width, "height": height, "count": len(tiles), "tiles": tiles}


def tile(map_data: dict, x: int, y: int) -> dict:
    return next(t for t in map_data["tiles"] if t["x"] == x and t["y"] == y)


def make_city(city_id: str = "city-a", x: int = 5, y: int = 4, population: int = 3, **extra) -> dict:
    city = {
        "id": city_id, "name": extra.pop("name", "Seoul"), "x": x, "y": y, "population": population,
        "civilization": None, "leader": None, "buildings": ["BUILDING_PALACE"], "districts": [], "wonders": [],
        "governor": None, "governor_promotions": [], "policies": [], "technologies": [], "civics": [],
        "religion": None, "housing": 2, "amenities": 0, "notes": "", "worked_tiles": [],
        "production_queue": [], "production_progress": {}, "current_production": None, "production_history": [],
        "stored_food": 0.0,
    }
    city.update(extra)
    return city


def claim(map_data: dict, city: dict, radius: int = 2) -> None:
    for t in map_data["tiles"]:
        if hex_distance(t["x"], t["y"], city["x"], city["y"]) <= radius:
            t["owner_city_id"] = city["id"]
    tile(map_data, city["x"], city["y"])["worked"] = True


def make_game(**extra) -> dict:
    game = {"turn": 1, "units": [], "game_speed": "STANDARD", "technologies": [], "civics": [],
            "research": {"technology": {"current_id": None, "progress": {}},
                         "civic": {"current_id": None, "progress": {}}}}
    game.update(extra)
    return game


def bundle(map_data: dict, cities: dict, game: dict | None = None) -> dict:
    return {"map": map_data, "cities": cities, "game": game or make_game()}


def statics(rules: dict | None = None) -> Statics:
    return Statics(real_catalog(), real_db(), rules or base_rules())


def context(b: dict, rules: dict | None = None):
    st = statics(rules)
    return build_context(b["map"], b["cities"], b["game"], st.catalog, st.db, st.rules)
