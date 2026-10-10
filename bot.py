"""Slack huddle snapshots. Python 3.10+, Socket Mode, PostgreSQL."""
import argparse
import json
import logging
import os
import threading
import time
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.response import SocketModeResponse

log = logging.getLogger("huddles")
load_dotenv(Path(__file__).with_name(".env"))


def user_group_members(client, group_id):
    """group_id is Slack's S... ID, not the @handle."""
    return client.usergroups_users_list(usergroup=group_id)["users"]

def paginated(client_method, key, **kwargs):
    cursor = None
    while True:
        result = client_method(limit=200, cursor=cursor, **kwargs)
        yield from result.get(key, [])
        cursor = result.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break


def resolve_channel(client):
    channel_id = os.getenv("SLACK_CHANNEL_ID")
    if channel_id:
        return channel_id
    name = os.getenv("SLACK_CHANNEL_NAME", "half-life").lstrip("#")
    for channel in paginated(client.conversations_list, "channels",
                             types="public_channel,private_channel", exclude_archived=True):
        if channel["name"] == name:
            return channel["id"]
    raise RuntimeError(f"Channel #{name} not visible; invite the bot or set SLACK_CHANNEL_ID")


def initialize_db(database_url):
    db = psycopg.connect(database_url, autocommit=True, row_factory=dict_row, connect_timeout=10)
    db.execute("""CREATE TABLE IF NOT EXISTS snapshots (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        poll_started_at TIMESTAMPTZ NOT NULL,
        poll_finished_at TIMESTAMPTZ NOT NULL,
        channel_id TEXT NOT NULL,
        huddle_id TEXT NOT NULL,
        user_count INTEGER NOT NULL CHECK (user_count >= 0),
        user_ids TEXT[] NOT NULL,
        CHECK (user_count = cardinality(user_ids))
    )""")
    db.execute("""CREATE OR REPLACE VIEW snapshot_users AS
        SELECT s.id AS snapshot_id, s.poll_started_at, s.channel_id, s.huddle_id,
               u.user_id FROM snapshots s
               CROSS JOIN LATERAL unnest(s.user_ids) AS u(user_id)""")
    sql = Path(__file__).with_name("shifts.sql").read_text(encoding="utf-8")
    with db.transaction():
        db.execute(sql, prepare=False)
    return db

def link_snapshot_to_shift(db, snapshot_id, channel_id, polled_at):
    db.execute("""INSERT INTO snapshot_slots(snapshot_id, time_slot_id)
        SELECT %s, ts.id FROM time_slots ts
        WHERE ts.channel_id = %s AND ts.starts_at <= %s AND ts.ends_at > %s ON CONFLICT DO NOTHING""", 
        (snapshot_id, channel_id, polled_at, polled_at)
    )

def current_huddle_members(client, channel_id):
    """Read the channel's latest huddle and return current user IDs (empty if inactive)."""
    room = Tracker(client, channel_id, None, 300).discover_room()
    if not room or room.get("has_ended") or room.get("date_end"):
        return []
    return room_participants(room)


def room_participants(room):
    """Current participants, never the historical attendee list."""
    ids = room.get("participants")
    if not isinstance(ids, list) or not all(isinstance(uid, str) for uid in ids):
        raise RuntimeError("Slack omitted the current huddle participant list")
    return sorted(set(ids))

