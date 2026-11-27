"""Access to the active map and city JSON documents."""

from typing import Any

from app.repositories import locations
from app.repositories.json_repository import (
    active_map_has_tile,
    read,
    write,
    write_atomically,
)


class MapRepository:
    def get_map(self, default: dict[str, Any] | None = None) -> dict[str, Any]:
        if default is None:
            default = {"width": 0, "height": 0, "count": 0, "tiles": []}
        return read(
            locations.map_file(),
            default,
        )

    def save_map(self, map_data: dict[str, Any], *, atomic: bool = False) -> None:
        saver = write_atomically if atomic else write
        saver(locations.map_file(), map_data)

    def has_tile(self, x: int, y: int) -> bool:
        return active_map_has_tile(x, y)

    def get_cities(self, default: dict[str, Any] | None = None) -> dict[str, Any]:
        return read(locations.cities_file(), {} if default is None else default)

    def save_cities(self, cities: dict[str, Any], *, atomic: bool = True) -> None:
        saver = write_atomically if atomic else write
        saver(locations.cities_file(), cities)


map_repository = MapRepository()
