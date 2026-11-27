"""tools/build_ruleset_db.py: XML operations and modinfo criteria."""

import importlib.util
import sqlite3
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("build_ruleset_db", Path(__file__).resolve().parents[1] / "tools" / "build_ruleset_db.py")
builder = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = builder  # dataclasses look the module up by name
SPEC.loader.exec_module(builder)

SCHEMA = """
CREATE TABLE Types (Type TEXT PRIMARY KEY, Kind TEXT);
CREATE TABLE Buildings (BuildingType TEXT PRIMARY KEY, Cost INTEGER, IsWonder BOOLEAN DEFAULT 0,
  FOREIGN KEY (BuildingType) REFERENCES Types(Type) ON DELETE CASCADE);
"""


class LoaderTests(unittest.TestCase):
    def setUp(self):
        self.loader = builder.Loader(sqlite3.connect(":memory:"))
        self.loader.run_sql(SCHEMA, "schema")

    def rows(self, table):
        return {name: table_rows for name, table_rows in self.loader.export().items()}[table]

    def test_row_update_replace_delete(self):
        self.loader.apply_xml(b"""<GameInfo>
          <Types><Row Type="BUILDING_A" Kind="KIND_BUILDING"/><Row Type="BUILDING_B" Kind="KIND_BUILDING"/></Types>
          <Buildings><Row BuildingType="BUILDING_A" Cost="60" IsWonder="false"/>
                     <Row><BuildingType>BUILDING_B</BuildingType><Cost>80</Cost><IsWonder>true</IsWonder></Row></Buildings>
        </GameInfo>""", "Base")
        self.loader.apply_xml(b"""<GameInfo>
          <Buildings><Update><Where BuildingType="BUILDING_A"/><Set Cost="90"/></Update></Buildings>
          <Types><Delete Type="BUILDING_B"/></Types>
        </GameInfo>""", "Expansion2_Buildings")
        buildings = {r["BuildingType"]: r for r in self.rows("Buildings")}
        self.assertEqual(set(buildings), {"BUILDING_A"})  # B removed by the cascading delete
        self.assertEqual(buildings["BUILDING_A"]["Cost"], "90")
        self.assertEqual(buildings["BUILDING_A"]["IsWonder"], "false")
        self.assertEqual(buildings["BUILDING_A"]["_file"], "Expansion2_Buildings")

    def test_unknown_table_is_reported(self):
        self.loader.apply_xml(b"<GameInfo><Nope><Row A='1'/></Nope></GameInfo>", "x")
        self.assertTrue(any("Nope" in p for p in self.loader.problems))


class CriteriaTests(unittest.TestCase):
    def check(self, xml, ruleset="RULESET_EXPANSION_2", mods=frozenset(), modes=frozenset()):
        notes = []
        return builder._criteria_ok(ET.fromstring(xml), ruleset, set(mods), set(modes), notes), notes

    def test_ruleset_and_mods(self):
        self.assertTrue(self.check("<Criteria><RuleSetInUse>RULESET_EXPANSION_1,RULESET_EXPANSION_2</RuleSetInUse></Criteria>")[0])
        self.assertFalse(self.check("<Criteria><RuleSetInUse>RULESET_STANDARD</RuleSetInUse></Criteria>")[0])
        self.assertTrue(self.check("<Criteria><ModInUse>abc</ModInUse></Criteria>", mods={"ABC"})[0])
        self.assertTrue(self.check("<Criteria any='1'><RuleSetInUse>RULESET_STANDARD</RuleSetInUse>"
                                   "<GameCoreInUse>Expansion2</GameCoreInUse></Criteria>")[0])

    def test_unknown_condition_is_false_with_note(self):
        ok, notes = self.check("<Criteria><SomethingNew>1</SomethingNew></Criteria>")
        self.assertFalse(ok)
        self.assertTrue(notes)


class RulesetSelectionTests(unittest.TestCase):
    def test_game_ruleset_selects_expansion_db(self):
        from app.repositories.locations import RULESET_FILES
        from app.repositories.rules_db_repository import clear_rules_db_cache, load_rules_db
        from app.services.turn_simulation_service import Statics

        clear_rules_db_cache()
        base = load_rules_db()
        statics = Statics({}, base, {})
        self.assertIs(statics.db_for({}), base)
        self.assertIn("RULESET_EXPANSION_2", RULESET_FILES)
        xp2 = statics.db_for({"ruleset": "RULESET_EXPANSION_2"})
        if not xp2.available:
            self.skipTest("rules_db_xp2.json not built")
        self.assertEqual(xp2.meta.get("ruleset"), "RULESET_EXPANSION_2")
        self.assertIn("BUILDING_BASILIKOI_PAIDES", xp2.index("Buildings", "BuildingType"))
        self.assertNotIn("BUILDING_BASILIKOI_PAIDES", base.index("Buildings", "BuildingType"))


if __name__ == "__main__":
    unittest.main()
