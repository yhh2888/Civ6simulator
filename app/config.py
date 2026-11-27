"""Load and validate gameplay rules stored outside the simulation code."""

from __future__ import annotations

import json
import math
from copy import deepcopy
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from app.core.game_speed import GAME_SPEEDS

YIELD_KEYS = ("Food", "Production", "Gold", "Science", "Culture", "Faith")


class RuleConfigurationError(ValueError):
    """Raised when the external gameplay rules cannot safely be used."""


def _fail(path: Path, message: str, rule_id: str | None = None) -> None:
    prefix = f"Rule '{rule_id}': " if rule_id else ""
    raise RuleConfigurationError(f"Invalid rules file {path}: {prefix}{message}")


def _object(
    value: Any,
    path: Path,
    label: str,
    allowed: set[str],
    required: set[str],
    rule_id: str | None = None,
) -> dict:
    if not isinstance(value, dict):
        _fail(path, f"{label} must be a JSON object.", rule_id)
    unknown = set(value) - allowed
    missing = required - set(value)
    if unknown:
        _fail(
            path,
            f"{label} has unknown key(s): {', '.join(sorted(unknown))}.",
            rule_id,
        )
    if missing:
        _fail(
            path,
            f"{label} is missing key(s): {', '.join(sorted(missing))}.",
            rule_id,
        )
    return value


def _integer(value: Any, path: Path, label: str, minimum: int = 0) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        _fail(path, f"{label} must be an integer >= {minimum}.")


def _finite_number(value: Any, path: Path, label: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        _fail(path, f"{label} must be a finite number.")


def _string_list(value: Any, path: Path, label: str) -> None:
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) or not item for item in value)
        or len(value) != len(set(value))
    ):
        _fail(path, f"{label} must be a list of unique, non-empty strings.")


_MODIFIER_CONDITIONS = {
    "terrain",
    "terrain_contains",
    "feature",
    "resource",
    "improvement",
    "district",
    "worked",
    "city_center",
    "is_hills",
    "river_adjacent",
}
_MODIFIER_STRING_CONDITIONS = {
    "terrain",
    "feature",
    "resource",
    "improvement",
    "district",
}
_ENTITY_FIELDS = {
    "districts": {
        "allowed_terrains",
        "required_tech",
        "required_civic",
        "max_per_city",
    },
    "buildings": {
        "required_district",
        "required_tech",
        "required_civic",
        "required_buildings",
    },
    "units": {
        "required_tech",
        "required_civic",
        "min_population",
        "build_charges",
        "base_moves",
        "population_cost",
    },
    "improvements": {
        "allowed_terrains",
        "allowed_features",
        "allowed_resources",
        "required_tech",
        "required_civic",
        "build_charges",
    },
}
_OPTIONAL_SECTIONS = {
    "districts": {"default_max_per_city": 1, "overrides": {}},
    "buildings": {"overrides": {}},
    "units": {
        "default_build_charges": 0,
        "default_base_moves": 1,
        "overrides": {},
    },
    "improvements": {
        "default_build_charges": 1,
        "feature_removal_charge_cost": 1,
        "overrides": {},
    },
}
_OPTIONAL_RULES = {
    "food": {
        "food_per_population": 2.0,
        "max_growths_per_turn": 1,
    },
    "game_speed": "STANDARD",
}


_SIMULATION_BOOLEANS = {
    "apply_modifiers", "apply_amenity_effects", "apply_housing_growth_penalty",
    "apply_game_speed_costs", "apply_cost_progression", "resource_visibility_requires_tech",
    "starvation_pop_loss",
}
_SIMULATION_CHOICES = {
    "population_yields_source": ("config", "global_parameters"),
    "luxury_amenities": ("auto", "off"),
    "overflow_policy": ("carry_to_next", "same_turn_queue", "stop_at_item"),
    "research_overflow": ("carry", "discard"),
    "default_citizen_mode": ("manual", "auto"),
    "default_strategy": ("balanced", "food", "production", "gold", "science", "culture",
                         "faith", "growth", "build", "custom"),
    "default_new_citizen_policy": ("auto", "manual"),
}


