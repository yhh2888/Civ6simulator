"""Build a rules database for a Civ VI *ruleset* (base + expansions + DLC).

``tools/build_rules_db.py`` reads the base-game XML only. Expansions and DLC
change the base data with ``<Update>``, ``<Delete>`` (with SQL cascades) and
``<Replace>`` and add whole tables through their own schema SQL, so their
effect can only be reproduced the way the game does it: load everything into
a real SQLite database. This tool does that:

1. Create the base schema (``Schema/01_GameplaySchema.sql``) and triggers
   (``Make_Hash`` = inverted CRC-32, as stored in saves).
2. Load the base XML (Data folder or Data.zip), alphabetical.
3. Read every ``.modinfo`` in the DLC folder, evaluate each in-game
   ``UpdateDatabase`` action's criteria for the chosen ruleset, sort the
   actions by ``LoadOrder`` (expansions first, then DLC in folder order) and
   apply their files by ``Priority``: ``.sql`` is executed, ``.xml`` rows are
   applied as Row/Replace/InsertOrIgnore/Update/Delete.
4. Export every table as JSON rows of strings (same shape as rules_db.json,
   booleans as "true"/"false", ``_file`` = file that last wrote the row).

Inserts and updates run with foreign keys off (file order does not matter,
like the game); deletes run with foreign keys on, so ``ON DELETE CASCADE``
removes dependent rows the same way the game does. Dangling references left
at the end are counted in ``stats.dangling_references``.

Usage (from the project root)::

    python tools/build_ruleset_db.py --base path/to/Data.zip --dlc path/to/DLC \\
        --ruleset RULESET_EXPANSION_2 --out app/data/rules_db_xp2.json

Criteria that cannot be decided offline (game modes, map scripts) count as
false unless passed with ``--mode GAMEMODE_X``. Scenario DLC never loads
because its criteria require a scenario ruleset.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sqlite3
import sys
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.civ6save.hashes import type_hash  # noqa: E402

GAME_CORE = {"RULESET_STANDARD": "Base", "RULESET_EXPANSION_1": "Expansion1", "RULESET_EXPANSION_2": "Expansion2"}
PLAYER_DOMAIN = {"RULESET_STANDARD": "StandardPlayers", "RULESET_EXPANSION_1": "Expansion1_Players",
                 "RULESET_EXPANSION_2": "Expansion2_Players"}
EXPANSION_FOLDERS = ("Expansion1", "Expansion2")
COLUMN_ALIASES = {"ModifierID": "ModifierId"}


# --------------------------------------------------------------- base files
class Source:
    """Files of the base Data folder, from a folder or a Data.zip."""

    def __init__(self, path: Path):
        self.path = path
        self.archive = zipfile.ZipFile(path) if path.is_file() else None

    def names(self) -> list[str]:
        if self.archive:
            return sorted(n for n in self.archive.namelist() if not n.endswith("/"))
        return sorted(str(p.relative_to(self.path)).replace("\\", "/") for p in self.path.rglob("*") if p.is_file())

    def read(self, name: str) -> bytes:
        if self.archive:
            return self.archive.read(name)
        return (self.path / name).read_bytes()


# ----------------------------------------------------------------- database
@dataclass
class Loader:
    db: sqlite3.Connection
    columns: dict[str, dict[str, str]] = field(default_factory=dict)  # table → lower col → (Name)
    types: dict[str, dict[str, str]] = field(default_factory=dict)    # table → Name → declared type
    stats: Counter = field(default_factory=Counter)
    problems: list[str] = field(default_factory=list)
    file_counts: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))

    def refresh_schema(self) -> None:
        self.columns.clear()
        self.types.clear()
        tables = [r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            info = self.db.execute(f'PRAGMA table_info("{table}")').fetchall()
            names = [row[1] for row in info]
            if "_file" not in names:
                self.db.execute(f'ALTER TABLE "{table}" ADD COLUMN "_file" TEXT')
            self.columns[table.lower()] = {row[1].lower(): row[1] for row in info}
            self.types[table.lower()] = {row[1]: (row[2] or "").upper() for row in info}
        self.table_names = {t.lower(): t for t in tables}

    def run_sql(self, text: str, label: str) -> None:
        self.db.commit()
        self.db.executescript(text.lstrip("﻿"))
        self.refresh_schema()
        self.stats["sql_files"] += 1
        self.file_counts[label]["sql"] += 1

    # values -------------------------------------------------------------
    def _value(self, table: str, column: str, raw: str | None):
        if raw is None:
            return None
        declared = self.types[table.lower()].get(column, "")
        text = raw.strip()
        if declared == "BOOLEAN":
            lowered = text.lower()
            if lowered in ("true", "1"):
                return 1
            if lowered in ("false", "0"):
                return 0
        return text

    def _record(self, table: str, element: ET.Element) -> dict[str, str]:
        record = {}
        for key, value in element.attrib.items():
            record[key] = value
        for child in element:
            if isinstance(child.tag, str):
                record[child.tag] = child.text or ""
        known = self.columns[table.lower()]
        out = {}
        for key, value in record.items():
            column = known.get(key.lower())
            if column is None:
                self.stats["unknown_columns"] += 1
                if len(self.problems) < 200:
                    self.problems.append(f"{table}: 알 수 없는 열 {key}")
                continue
            out[column] = self._value(table, column, value)
        return out

    # operations -------------------------------------------------------------
    def apply_xml(self, payload: bytes, label: str) -> None:
        try:
            root = ET.parse(io.BytesIO(payload)).getroot()
        except ET.ParseError as error:
            self.problems.append(f"{label}: XML 파싱 실패 {error}")
            return
        for table_el in root:
            if not isinstance(table_el.tag, str):
                continue
            table = self.table_names.get(table_el.tag.lower())
            if table is None:
                self.stats["unknown_tables"] += 1
                if len(self.problems) < 200:
                    self.problems.append(f"{label}: 스키마에 없는 표 {table_el.tag}")
                continue
            for op in table_el:
                if not isinstance(op.tag, str):
                    continue
                self._apply(table, op, label)

    def _fk(self, on: bool) -> None:
        self.db.commit()
        self.db.execute(f"PRAGMA foreign_keys = {'ON' if on else 'OFF'}")

    def _apply(self, table: str, op: ET.Element, label: str) -> None:
        tag = op.tag
        if tag in ("Row", "Replace", "InsertOrIgnore"):
            record = self._record(table, op)
            if not record:
                return
            record["_file"] = label
            verb = {"Row": "INSERT", "Replace": "INSERT OR REPLACE", "InsertOrIgnore": "INSERT OR IGNORE"}[tag]
            cols = ", ".join(f'"{c}"' for c in record)
            marks = ", ".join("?" for _ in record)
            try:
                self.db.execute(f'{verb} INTO "{table}" ({cols}) VALUES ({marks})', list(record.values()))
                self.stats[f"op_{tag}"] += 1
                self.file_counts[label][tag] += 1
            except sqlite3.IntegrityError as error:
                self.stats["insert_conflicts"] += 1
                if len(self.problems) < 200:
                    self.problems.append(f"{label}: {table} 삽입 실패 ({error})")
        elif tag == "Update":
            where = next((c for c in op if c.tag == "Where"), None)
            setter = next((c for c in op if c.tag == "Set"), None)
            if setter is None:
                return
            values = self._record(table, setter)
            values["_file"] = label
            conditions = self._record(table, where) if where is not None else {}
            sql = f'UPDATE "{table}" SET ' + ", ".join(f'"{c}" = ?' for c in values)
            params = list(values.values())
            if conditions:
                sql += " WHERE " + " AND ".join(f'"{c}" = ?' for c in conditions)
                params += list(conditions.values())
            # Foreign keys stay off for updates: a value may point at a row a
            # later file adds (the game checks references after loading).
            try:
                count = self.db.execute(sql, params).rowcount
                self.stats["op_Update"] += 1
                self.stats["rows_updated"] += max(0, count)
                self.file_counts[label]["Update"] += 1
            except sqlite3.IntegrityError as error:
                self.stats["update_conflicts"] += 1
                self.problems.append(f"{label}: {table} 변경 실패 ({error})")
        elif tag == "Delete":
            conditions = self._record(table, op)
            sql = f'DELETE FROM "{table}"'
            if conditions:
                sql += " WHERE " + " AND ".join(f'"{c}" = ?' for c in conditions)
            self._fk(True)
            count = self.db.execute(sql, list(conditions.values())).rowcount
            self._fk(False)
            self.stats["op_Delete"] += 1
            self.stats["rows_deleted"] += max(0, count)
            self.file_counts[label]["Delete"] += 1

    # export -------------------------------------------------------------------
    def export(self) -> dict[str, list[dict]]:
        self.db.commit()
        tables = {}
        for lower, table in sorted(self.table_names.items()):
            if table.startswith("sqlite_"):
                continue
            cursor = self.db.execute(f'SELECT * FROM "{table}"')
            names = [d[0] for d in cursor.description]
            declared = self.types[lower]
            rows = []
            for values in cursor:
                row = {}
                for name, value in zip(names, values):
                    if value is None:
                        continue
                    key = COLUMN_ALIASES.get(name, name)
                    if declared.get(name) == "BOOLEAN" and value in (0, 1):
                        row[key] = "true" if value else "false"
                    elif isinstance(value, float) and value.is_integer() and declared.get(name) != "REAL":
                        row[key] = str(int(value))
                    else:
                        row[key] = str(value)
                rows.append(row)
            if rows:
                tables[table] = rows
        return tables


# ------------------------------------------------------------------ modinfo
@dataclass
class Action:
    mod: str
    mod_id: str
    action_id: str
    load_order: int
    files: list[tuple[int, int, Path]]
    folder: Path
    mod_rank: int


def _criteria_ok(criteria: ET.Element, ruleset: str, mods: set[str], modes: set[str], notes: list[str]) -> bool:
    results = []
    for test in criteria:
        if not isinstance(test.tag, str):
            continue
        text = (test.text or "").strip()
        if test.tag == "RuleSetInUse":
            results.append(ruleset in {v.strip() for v in text.split(",")})
        elif test.tag == "GameCoreInUse":
            results.append(GAME_CORE.get(ruleset) in {v.strip() for v in text.split(",")})
        elif test.tag == "LeaderPlayable":
            domain = PLAYER_DOMAIN.get(ruleset, "")
            results.append(any(entry.strip().split("::")[0].split(":")[-1] == domain for entry in text.split(",")))
        elif test.tag == "ModInUse":
            results.append(text.upper() in mods)
        elif test.tag == "ConfigurationValueMatches":
            config = (test.findtext("ConfigurationId") or "").strip()
            value = (test.findtext("Value") or "").strip()
            results.append(config in modes and value in ("1", "true", "True"))
        else:
            notes.append(f"알 수 없는 조건 {test.tag} → 거짓")
            results.append(False)
    if not results:
        return True
    return any(results) if criteria.get("any") == "1" else all(results)


def collect_actions(dlc: Path, ruleset: str, modes: set[str], notes: list[str]) -> list[Action]:
    modinfos = sorted(dlc.rglob("*.modinfo"))
    # Expansions first, then the rest in folder order.
    modinfos.sort(key=lambda p: (0 if p.parent.name in EXPANSION_FOLDERS else 1,
                                 EXPANSION_FOLDERS.index(p.parent.name) if p.parent.name in EXPANSION_FOLDERS else 0,
                                 str(p).lower()))
    parsed = []
    for rank, path in enumerate(modinfos):
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError as error:
            notes.append(f"{path.name}: 파싱 실패 {error}")
            continue
        parsed.append((rank, path, root))
    mods = {(root.get("id") or "").upper() for _, _, root in parsed}
    actions: list[Action] = []
    for rank, path, root in parsed:
        criteria = {c.get("id"): c for c in root.iter("Criteria")}
        in_game = root.find("InGameActions")
        if in_game is None:
            continue
        for action in in_game:
            if action.tag != "UpdateDatabase":
                continue
            cid = action.get("criteria")
            if cid:
                crit = criteria.get(cid)
                if crit is None or not _criteria_ok(crit, ruleset, mods, modes, notes):
                    continue
            load_order = 0
            props = action.find("Properties")
            if props is not None and props.findtext("LoadOrder"):
                load_order = int(props.findtext("LoadOrder"))
            files = []
            for index, file_el in enumerate(action.findall("File")):
                priority = int(file_el.get("Priority") or 0)
                files.append((priority, index, path.parent / (file_el.text or "").strip().replace("\\", "/")))
            files.sort(key=lambda f: (-f[0], f[1]))
            actions.append(Action(path.parent.name, (root.get("id") or "").upper(), action.get("id") or "",
                                  load_order, files, path.parent, rank))
    actions.sort(key=lambda a: (a.load_order, a.mod_rank))
    return actions


# --------------------------------------------------------------------- main
def build(base: Path, dlc: Path, ruleset: str, out: Path, modes: set[str]) -> dict:
    db = sqlite3.connect(":memory:")
    db.create_function("Make_Hash", 1, lambda text: type_hash(text) if text else None)
    loader = Loader(db)
    source = Source(base)
    names = source.names()
    schema = [n for n in names if n.lower().endswith("schema/01_gameplayschema.sql")]
    triggers = [n for n in names if n.lower().endswith("schema/02_addtriggers.sql")]
    if not schema:
        raise SystemExit("기본 스키마(Schema/01_GameplaySchema.sql)를 찾지 못했습니다.")
    loader.db.executescript(source.read(schema[0]).decode("utf-8-sig"))
    if triggers:
        loader.db.executescript(source.read(triggers[0]).decode("utf-8-sig"))
    loader.refresh_schema()
    loader._fk(False)
    base_files = [n for n in names if n.lower().endswith(".xml") and "/schema/" not in n.lower()
                  and not n.lower().startswith("schema/")]
    for name in base_files:
        loader.apply_xml(source.read(name), Path(name).stem)
    notes: list[str] = []
    applied = []
    for action in collect_actions(dlc, ruleset, modes, notes):
        for _priority, _index, path in action.files:
            if not path.exists():
                notes.append(f"{action.mod}/{action.action_id}: 파일 없음 {path.name}")
                continue
            label = path.stem
            if path.suffix.lower() == ".sql":
                loader.run_sql(path.read_text(encoding="utf-8-sig"), label)
            elif path.suffix.lower() == ".xml":
                loader.apply_xml(path.read_bytes(), label)
            applied.append(f"{action.mod}/{action.action_id}/{path.name}")
    loader.db.commit()
    dangling = loader.db.execute("PRAGMA foreign_key_check").fetchall()
    loader.stats["dangling_references"] = len(dangling)
    for (table, parent), count in Counter((row[0], row[2]) for row in dangling).most_common():
        notes.append(f"참조 대상 없음: {table} → {parent} {count}행")
    tables = loader.export()
    gp = {row["Name"]: row.get("Value") for row in tables.get("GlobalParameters", []) if "Name" in row}
    digest = hashlib.sha256()
    for name in sorted(tables):
        digest.update(name.encode())
        digest.update(str(len(tables[name])).encode())
    per_file = defaultdict(dict)
    for table, rows in tables.items():
        for row in rows:
            file_stem = row.get("_file")
            if file_stem:
                per_file[file_stem][table] = per_file[file_stem].get(table, 0) + 1
    payload = {
        "format": "civ6-rules-db",
        "format_version": 2,
        "ruleset": ruleset,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": f"{base.name} + {dlc.name} ({ruleset})",
        "fingerprint": digest.hexdigest()[:16],
        "stats": {"tables": len(tables), "rows": sum(len(r) for r in tables.values()),
                  "files": len(per_file), **dict(loader.stats)},
        "global_parameters": gp,
        "tables": tables,
        "per_file_counts": dict(per_file),
        "applied_files": applied,
        "build_notes": sorted(set(notes)) + loader.problems[:200],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", type=Path, required=True, help="base game Data folder or Data.zip (with Schema/)")
    parser.add_argument("--dlc", type=Path, required=True, help="the game's DLC folder (XML/SQL/modinfo)")
    parser.add_argument("--ruleset", default="RULESET_EXPANSION_2", choices=sorted(GAME_CORE))
    parser.add_argument("--mode", action="append", default=[], help="active game mode, e.g. GAMEMODE_BARBARIAN_CLANS")
    parser.add_argument("--out", type=Path, default=ROOT / "app" / "data" / "rules_db_xp2.json")
    args = parser.parse_args(argv)
    payload = build(args.base, args.dlc, args.ruleset, args.out, set(args.mode))
    stats = payload["stats"]
    print(f"{args.out}: 표 {stats['tables']} · 행 {stats['rows']} · 적용 파일 {len(payload['applied_files'])}")
    print({k: v for k, v in stats.items() if k not in ("tables", "rows", "files")})
    for note in payload["build_notes"][:25]:
        print("  -", note)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
