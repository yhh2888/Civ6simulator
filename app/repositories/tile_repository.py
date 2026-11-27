"""Tile collection access through the active map repository."""

from app.repositories.map_repository import map_repository


class TileRepository:
    def get_all(self) -> list[dict]:
        return map_repository.get_map().get("tiles", [])

    def find(self, map_data: dict, x: int, y: int) -> dict | None:
        return next(
            (
                tile
                for tile in map_data.get("tiles", [])
                if tile.get("x") == x and tile.get("y") == y
            ),
            None,
        )


tile_repository = TileRepository()
