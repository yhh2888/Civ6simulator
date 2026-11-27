"""Difficulty (handicap) per civilization.

The game gives AI players their handicap bonuses through ordinary modifiers
attached to every major leader (TraitModifiers of TRAIT_LEADER_MAJOR_CIV):
HIGH_DIFFICULTY_*_SCALING with OwnerRequirementSetId PLAYER_IS_HIGH_DIFFICULTY_AI
(not human, handicap ≥ Prince) and LOW_DIFFICULTY_* for humans below Prince.
Their Amount uses ModifierArguments.Type = LinearScaleFromDefaultHandicap:

    amount = Value + Extra × (handicap index − base index)

with base = SecondExtra when given, otherwise Warlord. Checked against the
in-game handicap table: science/culture/faith 8·(n−2) = 8 … 40 % and
production/gold 20·(n−2) = 20 … 100 % for Prince … Deity, human XP
−15·(n−3) = 45/30/15 % for Settler/Chieftain/Warlord.

Settings live in ``game["difficulties"]``:
    {"default": "DIFFICULTY_EMPEROR",
     "players": {"0": {"difficulty": "DIFFICULTY_EMPEROR", "human": true}, ...}}
keyed by the save's player slot. The civilization being simulated takes its
entry (map save_import.imported_player); a game not made from a save uses
"self".

Bonus values can be overridden per modifier and level in
app/config/difficulty.json (``bonus_overrides``), edited from the dialog's
bonus table; an override replaces the data value wherever it is applied.
"""

from __future__ import annotations

from typing import Any

from app.core.exceptions import ApplicationError
from app.repositories.rules_db_repository import RulesDB

SCALE_TYPE = "LinearScaleFromDefaultHandicap"
SCALE_BASE = "DIFFICULTY_WARLORD"
FALLBACK_LEVELS = ["DIFFICULTY_SETTLER", "DIFFICULTY_CHIEFTAIN", "DIFFICULTY_WARLORD", "DIFFICULTY_PRINCE",
                   "DIFFICULTY_KING", "DIFFICULTY_EMPEROR", "DIFFICULTY_IMMORTAL", "DIFFICULTY_DEITY"]
LEVEL_KO = {"DIFFICULTY_SETTLER": "개척자", "DIFFICULTY_CHIEFTAIN": "족장", "DIFFICULTY_WARLORD": "대장군",
            "DIFFICULTY_PRINCE": "왕자", "DIFFICULTY_KING": "왕", "DIFFICULTY_EMPEROR": "황제",
            "DIFFICULTY_IMMORTAL": "불멸자", "DIFFICULTY_DEITY": "신"}
DEFAULT_LEVEL = "DIFFICULTY_PRINCE"
SELF_KEY = "self"

EFFECT_LABELS = {
    "MODIFIER_PLAYER_CITIES_ADJUST_CITY_YIELD_MODIFIER": "도시 산출 %",
    "MODIFIER_PLAYER_UNITS_ADJUST_COMBAT_DIFFICULTY": "전투력",
    "MODIFIER_PLAYER_UNITS_ADJUST_UNIT_EXPERIENCE_MODIFIER": "경험치 %",
    "MODIFIER_PLAYER_GRANT_RANDOM_TECHNOLOGY_BOOST_ON_NEW_ERA": "새 시대마다 무료 유레카",
    "MODIFIER_PLAYER_GRANT_RANDOM_CIVIC_BOOST_ON_NEW_ERA": "새 시대마다 무료 영감",
    "MODIFIER_PLAYER_LOYALTY_ADJUST_MARTIAL_LAW_MODIFIER": "계엄령(충성도)",
    "MODIFIER_PLAYER_ADJUST_GOLD_DISPERSAL": "야만인 전초기지 파괴 보상 금 증감",
}
YIELD_KO = {"YIELD_SCIENCE": "과학", "YIELD_CULTURE": "문화", "YIELD_FAITH": "신앙", "YIELD_PRODUCTION": "생산력",
            "YIELD_GOLD": "금", "YIELD_FOOD": "식량"}


def levels(db: RulesDB) -> list[str]:
    rows = [r.get("DifficultyType") for r in db.rows("Difficulties") if r.get("DifficultyType")]
    return rows or list(FALLBACK_LEVELS)


def level_index(db: RulesDB, difficulty: str | None) -> int | None:
    order = levels(db)
    return order.index(difficulty) if difficulty in order else None


CONFIG_NAME = "difficulty.json"


def overrides() -> dict[str, dict[str, float]]:
    from app.services import app_config_service as cfg

    raw = cfg.get(CONFIG_NAME, "bonus_overrides") or {}
    result: dict[str, dict[str, float]] = {}
    if isinstance(raw, dict):
        for mid, levels_ in raw.items():
            if isinstance(levels_, dict):
                values = {str(k): v for k, v in ((k, cfg.finite(v)) for k, v in levels_.items()) if v is not None}
                if values:
                    result[str(mid)] = values
    return result


