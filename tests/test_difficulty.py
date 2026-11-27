"""Difficulty (handicap) bonuses per civilization."""

import unittest

from app.core.exceptions import ApplicationError
from app.services import difficulty_service as ds
from app.services.modifier_engine import run_modifiers
from app.services.yield_breakdown_service import compute_city
from tests.sim_fixtures import make_game
from tests.test_governors_congress import xp2, xp2_context
from tests.test_trade_routes import world


class DifficultyTableTests(unittest.TestCase):
    def test_table_matches_handicap_chart(self):
        rows = {r["modifier"]: r["data_values"] for r in ds.bonus_table(xp2())}
        self.assertEqual(rows["HIGH_DIFFICULTY_SCIENCE_SCALING"], [0, 0, 0, 8, 16, 24, 32, 40])
        self.assertEqual(rows["HIGH_DIFFICULTY_PRODUCTION_SCALING"], [0, 0, 0, 20, 40, 60, 80, 100])
        self.assertEqual(rows["HIGH_DIFFICULTY_UNIT_XP_SCALING"], [0, 0, 0, 10, 20, 30, 40, 50])
        self.assertEqual(rows["LOW_DIFFICULTY_UNIT_XP_SCALING"][:2], [45, 30])

    def test_config_override_replaces_data_value(self):
        import tempfile
        from pathlib import Path
        from app.core import paths
        from app.services import app_config_service as cfg

        old = paths.CONFIG
        with tempfile.TemporaryDirectory() as tmp:
            paths.CONFIG = Path(tmp)
            try:
                db = xp2()
                row = {"ModifierId": "HIGH_DIFFICULTY_SCIENCE_SCALING", "Value": "0", "Extra": "8"}
                # Default config: no overrides, data values.
                self.assertEqual(ds.scaled_amount(db, row, "DIFFICULTY_EMPEROR"), 24)
                self.assertEqual(ds.scaled_amount(db, row, "DIFFICULTY_KING"), 16)
                saved = ds.save_overrides(db, {"HIGH_DIFFICULTY_SCIENCE_SCALING": {"DIFFICULTY_KING": 18, "DIFFICULTY_EMPEROR": 24}})
                self.assertEqual(saved, {"HIGH_DIFFICULTY_SCIENCE_SCALING": {"DIFFICULTY_KING": 18}})
                self.assertTrue((Path(tmp) / "difficulty.json").exists())
                self.assertEqual(ds.scaled_amount(db, row, "DIFFICULTY_KING"), 18)
                self.assertEqual(ds.scaled_amount(db, row, "DIFFICULTY_EMPEROR"), 24)
                with self.assertRaises(ApplicationError):
                    ds.save_overrides(db, {"NOT_A_SCALING": {"DIFFICULTY_KING": 1}})
            finally:
                paths.CONFIG = old
                cfg._cache.clear()

    def test_clean_rejects_unknown_level(self):
        with self.assertRaises(ApplicationError):
            ds.clean(xp2(), {"players": {"0": {"difficulty": "DIFFICULTY_NOPE"}}})


class DifficultyYieldTests(unittest.TestCase):
    def totals(self, difficulty, human):
        game = make_game(empire={"enforce_prerequisites": False, "leader": "LEADER_TRAJAN",
                                 "civilization": "CIVILIZATION_ROME"})
        game["difficulties"] = {"default": difficulty, "players": {"self": {"difficulty": difficulty, "human": human}}}
        ctx = xp2_context(world(game))
        return compute_city(ctx, run_modifiers(ctx), "city-a")

    def test_ai_gets_percent_bonus_human_does_not(self):
        human = self.totals("DIFFICULTY_DEITY", True)
        ai = self.totals("DIFFICULTY_DEITY", False)
        from types import SimpleNamespace

        extra = lambda level: ds.ai_extra_percent(SimpleNamespace(is_human=False, handicap=level))
        deity, prince_extra = extra("DIFFICULTY_DEITY"), extra("DIFFICULTY_PRINCE")
        self.assertEqual(ai["percent"].get("Production"),
                         human["percent"].get("Production", 0.0) + 100.0 + deity.get("Production", 0))
        self.assertEqual(ai["percent"].get("Science"),
                         human["percent"].get("Science", 0.0) + 40.0 + deity.get("Science", 0))
        prince = self.totals("DIFFICULTY_PRINCE", False)
        self.assertEqual(prince["percent"].get("Gold"),
                         human["percent"].get("Gold", 0.0) + 20.0 + prince_extra.get("Gold", 0))
        self.assertEqual(extra("DIFFICULTY_SETTLER"), {})
        settler = self.totals("DIFFICULTY_SETTLER", False)
        self.assertEqual(settler["percent"].get("Gold", 0.0), human["percent"].get("Gold", 0.0))


class FixedPointPercentTests(unittest.TestCase):
    def test_each_percent_rounds_to_256ths(self):
        game = make_game(empire={"enforce_prerequisites": False, "leader": "LEADER_TRAJAN",
                                 "civilization": "CIVILIZATION_ROME"})
        game["difficulties"] = {"default": "DIFFICULTY_EMPEROR",
                                "players": {"self": {"difficulty": "DIFFICULTY_EMPEROR", "human": False}}}
        ctx = xp2_context(world(game))
        ctx.settings["fixed_point_percent"] = True
        result = compute_city(ctx, run_modifiers(ctx), "city-a")
        culture = next(c for c in result["components"] if c["category"] == "percent")
        items = [i for i in culture["details"] if i["yield"] == "Culture"]
        self.assertTrue(items)
        self.assertAlmostEqual(sum(i["effective"] for i in items), 61 / 2.56)  # 24 % → 61/256


class SpectatorSaveTests(unittest.TestCase):
    SAVE = "/home/claude/saves4/k.Civ6Save"

    def test_mbanza_kongo_matches_game(self):
        import os

        if not os.path.exists(self.SAVE):
            self.skipTest("local spectator save not available")
        from app.services.save_import_service import export_city_yields

        rows = {r["city"]: r for r in export_city_yields(open(self.SAVE, "rb").read(), "k")}
        kongo = rows["Mbanza Kongo"]
        # In game: food 31, production 36.5, gold 18.2, science 15.5, culture 6.6, faith 8.6.
        self.assertEqual([kongo[k] for k in ("food", "production", "gold", "science", "culture", "faith")],
                         [31.0, 36.5, 18.2, 15.5, 6.6, 8.6])
        self.assertEqual(rows["Nidaros"]["science"], 7.7)
        self.assertEqual(rows["Naranjo"]["science"], 14.2)
        # Mali city centre desert adjacency, citizen culture 77/256.
        niani = rows["Niani"]
        self.assertEqual([niani[k] for k in ("food", "culture", "faith")], [9.0, 8.6, 1.2])
        # Theodora: holy site +1 faith per adjacent farm, culture = faith adjacency.
        self.assertEqual(rows["Constantinople"]["culture_raw"] > 6.7, True)
        # Lady Six Sky: +10 % all yields within 6 tiles of the capital.
        self.assertEqual(rows["Coba"]["food"], 22.0)


if __name__ == "__main__":
    unittest.main()
