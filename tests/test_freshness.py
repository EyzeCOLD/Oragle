"""Freshness check unit tests. Network is faked by patching carddb.bulk_entry."""

import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mtgjudge import carddb


def fake_entry(updated_at):
    return {
        "type": carddb.BULK_TYPE,
        "updated_at": updated_at,
        "jsonl_download_uri": "https://example.com/oracle-cards.jsonl.gz",
        "compressed_size": 123,
    }


class FreshnessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_file = Path(self.tmp.name) / "cards.db"
        self.addCleanup(self.tmp.cleanup)
        conn = carddb.connect(self.db_file)
        self.addCleanup(conn.close)

    def _set_meta(self, key, value):
        conn = carddb.connect(self.db_file)
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))
        conn.commit()
        conn.close()

    def test_fresh_no_notice(self):
        now = datetime.now(timezone.utc)
        self._set_meta("bulk_updated_at", "2026-11-18T22:01:55.902+00:00")
        self._set_meta("bulk_downloaded_at", now.isoformat())
        self._set_meta("bulk_check_at", now.isoformat())
        entry = fake_entry("2026-11-18T22:01:55.902+00:00")
        with unittest.mock.patch.object(carddb, "bulk_entry", return_value=entry):
            notice = carddb.freshness_notice(force_check=True, db_file=self.db_file)
        self.assertIsNone(notice)

    def test_stale_gives_notice(self):
        self._set_meta("bulk_updated_at", "2026-10-01T00:00:00+00:00")
        self._set_meta("bulk_downloaded_at", "2026-10-01T00:00:00+00:00")
        self._set_meta("bulk_check_at", datetime.now(timezone.utc).isoformat())
        entry = fake_entry("2026-11-18T22:01:55.902+00:00")
        with unittest.mock.patch.object(carddb, "bulk_entry", return_value=entry):
            notice = carddb.freshness_notice(force_check=True, db_file=self.db_file)
        self.assertIn("stale", notice)
        self.assertIn("refresh cards", notice)

    def test_check_throttled_within_interval(self):
        self._set_meta("bulk_updated_at", "2026-10-01T00:00:00+00:00")
        self._set_meta("bulk_check_at", datetime.now(timezone.utc).isoformat())
        called = []
        with unittest.mock.patch.object(
            carddb, "bulk_entry", side_effect=lambda: called.append(1) or fake_entry("x")
        ):
            notice = carddb.freshness_notice(db_file=self.db_file)  # no force
        self.assertIsNone(notice)
        self.assertEqual(called, [])  # throttled: not due yet

    def test_check_due_after_interval(self):
        old = datetime.now(timezone.utc) - timedelta(hours=carddb.CHECK_INTERVAL_HOURS + 1)
        self._set_meta("bulk_updated_at", "2026-10-01T00:00:00+00:00")
        self._set_meta("bulk_check_at", old.isoformat())
        entry = fake_entry("2026-10-01T00:00:00+00:00")  # upstream same as local
        with unittest.mock.patch.object(carddb, "bulk_entry", return_value=entry):
            notice = carddb.freshness_notice(db_file=self.db_file)
        self.assertIsNone(notice)  # updated_at matches -> no notice, but check ran

    def test_network_failure_is_silent(self):
        old = datetime.now(timezone.utc) - timedelta(hours=carddb.CHECK_INTERVAL_HOURS + 1)
        self._set_meta("bulk_updated_at", "2026-10-01T00:00:00+00:00")
        self._set_meta("bulk_check_at", old.isoformat())
        from mtgjudge.net import NetError

        with unittest.mock.patch.object(
            carddb, "bulk_entry", side_effect=NetError("down")
        ):
            notice = carddb.freshness_notice(db_file=self.db_file)
        self.assertIsNone(notice)

    def test_offline_skips_check(self):
        self._set_meta("bulk_updated_at", "2026-10-01T00:00:00+00:00")
        called = []
        with unittest.mock.patch.object(carddb, "bulk_entry", side_effect=lambda: called.append(1)):
            notice = carddb.freshness_notice(offline=True, db_file=self.db_file)
        self.assertIsNone(notice)
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