def _validate_simulation(section: Any, path: Path) -> None:
    allowed = _SIMULATION_BOOLEANS | set(_SIMULATION_CHOICES) | {"max_simulation_turns"}
    _object(section, path, "simulation", allowed, set())
    for key in _SIMULATION_BOOLEANS & set(section):
        if not isinstance(section[key], bool):
            _fail(path, f"simulation.{key} must be true or false.")
    for key, choices in _SIMULATION_CHOICES.items():
        if key in section and section[key] not in choices:
            _fail(path, f"simulation.{key} must be one of {', '.join(choices)}.")
    if "max_simulation_turns" in section:
        _integer(section["max_simulation_turns"], path, "simulation.max_simulation_turns", 1)


def validate_game_rules(rules: Any, path: Path) -> dict:
    if not isinstance(rules, dict):
        _fail(path, "Top-level value must be a JSON object.")
    for key, default in _OPTIONAL_RULES.items():
        rules.setdefault(key, deepcopy(default))
    allowed_top = {
        "simulation",
        "schema_version",
        "city",
        "production",
        "tile_yields",
        "tile_yield_modifiers",
        *_OPTIONAL_SECTIONS,
        *_OPTIONAL_RULES,
    }
    _object(
        rules,
        path,
        "top-level rules",
        allowed_top,
        allowed_top - set(_OPTIONAL_SECTIONS) - set(_OPTIONAL_RULES) - {"simulation"},
    )
    if "simulation" in rules:
        _validate_simulation(rules["simulation"], path)
    if rules["schema_version"] != 2 or isinstance(rules["schema_version"], bool):
        _fail(path, "schema_version must be 2.")
    if (
        not isinstance(rules["game_speed"], str)
        or rules["game_speed"] not in GAME_SPEEDS
    ):
        _fail(
            path,
            "game_speed must be one of "
            + ", ".join(sorted(GAME_SPEEDS))
            + ".",
        )
    food_rules = _object(
        rules["food"],
        path,
        "food",
        {"food_per_population", "max_growths_per_turn"},
        {"food_per_population", "max_growths_per_turn"},
    )
    _finite_number(
        food_rules["food_per_population"],
        path,
        "food.food_per_population",
    )
    if food_rules["food_per_population"] < 0:
        _fail(path, "food.food_per_population must be non-negative.")
    _integer(
        food_rules["max_growths_per_turn"],
        path,
        "food.max_growths_per_turn",
    )

    city_fields = {
        "minimum_distance",
        "initial_population",
        "initial_territory_radius",
        "maximum_population",
        "worked_tiles_per_population",
        "city_center_min_yields",
        "population_yields",
    }
    city = _object(rules["city"], path, "city", city_fields, city_fields)
    _integer(city["minimum_distance"], path, "city.minimum_distance", 1)
    _integer(city["initial_population"], path, "city.initial_population", 1)
    _integer(city["initial_territory_radius"], path, "city.initial_territory_radius")
    _integer(city["maximum_population"], path, "city.maximum_population", 1)
    if city["initial_population"] > city["maximum_population"]:
        _fail(path, "city.initial_population cannot exceed city.maximum_population.")
    _integer(
        city["worked_tiles_per_population"],
        path,
        "city.worked_tiles_per_population",
    )
    center_yields = _object(
        city["city_center_min_yields"],
        path,
        "city.city_center_min_yields",
        set(YIELD_KEYS),
        set(),
    )
    for name, amount in center_yields.items():
        _finite_number(amount, path, f"city.city_center_min_yields.{name}")
    population_yields = _object(
        city["population_yields"],
        path,
        "city.population_yields",
        set(YIELD_KEYS),
        set(),
    )
    for name, amount in population_yields.items():
        _finite_number(amount, path, f"city.population_yields.{name}")

    tile_yield_fields = {"fallback_terrain_yields", "hill_production_delta"}
    tile_yields = _object(
        rules["tile_yields"],
        path,
        "tile_yields",
        tile_yield_fields,
        tile_yield_fields,
    )
    fallbacks = tile_yields["fallback_terrain_yields"]
    if not isinstance(fallbacks, dict):
        _fail(path, "tile_yields.fallback_terrain_yields must be an object.")
    for terrain, yields in fallbacks.items():
        if not isinstance(terrain, str) or not terrain:
            _fail(path, "Fallback terrain keys must be non-empty strings.")
        if not isinstance(yields, dict) or set(yields) - set(YIELD_KEYS):
            _fail(path, f"Invalid fallback yields for terrain '{terrain}'.")
        for name, amount in yields.items():
            _finite_number(
                amount, path, f"tile_yields.fallback_terrain_yields.{terrain}.{name}"
            )
    _finite_number(
        tile_yields["hill_production_delta"],
        path,
        "tile_yields.hill_production_delta",
    )
    if tile_yields["hill_production_delta"] < 0:
        _fail(path, "tile_yields.hill_production_delta must be non-negative.")

    production_fields = {
        "initial_progress",
        "overflow_policy",
        "cost_overrides",
    }
    production = _object(
        rules["production"], path, "production", production_fields, production_fields
    )
    _finite_number(
        production["initial_progress"], path, "production.initial_progress"
    )
    if production["initial_progress"] < 0:
        _fail(path, "production.initial_progress must be non-negative.")
    if production["overflow_policy"] not in ("same_turn_queue", "stop_at_item"):
        _fail(
            path,
            "production.overflow_policy must be 'same_turn_queue' or "
            "'stop_at_item'.",
        )
    cost_overrides = _object(
        production["cost_overrides"],
        path,
        "production.cost_overrides",
        {"unit", "building", "wonder", "district"},
        {"unit", "building", "wonder", "district"},
    )
    for category, overrides in cost_overrides.items():
        overrides = _object(
            overrides, path, f"production.cost_overrides.{category}", set(overrides), set()
        )
        for target_id, cost in overrides.items():
            _integer(
                cost,
                path,
                f"production.cost_overrides.{category}.{target_id}",
                1,
            )

    modifiers = rules["tile_yield_modifiers"]
    if not isinstance(modifiers, list):
        _fail(path, "tile_yield_modifiers must be a list.")
    modifier_ids = set()
    for modifier in modifiers:
        if not isinstance(modifier, dict):
            _fail(path, "Each tile yield modifier must be an object.")
        modifier_id = modifier.get("id")
        if not isinstance(modifier_id, str) or not modifier_id.strip():
            _fail(path, "Each tile yield modifier needs a non-empty id.")
        if modifier_id in modifier_ids:
            _fail(path, "Duplicate tile yield modifier id.", modifier_id)
        modifier_ids.add(modifier_id)
        required = {"id", "when", "yield_changes"}
        allowed = required | {"enabled", "description"}
        _object(
            modifier, path, "tile yield modifier", allowed, required, modifier_id
        )
        if "enabled" in modifier and not isinstance(modifier["enabled"], bool):
            _fail(path, "enabled must be a boolean.", modifier_id)
        if "description" in modifier and not isinstance(
            modifier["description"], str
        ):
            _fail(path, "description must be a string.", modifier_id)
        conditions = modifier["when"]
        if not isinstance(conditions, dict):
            _fail(path, "when must be an object.", modifier_id)
        unknown_conditions = set(conditions) - _MODIFIER_CONDITIONS
        if unknown_conditions:
            _fail(
                path,
                "unsupported condition(s): "
                + ", ".join(sorted(unknown_conditions)),
                modifier_id,
            )
        for condition, expected in conditions.items():
            if condition == "terrain_contains":
                if not isinstance(expected, str):
                    _fail(path, "terrain_contains must be a string.", modifier_id)
            elif condition in _MODIFIER_STRING_CONDITIONS:
                if isinstance(expected, str) and expected:
                    continue
                if (
                    isinstance(expected, list)
                    and expected
                    and all(isinstance(value, str) and value for value in expected)
                    and len(expected) == len(set(expected))
                ):
                    continue
                _fail(
                    path,
                    f"{condition} must be a string or a list of unique strings.",
                    modifier_id,
                )
            elif not isinstance(expected, bool):
                _fail(path, f"{condition} must be a boolean.", modifier_id)
        changes = modifier["yield_changes"]
        if not isinstance(changes, dict) or not changes:
            _fail(path, "yield_changes must be a non-empty object.", modifier_id)
        for name, amount in changes.items():
            if name not in YIELD_KEYS:
                _fail(path, f"unsupported yield '{name}'.", modifier_id)
            _finite_number(amount, path, f"yield_changes.{name} for {modifier_id}")

    for section, defaults in _OPTIONAL_SECTIONS.items():
        rules.setdefault(section, defaults.copy())
        section_fields = set(defaults)
        config = _object(
            rules[section],
            path,
            section,
            section_fields,
            section_fields,
        )
        for field in section_fields - {"overrides"}:
            _integer(
                config[field],
                path,
                f"{section}.{field}",
                1
                if field
                in {
                    "default_max_per_city",
                    "default_base_moves",
                }
                else 0,
            )
        overrides = config["overrides"]
        if not isinstance(overrides, dict):
            _fail(path, f"{section}.overrides must be an object.")
        for entity_id, override in overrides.items():
            if not isinstance(entity_id, str) or not entity_id:
                _fail(path, f"{section}.overrides keys must be non-empty IDs.")
            if not isinstance(override, dict):
                _fail(path, f"{section} override must be an object.", entity_id)
            unknown_fields = set(override) - _ENTITY_FIELDS[section]
            if unknown_fields:
                _fail(
                    path,
                    "unsupported override field(s): "
                    + ", ".join(sorted(unknown_fields)),
                    entity_id,
                )
            for name, value in override.items():
                label = f"{section}.overrides.{entity_id}.{name}"
                if name in {"cost", "max_per_city", "min_population", "build_charges"}:
                    _integer(value, path, label, 1 if name in {"cost", "max_per_city"} else 0)
                elif name == "base_moves":
                    _integer(value, path, label, 1)
                elif name == "population_cost":
                    _integer(value, path, label)
                elif name in {
                    "required_tech",
                    "required_civic",
                    "required_district",
                }:
                    if not isinstance(value, str) or not value:
                        _fail(path, f"{label} must be a non-empty string.")
                elif name == "required_buildings":
                    _string_list(value, path, label)
                else:
                    _string_list(value, path, label)

    return rules


