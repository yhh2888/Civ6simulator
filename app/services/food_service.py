from __future__ import annotations

import math
from typing import Any

from app.core.game_speed import GROWTH_COST_MULTIPLIERS, GAME_SPEEDS
from app.core.exceptions import ApplicationError
from app.repositories.city_repository import city_repository
from app.repositories.game_data_repository import game_data_repository
from app.repositories.map_repository import map_repository
from app.services.game_data_service import game_rules, game_state
from app.services.yield_service import calculate_city_yields


def calculate_food_required(
    population: int, game_speed: str = "STANDARD"
) -> float:
    if isinstance(population, bool) or not isinstance(population, int):
        raise ValueError("Population must be an integer.")
    if population < 1:
        raise ValueError("Population must be at least 1.")
    if not isinstance(game_speed, str) or game_speed not in GAME_SPEEDS:
        raise ValueError(f"Unsupported game speed: {game_speed}.")
    multiplier = GROWTH_COST_MULTIPLIERS.get(game_speed)
    if multiplier is None:
        raise ValueError(
            f"Food growth requirements are not verified for {game_speed} speed."
        )
    n = population
    return (15 + 8 * (n - 1) + (n - 1) ** 1.5) * multiplier


def calculate_food_consumption(
    population: int, game_speed: str, rules: dict[str, Any]
) -> float:
    if isinstance(population, bool) or not isinstance(population, int):
        raise ValueError("Population must be an integer.")
    if population < 1:
        raise ValueError("Population must be at least 1.")
    if not isinstance(game_speed, str) or game_speed not in GAME_SPEEDS:
        raise ValueError(f"Unsupported game speed: {game_speed}.")
    return float(rules["food"]["food_per_population"]) * population


def _stored_food(city: dict[str, Any]) -> float:
    value = city.get("stored_food", 0.0)
    if isinstance(value, bool):
        raise ApplicationError(422, "Stored Food must be a finite non-negative number.")
    try:
        amount = float(value)
    except (TypeError, ValueError) as error:
        raise ApplicationError(
            422, "Stored Food must be a finite non-negative number."
        ) from error
    if not math.isfinite(amount) or amount < 0:
        raise ApplicationError(422, "Stored Food must be a finite non-negative number.")
    return amount


def city_food_status(
    city: dict[str, Any],
    food_yield: float,
    game_speed: str,
    rules: dict[str, Any],
) -> dict[str, Any]:
    population = city.get("population", rules["city"]["initial_population"])
    if isinstance(population, bool) or not isinstance(population, int) or population < 1:
        raise ApplicationError(422, "City population must be a positive integer.")
    if (
        isinstance(food_yield, bool)
        or not isinstance(food_yield, (int, float))
        or not math.isfinite(food_yield)
    ):
        raise ApplicationError(422, "City Food yield must be finite.")
    consumption = calculate_food_consumption(population, game_speed, rules)
    surplus = float(food_yield) - consumption
    try:
        required: float | None = calculate_food_required(population, game_speed)
    except ValueError as error:
        if game_speed not in GAME_SPEEDS:
            raise ApplicationError(422, str(error)) from error
        required = None
    stored = _stored_food(city)
    progress = (
        min(100.0, max(0.0, stored / required * 100.0))
        if required is not None
        else None
    )
    return {
        "population": population,
        "stored_food": stored,
        "food_yield": float(food_yield),
        "food_consumption": consumption,
        "food_surplus": surplus,
        "food_required": required,
        "growth_progress": progress,
        "game_speed": game_speed,
        "growth_supported": required is not None,
        "growth_enabled": (
            required is not None and rules["food"]["max_growths_per_turn"] > 0
        ),
    }


def refresh_city_food_state(
    city: dict[str, Any],
    map_data: dict[str, Any],
    catalog: dict[str, Any],
    game_speed: str,
    rules: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rules = rules or game_rules()
    totals = calculate_city_yields(city, map_data, catalog, rules)["totals"]
    status = city_food_status(city, totals["Food"], game_speed, rules)
    city.update(
        {
            key: status[key]
            for key in (
                "stored_food",
                "food_yield",
                "food_consumption",
                "food_surplus",
                "food_required",
            )
        }
    )
    return status


def ensure_city_food_state(
    city: dict[str, Any], game_speed: str, rules: dict[str, Any]
) -> None:
    city.setdefault("stored_food", 0.0)
    city.setdefault("food_yield", 0.0)
    if (
        isinstance(city["food_yield"], bool)
        or not isinstance(city["food_yield"], (int, float))
        or not math.isfinite(city["food_yield"])
    ):
        raise ValueError("food_yield must be finite.")
    for key in ("food_consumption", "food_surplus"):
        if key in city and (
            isinstance(city[key], bool)
            or not isinstance(city[key], (int, float))
            or not math.isfinite(city[key])
        ):
            raise ValueError(f"{key} must be finite.")
    if city.get("food_consumption", 0) < 0:
        raise ValueError("food_consumption must be non-negative.")
    if "food_required" in city and city["food_required"] is not None:
        required_value = city["food_required"]
        if (
            isinstance(required_value, bool)
            or not isinstance(required_value, (int, float))
            or not math.isfinite(required_value)
            or required_value < 0
        ):
            raise ValueError("food_required must be a finite non-negative number.")
    status = city_food_status(
        city, city["food_yield"], game_speed, rules
    )
    city["food_consumption"] = status["food_consumption"]
    city["food_surplus"] = status["food_surplus"]
    city["food_required"] = status["food_required"]


def process_city_food_turn(
    city: dict[str, Any],
    food_yield: float,
    game_speed: str,
    rules: dict[str, Any],
) -> dict[str, Any]:
    status = city_food_status(city, food_yield, game_speed, rules)
    stored = max(0.0, status["stored_food"] + status["food_surplus"])
    population = status["population"]
    growth_limit = rules["food"]["max_growths_per_turn"]
    growths = 0
    required = status["food_required"]
    while (
        required is not None
        and growths < growth_limit
        and population < rules["city"]["maximum_population"]
        and stored >= required
    ):
        stored -= required
        population += 1
        growths += 1
        required = calculate_food_required(population, game_speed)

    city["population"] = population
    city["stored_food"] = stored
    city["food_yield"] = status["food_yield"]
    city["food_consumption"] = calculate_food_consumption(
        population, game_speed, rules
    )
    city["food_surplus"] = (
        status["food_yield"] - city["food_consumption"]
    )
    city["food_required"] = required
    return {
        **status,
        "population": population,
        "stored_food": stored,
        "food_consumption": city["food_consumption"],
        "food_surplus": city["food_surplus"],
        "food_required": required,
        "growth_progress": (
            min(100.0, max(0.0, stored / required * 100.0))
            if required is not None
            else None
        ),
        "growths": growths,
    }


def get_city_food(city_id: str) -> dict[str, Any]:
    cities = city_repository.get_all()
    city = cities.get(city_id)
    if city is None:
        raise ApplicationError(404, "City not found.")
    rules = game_rules()
    state = game_state()
    totals = calculate_city_yields(
        city,
        map_repository.get_map({}),
        game_data_repository.catalog(),
        rules,
    )["totals"]
    return city_food_status(city, totals["Food"], state["game_speed"], rules)
