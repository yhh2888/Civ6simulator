from typing import TypedDict


class Tile(TypedDict, total=False):
    x: int
    y: int
    terrain: str
    feature: str | None
    resource: str | None
    improvement: str | None
    district: str | None
    owner_city_id: str | None
    worked: bool
