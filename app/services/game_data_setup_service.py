"""First-run game data setup.

The game's rule data is not shipped with this project. On first run the user
points the app at their own Civilization VI installation (or at the folder
that ``collect_game_data.bat`` copied out of it) and the app builds its local
indexes from it:

* app/data/catalog.json        ← tools/build_catalog.py   (Base/Assets/Gameplay/Data)
* app/data/rules_db.json       ← tools/build_rules_db.py  (same folder)
* app/data/rules_db_xp2.json   ← tools/build_ruleset_db.py (base + DLC folder; optional)

The build runs in a background thread; the web page polls ``status()``.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import threading
import time
import traceback
import zipfile
from pathlib import Path
from typing import Any

from app.core import paths
from app.core.exceptions import ApplicationError

PROJECT = Path(__file__).resolve().parents[2]
TOOLS = PROJECT / "tools"
COLLECTED = PROJECT / "game_data"
REQUIRED = ("catalog.json", "rules_db.json")
OPTIONAL = ("rules_db_xp2.json",)
GAME_FOLDER_NAMES = ("Sid Meier's Civilization VI", "SidMeiersCivilizationVI")

_job: dict[str, Any] = {"state": "idle", "step": "", "pct": 0, "message": "", "error": None}
_lock = threading.Lock()


def _candidates() -> list[Path]:
    """Places a Civ VI installation (or the collected copy) usually lives."""
    roots = [COLLECTED]
    drives = [f"{d}:\\" for d in "CDEFG"] if os.name == "nt" else ["/"]
    for drive in drives:
        for sub in ("Program Files (x86)/Steam/steamapps/common", "Program Files/Steam/steamapps/common",
                    "SteamLibrary/steamapps/common", "Steam/steamapps/common", "Program Files/Epic Games"):
            for name in GAME_FOLDER_NAMES:
                roots.append(Path(drive) / sub / name)
    home = Path.home()
    for name in GAME_FOLDER_NAMES:
        roots.append(home / ".steam/steam/steamapps/common" / name)
        roots.append(home / "Library/Application Support/Steam/steamapps/common" / name)
    return roots


def resolve(root: str | Path, extract: bool = True) -> dict[str, Path | None]:
    """Find the base Data folder (or Data.zip) and the DLC folder under ``root``."""
    path = Path(str(root).strip().strip('"')).expanduser()
    if not path.exists():
        raise ApplicationError(422, f"경로를 찾을 수 없습니다 / Path not found: {path}")
    base = None
    if path.is_file() and zipfile.is_zipfile(path):
        base = path
    else:
        for candidate in (path / "Base" / "Assets" / "Gameplay" / "Data", path / "Assets" / "Gameplay" / "Data",
                          path / "Data", path):
            if (candidate / "Terrains.xml").exists():
                base = candidate
                break
    if base is None:
        raise ApplicationError(422, "기본 게임 데이터(Base/Assets/Gameplay/Data)를 찾지 못했습니다. "
                                    "/ Could not find Base/Assets/Gameplay/Data under this path.")
    dlc = None
    folder = path.parent if path.is_file() else path
    candidates = [path / "DLC", folder / "DLC", folder / "civ6_dlc_data"]
    candidates += sorted(p for p in folder.iterdir() if p.is_dir()) if folder.is_dir() else []
    for candidate in candidates:
        if candidate.is_dir() and any(candidate.glob("*/*.modinfo")):
            dlc = candidate
            break
    if dlc is None and folder.is_dir():
        # A zipped DLC folder next to Data.zip (e.g. civ6_dlc_data.zip): unpack it once.
        for archive in sorted(folder.glob("*.zip")):
            if archive == path or not zipfile.is_zipfile(archive):
                continue
            with zipfile.ZipFile(archive) as zf:
                if not any(n.endswith(".modinfo") for n in zf.namelist()):
                    continue
                if not extract:
                    dlc = archive
                    break
                target = paths.DATA / "game_data_dlc"
                zf.extractall(target)
            roots = [p.parent.parent for p in target.glob("**/*.modinfo")]
            if roots:
                dlc = min(roots, key=lambda p: len(p.parts))
                break
    return {"base": base, "dlc": dlc}


def status() -> dict[str, Any]:
    data = paths.DATA
    files = {name: (data / name).exists() for name in REQUIRED + OPTIONAL}
    found = []
    for candidate in _candidates():
        try:
            if candidate.exists():
                resolved = resolve(candidate, extract=False)
                found.append({"path": str(candidate), "dlc": resolved["dlc"] is not None})
        except (ApplicationError, OSError):
            continue
    with _lock:
        job = dict(_job)
    return {"ready": all(files[name] for name in REQUIRED), "files": files, "job": job,
            "suggestions": found[:5], "collected_folder": str(COLLECTED)}


def _tool(name: str):
    spec = importlib.util.spec_from_file_location(f"civ6tool_{name}", TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _update(**values) -> None:
    with _lock:
        _job.update(values)


def _run(base: Path, dlc: Path | None) -> None:
    import json

    started = time.time()
    try:
        data = paths.DATA
        data.mkdir(parents=True, exist_ok=True)
        _update(step="catalog", pct=5, message="카탈로그 만드는 중 / Building catalog")
        catalog = _tool("build_catalog").build_catalog(base)
        (data / "catalog.json").write_text(json.dumps(catalog, ensure_ascii=False, indent=1), encoding="utf-8")

        _update(step="rules", pct=30, message="기본 규칙 DB 만드는 중 / Building base rules")
        _tool("build_rules_db").build(base, data)

        if dlc is not None:
            _update(step="expansions", pct=55,
                    message="확장팩·DLC 규칙 DB 만드는 중 (1분 안팎) / Building expansion rules (about a minute)")
            _tool("build_ruleset_db").build(base, dlc, "RULESET_EXPANSION_2", data / "rules_db_xp2.json", set())
        from app.repositories.rules_db_repository import clear_rules_db_cache
        from app.services import save_import_service

        clear_rules_db_cache()
        save_import_service.clear_name_cache()
        _update(state="done", step="done", pct=100, error=None,
                message=f"완료 ({time.time() - started:.0f}초) / Done" + ("" if dlc else
                        " — DLC 폴더가 없어 확장팩 규칙은 만들지 않았습니다 / no DLC folder: expansion rules skipped"))
    except Exception as error:  # reported to the page
        _update(state="error", error=f"{type(error).__name__}: {error}", message="실패 / Failed",
                trace=traceback.format_exc(limit=3))


def start(root: str) -> dict[str, Any]:
    resolved = resolve(root)
    with _lock:
        if _job.get("state") == "running":
            raise ApplicationError(409, "이미 추출 중입니다 / Already running.")
        _job.clear()
        _job.update(state="running", step="start", pct=1, message="시작 / Starting", error=None,
                    base=str(resolved["base"]), dlc=str(resolved["dlc"]) if resolved["dlc"] else None)
    threading.Thread(target=_run, args=(resolved["base"], resolved["dlc"]), daemon=True).start()
    return status()
