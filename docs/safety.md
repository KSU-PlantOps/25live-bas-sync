[Documentation](README.md) › Safety rails

# Safety rails

The dangerous failure mode here is not a crash — it's a **successful-looking run
that writes empty schedules everywhere**. An expired 25Live service account, a
changed `state` query parameter, or a Series25 version bump all return HTTP 200
with zero events, and a naive sync would faithfully stand the entire campus
down. Nobody notices until Monday morning.

## The mass-clear check

So each run compares itself to the last and refuses to write if:

- fewer than `safety.min_events` assignments came back from 25Live at all
  ([extra bookings](configuration.md#extra-bookings) don't count), or
- more than `safety.max_cleared_fraction` (default 34%) of the schedules that
  had bookings last time would be emptied now.

Both are about *change*, not absolute counts, so a genuinely quiet week still
has last week's state to compare against, and a first-ever run is allowed
through. [Low-temp schedules](configuration.md#low-temp) are left out: they
only ever hold a few marked events, so one ending says nothing about 25Live.
The web UI's **Settings → Safety** page sets both, turns the check off (after
asking), and shows the current baseline.

The comparison state lives in **`state/last_run.json`**. It is kept apart from
the logs, written atomically, and backed by the previous copy (`.prev`).
A run with no baseline says so at WARNING level every time, rather than quietly
passing. `--dry-run` shows the verdict a live run would get. In Docker,
[keep the `state` volume](docker.md#state-logs-and-the-schedule).

`--force` (*Force* on the web UI — Admin by default) overrides it — the right answer
at semester break, when the drop is real. A blocked run exits `7` and emails
the reason, including which schedules would have been cleared.

## The other layers

This complements, rather than replaces:

- `--validate` before deploying
- `--dry-run` before each change
- the `preview` driver for staged cut-over: point a building at it and it logs
  (and CSV-exports) what it would do while the rest write for real
- `verify_writes` reading BACnet writes back to confirm they took
- `verify_device` refusing to write through a stale pinned address
- a broken room-map row or extra booking leaving its schedules alone rather
  than rewriting them without its bookings — see
  [A broken row doesn't cost you the campus](configuration.md#a-broken-row-doesnt-cost-you-the-campus) —
  and a low-temp events file that can't be read leaving every low-temp
  schedule as it is
- one live sync at a time: a second one exits `8` without touching anything
