# 25Live -> BAS Schedule Sync — core data model
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
The data that flows through a sync run, in order:

    RawEvent          one 25Live space assignment, buffers already applied
      -> OccupancyWindow   a continuous "this space is occupied" period
      -> Destination       where that occupancy gets written (system + target)

`Destination` is what makes a mixed-vendor campus work: every schedule the sync
writes names both the BAS it lives on and the driver-specific address inside
that BAS, so one run can fan out across a mixed campus — WebCTRL here,
EcoStruxure there, a Niagara supervisor on the older wings — in one pass.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional


@dataclass(frozen=True)
class Destination:
    """
    One schedule the sync writes to.

    system:  key into the `systems:` block of config.yaml — picks the driver
             and its connection settings.
    target:  driver-specific address of the schedule, in the driver's
             canonical form (see ScheduleWriter.normalize_targets). Its syntax
             is documented in that driver's module:
               bacnet    "12001:5"                     (device instance : schedule instance)
               niagara   "SocialSciences/Rm1021_Occ"   (ORD under schedule_base_path)
               rest      whatever the site's path template expects
               preview   anything; nothing is contacted

    A room may have no destination of its own at all; see SpaceConfig.

    Frozen so it can key the schedule dict and dedupe cleanly.
    """
    system: str
    target: str

    def __str__(self) -> str:
        return f"{self.system}:{self.target}"


@dataclass
class OccupancyWindow:
    """A single continuous occupied period after merging overlapping events.

    Both bounds are timezone-aware. Half-open by convention: the space is
    occupied at `start` and released at `end`.
    """
    start: datetime
    end: datetime
    source_event_ids: list = field(default_factory=list)

    def overlaps_or_adjacent(self, other: "OccupancyWindow", gap_minutes: int) -> bool:
        gap = timedelta(minutes=gap_minutes)
        return self.start <= other.end + gap and other.start <= self.end + gap

    def merge(self, other: "OccupancyWindow") -> "OccupancyWindow":
        return OccupancyWindow(
            start=min(self.start, other.start),
            end=max(self.end, other.end),
            source_event_ids=self.source_event_ids + other.source_event_ids,
        )

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    def __repr__(self) -> str:
        return (f"OccupancyWindow({self.start.strftime('%a %m/%d %H:%M')}"
                f"-{self.end.strftime('%H:%M')})")


@dataclass
class RawEvent:
    """One space assignment parsed from a 25Live reservation, buffers applied."""
    event_id: str
    title: str
    space_id: str
    start: datetime          # effective start (after pre-conditioning run-up)
    end: datetime            # effective end (after post-event run-down)
    booked_start: Optional[datetime] = None   # the booking itself, before the
    booked_end: Optional[datetime] = None     # run-up and run-down
    low_temp: bool = False   # marked low temp: also drives the room's low-temp
                             # schedules (see SpaceConfig.low_temp_destinations)


@dataclass
class SpaceConfig:
    """One row from space_mapping.yaml, with inheritance already resolved."""
    space_id: str
    space_name: str
    space_type: str                          # "room" or "building"
    destination: Optional[Destination]       # this space's own schedule, if it
                                             # has one — see below
    building_destination: Optional[Destination]  # building roll-up, if any
    pre_condition_minutes: int
    post_buffer_minutes: int
    merge_gap_minutes: int                   # collapse this space's own windows
                                             # within this gap; falls back to the
                                             # global default
    floor: Optional[int] = None              # which floor the room is on
    floor_destination: Optional[Destination] = None
    # The floor's corridor schedule this room also feeds. A room drives its own
    # zone, its floor's corridor, AND its building's common areas — so a single
    # evening booking on the third floor lights and conditions that corridor
    # without running the whole tower.
    equipment_destinations: tuple = ()
    # Equipment that serves this room besides its own schedule: an AHU it
    # shares with other rooms, or a second VAV. Each one's schedule is the
    # union of every room it serves, like a floor's.
    building_id: Optional[str] = None
    # The room-map building it's in (for a bookable building, itself) — what
    # a sync limited to some buildings (--building) goes by.
    low_temp_destinations: tuple = ()
    # Schedules that put this room's zone (its own `low_temp_target:`, and its
    # equipment's) into a colder mode. Only bookings marked low temp drive
    # them — a blood drive, say — and the BAS decides what colder means.

    # `destination` is optional because how finely a building can be scheduled
    # depends on how it was built out. A room-level VAV retrofit gets its own
    # schedule object; an older building may only be schedulable at the floor
    # or building air handler. A room with no `target:` still contributes its
    # bookings to whatever roll-ups it belongs to — it just doesn't write a
    # schedule of its own. That is a normal, supported mapping, not a gap.

    def all_destinations(self) -> list:
        """Every schedule this space writes to, most specific first."""
        return [d for d in (self.destination, *self.equipment_destinations,
                            self.floor_destination, self.building_destination)
                if d is not None]

    def rollup_destinations(self) -> list:
        """The shared schedules this space contributes to, nearest first:
        its equipment, its floor, its building."""
        return [d for d in (*self.equipment_destinations, self.floor_destination,
                            self.building_destination) if d is not None]