def ai_extra_percent(player) -> dict[str, float]:
    """Extra city % for AI players from app/config/difficulty.json
    (``ai_extra_percent``, from ``ai_extra_min_level`` up). Spectator saves at
    Emperor show +6 % science, production and gold on top of the data's
    handicap bonus (culture and faith match the data)."""
    from app.services import app_config_service as cfg

    if player is None or getattr(player, "is_human", True):
        return {}
    data = cfg.load(CONFIG_NAME)
    order = FALLBACK_LEVELS
    minimum = data.get("ai_extra_min_level") or "DIFFICULTY_PRINCE"
    level = getattr(player, "handicap", None)
    if level not in order or (minimum in order and order.index(level) < order.index(minimum)):
        return {}
    raw = data.get("ai_extra_percent") or {}
    return {k: v for k, v in ((k, cfg.finite(v)) for k, v in raw.items()) if v is not None} if isinstance(raw, dict) else {}


def default_level(db: RulesDB | None = None) -> str:
    from app.services import app_config_service as cfg

    level = cfg.get(CONFIG_NAME, "default_level")
    order = levels(db) if db is not None else FALLBACK_LEVELS
    return level if level in order else DEFAULT_LEVEL


def data_amount(db: RulesDB, row: dict, difficulty: str | None) -> float | None:
    """Amount of a LinearScaleFromDefaultHandicap argument at a handicap, from the game data."""
    index = level_index(db, difficulty)
    base = level_index(db, row.get("SecondExtra") or SCALE_BASE)
    if index is None or base is None:
        return None
    try:
        value = float(row.get("Value") or 0)
        extra = float(row.get("Extra") or 0)
    except (TypeError, ValueError):
        return None
    return value + extra * (index - base)


def scaled_amount(db: RulesDB, row: dict, difficulty: str | None) -> float | None:
    """The data amount, unless app/config/difficulty.json overrides it."""
    override = overrides().get(str(row.get("ModifierId") or ""), {}).get(str(difficulty))
    return override if override is not None else data_amount(db, row, difficulty)


def settings(game: dict) -> dict:
    raw = game.get("difficulties") if isinstance(game.get("difficulties"), dict) else {}
    default = raw.get("default") if isinstance(raw.get("default"), str) else default_level()
    players = {str(k): v for k, v in (raw.get("players") or {}).items() if isinstance(v, dict)}
    return {"default": default, "players": players}


def player_key(map_data: dict | None) -> str:
    meta = (map_data or {}).get("save_import") or {}
    player = meta.get("imported_player")
    return str(player) if isinstance(player, int) else SELF_KEY


def for_player(game: dict, map_data: dict | None) -> tuple[str, bool]:
    """(handicap, is_human) of the civilization being simulated."""
    current = settings(game)
    entry = current["players"].get(player_key(map_data)) or {}
    difficulty = entry.get("difficulty") if isinstance(entry.get("difficulty"), str) else current["default"]
    human = entry.get("human")
    return difficulty, (True if human is None else bool(human))


def _scaling_rows(db: RulesDB) -> list[dict]:
    """Modifiers whose Amount scales with the handicap, with who they target."""
    args = db.group("ModifierArguments", "ModifierId")
    req_sets = db.group("RequirementSetRequirements", "RequirementSetId")
    reqs = db.index("Requirements", "RequirementId")
    req_args = db.group("RequirementArguments", "RequirementId")
    result = []
    for modifier in db.rows("Modifiers"):
        mid = modifier.get("ModifierId")
        rows = args.get(mid, [])
        amount = next((r for r in rows if r.get("Name") == "Amount" and r.get("Type") == SCALE_TYPE), None)
        if not amount:
            continue
        set_id = modifier.get("OwnerRequirementSetId") or modifier.get("SubjectRequirementSetId")
        who, gate, gate_inverse = "모두", None, False
        for link in req_sets.get(set_id, []):
            req = reqs.get(link.get("RequirementId")) or {}
            if req.get("RequirementType") == "REQUIREMENT_PLAYER_IS_HUMAN":
                who = "AI" if str(req.get("Inverse")).lower() == "true" else "플레이어"
            elif req.get("RequirementType") == "REQUIREMENT_PLAYER_HANDICAP_AT_OR_ABOVE":
                gate = next((a.get("Value") for a in req_args.get(link.get("RequirementId"), []) if a.get("Name") == "Handicap"), None)
                gate_inverse = str(req.get("Inverse")).lower() == "true"
        yield_type = next((r.get("Value") for r in rows if r.get("Name") == "YieldType"), None)
        label = EFFECT_LABELS.get(modifier.get("ModifierType"), modifier.get("ModifierType"))
        if yield_type:
            label = f"{YIELD_KO.get(yield_type, yield_type)} {label}"
        result.append({"modifier": mid, "who": who, "label": label, "yield": yield_type,
                       "gate": gate, "gate_inverse": gate_inverse, "amount_row": amount})
    return result


