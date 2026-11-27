"""Runtime paths for persistent application data."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
# Settings the user may edit (game rules, difficulty bonuses, import options).
CONFIG = ROOT / "config"
STATIC = ROOT / "static"
SAVED_MAPS = DATA / "saved_maps"
GAME_STATE = DATA / "game_state.json"


def configure_paths(
    *,
    data: Path | None = None,
    saved_maps: Path | None = None,
    game_state: Path | None = None,
    config: Path | None = None,
) -> None:
    """Update paths for an isolated runtime or a test fixture."""
    global DATA, SAVED_MAPS, GAME_STATE, CONFIG

    if config is not None:
        CONFIG = Path(config)

    if data is not None:
        DATA = Path(data)
    if saved_maps is not None:
        SAVED_MAPS = Path(saved_maps)
    elif data is not None:
        SAVED_MAPS = DATA / "saved_maps"
    if game_state is not None:
        GAME_STATE = Path(game_state)
    elif data is not None:
        GAME_STATE = DATA / "game_state.json"
