"""Most tests need the game data built from a local Civilization VI install
(app/data/catalog.json, rules_db.json). The data is not part of the repository;
build it with the first-run screen or:

    python tools/build_catalog.py "<Civ VI>/Base/Assets/Gameplay/Data"
    python tools/build_rules_db.py "<Civ VI>/Base/Assets/Gameplay/Data"
"""

from pathlib import Path

import pytest

DATA = Path(__file__).resolve().parents[1] / "app" / "data"
NEEDS_NO_DATA = {"test_ruleset_builder.py", "test_game_data_setup.py"}


def pytest_collection_modifyitems(config, items):
    if (DATA / "rules_db.json").exists() and (DATA / "catalog.json").exists():
        return
    skip = pytest.mark.skip(reason="game data not built (app/data/rules_db.json, catalog.json) — see tests/conftest.py")
    for item in items:
        if Path(str(item.fspath)).name not in NEEDS_NO_DATA:
            item.add_marker(skip)
