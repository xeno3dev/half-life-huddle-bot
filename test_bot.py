import os
import unittest
from unittest.mock import Mock, MagicMock

from bot import Tracker, initialize_db, paginated, participants, user_group_members, save_snapshot, current_huddle_members


class Tests(unittest.TestCase):
    def test_only_current_users_in_target_huddle(self):
        def user(uid, room, state="in_a_huddle", **extra):
            return dict(id=uid, profile=dict(huddle_state=state, huddle_state_call_id=room), **extra)
        users = [user("U1", "R1"), user("U2", "R2"), user("U3", "R1", "default_unset"),
                 user("U4", "R1", deleted=True), user("U1", "R1")]
        self.assertEqual(participants(users, "R1"), ["U1"])

    def test_pagination(self):
        method = Mock(side_effect=[{"members": [1], "response_metadata": {"next_cursor": "next"}},
                                   {"members": [2]}])
        self.assertEqual(list(paginated(method, "members")), [1, 2])
        self.assertEqual(method.call_args.kwargs["cursor"], "next")

    def test_insert_uses_parameters_and_native_array(self):
        db = MagicMock()
        save_snapshot(db, "start", "end", "C1", "R1", ["U1", "U2"])
        self.assertEqual(db.execute.call_args_list[0].args[1],
                         ("start", "end", "C1", "R1", 2, ["U1", "U2"]))

    def test_parent_shift_schedule(self):
        from datetime import date, timedelta
        from seed_shifts import shifts
        rows = list(shifts(date(2026, 10, 12)))
        self.assertEqual(len(rows), 29)
        self.assertEqual(rows[0][1].utcoffset(), timedelta(hours=-4))
        self.assertTrue(all(len(r[3]) == 1 for r in rows))
        self.assertTrue(all(h[2] == "scheduled" and h[3] is None for r in rows for h in r[3]))
        self.assertTrue(all(start < end for _, start, end, _ in rows))
        self.assertEqual(rows[-1][2].isoformat(), "2026-10-18T23:00:00-04:00")

    @unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "Needs a dedicated PostgreSQL test database")
    def test_postgres_roundtrip(self):
        from datetime import datetime, timezone
        db = initialize_db(os.environ["TEST_DATABASE_URL"])
        try:
            with db.transaction():
                save_snapshot(db, datetime.now(timezone.utc), datetime.now(timezone.utc),
                              "C_TEST", "R_TEST", ["U1", "U2"])
                row = db.execute("SELECT * FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
                self.assertEqual(row["user_ids"], ["U1", "U2"])
                rows = db.execute("SELECT user_id FROM snapshot_users WHERE snapshot_id = %s",
                                  (row["id"],)).fetchall()
                self.assertEqual([r["user_id"] for r in rows], ["U1", "U2"])
                db.execute("DELETE FROM snapshots WHERE id = %s", (row["id"],))
        finally:
            db.close()

    def test_inactive_helper_returns_empty(self):
        client = Mock()
        client.conversations_history.return_value = {"messages": []}
        self.assertEqual(current_huddle_members(client, "C1"), [])
        client.users_list.assert_not_called()

    def test_ended_latest_huddle_does_not_resurrect_old_room(self):
        client = Mock()
        client.conversations_history.return_value = {"messages": [
            {"subtype": "huddle_thread", "room": {"id": "R2", "has_ended": True}},
            {"subtype": "huddle_thread", "room": {"id": "R1", "has_ended": False}}]}
        self.assertEqual(Tracker(client, "C1", None, 300).discover_room()["id"], "R2")

    def test_group_helper(self):
        client = Mock()
        client.usergroups_users_list.return_value = {"users": ["U1"]}
        self.assertEqual(user_group_members(client, "S1"), ["U1"])
        client.usergroups_users_list.assert_called_once_with(usergroup="S1")


if __name__ == "__main__":
    unittest.main()
