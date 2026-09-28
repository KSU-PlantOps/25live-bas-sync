# 25Live -> BAS Schedule Sync — bookings that aren't in 25Live
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
Extra bookings: occupancy the sync should schedule that 25Live doesn't know
about — a building open house, an evening custodial shift, a make-up lab
session, a building that isn't in 25Live at all.

They live in `extra_bookings.yaml`, beside config.yaml, edited on the web UI's
Extra bookings page (or by hand):

    bookings:
      - title: Open house
        building: science_hall      # a building's roll-up schedule
        date: 2026-10-05            # one day...
        start: "08:00"
        end: "14:00"
      - title: Evening custodial
        building: library
        floor: 2                    # ...a floor's corridor schedule
        days: [mon, wed]            # ...or every week on these days,
        from: 2026-09-01            #    between these dates (both optional)
        until: 2026-12-12
        start: "18:00"
        end: "21:00"
      - title: Chem lab make-up
        space_id: 3101              # a room in the room map, by 25Live id
        date: 2026-10-07
        start: "13:00"
        end: "16:00"
        exact: true                 # no run-up or run-down

Each run turns them into bookings exactly like 25Live's: a room booking gets
the room's run-up and run-down and rolls up into its floor and building; a
building or floor booking gets the building's. An end at or before the start
runs past midnight; "24:00" is midnight at the end of the day.

They are never counted as 25Live bookings, so they can't hide a 25Live
outage from the safety check. A broken row is reported and left out, and the
schedules it would drive are left as they are that run — the same rule as a
broken room-map row.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Optional

from .config import ConfigError, parse_hhmm, read_yaml
from .model import Destination, OccupancyWindow, RawEvent
from .spacemap import _space_id

FILE_NAME = "extra_bookings.yaml"
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
KEYS = ("title", "space_id", "building", "floor", "date", "days", "from", "until",
        "start", "end", "exact", "note", "added_by")
HEADER = """\
# Bookings the sync schedules that aren't in 25Live. Managed on the web UI's
# Extra bookings page, but plain YAML and safe to hand-edit. Each names a room
# (space_id), a building, or a building and floor; a date, or weekly days with
# optional from/until dates; and start and end times. See bassync/extras.py.
"""


class BookingError(ValueError):
    """One extra booking can't be used; the message says why."""


@dataclass(frozen=True)
class Booking:
    index: int                    # position in the file, for messages and edits
    title: str
    space_id: str                 # a room, or "" for a building or floor
    building: str
    floor: Optional[int]
    day: Optional[date]           # one day (`date:`), or None if weekly
    days: tuple                   # weekdays 0-6 (Monday 0) for a weekly one
    first: Optional[date]         # weekly: not before this date
    last: Optional[date]          # weekly: not after this date
    start: time
    end: time
    overnight: bool               # the end is on the next day
    exact: bool                   # no run-up or run-down

    @property
    def where(self) -> str:
        if self.space_id:
            return f"room {self.space_id}"
        if self.floor is not None:
            return f"floor {self.floor} of {self.building}"
        return f"building {self.building}"

    def span(self) -> str:
        """"08:00–14:00", "22:00–24:00", or "22:00–02:00 (next day)"."""
        if self.end == time(0):
            return f"{self.start:%H:%M}–24:00"
        return (f"{self.start:%H:%M}–{self.end:%H:%M}"
                + (" (next day)" if self.overnight else ""))

    def when(self) -> str:
        span = self.span()
        if self.day is not None:
            return f"{self.day:%a %b %d, %Y} {span}"
        days = ", ".join(DAY_LABELS[d] for d in self.days)
        bounds = ""
        if self.first and self.last:
            bounds = f", {self.first:%b %d} – {self.last:%b %d, %Y}"
        elif self.first:
            bounds = f", from {self.first:%b %d, %Y}"
        elif self.last:
            bounds = f", until {self.last:%b %d, %Y}"
        return f"Every {days} {span}{bounds}"

    def start_dates(self, first_day: date, last_day: date) -> list:
        """The days an occurrence starts on, between the two (inclusive)."""
        if self.day is not None:
            return [self.day] if first_day <= self.day <= last_day else []
        lo = max(first_day, self.first) if self.first else first_day
        hi = min(last_day, self.last) if self.last else last_day
        out = []
        day = lo
        while day <= hi:
            if day.weekday() in self.days:
                out.append(day)
            day += timedelta(days=1)
        return out

    def ended(self, today: date) -> bool:
        """True once no occurrence can still be running or to come."""
        last = self.day if self.day is not None else self.last
        if last is None:
            return False
        return last + timedelta(days=1 if self.overnight else 0) < today


