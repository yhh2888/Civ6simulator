"""Saved map snapshot file access."""

import json
from pathlib import Path
from typing import Any

from app.repositories import locations


class SnapshotRepository:
    def path(self, name: str) -> Path:
        return locations.saved_maps_dir() / name

    def ensure_directory(self) -> None:
        locations.saved_maps_dir().mkdir(parents=True, exist_ok=True)

    def list_names(self) -> list[str]:
        if not locations.saved_maps_dir().exists():
            return []
        return sorted(
            path.stem
            for path in locations.saved_maps_dir().glob("*.json")
            if path.is_file()
        )

    def exists(self, path: Path) -> bool:
        return path.is_file()

    def load(self, path: Path) -> Any:
        return json.loads(path.read_text(encoding="utf-8"))

    def save_new(self, path: Path, snapshot: dict[str, Any]) -> None:
        with path.open("x", encoding="utf-8") as saved_file:
            json.dump(snapshot, saved_file, ensure_ascii=False, indent=2)


snapshot_repository = SnapshotRepository()