def bonus_table(db: RulesDB) -> list[dict]:
    """Per-level amounts, as the game would apply them (0 where the gate fails)."""
    order = levels(db)
    table = []
    custom = overrides()
    for row in _scaling_rows(db):
        values, data_values = [], []
        for level in order:
            gate_ok = True
            if row["gate"]:
                at_or_above = order.index(level) >= (level_index(db, row["gate"]) or 0)
                gate_ok = not at_or_above if row["gate_inverse"] else at_or_above
            amount = data_amount(db, row["amount_row"], level) if gate_ok else 0.0
            data_values.append(round(amount or 0.0, 4))
            override = custom.get(row["modifier"], {}).get(level)
            values.append(round(override, 4) if override is not None and gate_ok else data_values[-1])
        table.append({"modifier": row["modifier"], "who": row["who"], "label": row["label"],
                      "yield": row["yield"], "values": values, "data_values": data_values,
                      "applies_to_yields": bool(row["yield"])})
    table.sort(key=lambda r: (r["who"] != "AI", not r["applies_to_yields"], r["label"]))
    return table


def overview(db: RulesDB, game: dict, map_data: dict | None) -> dict:
    current = settings(game)
    meta = (map_data or {}).get("save_import") or {}
    players = []
    configured = set(current["players"])
    for key, info in sorted((meta.get("players") or {}).items(), key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 999):
        if not isinstance(info, dict) or info.get("minor") or info.get("civilization") in ("CIVILIZATION_FREE_CITIES", "CIVILIZATION_BARBARIAN"):
            continue
        if configured and str(key) not in configured:
            continue
        entry = current["players"].get(str(key)) or {}
        players.append({"key": str(key), "label": info.get("label") or info.get("civilization") or key,
                        "civilization": info.get("civilization"), "leader": info.get("leader"),
                        "cities": info.get("cities", 0),
                        "difficulty": entry.get("difficulty") or current["default"],
                        "human": entry.get("human") is True})
    if not players:
        entry = current["players"].get(SELF_KEY) or {}
        players.append({"key": SELF_KEY, "label": "이 문명", "cities": None,
                        "difficulty": entry.get("difficulty") or current["default"],
                        "human": entry.get("human", True) is not False})
    order = levels(db)
    return {"levels": [{"id": level, "label": LEVEL_KO.get(level, level)} for level in order],
            "default": current["default"], "current": player_key(map_data), "players": players,
            "table": bonus_table(db), "overrides": overrides(), "config_file": "app/config/difficulty.json",
            "ai_extra_percent": __import__("app.services.app_config_service", fromlist=["get"]).get(CONFIG_NAME, "ai_extra_percent") or {}}


def save_overrides(db: RulesDB, payload: Any) -> dict[str, dict[str, float]]:
    """Validate {modifier: {level: value}} and write it to app/config/difficulty.json.
    Values equal to the game data are dropped, so the file keeps only real changes."""
    from app.services import app_config_service as cfg

    if not isinstance(payload, dict):
        raise ApplicationError(422, "bonus_overrides는 {모디파이어: {난이도: 값}} 형식입니다.")
    order = levels(db)
    rows = {r["modifier"]: r for r in _scaling_rows(db)}
    result: dict[str, dict[str, float]] = {}
    for mid, per_level in payload.items():
        if mid not in rows or not isinstance(per_level, dict):
            raise ApplicationError(422, f"난이도 보너스가 아닌 모디파이어 {mid}")
        for level, value in per_level.items():
            number = cfg.finite(value)
            if level not in order or number is None or abs(number) > 10000:
                raise ApplicationError(422, f"{mid} {level} 값이 잘못되었습니다.")
            if abs(number - (data_amount(db, rows[mid]["amount_row"], level) or 0.0)) > 1e-9:
                result.setdefault(mid, {})[level] = number
    current = cfg.load(CONFIG_NAME)
    current["bonus_overrides"] = result
    cfg.save(CONFIG_NAME, current)
    return result


def clean(db: RulesDB, payload: Any, previous: dict | None = None) -> dict:
    order = levels(db)
    if not isinstance(payload, dict):
        raise ApplicationError(422, "difficulties는 {\"players\": {...}} 형식입니다.")
    base = settings({"difficulties": previous or {}})
    default = payload.get("default", base["default"])
    if default not in order:
        raise ApplicationError(422, f"알 수 없는 난이도 {default}")
    players = dict(base["players"])
    raw_players = payload.get("players") or {}
    if not isinstance(raw_players, dict) or len(raw_players) > 64:
        raise ApplicationError(422, "players는 {플레이어: {difficulty, human}} 형식입니다 (최대 64).")
    for key, entry in raw_players.items():
        if not isinstance(entry, dict):
            raise ApplicationError(422, f"플레이어 {key} 설정 형식 오류")
        difficulty = entry.get("difficulty", default)
        if difficulty not in order:
            raise ApplicationError(422, f"알 수 없는 난이도 {difficulty}")
        players[str(key)[:12]] = {"difficulty": difficulty, "human": entry.get("human") is True}
    return {"default": default, "players": players}
