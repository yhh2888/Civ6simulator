from typing import TypedDict

from app.models.map import GameMap


class MapResponse(GameMap, total=False):
    pass
