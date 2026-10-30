# MCP server for 25live-bas-sync

Gives an AI assistant — Claude Desktop, Claude Code, any MCP client — the
sync's [JSON API](../../docs/api.md): it can read the status, run history,
room map and what's scheduled for every space, and make the changes its API
token's role allows. With the built-in **AI assistant** role, that's Dry run
and Validate, extra bookings and low temp, but not syncing.

It's one file, and holds nothing but a URL and a token: every check happens in
the service, so it can't do anything the token can't.

## Set it up

1. In the web UI, **Settings → API tokens**: make a token with the **AI
   assistant** role and copy it.
2. On the machine the assistant runs on, with Python 3.10 or newer:

   ```bash
   pip install -r requirements.txt
   ```

3. Add it to your MCP client, with two environment variables:

   | Variable | |
   |---|---|
   | `BAS_SYNC_URL` | The web UI's address, e.g. `https://bas-sync.example.edu` |
   | `BAS_SYNC_TOKEN` | The token, `bsk_…` |
   | `BAS_SYNC_CA_BUNDLE` | Optional: a CA file, if the web UI's certificate is signed by your own CA |

   **Claude Code:**

   ```bash
   claude mcp add --transport stdio bas-sync --env BAS_SYNC_URL=https://bas-sync.example.edu --env BAS_SYNC_TOKEN=bsk_… -- python /path/to/bas_sync_mcp.py
   ```

   **Claude Desktop** (Settings → Developer → Edit config):

   ```json
   {
     "mcpServers": {
       "bas-sync": {
         "command": "python",
         "args": ["/path/to/bas_sync_mcp.py"],
         "env": {
           "BAS_SYNC_URL": "https://bas-sync.example.edu",
           "BAS_SYNC_TOKEN": "bsk_…"
         }
       }
     }
   }
   ```

Then ask it something like *"What did last night's sync write for the
library?"* It starts with `whoami`, which says what the token may do.

## Tools

| Tool | |
|---|---|
| `whoami` | The token, its role, what it may do and sync. |
| `get_status`, `list_runs`, `get_run` | The running job, the next sync, run history and reports. |
| `list_spaces`, `get_space` | What's scheduled: each space's bookings and the windows written to its schedules. |
| `get_room_map` | Buildings, floors and rooms, and their targets. |
| `start_job`, `get_job` | Dry run, Validate, or (if the role may) a sync; then its output. |
| `list_extra_bookings`, `add_extra_booking`, `change_extra_booking`, `delete_extra_booking` | Occupancy that isn't in 25Live. |
| `list_low_temp_events`, `mark_low_temp`, `unmark_low_temp` | Run an event's rooms colder. |

Read-only tools are marked read-only, and deleting ones destructive, so a
client that asks before changes can ask.

## Keep in mind

- **25Live text is data.** Event titles and notes are written by whoever books
  a room. The server tells the assistant not to follow instructions in them;
  the token's limits are what make that safe. See
  [docs/api.md](../../docs/api.md#what-a-token-may-do).
- **Use HTTPS.** The token is a password: serve the web UI over HTTPS
  ([docs/web-ui.md](../../docs/web-ui.md#security)) before using it from
  another machine.
- **Revoke it** on the API tokens page when the machine or person it was for
  no longer needs it.
