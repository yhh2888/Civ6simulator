"""Build app/data/rules_db.json from the Civilization VI XML in Data.zip.

The existing catalog.json extractor only read XML *attributes*. Many Civ VI
tables (Modifiers, ModifierArguments, RequirementSets, GameSpeeds, ...) store
values as child elements, so those rows were saved as empty objects and the
whole modifier system was lost. GlobalParameters uses <Replace> rows and was
skipped entirely.

This tool re-reads the XML directly:

* attribute rows and child-element rows are both supported;
* <Row>, <Replace> and <InsertOrIgnore> are treated as inserts;
* tables with the same name in different files are merged, keeping the
  source file in ``_file`` so provenance is never lost;
* the result is compared with catalog.json and an audit report is written.

Usage (from the project root)::

    python tools/build_rules_db.py path/to/Data.zip            # or an extracted Data folder
    python tools/build_rules_db.py path/to/Data.zip --out app/data

The game data belongs to its rights holders; the generated JSON is a local
derived index for this simulator.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROW_TAGS = {"Row", "Replace", "InsertOrIgnore"}
# ID-column aliases seen in the XML (Projects.xml spells ModifierID).
COLUMN_ALIASES = {"ModifierID": "ModifierId"}


def _iter_xml_sources(source: Path):
    if source.is_file() and source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as archive:
            for name in sorted(archive.namelist()):
                if name.lower().endswith(".xml") and "/schema/" not in name.lower():
                    yield Path(name).stem, archive.read(name)
        return
    for path in sorted(source.rglob("*.xml")):
        if "schema" in {part.lower() for part in path.parts}:
            continue
        yield path.stem, path.read_bytes()


def parse_rows(source: Path) -> tuple[dict, dict]:
    tables: dict[str, list[dict]] = defaultdict(list)
    per_file: dict[str, dict[str, int]] = defaultdict(dict)
    for file_stem, payload in _iter_xml_sources(source):
        root = ET.parse(io.BytesIO(payload)).getroot()
        for table in root:
            if not isinstance(table.tag, str):
                continue
            count = 0
            for row in table:
                if not isinstance(row.tag, str) or row.tag not in ROW_TAGS:
                    continue
                record = {COLUMN_ALIASES.get(k, k): v for k, v in row.attrib.items()}
                for child in row:
                    if isinstance(child.tag, str):
                        record[COLUMN_ALIASES.get(child.tag, child.tag)] = (
                            child.text or ""
                        ).strip()
                record["_file"] = file_stem
                tables[table.tag].append(record)
                count += 1
            if count:
                per_file[file_stem][table.tag] = (
                    per_file[file_stem].get(table.tag, 0) + count
                )
    return dict(tables), dict(per_file)


def global_parameters(tables: dict) -> dict:
    result = {}
    for row in tables.get("GlobalParameters", []):
        name = row.get("Name")
        if name:
            result[name] = row.get("Value")
    return result


def audit_against_catalog(per_file: dict, tables: dict, catalog_path: Path) -> dict:
    report = {
        "catalog_path": str(catalog_path),
        "catalog_found": catalog_path.exists(),
        "missing_tables": [],
        "empty_row_tables": [],
        "row_count_mismatches": [],
        "value_mismatches": [],
    }
    if not catalog_path.exists():
        return report
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    raw = catalog.get("raw_tables", {})
    for file_stem, counts in sorted(per_file.items()):
        for table, xml_count in sorted(counts.items()):
            key = f"{file_stem}::{table}"
            rows = raw.get(key)
            if rows is None:
                report["missing_tables"].append({"table": key, "xml_rows": xml_count})
                continue
            non_empty = [row for row in rows if row]
            if rows and not non_empty:
                report["empty_row_tables"].append(
                    {"table": key, "xml_rows": xml_count, "catalog_rows": len(rows)}
                )
                continue
            if len(rows) != xml_count:
                report["row_count_mismatches"].append(
                    {"table": key, "xml_rows": xml_count, "catalog_rows": len(rows)}
                )
    # Spot-check value equality for yield tables that the legacy engine uses.
    for key, table, columns in (
        ("Terrains::Terrain_YieldChanges", "Terrain_YieldChanges", ("TerrainType", "YieldType", "YieldChange")),
        ("Features::Feature_YieldChanges", "Feature_YieldChanges", ("FeatureType", "YieldType", "YieldChange")),
        ("Resources::Resource_YieldChanges", "Resource_YieldChanges", ("ResourceType", "YieldType", "YieldChange")),
        ("Improvements::Improvement_YieldChanges", "Improvement_YieldChanges", ("ImprovementType", "YieldType", "YieldChange")),
        ("Buildings::Building_YieldChanges", "Building_YieldChanges", ("BuildingType", "YieldType", "YieldChange")),
    ):
        file_stem = key.split("::")[0]
        xml_rows = {
            tuple(row.get(c) for c in columns)
            for row in tables.get(table, [])
            if row.get("_file") == file_stem
        }
        cat_rows = {
            tuple(row.get(c) for c in columns) for row in raw.get(key, []) if row
        }
        if xml_rows != cat_rows:
            report["value_mismatches"].append(
                {
                    "table": key,
                    "only_in_xml": sorted(map(list, xml_rows - cat_rows))[:20],
                    "only_in_catalog": sorted(map(list, cat_rows - xml_rows))[:20],
                }
            )
    report["summary"] = {
        "missing_tables": len(report["missing_tables"]),
        "empty_row_tables": len(report["empty_row_tables"]),
        "row_count_mismatches": len(report["row_count_mismatches"]),
        "value_mismatches": len(report["value_mismatches"]),
    }
    return report


def build(source: Path, out_dir: Path) -> dict:
    tables, per_file = parse_rows(source)
    digest = hashlib.sha256()
    for name in sorted(tables):
        digest.update(name.encode())
        digest.update(str(len(tables[name])).encode())
    db = {
        "format": "civ6-rules-db",
        "format_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": source.name,
        "fingerprint": digest.hexdigest()[:16],
        "stats": {
            "tables": len(tables),
            "rows": sum(len(rows) for rows in tables.values()),
            "files": len(per_file),
        },
        "global_parameters": global_parameters(tables),
        "tables": tables,
        "per_file_counts": per_file,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "rules_db.json").write_text(
        json.dumps(db, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    audit = audit_against_catalog(per_file, tables, out_dir / "catalog.json")
    audit["generated_at"] = db["generated_at"]
    audit["rules_db_stats"] = db["stats"]
    (out_dir / "data_audit_report.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"db": db["stats"], "audit": audit.get("summary", {})}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path, help="Data.zip or extracted Data folder")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "app" / "data",
    )
    args = parser.parse_args(argv)
    if not args.source.exists():
        print(f"Source not found: {args.source}", file=sys.stderr)
        return 2
    result = build(args.source, args.out)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
