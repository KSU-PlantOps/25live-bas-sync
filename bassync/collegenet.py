# 25Live -> BAS Schedule Sync — CollegeNET Series25 client
# Copyright (C) 2026 Ryan Bibby and contributors
# Licensed under the GNU General Public License v3.0 or later. See LICENSE.
"""
Reads events from the CollegeNET 25Live Series25 WebServices XML API.

    Endpoint:  GET {base_url}/events.xml
    Auth:      HTTP Basic over HTTPS (a LOCAL service account, not SSO)
    Response:  XML in the http://www.collegenet.com/r25 namespace

Read-only. Nothing in this module writes to 25Live.
"""

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import requests
from dateutil import parser as dateparser

from .httputil import mount_retries
from .model import RawEvent

# Events returned per API page.
PAGE_SIZE = 100

# Safety cap on pages per batch. The no-progress check below is what actually
# stops a server that ignores paging; this only bounds a pathological one.
MAX_PAGES = 1000

# How many space IDs to request per call, to keep the query string under
# typical URL-length limits when the space map is large.
SPACE_IDS_PER_REQUEST = 50

HTTP_TIMEOUT_FETCH = 60
HTTP_TIMEOUT_HEALTH = 15

R25_NS = {"r25": "http://www.collegenet.com/r25"}

STATE_PARAM_STYLES = ("plus", "space", "comma", "repeat", "none")

CANCELLED_RESERVATION_STATES = (99,)


class CollegeNetError(RuntimeError):
    """25Live returned something the sync cannot use."""


