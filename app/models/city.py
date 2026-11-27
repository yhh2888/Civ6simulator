from typing import Any, TypedDict


class City(TypedDict, total=False):
    id: str
    name: str
    x: int
    y: int
    population: int
    stored_food: float
    food_yield: float
    food_consumption: float
    food_surplus: float
    food_required: float | None
    buildings: list[str]
    districts: list[str]
    wonders: list[str]
    production_queue: list[dict[str, Any]]
