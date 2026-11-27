from typing import TypedDict


class Building(TypedDict, total=False):
    id: str
    yields: dict[str, float]
