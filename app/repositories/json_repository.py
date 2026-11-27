"""JSON file persistence primitives."""

import json
import os
import tempfile
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

_ACTIVE_DOCUMENTS: dict[Path, Any] = {}
_ACTIVE_MAP_TILE_INDEXES: dict[
    Path, tuple[int, frozenset[tuple[int, int]]]
] = {}
_ACTIVE_DOCUMENTS_LOCK = threading.RLock()


def _is_active_document(path: Path) -> bool:
    from app.repositories import locations

    return path in {
        locations.map_file(),
        locations.cities_file(),
        locations.game_state_file(),
    }


def invalidate_active_document_cache(path: Path) -> None:
    with _ACTIVE_DOCUMENTS_LOCK:
        _ACTIVE_DOCUMENTS.pop(path, None)
        _ACTIVE_MAP_TILE_INDEXES.pop(path, None)


def active_map_has_tile(x: int, y: int) -> bool:
    from app.repositories import locations

    path = locations.map_file()
    with _ACTIVE_DOCUMENTS_LOCK:
        if path not in _ACTIVE_DOCUMENTS:
            _ACTIVE_DOCUMENTS[path] = (
                json.loads(path.read_text(encoding="utf-8"))
                if path.exists()
                else {"width": 0, "height": 0, "count": 0, "tiles": []}
            )
        map_data = _ACTIVE_DOCUMENTS[path]
        cached_index = _ACTIVE_MAP_TILE_INDEXES.get(path)
        if cached_index is None or cached_index[0] != id(map_data):
            tile_coordinates = frozenset(
                (tile["x"], tile["y"])
                for tile in map_data.get("tiles", [])
                if isinstance(tile, dict) and "x" in tile and "y" in tile
            )
            cached_index = (id(map_data), tile_coordinates)
            _ACTIVE_MAP_TILE_INDEXES[path] = cached_index
        return (x, y) in cached_index[1]


def read(path: Path, default: Any) -> Any:
    if _is_active_document(path):
        with _ACTIVE_DOCUMENTS_LOCK:
            if path not in _ACTIVE_DOCUMENTS:
                _ACTIVE_DOCUMENTS[path] = (
                    json.loads(path.read_text(encoding="utf-8"))
                    if path.exists()
                    else deepcopy(default)
                )
            return deepcopy(_ACTIVE_DOCUMENTS[path])
    return (
        json.loads(path.read_text(encoding="utf-8"))
        if path.exists()
        else default
    )


def write(path: Path, data: Any) -> None:
    if _is_active_document(path):
        with _ACTIVE_DOCUMENTS_LOCK:
            _ACTIVE_DOCUMENTS[path] = deepcopy(data)
            _ACTIVE_MAP_TILE_INDEXES.pop(path, None)
        return
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def write_atomically(path: Path, data: Any) -> None:
    if _is_active_document(path):
        with _ACTIVE_DOCUMENTS_LOCK:
            _ACTIVE_DOCUMENTS[path] = deepcopy(data)
            _ACTIVE_MAP_TILE_INDEXES.pop(path, None)
        return
    _write_atomically_to_disk(path, data)


def _write_atomically_to_disk(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
        ) as temp_file:
            temp_path = Path(temp_file.name)
            json.dump(data, temp_file, ensure_ascii=False, indent=2)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def _write_json_transaction_to_disk(updates: dict[Path, Any]) -> None:
    staged: dict[Path, Path] = {}
    originals = {
        path: path.read_bytes() if path.exists() else None for path in updates
    }
    try:
        for path, data in updates.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                json.dump(data, temp_file, ensure_ascii=False, indent=2)
                staged[path] = Path(temp_file.name)
        for path, temp_path in staged.items():
            os.replace(temp_path, path)
    except Exception:
        for path, original in originals.items():
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(original)
        raise
    finally:
        for temp_path in staged.values():
            temp_path.unlink(missing_ok=True)


def write_json_transaction(
    updates: dict[Path, Any], *, persist: bool = False
) -> None:
    active_updates = {
        path: data for path, data in updates.items() if _is_active_document(path)
    }
    disk_updates = {
        path: data for path, data in updates.items() if path not in active_updates
    }
    if persist:
        _write_json_transaction_to_disk(updates)
        with _ACTIVE_DOCUMENTS_LOCK:
            _ACTIVE_DOCUMENTS.update(
                {path: deepcopy(data) for path, data in active_updates.items()}
            )
            for path in active_updates:
                _ACTIVE_MAP_TILE_INDEXES.pop(path, None)
        return
    if disk_updates:
        _write_json_transaction_to_disk(disk_updates)
    with _ACTIVE_DOCUMENTS_LOCK:
        _ACTIVE_DOCUMENTS.update(
            {path: deepcopy(data) for path, data in active_updates.items()}
        )
        for path in active_updates:
            _ACTIVE_MAP_TILE_INDEXES.pop(path, None)
