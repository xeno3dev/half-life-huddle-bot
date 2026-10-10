"""Published Oct 10-11 lock-in host shifts. Rerunnable; preserves existing polls."""
import os
from datetime import date, datetime, time

from bot import initialize_db
from seed_shifts import CHANNEL, EDT, HOSTS

SOURCE = "https://hackclub.slack.com/archives/C0BT9KNC24T/p1791557467121059"
SCHEDULE = [("06:00", "10:00", "AjuBrokeHisHead"),
            ("10:00", "15:00", "Xeno3ra"), ("15:00", "19:30", "Ayush")]


def seed_weekend(db):
    with db.transaction():
        for day in (date(2026, 10, 10), date(2026, 10, 11)):
            for start, end, host in SCHEDULE:
                begins = datetime.combine(day, time.fromisoformat(start), EDT)
                ends = datetime.combine(day, time.fromisoformat(end), EDT)
                name = f"{host} - {begins:%I:%M %p}–{ends:%I:%M %p} EDT (Oct {day.day} lock-in)"
                slot = db.execute("""INSERT INTO time_slots
                    (channel_id,name,starts_at,ends_at,source_url) VALUES (%s,%s,%s,%s,%s)
                    ON CONFLICT (channel_id,starts_at,ends_at) DO UPDATE SET
                    name=EXCLUDED.name, source_url=EXCLUDED.source_url RETURNING id""",
                    (CHANNEL, name, begins, ends, SOURCE)).fetchone()["id"]
                db.execute("""INSERT INTO shift_hosts(time_slot_id,user_id,display_name,status)
                    VALUES (%s,%s,%s,'scheduled') ON CONFLICT DO NOTHING""",
                    (slot, HOSTS[host], host))
                db.execute("""INSERT INTO snapshot_slots(snapshot_id,time_slot_id)
                    SELECT id,%s FROM snapshots WHERE channel_id=%s
                    AND poll_started_at >= %s AND poll_started_at < %s
                    ON CONFLICT DO NOTHING""", (slot, CHANNEL, begins, ends))

        # One overnight commitment, confirmed in the signup thread; not repeated Sunday.
        begins = datetime(2026, 10, 10, 20, tzinfo=EDT)
        ends = datetime(2026, 10, 11, 1, tzinfo=EDT)
        source = "https://hackclub.slack.com/archives/C0BT9KNC24T/p1791574554271799"
        slot = db.execute("""INSERT INTO time_slots
            (channel_id,name,starts_at,ends_at,source_url) VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (channel_id,starts_at,ends_at) DO UPDATE SET
            name=EXCLUDED.name, source_url=EXCLUDED.source_url RETURNING id""",
            (CHANNEL, "Xeno3ra - 08:00 PM–01:00 AM EDT (Oct 10–11 lock-in)",
             begins, ends, source)).fetchone()["id"]
        db.execute("""INSERT INTO shift_hosts(time_slot_id,user_id,display_name,status,note)
            VALUES (%s,%s,'Xeno3ra','scheduled',%s) ON CONFLICT DO NOTHING""",
            (slot, HOSTS['Xeno3ra'], "End time approximate: host said 'Till abt 1am'."))
        db.execute("""INSERT INTO snapshot_slots(snapshot_id,time_slot_id)
            SELECT id,%s FROM snapshots WHERE channel_id=%s
            AND poll_started_at >= %s AND poll_started_at < %s
            ON CONFLICT DO NOTHING""", (slot, CHANNEL, begins, ends))


if __name__ == "__main__":
    with initialize_db(os.environ["DATABASE_URL"]) as db:
        seed_weekend(db)
    print("Seeded seven Oct 10-11 shifts, including one overnight, and linked available saved polls.")
