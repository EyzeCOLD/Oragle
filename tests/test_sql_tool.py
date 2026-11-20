"""query_local_cards end to end: the cards.db schema v2 (colors, padded
text columns, legality columns, schema_version meta) and carddb.run_sql
safety (read-only by construction, single statement, payload budget)."""
import gzip
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mtgjudge import carddb


def _bulk_bytes(cards):
    raw = "\n".join(json.dumps(c) for c in cards).encode("utf-8")
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb") as gz:
        gz.write(raw)
    return out.getvalue()


HELIK = {
    "id": "id-helix", "name": "Lightning Helix", "mana_cost": "{1}{R}{W}",
    "cmc": 2, "colors": ["R", "W"], "color_identity": ["R", "W"],
    "type_line": "Instant", "oracle_text": "deals 3 damage and gain 3 life",
    "keywords": ["Lifelink"], "layout": "normal", "released_at": "2006-02-03",
    "edhrec_rank": 500,
    "legalities": {"pauper": "not_legal", "commander": "legal"},
}
BEAR = {
    "id": "id-bear", "name": "Grizzly Bears", "mana_cost": "{1}{G}",
    "cmc": 2, "colors": ["G"], "color_identity": ["G"],
    "type_line": "Creature — Bear", "oracle_text": "",
    "keywords": [], "layout": "normal", "released_at": "1997-02-01",
    "edhrec_rank": None,
    "legalities": {"pauper": "legal", "commander": "legal"},
}
BRAVEN = {
    # adventure: cards has no top-level type_line/oracle_text, both faces
    "id": "id-braven", "name": "Brazen Borrower", "mana_cost": "{1}{U}",
    "cmc": 3, "colors": [], "color_identity": ["U"],
    "layout": "adventure", "released_at": "2019-10-04", "edhrec_rank": 78,
    "card_faces": [
        {"name": "Brazen Borrower", "colors": ["U"],
         "type_line": "Creature — Faerie Rogue",
         "oracle_text": "Flash. Return target nonland permanent",
         "mana_cost": "{1}{U}"},
        {"name": "Petty Theft", "colors": ["U"],
         "type_line": "Sorcery — Adventure",
         "oracle_text": "Return target nonland permanent an opponent",
         "mana_cost": ""},
    ],
    "legalities": {"commander": "legal"},
}


def build_db(*cards):
    """Build a real tmp cards.db from fake bulk entries (no network)."""
    tmp = tempfile.mkdtemp()
    db_file = Path(tmp) / "cards.db"
    payload = _bulk_bytes(list(cards))
    with mock.patch.object(carddb.net, "_open", return_value=payload):
        count = carddb.build_from_bulk(
            {"updated_at": "2026-11-20", "jsonl_download_uri": "fake"},
            db_file=db_file)
    return db_file, count


class SchemaTest(unittest.TestCase):
    def test_query_columns_imported(self):
        db_file, count = build_db(HELIK, BEAR, BRAVEN)
        self.assertEqual(count, 3)
        conn = carddb.connect(db_file)
        try:
            row = conn.execute(
                "SELECT * FROM cards WHERE name = 'Lightning Helix'").fetchone()
            self.assertEqual(row["colors"], "WR")  # canonical WUBRG order
            self.assertEqual(row["color_identity"], "WR")
            self.assertEqual(row["cmc"], 2.0)
            self.assertEqual(row["keywords"], " Lifelink ")
            self.assertEqual(row["oracle_all"],
                             " deals 3 damage and gain 3 life ")
            self.assertEqual(row["legality_commander"], "legal")
            self.assertEqual(row["legality_pauper"], "not_legal")
            self.assertEqual(row["legality_standard"], "not_legal")
            meta = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            self.assertEqual(meta["value"], carddb.SCHEMA_VERSION)
        finally:
            conn.close()

    def test_faces_merged_and_padded(self):
        db_file, _count = build_db(BRAVEN)
        conn = carddb.connect(db_file)
        try:
            row = conn.execute(
                "SELECT * FROM cards WHERE name = 'Brazen Borrower'"
            ).fetchone()
            # colors unioned across faces, canonical order
            self.assertEqual(row["colors"], "U")
            # type and oracle text merged across faces
            self.assertIn(" // ", row["type_line"])
            self.assertIn("flash", row["oracle_text"].lower())
            self.assertIn(" // ", row["oracle_all"])
            # both faces are findable in the padded lookup column
            for face in ("brazen borrower", "petty theft"):
                self.assertIn(face, row["lookup_all"])
            self.assertTrue(row["oracle_all"].startswith(" "))
        finally:
            conn.close()


