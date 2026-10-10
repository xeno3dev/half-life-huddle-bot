"""Seed the published Oct 12-18 schedule; safe to rerun for the same week."""
import argparse
import os
from pathlib import Path
from datetime import date, datetime, time, timedelta, timezone

from bot import initialize_db

SOURCE = "https://hackclub.slack.com/archives/C0BT9KNC24T/p1791582055427589"
CHANNEL = "C0BTE0WN23S"
HOSTS = {
    "eceWire": "U0B9PTXJ99C", "Abean": "U0ALMN32YTU",
    "cocotrilito": "U0A61J3AYCS", "Ayush": "U09HWTM6WD6",
    "OJOkhiria": "U0B0K8Z8F51", "Anay": "U083EP11GCS",
    "Xeno3ra": "U0B9YRG67PG", "AjuBrokeHisHead": "U0A13N316LB",
    "ksshfii": "U0AFQU2DPLP", "Ryaan": "U0AG48VALL8",
    "Vynu": "U08R6ME1880",
}
# Monday=0. Source explicitly says EDT, so use fixed UTC-04:00.
EDT = timezone(timedelta(hours=-4))
SCHEDULE = [
    (0, "14:30", "16:30", "eceWire"), (0, "16:30", "18:00", "Abean"), (0, "18:00", "21:00", "cocotrilito"),
    (1, "14:30", "16:00", "Ayush"), (1, "17:00", "20:00", "Abean"), (1, "20:00", "22:00", "OJOkhiria"),
    (2, "13:00", "15:00", "Anay"), (2, "15:00", "17:30", "Xeno3ra"), (2, "20:00", "22:00", "eceWire"),
    (3, "06:30", "09:30", "AjuBrokeHisHead"), (3, "18:00", "21:00", "cocotrilito"),
    (4, "08:00", "09:30", "ksshfii"), (4, "13:00", "16:00", "Ryaan"), (4, "19:00", "21:00", "OJOkhiria"),
    (5, "03:30", "06:30", "Ayush"), (5, "06:30", "09:30", "ksshfii"), (5, "09:30", "12:30", "Xeno3ra"),
    (5, "12:30", "14:30", "AjuBrokeHisHead"), (5, "14:30", "17:00", "Anay"), (5, "17:00", "20:00", "Abean"), (5, "20:00", "21:30", "cocotrilito"),
    (6, "00:30", "02:00", "Vynu"), (6, "03:30", "06:30", "Ayush"), (6, "06:30", "09:00", "AjuBrokeHisHead"),
    (6, "09:00", "12:00", "ksshfii"), (6, "12:00", "13:30", "Xeno3ra"), (6, "13:30", "16:00", "Anay"),
    (6, "16:00", "19:00", "eceWire"), (6, "20:00", "23:00", "OJOkhiria"),
]


def shifts(week_start):
    if week_start.weekday() != 0:
        raise ValueError("week-start must be a Monday")
    for day, start, end, host in SCHEDULE:
        shift_date = week_start + timedelta(days=day)
        begins = datetime.combine(shift_date, time.fromisoformat(start), EDT)
        ends = datetime.combine(shift_date, time.fromisoformat(end), EDT)
        hosts = [(HOSTS[host], host, "scheduled", None)]
        yield f"{host} - {begins:%I:%M %p}–{ends:%I:%M %p} EDT", begins, ends, hosts


def seed(db, week_start):
    with db.transaction():
        for name, starts, ends, hosts in shifts(week_start):
            slot = db.execute("""INSERT INTO time_slots(channel_id,name,starts_at,ends_at,source_url)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT (channel_id,starts_at,ends_at) DO UPDATE SET
                    name=EXCLUDED.name, source_url=EXCLUDED.source_url
                RETURNING id""", (CHANNEL, name, starts, ends, SOURCE)).fetchone()["id"]
            for uid, display_name, status, note in hosts:
                db.execute("""INSERT INTO shift_hosts(time_slot_id,user_id,display_name,status,note)
                    VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                    (slot, uid, display_name, status, note))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--week-start", type=date.fromisoformat, default=date(2026, 10, 12))
    args = parser.parse_args()
    with initialize_db(os.environ["DATABASE_URL"]) as db:
        db.execute(Path(__file__).with_name("shifts.sql").read_text(encoding="utf-8"))
        seed(db, args.week_start)
    print(f"Seeded 29 shifts for {args.week_start} from the parent schedule, including host IDs.")
