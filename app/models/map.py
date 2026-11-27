from typing import TypedDict

from app.models.tile import Tile


class GameMap(TypedDict, total=False):
    width: int
    height: int
    count: int
    tiles: list[Tile]
