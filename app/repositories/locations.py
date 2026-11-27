"""Canonical file locations for persistent simulator data."""

from pathlib import Path

from app.core import paths


def map_file() -> Path:
    return paths.DATA / "map.json"


def cities_file() -> Path:
    return paths.DATA / "cities.json"


def catalog_file() -> Path:
    return paths.DATA / "catalog.json"


def rules_file() -> Path:
    """app/config/game_rules.json; a copy in the data folder (an isolated
    runtime or an older install) takes precedence."""
    legacy = paths.DATA / "game_rules.json"
    return legacy if legacy.exists() else paths.CONFIG / "game_rules.json"


def config_file(name: str) -> Path:
    return paths.CONFIG / name


def game_state_file() -> Path:
    return paths.GAME_STATE


def saved_maps_dir() -> Path:
    return paths.SAVED_MAPS


def rules_db_file() -> Path:
    return paths.DATA / "rules_db.json"


# Rules databases per Civ VI ruleset. The base file is the default; others are
# built with tools/build_ruleset_db.py from the game's expansion/DLC data.
RULESET_FILES = {
    "RULESET_EXPANSION_2": "rules_db_xp2.json",
}


def rules_db_file_for(ruleset: str | None) -> Path:
    name = RULESET_FILES.get(ruleset or "")
    if name:
        candidate = paths.DATA / name
        if candidate.exists():
            return candidate
    return rules_db_file()


def data_audit_file() -> Path:
    return paths.DATA / "data_audit_report.json"


def scenarios_dir() -> Path:
    return paths.DATA / "scenarios"


def validation_cases_file() -> Path:
    return paths.DATA / "validation_cases.json"
