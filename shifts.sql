CREATE TABLE IF NOT EXISTS time_slots (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    channel_id TEXT NOT NULL,
    name TEXT NOT NULL,
    starts_at TIMESTAMPTZ NOT NULL,
    ends_at TIMESTAMPTZ NOT NULL,
    source_url TEXT,
    CHECK (ends_at > starts_at),
    UNIQUE (channel_id, starts_at, ends_at)
);
ALTER TABLE time_slots ADD COLUMN IF NOT EXISTS processed_at TIMESTAMPTZ;
ALTER TABLE time_slots ADD COLUMN IF NOT EXISTS process_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE time_slots ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ;
ALTER TABLE time_slots ADD COLUMN IF NOT EXISTS last_error TEXT;
ALTER TABLE time_slots ADD COLUMN IF NOT EXISTS report JSONB;
CREATE TABLE IF NOT EXISTS shift_hosts (
    time_slot_id BIGINT NOT NULL REFERENCES time_slots(id),
    user_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('scheduled', 'unavailable', 'offered')),
    note TEXT,
    PRIMARY KEY (time_slot_id, user_id)
);
CREATE TABLE IF NOT EXISTS snapshot_slots (
    snapshot_id BIGINT NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    time_slot_id BIGINT NOT NULL REFERENCES time_slots(id),
    PRIMARY KEY (snapshot_id, time_slot_id)
);
CREATE INDEX IF NOT EXISTS time_slots_window ON time_slots(channel_id, starts_at, ends_at);
CREATE OR REPLACE VIEW shift_attendance AS
    SELECT t.id AS time_slot_id, t.name, t.starts_at, t.ends_at,
           h.user_id, h.display_name, h.status,
           s.id AS snapshot_id, s.poll_started_at,
           h.user_id = ANY(s.user_ids) AS present
    FROM time_slots t JOIN shift_hosts h ON h.time_slot_id = t.id
    JOIN snapshot_slots ss ON ss.time_slot_id = t.id
    JOIN snapshots s ON s.id = ss.snapshot_id;