class RunSqlTest(unittest.TestCase):
    def setUp(self):
        self.db_file, _ = build_db(HELIK, BEAR, BRAVEN)

    def test_aggregate_fits_entirely(self):
        out = carddb.run_sql(
            "SELECT COUNT(*) AS n FROM cards WHERE type_line LIKE '%Creature%'",
            db_file=self.db_file)
        self.assertIn('"n": 2', out)  # bears + brazen, joins both faces
        self.assertNotIn("truncated", out)

    def test_exact_lookup_idiom(self):
        out = carddb.run_sql(
            "SELECT name, mana_cost, type_line, oracle_text FROM cards "
            "WHERE lookup_all LIKE '%lightning helix %'", db_file=self.db_file)
        self.assertIn('"name": "Lightning Helix"', out)
        self.assertIn('"mana_cost": "{1}{R}{W}"', out)

    def test_property_query(self):
        out = carddb.run_sql(
            "SELECT name FROM cards WHERE colors LIKE '%U%' "
            "AND legality_commander = 'legal' ORDER BY edhrec_rank",
            db_file=self.db_file)
        self.assertIn("Brazen Borrower", out)
        self.assertNotIn("Grizzly Bears", out)

    def test_writes_denied(self):
        for bad in (
            "INSERT INTO cards (id) VALUES ('x')",
            "UPDATE cards SET name = 'x'",
            "DELETE FROM cards",
            "DROP TABLE cards",
            "ATTACH DATABASE ':memory:' AS evil",
        ):
            out = carddb.run_sql(bad, db_file=self.db_file)
            self.assertTrue(out.startswith("error:"), bad)

    def test_one_statement_only(self):
        out = carddb.run_sql("SELECT 1; SELECT 2", db_file=self.db_file)
        self.assertTrue(out.startswith("error:"))

    def test_self_join_runs(self):
        # legal SQL that isn't a flat select must not be broken by the
        # authorizer (CTEs, joins, functions all allowed)
        out = carddb.run_sql(
            "SELECT COUNT(*) AS n FROM cards a JOIN cards b "
            "ON a.id = b.id", db_file=self.db_file)
        self.assertIn('"n": 3', out)

    def test_payload_budget_truncates_wide_results(self):
        out = carddb.run_sql("SELECT * FROM cards "
                             "CROSS JOIN cards c",  # 9 wide rows
                             db_file=self.db_file, budget=300)
        self.assertIn("truncated", out)
        self.assertIn("add a LIMIT", out)

    def test_cell_truncation(self):
        wordy = dict(HELIK, id="id-wordy", name="Wordyfin",
                     oracle_text="words " * 120, edhrec_rank=1)
        db_file, _ = build_db(wordy)
        out = carddb.run_sql("SELECT oracle_all FROM cards", db_file=db_file)
        self.assertIn("...", out)  # the long padded text is cut

    def test_missing_db_instructive(self):
        out = carddb.run_sql("SELECT 1", db_file=Path("/nonexistent/x.db"))
        self.assertIn("no local card database", out)
        self.assertIn("refresh cards", out)

    def test_pre_v2_db_instructive(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "old.db"
            conn = sqlite3.connect(db_file)
            conn.executescript(
                "CREATE TABLE cards (id TEXT, name TEXT); "
                "CREATE TABLE meta (key TEXT, value TEXT); "
                "CREATE TABLE rulings (card_id TEXT);")
            conn.commit()
            conn.close()
            out = carddb.run_sql("SELECT COUNT(*) FROM cards", db_file=db_file)
            self.assertIn("predates the queryable", out)
            self.assertIn("refresh cards", out)

    def test_never_raises(self):
        # whatever the model writes, the tool returns a string
        for bad in ("", "totally not sql", "SELECT nonexistent_col FROM cards"):
            out = carddb.run_sql(bad, db_file=self.db_file)
            self.assertIsInstance(out, str)


if __name__ == "__main__":
    unittest.main()
