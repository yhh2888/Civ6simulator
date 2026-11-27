"""Build app/data/catalog.json from the base game's Gameplay Data folder.

The catalog is the simulator's quick index of terrains, features,
improvements, resources, buildings, districts, policies, leaders, civics and
technologies, plus every XML table as attribute rows (``raw_tables``, keyed
``File::Table``). Only XML attributes are read, as the original extractor did;
the full rules (child-element rows, expansions) live in rules_db*.json.

Usage (from the project root)::

    python tools/build_catalog.py "<Civ VI>/Base/Assets/Gameplay/Data"   # or Data.zip
    python tools/build_catalog.py game_data/Base/Data --out app/data/catalog.json

The game data belongs to its rights holders. The generated file is a local
index built from your own installation and is not distributed with this project.
"""

from __future__ import annotations

import argparse
import io
import json
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROW_TAGS = {"Row"}  # the original extractor read <Row> only
YIELD_NAMES = {"YIELD_FOOD": "Food", "YIELD_PRODUCTION": "Production", "YIELD_GOLD": "Gold",
               "YIELD_SCIENCE": "Science", "YIELD_CULTURE": "Culture", "YIELD_FAITH": "Faith"}

# category → (File::Table, id column, prefix removed from the label)
CATEGORIES = {
    "terrains": ("Terrains::Terrains", "TerrainType", "TERRAIN_"),
    "features": ("Features::Features", "FeatureType", "FEATURE_"),
    "improvements": ("Improvements::Improvements", "ImprovementType", "IMPROVEMENT_"),
    "resources": ("Resources::Resources", "ResourceType", "RESOURCE_"),
    "buildings": ("Buildings::Buildings", "BuildingType", "BUILDING_"),
    "districts": ("Districts::Districts", "DistrictType", "DISTRICT_"),
    "policies": ("Policies::Policies", "PolicyType", "POLICY_"),
    "leaders": ("Leaders::Leaders", "LeaderType", ""),
    "civics": ("Civics::Civics", "CivicType", ""),
    "technologies": ("Technologies::Technologies", "TechnologyType", ""),
}
YIELD_TABLES = {  # catalog key → (File::Table, id column, value column)
    "building_yields": ("Buildings::Building_YieldChanges", "BuildingType", "YieldChange"),
    "terrain_yields": ("Terrains::Terrain_YieldChanges", "TerrainType", "YieldChange"),
    "feature_yields": ("Features::Feature_YieldChanges", "FeatureType", "YieldChange"),
    "improvement_yields": ("Improvements::Improvement_YieldChanges", "ImprovementType", "YieldChange"),
}
ITEM_YIELDS = {"terrains": "terrain_yields", "features": "feature_yields",
               "improvements": "improvement_yields", "buildings": "building_yields"}


def _xml_files(source: Path) -> list[tuple[str, bytes]]:
    """(file stem, bytes) of the top-level XML files in a Data folder or Data.zip."""
    files = []
    if source.is_file() and zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            for name in archive.namelist():
                parts = name.replace("\\", "/").split("/")
                if name.lower().endswith(".xml") and len(parts) == 2:
                    files.append((Path(parts[1]).stem, archive.read(name)))
    elif source.is_dir():
        for path in source.glob("*.xml"):
            files.append((path.stem, path.read_bytes()))
    else:
        raise SystemExit(f"Data folder or Data.zip not found: {source}")
    if not files:
        raise SystemExit(f"No XML files in {source} (expected Base/Assets/Gameplay/Data)")
    return sorted(files)


STRIPPED = ("TERRAIN_", "FEATURE_", "IMPROVEMENT_", "RESOURCE_", "BUILDING_", "DISTRICT_", "POLICY_")


def _label(item_id: str, prefix: str) -> str:
    text = item_id
    if prefix:  # the original labels drop every leading category prefix
        while any(text.startswith(p) for p in STRIPPED):
            text = text[next(len(p) for p in STRIPPED if text.startswith(p)):]
    return " ".join(word.capitalize() for word in text.split("_"))


def build_catalog(source: Path) -> dict:
    raw_tables: dict[str, list[dict]] = {}
    for stem, data in _xml_files(source):
        try:
            root = ET.parse(io.BytesIO(data)).getroot()
        except ET.ParseError:
            continue
        for table in root:
            rows = [dict(row.attrib) for row in table if row.tag in ROW_TAGS]
            if rows:
                # A table repeated inside one file keeps its last block, as before.
                raw_tables[f"{stem}::{table.tag}"] = rows

    yield_maps: dict[str, dict] = {}
    for key, (table, id_column, value_column) in YIELD_TABLES.items():
        values: dict[str, dict] = {}
        for row in raw_tables.get(table, []):
            name = YIELD_NAMES.get(row.get("YieldType", ""))
            if name and row.get(id_column):
                bucket = values.setdefault(row[id_column], {})
                bucket[name] = bucket.get(name, 0.0) + float(row.get(value_column) or 0)
        yield_maps[key] = values

    catalog: dict = {
        "source": source.name,
        "stats": {"xml_files": len(_xml_files(source)), "tables": len(raw_tables),
                  "rows": sum(len(r) for r in raw_tables.values()),
                  "table_row_counts": {k: len(v) for k, v in raw_tables.items()}},
        "yields": [{"id": k, "name": v} for k, v in YIELD_NAMES.items()],
    }
    for category, (table, id_column, prefix) in CATEGORIES.items():
        items = []
        for row in raw_tables.get(table, []):
            item_id = row.get(id_column)
            if not item_id:
                continue
            item = {"id": item_id, "name_key": row.get("Name"), "label": _label(item_id, prefix), "raw": row}
            if category in ITEM_YIELDS:
                item["yields"] = yield_maps[ITEM_YIELDS[category]].get(item_id, {})
            if category == "buildings":
                item["is_wonder"] = row.get("IsWonder") == "true"
            items.append(item)
        catalog[category] = items
    catalog.update(yield_maps)
    catalog["raw_tables"] = raw_tables
    return catalog


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", type=Path, help="Base/Assets/Gameplay/Data folder or Data.zip")
    parser.add_argument("--out", type=Path, default=ROOT / "app" / "data" / "catalog.json")
    args = parser.parse_args()
    catalog = build_catalog(args.source)
    args.out.write_text(json.dumps(catalog, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"wrote {args.out} ({catalog['stats']['tables']} tables, {catalog['stats']['rows']} rows)")


if __name__ == "__main__":
    main()
