"""Scenario storage: independent copies of the game state for experiments.

Each scenario is one JSON file in ``app/data/scenarios/<id>.json``::

    {
      "format": "civ6-sim-scenario", "format_version": 1,
      "id", "name", "description", "created_at", "updated_at", "source",
      "base":  {"map", "cities", "game"},   # state at creation (for reset)
      "state": {"map", "cities", "game"},   # current scenario state
      "history": [ ...turn records... ]
    }

Scenarios never share objects with the active game or with each other: every
read returns a fresh copy and every write replaces the whole file atomically.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.exceptions import ApplicationError
from app.repositories import locations
from app.services.sim_context import copy_bundle

_LOCK = threading.RLock()
_ID = re.compile(r"^scn-[0-9a-f]{10}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path(scenario_id: str) -> Path:
    if not _ID.match(scenario_id or ""):
        raise ApplicationError(404, "Scenario not found.")
    return locations.scenarios_dir() / f"{scenario_id}.json"


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def load(scenario_id: str) -> dict:
    path = _path(scenario_id)
    with _LOCK:
        if not path.exists():
            raise ApplicationError(404, "Scenario not found.")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ApplicationError(422, "Scenario file is unreadable.") from error


def save(scenario: dict) -> dict:
    scenario["updated_at"] = _now()
    with _LOCK:
        _write(_path(scenario["id"]), scenario)
    return scenario


def summary(scenario: dict) -> dict:
    state = scenario.get("state") or {}
    cities = state.get("cities") or {}
    return {
        "id": scenario["id"],
        "name": scenario.get("name"),
        "description": scenario.get("description", ""),
        "created_at": scenario.get("created_at"),
        "updated_at": scenario.get("updated_at"),
        "source": scenario.get("source"),
        "turn": (state.get("game") or {}).get("turn"),
        "base_turn": ((scenario.get("base") or {}).get("game") or {}).get("turn"),
        "turns_simulated": len(scenario.get("history") or []),
        "cities": [
            {"id": cid, "name": c.get("name"), "population": c.get("population"),
             "strategy": c.get("strategy"), "citizen_mode": c.get("citizen_mode")}
            for cid, c in sorted(cities.items())
        ],
    }


def list_all() -> list[dict]:
    directory = locations.scenarios_dir()
    if not directory.exists():
        return []
    items = []
    for path in sorted(directory.glob("scn-*.json")):
        try:
            items.append(summary(json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, json.JSONDecodeError, KeyError):
            continue
    items.sort(key=lambda item: (item.get("created_at") or "", item["id"]))
    return items


def create(name: str, bundle: dict, *, source: str, description: str = "") -> dict:
    name = (name or "").strip()
    if not name or len(name) > 80:
        raise ApplicationError(422, "시나리오 이름은 1~80자여야 합니다.")
    scenario_id = "scn-" + uuid4().hex[:10]
    scenario = {
        "format": "civ6-sim-scenario",
        "format_version": 1,
        "id": scenario_id,
        "name": name,
        "description": description,
        "created_at": _now(),
        "updated_at": _now(),
        "source": source,
        "base": copy_bundle(bundle),
        "state": copy_bundle(bundle),
        "history": [],
    }
    return save(scenario)


def rename(scenario_id: str, name: str, description: str | None = None) -> dict:
    scenario = load(scenario_id)
    name = (name or "").strip()
    if not name or len(name) > 80:
        raise ApplicationError(422, "시나리오 이름은 1~80자여야 합니다.")
    scenario["name"] = name
    if description is not None:
        scenario["description"] = description
    return summary(save(scenario))


def delete(scenario_id: str) -> dict:
    path = _path(scenario_id)
    with _LOCK:
        if not path.exists():
            raise ApplicationError(404, "Scenario not found.")
        path.unlink()
    return {"deleted": scenario_id}


def reset(scenario_id: str) -> dict:
    scenario = load(scenario_id)
    scenario["state"] = copy_bundle(scenario["base"])
    scenario["history"] = []
    return summary(save(scenario))


def state_of(scenario_id: str) -> dict:
    return copy_bundle(load(scenario_id)["state"])


def store_state(scenario_id: str, bundle: dict, history: list[dict] | None = None) -> dict:
    scenario = load(scenario_id)
    scenario["state"] = copy_bundle(bundle)
    if history:
        scenario["history"] = (scenario.get("history") or []) + history
    return save(scenario)


def history_of(scenario_id: str) -> list[dict]:
    return list(load(scenario_id).get("history") or [])


def stats() -> dict[str, Any]:
    return {"count": len(list_all()), "directory": str(locations.scenarios_dir())}