class CollegeNetClient:
    def __init__(self, cfg: dict, tz: ZoneInfo, retry: Optional[dict] = None):
        self.base_url = (cfg.get("base_url") or "").rstrip("/")
        self.lookahead_days = int(cfg.get("lookahead_days", 7))
        self.include_states = {int(s) for s in (cfg.get("include_states") or [])}
        # 99 is Series25's cancelled-reservation state. An explicit empty
        # list turns the filter off; leaving the key out keeps it on.
        excluded = cfg.get("exclude_reservation_states")
        self.exclude_reservation_states = {
            int(s) for s in (CANCELLED_RESERVATION_STATES if excluded is None
                             else excluded)}
        self.state_param_style = (cfg.get("state_param_style") or "plus").lower()
        self.tz = tz
        self.session = requests.Session()
        self.session.auth = (cfg.get("username", ""), cfg.get("password", ""))
        self.session.headers.update({"Accept": "application/xml"})
        mount_retries(self.session, retry, allowed_methods=["GET"])

    def close(self) -> None:
        self.session.close()

    # ── query construction ───────────────────────────────────────────────────

    def _state_params(self, style: Optional[str] = None) -> dict:
        """
        The `state` filter, encoded the way this instance expects.

        Series25 deployments genuinely differ here, and getting it wrong is
        quiet: the API answers 200 with zero events and the sync would clear
        every schedule. Hence both the configurable style, the probe in
        `--validate`, and the mass-clear rail in bassync/safety.py.

          plus    state=2%2B4   a literal '+' reaches the server
          space   state=2+4     space-joined, which is how `space_id` lists
                                are sent; the server decodes it as "2 4"
          comma   state=2,4
          repeat  state=2&state=4
          none    omit the parameter and filter client-side on the returned
                  <state> element

        With a single state (the default, confirmed only) every style but
        `none` produces the same request.
        """
        style = (style or self.state_param_style).lower()
        if not self.include_states or style == "none":
            return {}
        states = [str(s) for s in sorted(self.include_states)]
        if style == "comma":
            return {"state": ",".join(states)}
        if style == "repeat":
            return {"state": states}
        if style == "space":
            return {"state": " ".join(states)}
        return {"state": "+".join(states)}

    @staticmethod
    def _window_params(start: datetime, end: datetime) -> dict:
        return {
            "start_dt": start.strftime("%Y-%m-%dT00:00:00"),
            "end_dt": end.strftime("%Y-%m-%dT23:59:59"),
        }

    # ── helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _local(tag: str) -> str:
        """Strip the namespace prefix from an XML tag."""
        return tag.rsplit("}", 1)[-1]

    def _child_text(self, elem: ET.Element, local_name: str) -> Optional[str]:
        """Text of the first direct child whose local tag name matches."""
        for child in elem:
            if self._local(child.tag) == local_name and child.text:
                return child.text.strip()
        return None

    @staticmethod
    def _chunk(items: list, size: int) -> list:
        return [items[i:i + size] for i in range(0, len(items), size)]

    def _to_tz(self, dt_text: str) -> datetime:
        """
        Parse a 25Live datetime into the configured timezone.

        25Live returns instance-local timestamps. If the string is naive,
        attach the configured tz — do NOT use .astimezone(), which would assume
        the *server's* local zone and shift every booking when the sync runs on
        a host in another timezone (or in UTC, as containers usually are).
        """
        dt = dateparser.parse(dt_text)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=self.tz)
        return dt.astimezone(self.tz)

    def _parse_xml(self, text: str, context: str) -> ET.Element:
        try:
            return ET.fromstring(text)
        except ET.ParseError as exc:
            # A login page, a proxy error page, or an API version change all
            # land here. Say so plainly rather than raising a bare ParseError.
            snippet = " ".join(text[:200].split())
            raise CollegeNetError(
                f"25Live returned data that is not valid XML while {context}: "
                f"{exc}. First 200 characters: {snippet!r}") from exc

    def _today(self, now: Optional[datetime] = None) -> datetime:
        now = now or datetime.now(self.tz)
        return now.replace(hour=0, minute=0, second=0, microsecond=0)

    # ── connection check ─────────────────────────────────────────────────────

    def check_connection(self) -> tuple[bool, str]:
        """One small authenticated request, for --validate."""
        now = datetime.now(self.tz)
        params = {**self._window_params(now, now), **self._state_params(),
                  "page_size": 1}
        try:
            r = self.session.get(f"{self.base_url}/events.xml", params=params,
                                 timeout=HTTP_TIMEOUT_HEALTH)
        except requests.RequestException as exc:
            return False, f"connection error: {exc}"
        if r.status_code in (401, 403, 407):
            return False, (f"auth failed (HTTP {r.status_code}) — check the "
                           "username and BAS_25LIVE_PASSWORD, and that the "
                           "account is LOCAL (not SSO) with WebServices enabled")
        if r.status_code >= 400:
            # A 404 here is the classic wrong-instance/wrong-base_url symptom,
            # and the old code reported it as a pass.
            return False, (f"HTTP {r.status_code} from {self.base_url}/events.xml "
                           "— check collegenet.instance / base_url")
        try:
            root = self._parse_xml(r.text, "checking the connection")
        except CollegeNetError as exc:
            return False, str(exc)
        # A well-formed document is not necessarily a Series25 one. An SSO
        # redirect to an XHTML login page, or a proxy's XML error envelope,
        # parses cleanly and then yields zero events — which a live run would
        # read as "the campus is empty" and act on. Check the namespace here so
        # --validate names the real problem.
        if not self._local(root.tag).startswith(("results", "events", "index")) \
                and R25_NS["r25"] not in root.tag:
            snippet = " ".join(r.text[:150].split())
            return False, (f"responded, but the document root is <{root.tag}> "
                           "rather than Series25 XML — this is usually an SSO "
                           "redirect (the service account must be LOCAL, not "
                           f"SSO) or a proxy error page. First 150 chars: {snippet!r}")
        return True, f"HTTP {r.status_code} from {self.base_url}/events.xml"

    def probe_state_styles(self, space_map) -> dict:
        """
        {style: assignments returned} for every `state` encoding, for
        --validate to suggest a working one when the configured style returns
        nothing. Errors are reported as strings in place of a count.
        """
        results: dict = {}
        original = self.state_param_style
        try:
            for style in STATE_PARAM_STYLES:
                self.state_param_style = style
                try:
                    results[style] = len(self.fetch_events(space_map))
                except (requests.RequestException, CollegeNetError) as exc:
                    results[style] = f"error: {exc}"
        finally:
            self.state_param_style = original
        return results

    # ── discovery ────────────────────────────────────────────────────────────

    def discover_spaces(self, days: int) -> list:
        """
        Distinct {space_id, space_name} seen in events over the next `days`.

        Uses the same events endpoint as the sync — no extra API surface to
        validate — so it finds spaces that have bookings in the window. Spaces
        with no upcoming events won't appear; widen `days` to surface more.
        """
        now = datetime.now(self.tz)
        seen: dict = {}
        for root in self._pages({}, now, now + timedelta(days=days),
                                "discovering spaces"):
            self._collect_spaces_from(root, seen)
        return [{"space_id": sid, "space_name": name}
                for sid, name in sorted(seen.items(), key=lambda kv: kv[1].lower())]

    def _collect_spaces_from(self, root: ET.Element, into: dict) -> None:
        """Collect {space_id: space_name} from any element having a space_id
        child, preferring space_name then formal_name."""
        for elem in root.iter():
            sid = self._child_text(elem, "space_id")
            if sid:
                name = (self._child_text(elem, "space_name")
                        or self._child_text(elem, "formal_name") or sid)
                into.setdefault(sid, name)

    # ── paging ───────────────────────────────────────────────────────────────

    def _pages(self, extra_params: dict, start: datetime, end: datetime,
               context: str):
        """
        Yield each page's XML root for one query, stopping safely whatever
        the server does with the paging parameters.

        Three behaviours are handled:
          * paging honored — stop on the first short page;
          * page_size ignored, everything returned at once (a page longer
            than asked for) — stop, we already have it all;
          * page_size honored but the offset ignored, so the same first page
            comes back again — the results would be silently truncated, which
            would leave most of the campus unscheduled. That is an error, not
            something to paper over.
        Without these checks the second case re-fetched the same page up to
        MAX_PAGES times: 1,000 requests and 1,000 copies of every event.
        """
        offset = 0
        seen_ids: set = set()
        for _page in range(MAX_PAGES):
            params = {
                **extra_params,
                **self._window_params(start, end),
                **self._state_params(),
                "scope": "extended",                  # include setup/pre-event times
                "page_size": PAGE_SIZE,
                "page_offset": offset,
            }
            resp = self.session.get(f"{self.base_url}/events.xml", params=params,
                                    timeout=HTTP_TIMEOUT_FETCH)
            resp.raise_for_status()
            root = self._parse_xml(resp.text, context)
            events = root.findall("r25:event", R25_NS)
            ids = {ev.findtext("r25:event_id", "", R25_NS) or f"#{i}"
                   for i, ev in enumerate(events)}
            if offset and events and not (ids - seen_ids):
                raise CollegeNetError(
                    f"25Live returned the same {len(events)} event(s) for page "
                    f"offset {offset} as for an earlier page while {context} — "
                    "it is ignoring the page_offset parameter, so everything "
                    f"after the first {PAGE_SIZE} events would be silently "
                    "dropped. Check the paging parameters your Series25 "
                    "version expects before running live.")
            seen_ids |= ids
            yield root
            # Stop on the final (short) page. We deliberately do NOT rely on a
            # total-count element: if a response omits it, or names it
            # differently across API versions, stopping early would silently
            # drop every event past the first page.
            if len(events) != PAGE_SIZE:
                if len(events) > PAGE_SIZE:
                    logging.debug("25Live ignored page_size (%d events in one "
                                  "page); treating it as the full result.",
                                  len(events))
                return
            offset += PAGE_SIZE
        logging.warning("Reached MAX_PAGES (%d) while %s; results may be "
                        "truncated.", MAX_PAGES, context)

    # ── the fetch ────────────────────────────────────────────────────────────

    def fetch_events(self, space_map, now: Optional[datetime] = None) -> list:
        """
        All configured-state events for mapped spaces over the lookahead
        window, as RawEvents with per-space buffers already applied.

        The query starts at YESTERDAY's midnight: an overnight booking that
        began last night is still running when the nightly job fires, and if
        the API matches on start time a today-only query would miss it — and
        the rewrite would drop the rest of it mid-event. Windows are then
        clipped to start no earlier than today's midnight, so nothing is
        written for dates already past.
        """
        spaces = getattr(space_map, "spaces", space_map)
        today = self._today(now)
        end = (now or datetime.now(self.tz)) + timedelta(days=self.lookahead_days)

        unique: dict = {}
        for batch in self._chunk(list(spaces.keys()), SPACE_IDS_PER_REQUEST):
            for ev in self._fetch_batch(batch, today - timedelta(days=1), end, spaces):
                if ev.end <= today:
                    continue
                if ev.start < today:
                    ev = RawEvent(ev.event_id, ev.title, ev.space_id, today, ev.end)
                # An event booked in rooms from two different 50-id batches
                # comes back in both; a repeated reservation id does too.
                unique.setdefault((ev.event_id, ev.space_id, ev.start, ev.end), ev)

        raw_events = list(unique.values())
        logging.info("Fetched %d space-assignment(s) from 25Live across %d space(s)",
                     len(raw_events), len(spaces))
        return raw_events

    def _fetch_batch(self, space_ids: list, start: datetime, end: datetime,
                     spaces: dict) -> list:
        raw_events: list = []
        params = {"space_id": " ".join(space_ids)}          # space-separated IDs
        for root in self._pages(params, start, end, "fetching events"):
            for ev in root.findall("r25:event", R25_NS):
                try:
                    raw_events.extend(self._parse_event(ev, spaces))
                except Exception as exc:              # noqa: BLE001
                    eid = ev.findtext("r25:event_id", "?", R25_NS)
                    logging.warning("Skipping malformed event %s: %s", eid, exc)
        return raw_events

    def _find_space_ids(self, reservation: ET.Element) -> list:
        """
        Every distinct space_id under a reservation.

        Scans the whole subtree because API versions differ in how deeply
        space_reservation is nested, and de-duplicates because some versions
        repeat the id in both the reservation and its space detail — which
        would otherwise produce two identical RawEvents per booking.
        """
        ids: list = []
        for elem in reservation.iter():
            if self._local(elem.tag) == "space_id" and elem.text:
                sid = elem.text.strip()
                if sid and sid not in ids:
                    ids.append(sid)
        return ids

    def _reservation_cancelled(self, res: ET.Element) -> bool:
        """
        True when this occurrence is cancelled even though the event isn't.

        Cancelling one meeting of a recurring class leaves the event
        confirmed; only that reservation's state changes (99 = cancelled).
        Filtering on the event's state alone would keep conditioning the room
        for a meeting that isn't happening.
        """
        text = res.findtext("r25:reservation_state", namespaces=R25_NS)
        if not text or not self.exclude_reservation_states:
            return False
        try:
            return int(text) in self.exclude_reservation_states
        except ValueError:
            return False

    def _parse_event(self, ev: ET.Element, spaces: dict) -> list:
        """One <r25:event> into RawEvents — one per mapped space it touches."""
        event_id = ev.findtext("r25:event_id", namespaces=R25_NS) or "?"
        title = ev.findtext("r25:event_name", namespaces=R25_NS) or "Unnamed"

        # Client-side state filter. Always applied, not just when the server
        # ignored our parameter — with state_param_style: none it is the only
        # filter there is.
        state_txt = ev.findtext("r25:state", namespaces=R25_NS)
        if state_txt and self.include_states:
            try:
                if int(state_txt) not in self.include_states:
                    return []
            except ValueError:
                pass

        reservations = ev.find("r25:reservations", R25_NS)
        if reservations is None:
            return []

        results: list = []
        for res in reservations.findall("r25:reservation", R25_NS):
            if self._reservation_cancelled(res):
                continue
            start_txt = res.findtext("r25:event_start_dt", namespaces=R25_NS)
            end_txt = res.findtext("r25:event_end_dt", namespaces=R25_NS)
            if not (start_txt and end_txt):
                continue

            event_start = self._to_tz(start_txt)
            event_end = self._to_tz(end_txt)
            if event_end <= event_start:
                logging.warning("Event %s reservation ends at or before it "
                                "starts (%s -> %s); skipping.",
                                event_id, start_txt, end_txt)
                continue

            # 25Live's own setup/teardown times (present with scope=extended).
            native_pre_txt = (res.findtext("r25:setup_dt", namespaces=R25_NS)
                              or res.findtext("r25:pre_event_dt", namespaces=R25_NS))
            native_post_txt = (res.findtext("r25:takedown_dt", namespaces=R25_NS)
                               or res.findtext("r25:post_event_dt", namespaces=R25_NS))
            native_pre = self._to_tz(native_pre_txt) if native_pre_txt else event_start
            native_post = self._to_tz(native_post_txt) if native_post_txt else event_end

            for space_id in self._find_space_ids(res):
                sc = spaces.get(space_id)
                if sc is None:
                    continue
                # Effective window = earliest of (25Live setup, event - run-up)
                #                 to latest of  (25Live takedown, event + run-down)
                configured_start = event_start - timedelta(minutes=sc.pre_condition_minutes)
                configured_end = event_end + timedelta(minutes=sc.post_buffer_minutes)
                results.append(RawEvent(
                    event_id=event_id,
                    title=title,
                    space_id=space_id,
                    start=min(native_pre, configured_start),
                    end=max(native_post, configured_end),
                ))

        return results
