"""User-editable settings in app/config/*.json.

Each file has defaults here; a missing or unreadable file falls back to them
(and is written back on the first save). Files are re-read when they change on
disk, so editing a JSON by hand takes effect on the next request.

Files:
- game_rules.json — gameplay rules (validated by app/config.py)
- difficulty.json — handicap bonus overrides and the default level
- save_import.json — options used when a .Civ6Save is imported
"""

from __future__ import annotations

import json
import math
from copy import deepcopy
from typing import Any

from app.repositories.locations import config_file

DEFAULTS: dict[str, dict] = {
    "difficulty.json": {
        "_설명": "bonus_overrides: {모디파이어 ID: {난이도: 값}}. 게임 데이터 값 대신 이 값을 씁니다. "
                 "난이도 설정 창의 보너스 표에서 고치고 저장하면 여기에 기록됩니다.",
        "_설명_ai_extra": "ai_extra_percent: AI 문명 도시에 더하는 % (게임 데이터에 없는 값). 관전 세이브 31턴 "
                          "황제 AI에서 과학·생산·금 +6%가 맞았습니다. ai_extra_min_level 이상 난이도의 AI에만 적용.",
        "default_level": "DIFFICULTY_PRINCE",
        "bonus_overrides": {},
        "ai_extra_percent": {"Science": 6, "Production": 6, "Gold": 6},
        "ai_extra_min_level": "DIFFICULTY_PRINCE",
    },
    "save_import.json": {
        "_설명": "세이브 가져오기 옵션. apply_amenity_effects: 가져온 도시에 쾌적도 효과(성장·산출 %)를 켤지. "
                 "spectator_all_ai: 관전 세이브면 모든 문명을 AI로 둘지.",
        "apply_amenity_effects": True,
        "spectator_all_ai": True,
    },
}

_cache: dict[str, tuple[float, dict]] = {}


def load(name: str) -> dict:
    path = config_file(name)
    defaults = deepcopy(DEFAULTS.get(name, {}))
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return defaults
    cached = _cache.get(str(path))
    if cached and cached[0] == mtime:
        return deepcopy(cached[1])
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return defaults
    if not isinstance(data, dict):
        return defaults
    merged = {**defaults, **data}
    _cache[str(path)] = (mtime, merged)
    return deepcopy(merged)


def save(name: str, data: dict) -> dict:
    path = config_file(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = {**DEFAULTS.get(name, {}), **data}
    path.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _cache.pop(str(path), None)
    return merged


def get(name: str, key: str, default: Any = None) -> Any:
    return load(name).get(key, default)


def finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)