def save_snapshot(db, started, finished, channel_id, room_id, ids):
    with db.transaction():
        row = db.execute("""INSERT INTO snapshots
            (poll_started_at,poll_finished_at,channel_id,huddle_id,user_count,user_ids)
            VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
            (started, finished, channel_id, room_id, len(ids), ids)).fetchone()
        snapshot_id = row["id"]
        link_snapshot_to_shift(db, snapshot_id, channel_id, started)
    return snapshot_id

def participants(users, room_id):
    return sorted({u["id"] for u in users if not u.get("deleted")
                   and u.get("profile", {}).get("huddle_state") == "in_a_huddle"
                   and u.get("profile", {}).get("huddle_state_call_id") == room_id})

def next_poll_boundary(interval=300):
    now = time.time()
    return (int(now // interval) + 1) * interval

def get_shift_participants(db, shift_id):
    rows = db.execute("""SELECT DISTINCT u.user_id FROM snapshot_slots ss
        JOIN snapshot_users u ON ss.snapshot_id = u.snapshot_id
        WHERE ss.time_slot_id = %s
        ORDER BY u.user_id""", (shift_id,)).fetchall()

    return [row["user_id"] for row in rows]

def get_shift_poll_counts(db, shift_id):
    """One chart point per actual snapshot, including genuine zero-count polls."""
    return db.execute("""SELECT s.id AS snapshot_id,
        s.poll_started_at AS polled_at, s.user_count
        FROM snapshot_slots ss JOIN snapshots s ON s.id = ss.snapshot_id
        WHERE ss.time_slot_id = %s ORDER BY s.poll_started_at, s.id""",
        (shift_id,)).fetchall()

def on_shift_ended(db, shift):
    """Customize here. Return JSON-serializable data to store in time_slots.report."""
    user_ids = get_shift_participants(db, shift["id"])
    polls = get_shift_poll_counts(db, shift["id"])
    hosts = db.execute("""SELECT user_id, display_name FROM shift_hosts
        WHERE time_slot_id = %s AND status = 'scheduled' ORDER BY user_id""",
        (shift["id"],)).fetchall()
    client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    groups = client.usergroups_list()["usergroups"]
    group = next(g for g in groups if g["handle"] == "halflifehosts")
    standings = [
        (uid, len(get_host_participants(db, uid, shift["ends_at"])))
        for uid in user_group_members(client, group["id"])
    ]
    standings.sort(key=lambda item: item[1], reverse=True)
    positions = {uid: rank for rank, (uid, _) in enumerate(standings, 1)}
    host_attendance = []
    for host in hosts:
        present_polls = db.execute("""SELECT count(*) AS n
            FROM snapshot_slots ss JOIN snapshots s ON s.id = ss.snapshot_id
            WHERE ss.time_slot_id = %s AND %s = ANY(s.user_ids)""",
            (shift["id"], host["user_id"])).fetchone()["n"]
        host_attendance.append({
            **host, "present": present_polls > 0,
            "current_ups": len(get_host_participants(db, host["user_id"], shift["ends_at"])),
            "leaderboard_position": positions.get(host["user_id"]),
            "present_polls": present_polls, "total_polls": len(polls),
            "attendance_percent": present_polls / len(polls) * 100 if polls else None,
        })
    report = {
        "shift_id": shift["id"], "name": shift["name"],
        "participant_ids": user_ids, "unique_participant_count": len(user_ids),
        "polls": [{**p, "polled_at": p["polled_at"].isoformat()} for p in polls],
        "huddle_observed": bool(polls),
        "host_present": any(host["present"] for host in host_attendance),
        "hosts": host_attendance,
    }
    log.info("Shift ended: id=%s unique_participants=%d polls=%d",
             shift["id"], len(user_ids), len(polls))
    for host in host_attendance:
        log.info("Shift UPs: shift=%s host=%s current_ups=%d",
                 shift["id"], host["user_id"], host["current_ups"])

    from shift_message import send_shift_report

    send_shift_report(
        client,
        os.getenv("REPORT_CHANNEL_ID") or shift["channel_id"],
        report,
    )
    return report

def get_host_participants(db, host_id, before=None):
    rows = db.execute("""
        SELECT DISTINCT participant_id AS user_id
        FROM host_participant_polls
        WHERE host_id = %s
          AND poll_started_at < COALESCE(%s::timestamptz, NOW())
        ORDER BY participant_id
    """, (host_id, before)).fetchall()

    return [row["user_id"] for row in rows]

def process_ended_shift(db, channel_id, callback=None, grace_seconds=15):
    """Process one due shift. Returns False if none are due; retries failures later."""
    callback = callback or on_shift_ended
    shift = None
    try:
        with db.transaction():
            shift = db.execute("""SELECT * FROM time_slots
                WHERE channel_id = %s AND processed_at IS NULL
                  AND ends_at <= NOW() - (%s * INTERVAL '1 second')
                  AND (next_attempt_at IS NULL OR next_attempt_at <= NOW())
                ORDER BY ends_at, id FOR UPDATE SKIP LOCKED LIMIT 1""",
                (channel_id, grace_seconds)).fetchone()
            if shift is None:
                return False
            report = callback(db, shift)
            db.execute("""UPDATE time_slots SET processed_at = NOW(),
                process_attempts = process_attempts + 1, next_attempt_at = NULL,
                last_error = NULL, report = %s WHERE id = %s""",
                (Jsonb(report), shift["id"]))
        return True
    except Exception as exc:
        if shift is None:
            raise
        log.exception("Shift callback failed: id=%s; retrying later", shift["id"])
        # The callback transaction rolled back, including any DB writes it made.
        db.execute("""UPDATE time_slots SET process_attempts = process_attempts + 1,
            last_error = %s, next_attempt_at = NOW() + INTERVAL '60 seconds'
            WHERE id = %s AND processed_at IS NULL""", (str(exc)[:2000], shift["id"]))
        return True


def shift_end_worker(database_url, channel_id, stop, poll_guard,
                     check_seconds=15, grace_seconds=15):
    """Dedicated connection; wakes for overdue shifts even without an active huddle."""
    db = None
    try:
        while not stop.is_set():
            try:
                if db is None or db.closed or db.broken:
                    db = psycopg.connect(database_url, autocommit=True, row_factory=dict_row, connect_timeout=10)
                while not stop.is_set():
                    # The tracker holds this lock from collection through snapshot commit.
                    with poll_guard:
                        processed = process_ended_shift(db, channel_id, grace_seconds=grace_seconds)
                    if not processed:
                        break
            except Exception:
                log.exception("Shift worker failed; will reconnect and retry")
                if db is not None:
                    db.close()
                db = None
            stop.wait(check_seconds)
    finally:
        if db is not None:
            db.close()

class Tracker:
    def __init__(self, client, channel, db, interval, poll_guard=None):
        self.client, self.channel, self.db, self.interval = client, channel, db, interval
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.room = None
        self.needs_discovery = True
        self.poll_guard = poll_guard if poll_guard is not None else threading.Lock()

    def event(self, socket, request):
        if request.type != "events_api":
            return
        socket.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
        event = request.payload.get("event", {})
        message = event.get("message", event)
        room = message.get("room")
        if event.get("channel") == self.channel and room:
            with self.lock:
                self.room = room
            self.wake.set()
        # Huddle status changes are pushed; actual membership is freshly read at each poll.
        if event.get("type") == "user_huddle_changed":
            with self.lock:
                self.needs_discovery = True
            self.wake.set()

    def discover_room(self):
        # Read beyond the newest messages so an existing huddle is found on startup.
        for message in paginated(self.client.conversations_history, "messages", channel=self.channel):
            room = message.get("room")
            if room and (message.get("subtype") == "huddle_thread" or room.get("call_family") == "huddle"):
                return room  # history is newest-first; don't resurrect an older huddle
        return None

    def run(self):
        next_poll = next_poll_boundary(self.interval)
        room_id = None
        while True:
            self.wake.clear()
            try:
                now = time.time()
                with self.lock:
                    room = self.room
                    discover = self.needs_discovery
                    self.needs_discovery = False
                # Periodic discovery recovers missing events and checks for already-active calls.
                if now >= next_poll or discover:
                    room = self.discover_room()
                    with self.lock:
                        self.room = room
                active = room and not room.get("has_ended") and not room.get("date_end")
                current = room["id"] if active else None
                if current != room_id:
                    room_id = current
                if now >= next_poll:
                    next_poll = next_poll_boundary(self.interval)
                    if room_id:
                        with self.poll_guard:
                            started = datetime.now(timezone.utc)
                            ids = room_participants(room)
                            finished = datetime.now(timezone.utc)
                            if self.db.closed or self.db.broken:
                                self.db = initialize_db(os.environ["DATABASE_URL"])
                            save_snapshot(self.db, started, finished, self.channel, room_id, ids)
                        log.info("Saved snapshot: huddle=%s count=%d", room_id, len(ids))
            except Exception:
                log.exception("Poll failed; no snapshot recorded")
                next_poll = next_poll_boundary(self.interval)
            self.wake.wait(timeout=max(0, next_poll - time.time()))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--group", help="Print user IDs in a Slack user group (S... ID), then exit")
    parser.add_argument("--init-db", action="store_true", help="Apply database schema and exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.init_db:
        initialize_db(os.environ["DATABASE_URL"]).close()
        return
    client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    if args.group:
        print(json.dumps(user_group_members(client, args.group)))
        return
    interval = int(os.getenv("POLL_SECONDS", "300"))
    if interval < 60:
        raise ValueError("POLL_SECONDS must be at least 60")
    db = initialize_db(os.environ["DATABASE_URL"])
    tracker = Tracker(client, resolve_channel(client), db, interval)
    stop = threading.Event()
    from leaderboard_worker import leaderboard_worker
    leaderboard_thread = threading.Thread(target=leaderboard_worker,
        args=(stop,), name="leaderboard-worker", daemon=True)
    check_seconds = int(os.getenv("SHIFT_CHECK_SECONDS", "15"))
    grace_seconds = int(os.getenv("SHIFT_GRACE_SECONDS", "15"))
    if check_seconds < 1 or grace_seconds < 0:
        raise ValueError("SHIFT_CHECK_SECONDS must be positive; SHIFT_GRACE_SECONDS must be nonnegative")
    worker = threading.Thread(target=shift_end_worker,
        args=(os.environ["DATABASE_URL"], tracker.channel, stop, tracker.poll_guard,
              check_seconds, grace_seconds), name="shift-end-worker", daemon=True)
    socket = SocketModeClient(app_token=os.environ["SLACK_APP_TOKEN"], web_client=client)
    socket.socket_mode_request_listeners.append(tracker.event)
    socket.connect()
    try:
        worker.start()
        leaderboard_thread.start()
        tracker.run()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if worker.is_alive():
            worker.join(timeout=5)
        socket.close()
        tracker.db.close()


if __name__ == "__main__":
    main()
