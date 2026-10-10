import os
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from bot import (initialize_db, save_snapshot, get_shift_poll_counts,
                 get_shift_participants, process_ended_shift, shift_end_worker)


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "Requires PostgreSQL test database")
class ShiftWorkerTests(unittest.TestCase):
    def setUp(self):
        self.db = initialize_db(os.environ["TEST_DATABASE_URL"])
        self.db.execute("BEGIN")
        self.end = datetime.now(timezone.utc) - timedelta(minutes=1)
        self.start = self.end - timedelta(hours=1)
        self.shift_id = self.db.execute("""INSERT INTO time_slots(channel_id,name,starts_at,ends_at)
            VALUES ('C_WORKER_TEST','test',%s,%s) RETURNING id""",
            (self.start, self.end)).fetchone()["id"]

    def tearDown(self):
        self.db.execute("ROLLBACK")
        self.db.close()

    def test_report_counts_union_and_processed_once(self):
        for uid in ["U1", "U_ABSENT"]:
            self.db.execute("""INSERT INTO shift_hosts(time_slot_id,user_id,display_name,status)
                VALUES (%s,%s,%s,'scheduled')""", (self.shift_id, uid, uid))
        for offset, ids in [(0, ["U1", "U2"]), (5, ["U2", "U3"]), (10, [])]:
            started = self.start + timedelta(minutes=offset)
            save_snapshot(self.db, started, started + timedelta(seconds=1), "C_WORKER_TEST", "R1", ids)
        self.assertEqual(get_shift_participants(self.db, self.shift_id), ["U1", "U2", "U3"])
        self.assertEqual([p["user_count"] for p in get_shift_poll_counts(self.db, self.shift_id)], [2, 2, 0])
        self.assertTrue(process_ended_shift(self.db, "C_WORKER_TEST"))
        row = self.db.execute("SELECT * FROM time_slots WHERE id=%s", (self.shift_id,)).fetchone()
        self.assertIsNotNone(row["processed_at"])
        self.assertEqual(row["report"]["unique_participant_count"], 3)
        self.assertEqual([p["user_count"] for p in row["report"]["polls"]], [2, 2, 0])
        self.assertTrue(row["report"]["huddle_observed"])
        self.assertTrue(row["report"]["host_present"])
        self.assertEqual({h["user_id"]: h["present"] for h in row["report"]["hosts"]},
                         {"U1": True, "U_ABSENT": False})
        self.assertFalse(process_ended_shift(self.db, "C_WORKER_TEST"))

    def test_callback_failure_is_retryable_and_rolls_back(self):
        def fail(db, shift):
            db.execute("UPDATE time_slots SET name='should rollback' WHERE id=%s", (shift["id"],))
            raise RuntimeError("test failure")
        with self.assertLogs("huddles", level="ERROR"):
            self.assertTrue(process_ended_shift(self.db, "C_WORKER_TEST", callback=fail))
        row = self.db.execute("SELECT * FROM time_slots WHERE id=%s", (self.shift_id,)).fetchone()
        self.assertIsNone(row["processed_at"])
        self.assertEqual(row["name"], "test")
        self.assertEqual(row["process_attempts"], 1)
        self.assertIn("test failure", row["last_error"])
        self.assertFalse(process_ended_shift(self.db, "C_WORKER_TEST"))
        self.db.execute("UPDATE time_slots SET next_attempt_at=NULL WHERE id=%s", (self.shift_id,))
        self.assertTrue(process_ended_shift(self.db, "C_WORKER_TEST"))

    def test_boundaries_and_collection_finish_in_next_shift(self):
        # Poll start chooses the shift, even if collection finishes after it ends.
        first = save_snapshot(self.db, self.end - timedelta(seconds=1), self.end + timedelta(seconds=1),
                              "C_WORKER_TEST", "R1", ["U1"])
        second = save_snapshot(self.db, self.end, self.end + timedelta(seconds=1),
                               "C_WORKER_TEST", "R1", ["U2"])
        rows = self.db.execute("SELECT snapshot_id FROM snapshot_slots WHERE time_slot_id=%s",
                               (self.shift_id,)).fetchall()
        self.assertEqual([r["snapshot_id"] for r in rows], [first])
        self.assertNotEqual(first, second)

    def test_grace_period_and_channel_filter(self):
        self.assertFalse(process_ended_shift(self.db, "C_OTHER"))
        self.assertFalse(process_ended_shift(self.db, "C_WORKER_TEST", grace_seconds=3600))

    def test_empty_shift_reports_no_samples_not_zero_samples(self):
        self.assertTrue(process_ended_shift(self.db, "C_WORKER_TEST"))
        report = self.db.execute("SELECT report FROM time_slots WHERE id=%s", (self.shift_id,)).fetchone()["report"]
        self.assertEqual(report["polls"], [])
        self.assertEqual(report["participant_ids"], [])
        self.assertFalse(report["huddle_observed"])
        self.assertFalse(report["host_present"])

    def test_huddle_observed_but_host_absent(self):
        self.db.execute("""INSERT INTO shift_hosts(time_slot_id,user_id,display_name,status)
            VALUES (%s,'U_HOST','Host','scheduled')""", (self.shift_id,))
        save_snapshot(self.db, self.start, self.start, "C_WORKER_TEST", "R1", ["U_GUEST"])
        process_ended_shift(self.db, "C_WORKER_TEST")
        report = self.db.execute("SELECT report FROM time_slots WHERE id=%s", (self.shift_id,)).fetchone()["report"]
        self.assertTrue(report["huddle_observed"])
        self.assertFalse(report["host_present"])


if __name__ == "__main__":
    unittest.main()
