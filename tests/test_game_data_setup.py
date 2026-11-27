"""First-run setup: locating the game data and building the catalog."""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

from app.core.exceptions import ApplicationError
from app.services import game_data_setup_service as setup

ROOT = Path(__file__).resolve().parents[1]

TERRAINS = """<?xml version="1.0" encoding="utf-8"?>
<GameInfo>
  <Terrains><Row TerrainType="TERRAIN_GRASS" Name="LOC_TERRAIN_GRASS_NAME" MovementCost="1"/></Terrains>
  <Terrain_YieldChanges><Row TerrainType="TERRAIN_GRASS" YieldType="YIELD_FOOD" YieldChange="2"/></Terrain_YieldChanges>
</GameInfo>"""


def _load_tool(name):
    spec = importlib.util.spec_from_file_location(f"test_{name}", ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ResolveTests(unittest.TestCase):
    def test_install_layout_and_collected_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp)
            data = game / "Base" / "Assets" / "Gameplay" / "Data"
            data.mkdir(parents=True)
            (data / "Terrains.xml").write_text(TERRAINS, encoding="utf-8")
            resolved = setup.resolve(game)
            self.assertEqual(resolved["base"], data)
            self.assertIsNone(resolved["dlc"])  # no DLC folder: base rules only
            (game / "DLC" / "Expansion2").mkdir(parents=True)
            (game / "DLC" / "Expansion2" / "Expansion2.modinfo").write_text("<Mod/>", encoding="utf-8")
            self.assertEqual(setup.resolve(game)["dlc"], game / "DLC")
            self.assertEqual(setup.resolve(data)["base"], data)  # the Data folder itself

    def test_bad_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ApplicationError):
                setup.resolve(Path(tmp) / "missing")
            with self.assertRaises(ApplicationError):
                setup.resolve(tmp)  # exists but has no game data


class CatalogTests(unittest.TestCase):
    def test_catalog_from_data_folder(self):
        catalog_tool = _load_tool("build_catalog")
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "Terrains.xml").write_text(TERRAINS, encoding="utf-8")
            catalog = catalog_tool.build_catalog(Path(tmp))
        self.assertEqual(catalog["terrains"][0]["label"], "Grass")
        self.assertEqual(catalog["terrain_yields"], {"TERRAIN_GRASS": {"Food": 2.0}})
        self.assertIn("Terrains::Terrains", catalog["raw_tables"])


if __name__ == "__main__":
    unittest.main()