def default_path(config_path) -> Path:
    return Path(config_path).parent / FILE_NAME


def read(path) -> list:
    """The rows as written (dicts), for the editor. Raises ConfigError if the
    file exists but can't be read."""
    data = read_yaml(path)
    rows = data.get("bookings")
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ConfigError(f"{path}: `bookings:` must be a list.")
    return rows


def _as_date(value, what: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        raise BookingError(f"`{what}` must be a date like 2026-10-05, not {value!r}.")


def _as_time(value, what: str) -> time:
    if str(value).strip() == "24:00":
        return time(0)
    try:
        return time.fromisoformat(parse_hhmm(value))
    except ValueError:
        raise BookingError(f"`{what}` must be a 24-hour time like 18:00, not {value!r}.")


def _day(value) -> int:
    text = str(value).strip().lower()[:3]
    if text not in DAYS:
        raise BookingError(f"{value!r} is not a day of the week (mon, tue, …).")
    return DAYS.index(text)


def parse(row, index: int = 0) -> Booking:
    """One row of the file, checked. Raises BookingError."""
    if not isinstance(row, dict):
        raise BookingError("an entry is not a mapping.")
    title = str(row.get("title") or "").strip() or "Extra booking"
    space_id = _space_id(row["space_id"]) if row.get("space_id") not in (None, "") else ""
    building = str(row.get("building") or "").strip()
    if bool(space_id) == bool(building):
        raise BookingError("give either a room (`space_id`) or a `building` "
                           "(optionally with a `floor`), not both.")
    floor = None
    if row.get("floor") not in (None, ""):
        if space_id:
            raise BookingError("`floor` goes with `building`, not with a room.")
        try:
            floor = int(str(row["floor"]).strip())
        except ValueError:
            raise BookingError(f"`floor` must be a whole number, not {row['floor']!r}.")
    one_day = row.get("date") not in (None, "")
    weekly = row.get("days") not in (None, "", [])
    if one_day == weekly:
        raise BookingError("give either a `date`, or weekly `days`, not both.")
    day = first = last = None
    days: tuple = ()
    if one_day:
        if row.get("from") or row.get("until"):
            raise BookingError("`from` and `until` are for weekly bookings; this one has a `date`.")
        day = _as_date(row["date"], "date")
    else:
        raw_days = row["days"] if isinstance(row["days"], list) else str(row["days"]).split(",")
        days = tuple(sorted({_day(d) for d in raw_days}))
        first = _as_date(row["from"], "from") if row.get("from") not in (None, "") else None
        last = _as_date(row["until"], "until") if row.get("until") not in (None, "") else None
        if first and last and last < first:
            raise BookingError("`until` is before `from`.")
    if row.get("start") in (None, "") or row.get("end") in (None, ""):
        raise BookingError("give a `start` and an `end` time.")
    if str(row["start"]).strip() == "24:00":
        raise BookingError("`start` must be before midnight; use 00:00 on the next day.")
    start = _as_time(row["start"], "start")
    end = _as_time(row["end"], "end")
    if start == end and str(row["end"]).strip() != "24:00":
        raise BookingError("`start` and `end` are the same time.")
    overnight = end <= start
    exact = row.get("exact") in (True, "true", "yes", 1)
    return Booking(index, title, space_id, building, floor, day, days, first, last,
                   start, end, overnight, exact)


def unknown_keys(row) -> list:
    return sorted(k for k in row if k not in KEYS) if isinstance(row, dict) else []


@dataclass
class Expansion:
    """What the extra bookings add to one run."""
    events: list                  # RawEvents on mapped rooms (buffers applied)
    windows: dict                 # { Destination: [OccupancyWindow] } on buildings/floors
    held: set                     # schedules a broken row would have driven
    errors: list                  # broken rows (left out)
    warnings: list                # rows naming a room or building the map lacks
    occurrences: int = 0          # occurrences inside this run's window


def expand(path, space_map, tz, lookahead_days: int,
           now: Optional[datetime] = None) -> Expansion:
    """Turn the file into bookings for one run: every occurrence that ends
    after midnight today and starts within the lookahead. Raises ConfigError
    if the file can't be read at all."""
    out = Expansion([], {}, set(), [], [])
    if not path or not Path(path).exists():
        return out
    rows = read(path)
    now = now or datetime.now(tz)
    today = datetime.combine(now.date(), time(0), tzinfo=tz)
    horizon = now + timedelta(days=lookahead_days)
    for index, row in enumerate(rows):
        label = f"Extra booking {index + 1}"
        if isinstance(row, dict) and row.get("title"):
            label += f" ({row['title']})"
        try:
            booking = parse(row, index)
        except BookingError as exc:
            out.errors.append(f"{label}: {exc}")
            target = _targets_of_row(row, space_map)
            out.held.update(target)
            continue
        if booking.space_id:
            sc = space_map.spaces.get(booking.space_id)
            if sc is None or sc.space_type != "room":
                out.warnings.append(f"{label}: room {booking.space_id} isn't in the room "
                                    "map, so it drives nothing.")
                continue
            pre, post = sc.pre_condition_minutes, sc.post_buffer_minutes
            dest = None
        else:
            info = space_map.buildings.get(booking.building)
            if info is None:
                out.warnings.append(f"{label}: building '{booking.building}' isn't in the "
                                    "room map (or its schedule is invalid), so it drives "
                                    "nothing.")
                continue
            pre, post = info.pre_condition_minutes, info.post_buffer_minutes
            if booking.floor is None:
                dest = info.destination
            else:
                dest = space_map.floors.get((booking.building, booking.floor))
                if dest is None:
                    out.warnings.append(f"{label}: floor {booking.floor} of "
                                        f"'{booking.building}' has no floors: entry in "
                                        "the room map, so it drives nothing.")
                    continue
        if booking.exact:
            pre = post = 0
        first_day = (today - timedelta(days=1)).date()
        for day in booking.start_dates(first_day, horizon.date()):
            start = datetime.combine(day, booking.start, tzinfo=tz)
            end = datetime.combine(day + timedelta(days=1 if booking.overnight else 0),
                                   booking.end, tzinfo=tz)
            start -= timedelta(minutes=pre)
            end += timedelta(minutes=post)
            if end <= today or start >= horizon:
                continue
            start = max(start, today)
            out.occurrences += 1
            event_id = f"extra:{index + 1}:{day.isoformat()}"
            if dest is None:
                out.events.append(RawEvent(event_id, booking.title, booking.space_id,
                                           start, end))
            else:
                out.windows.setdefault(dest, []).append(
                    OccupancyWindow(start, end, [event_id]))
    return out


def _targets_of_row(row, space_map) -> set:
    """The schedules a (broken) row would drive, as far as it can be read."""
    if not isinstance(row, dict):
        return set()
    space_id = _space_id(row["space_id"]) if row.get("space_id") not in (None, "") else ""
    if space_id:
        sc = space_map.spaces.get(space_id)
        return set(sc.all_destinations()) if sc is not None else set()
    building = str(row.get("building") or "").strip()
    info = space_map.buildings.get(building)
    if info is None:
        return set()
    try:
        floor = int(str(row.get("floor")).strip()) if row.get("floor") not in (None, "") else None
    except ValueError:
        return {info.destination}
    if floor is None:
        return {info.destination}
    dest: Optional[Destination] = space_map.floors.get((building, floor))
    return {dest} if dest is not None else set()


def clean_row(row: dict) -> dict:
    """A row in the file's key order, without empty values."""
    out = {k: row[k] for k in KEYS if k in row and row[k] not in (None, "", [])}
    for key in ("date", "from", "until"):
        if isinstance(out.get(key), (date, datetime)):
            out[key] = out[key].isoformat()[:10]
    for key in ("start", "end"):
        if key in out:
            out[key] = str(out[key])
    if out.get("exact") is not True:
        out.pop("exact", None)
    return out


def dump(rows: list) -> str:
    import yaml
    body = yaml.safe_dump({"bookings": [clean_row(r) for r in rows]}, sort_keys=False,
                          default_flow_style=None, allow_unicode=True, width=100)
    return HEADER + "\n" + body


def save(path, rows: list) -> bool:
    from .mapedit import write_if_changed
    return write_if_changed(path, dump(rows))


def log_expansion(result: Expansion) -> None:
    for message in result.errors:
        logging.error("%s", message)
    for message in result.warnings:
        logging.warning("%s", message)
    if result.occurrences:
        logging.info("Extra bookings: %d occurrence(s) in this run's window",
                     result.occurrences)