def load_game_rules(path: Path) -> dict:
    """Read the current rules file; missing or invalid files are never ignored."""
    try:
        rules = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise RuleConfigurationError(
            f"Gameplay rules file is missing: {path}. Restore app/data/game_rules.json."
        ) from error
    except (OSError, json.JSONDecodeError) as error:
        raise RuleConfigurationError(
            f"Cannot read gameplay rules file {path}: {error}"
        ) from error
    if isinstance(rules, dict) and rules.get("schema_version") == 1:
        rules = deepcopy(rules)
        city = rules.get("city")
        if not isinstance(city, dict):
            _fail(path, "city must be a JSON object.")
        city.setdefault("population_yields", {"Science": 0.5, "Culture": 0.2})
        rules["schema_version"] = 2
    return validate_game_rules(rules, path)


def matching_tile_yield_modifiers(
    context: Mapping[str, Any], modifiers: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Return enabled modifiers whose declared conditions all match."""
    matched = []
    for modifier in modifiers:
        if not modifier.get("enabled", True):
            continue
        matches = True
        for condition, expected in modifier["when"].items():
            if condition == "terrain_contains":
                matches = expected in str(context.get("terrain", ""))
            else:
                actual = context.get(condition)
                matches = (
                    actual in expected
                    if isinstance(expected, list)
                    else actual == expected
                )
            if not matches:
                break
        if matches:
            matched.append(modifier)
    return matched
