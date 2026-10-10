# Add shift tracking yourself

Current state: bot.py collects ordinary PostgreSQL snapshots only. Automatic shift schema creation and poll-to-shift linking have been removed from the bot. seed_shifts.py still creates/populates the schedule tables when explicitly run, but no longer categorizes polls. The local database and 29 saved shifts are retained.

The following are instructions for changes you can make; they are not implemented in bot.py.

## Understand the existing data

| Table/view | Purpose |
|---|---|
| snapshots | One actual poll: timestamps, channel, huddle, user_count, user_ids array |
| snapshot_users | Expands each poll's user_ids into rows for queries |
| time_slots | Named shifts, start/end timestamps, channel and source |
| shift_hosts | Assigned Slack user IDs for each shift, with status and optional note |
| snapshot_slots | Links a poll to one or more shifts |
| shift_attendance | Joins linked polls to shift hosts and computes present using the user_ids array |

Keep snapshots as the original attendance evidence. Do not replace the participant array with only scheduled hosts. A host is present when their Slack user ID occurs in that array; being assigned is not proof of attending.

Use poll_started_at to choose a shift. Match channel_id and starts_at <= timestamp < ends_at. At exactly 16:30, a poll belongs to the shift starting at 16:30, not one ending then. Match all overlapping shifts if any exist; snapshot_slots supports that. A poll outside any shift must still be saved, with no shift links.

## 1. initialize_db(database_url): modify

Existing structure:
- Input: PostgreSQL connection string.
- Connects using autocommit=True and dict_row, creates snapshots and snapshot_users.
- Returns an open psycopg connection.

Add execution of shifts.sql after the existing table/view statements and before return db. The module already imports Path, so use Path(__file__).with_name('shifts.sql').read_text(encoding='utf-8'). Pass that SQL to db.execute(). The file defines the schedule tables, linking table and attendance view.

This makes a fresh installation create the required structure too. Creating tables does not populate the schedule; keep schedule seeding separate. The existing local database already has these tables.

## 2. link_snapshot_to_shifts(db, snapshot_id, channel_id, polled_at): add

Suggested signature:

```python
def link_snapshot_to_shifts(db, snapshot_id, channel_id, polled_at):
    # Find matching shifts and insert their links. No commit here.
    ...
```

Inputs:
- db: same connection used to insert the poll.
- snapshot_id: integer ID returned by that insert.
- channel_id: the monitored Slack channel.
- polled_at: timezone-aware datetime, specifically the poll start.

Use one INSERT ... SELECT statement with these parts:
- Insert into snapshot_slots(snapshot_id, time_slot_id).
- Select the supplied snapshot_id and id from time_slots.
- Filter channel_id = supplied channel, starts_at <= polled_at, ends_at > polled_at.
- End with ON CONFLICT DO NOTHING so repeated linking cannot duplicate a pair.

Use %s placeholders and a parameter tuple (snapshot_id, channel_id, polled_at, polled_at). Do not interpolate values into SQL strings. No matching slots is a normal success with zero inserted links. Let database errors propagate to the caller so its transaction can roll back.

## 3. save_snapshot(db, started, finished, channel_id, room_id, ids): modify

Existing structure:
- Inputs: connection, aware start/end datetimes, channel/huddle IDs, list of participant IDs.
- Inserts one snapshots row with len(ids) and the native Python list.
- Currently returns None and does not query shifts.

Change its structure to:

```text
open db.transaction()
    insert the snapshot with RETURNING id
    fetchone() and read row['id']
    call link_snapshot_to_shifts(db, snapshot_id, channel_id, started)
return snapshot_id
```

Because initialize_db uses autocommit, the explicit transaction is important: the snapshot and its links should succeed or fail together. Keep the transaction entirely in this function; the linking helper shouldn't commit. Keep the original participant IDs and both poll timestamps unchanged.

Returning the ID is useful for logging and tests; current callers can safely ignore it.

## 4. Tracker.run(self): optional small modification

