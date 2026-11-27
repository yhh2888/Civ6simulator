"""Access to extracted catalog and gameplay-rule JSON data."""

from typing import Any

from app.repositories import locations
from app.repositories.json_repository import read, write_atomically


class GameDataRepository:
    def catalog(self) -> dict[str, Any]:
        return read(locations.catalog_file(), {})

    def rules(self) -> dict[str, Any]:
        return read(locations.rules_file(), {})

    def game_state(self, default: dict[str, Any]) -> dict[str, Any]:
        return read(locations.game_state_file(), default)

    def save_game_state(self, state: dict[str, Any]) -> None:
        write_atomically(locations.game_state_file(), state)


game_data_repository = GameDataRepository()