Existing structure:
- Discovers/reconciles the channel huddle.
- When a poll is due, collects started, users, ids and finished.
- Reconnects to PostgreSQL if needed, then calls save_snapshot().
- Logs success or errors and schedules the next poll.

No change is required to activate linking if you implement it inside save_snapshot(). Optionally capture its returned snapshot_id and include that in the success log.

Do not skip polling based on the schedule: ordinary snapshots outside a shift are still useful. Do not assign five minutes of credit in this loop; that would mix observed attendance with an award policy. Keep schedule lookups on the database side of the workflow, after Slack collection.

## 5. main(): no shift-specific change needed

Existing structure: parse --group, create Slack client, validate polling interval, initialize DB, create Tracker and Socket Mode listener, run until interrupted, close connections.

Keep DATABASE_URL, .env loading and the Slack channel ID setup. Do not automatically seed shifts on every bot startup; it can accidentally restore an outdated schedule. The schema initialization can be automatic, while schedule updates are explicit.

## 6. Optional reporting helper: get_shift_attendance(db, slot_id)

Suggested interface:

```python
def get_shift_attendance(db, slot_id):
    # Return list of dict rows: user_id, display_name, status,
    # snapshot_id, poll_started_at, present.
    ...
```

Query shift_attendance with WHERE time_slot_id = %s and ORDER BY poll_started_at, user_id. If reporting assigned volunteer credit, filter status = 'scheduled'. Return fetchall(), which is already a list of dictionaries with dict_row.

This view only contains actual linked polls. No poll means unknown attendance, not necessarily absence. Repeatedly appearing in overlapping shifts must not automatically earn duplicate credit. Decide award rules separately before adding a duration calculation.

## Functions to keep as they are

| Function | Current structure | Why no shift change is needed |
|---|---|---|
| user_group_members(client, group_id) | Calls usergroups.users.list; returns list of IDs | Group membership is independent of shift assignment |
| paginated(client_method, key, **kwargs) | Yields items across cursor pages | API pagination utility |
| resolve_channel(client) | Uses configured ID or resolves name; returns channel ID | That ID is already passed to save_snapshot |
| current_huddle_members(client, channel_id) | Discovers current room and reads matching user IDs | On-demand Slack helper, not a storage function |
| participants(users, room_id) | Pure filter; returns sorted unique participant IDs | Preserve all observed participants |
| Tracker.__init__ | Stores client/channel/DB/interval and synchronization state | No cached shifts needed |
| Tracker.event | Acknowledges events and signals discovery | Keep it fast; avoid database work here |
| Tracker.discover_room | Reads latest huddle from channel history | Shift schedule doesn't change huddle discovery |

## Schedule maintenance functions

In seed_shifts.py:
- shifts(week_start): converts SCHEDULE entries into dated shifts and assigned host IDs. Modify SCHEDULE/HOSTS when the published schedule changes. It currently uses fixed EDT (UTC-04:00).
- seed(db, week_start): inserts slots and hosts in a transaction. Its ON CONFLICT DO NOTHING for hosts preserves existing assignments; simply editing HOSTS and rerunning will not remove an old assignment. Implement explicit reassignment/update logic when that is needed, preserving historical shifts.
- Script entry point: initializes the schema and calls seed for the requested Monday. Keep this an explicit administrative action, separate from starting the listener.

For older snapshots, optionally write a separate backfill operation: join snapshots to time_slots by matching channel and the same start-inclusive/end-exclusive timestamp rule, then insert snapshot_slots pairs with ON CONFLICT DO NOTHING. Do not change original poll timestamps or IDs.

## Verify your implementation

Use the separate huddles_test database, not the real schedule database, and check:
1. A poll inside a shift creates one snapshot and the expected link.
2. Exactly at a shift boundary, it links only to the next shift.
3. Outside any shift, the snapshot is saved without a link.
4. A host included in user_ids is present=True; a missing host is False.
5. Linking the same poll twice doesn't create duplicate pairs.
6. A linking failure rolls back the snapshot insert too.

After changing save_snapshot(), keep the existing parameterization test and add checks for its new returned ID and linking behavior. Existing Slack/group helpers should continue passing unchanged.
