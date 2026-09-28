"""Offline tests: 25Live client."""

from datetime import datetime, timedelta

import requests

from tests.helpers import TZ, dt, space


def test_naive_25live_datetime_uses_configured_tz():
    """A naive 25Live timestamp is read in the configured campus timezone, not
    the server's. Regression for .astimezone() on a naive datetime, which
    shifted every booking when the job ran on a UTC host."""
    from bassync.collegenet import CollegeNetClient
    client = CollegeNetClient({"base_url": "http://x"}, TZ)
    d = client._to_tz("2026-06-10T09:00:00")
    assert d.tzinfo is not None and (d.hour, d.minute) == (9, 0), d
    assert d.utcoffset() == timedelta(hours=-4), d.utcoffset()
    client.close()


def test_state_param_styles():
    """Series25 instances differ in how they want the state filter, and getting
    it wrong returns 200 with zero events."""
    from bassync.collegenet import CollegeNetClient

    def client(style):
        return CollegeNetClient({"base_url": "http://x", "include_states": [2, 4],
                                 "state_param_style": style}, TZ)
    assert client("plus")._state_params() == {"state": "2+4"}
    assert client("comma")._state_params() == {"state": "2,4"}
    assert client("repeat")._state_params() == {"state": ["2", "4"]}
    assert client("none")._state_params() == {}


def test_parse_event_applies_buffers_and_dedupes_spaces():
    """Buffers widen the window, and a space id repeated in the XML yields ONE
    RawEvent rather than a duplicate per nesting level."""
    import xml.etree.ElementTree as ET

    from bassync.collegenet import CollegeNetClient
    client = CollegeNetClient({"base_url": "http://x", "include_states": [2]}, TZ)
    xml = """<r25:event xmlns:r25="http://www.collegenet.com/r25">
      <r25:event_id>E9</r25:event_id><r25:event_name>Chem 101</r25:event_name>
      <r25:state>2</r25:state>
      <r25:reservations><r25:reservation>
        <r25:event_start_dt>2026-06-10T09:00:00</r25:event_start_dt>
        <r25:event_end_dt>2026-06-10T10:00:00</r25:event_end_dt>
        <r25:space_reservation><r25:space_id>1</r25:space_id>
          <r25:space><r25:space_id>1</r25:space_id></r25:space>
        </r25:space_reservation>
      </r25:reservation></r25:reservations>
    </r25:event>"""
    sm = {"1": space(1, "room", "B/Rm1")}
    sm["1"].pre_condition_minutes = 30
    sm["1"].post_buffer_minutes = 15
    events = client._parse_event(ET.fromstring(xml), sm)
    assert len(events) == 1, events
    assert events[0].start == dt(8, 30, day=10), events[0].start
    assert events[0].end == dt(10, 15, day=10), events[0].end
    client.close()


def test_parse_event_honors_25live_setup_teardown():
    """When 25Live's own setup/takedown is wider than our buffers, it wins —
    the room really is in use for the setup crew."""
    import xml.etree.ElementTree as ET

    from bassync.collegenet import CollegeNetClient
    client = CollegeNetClient({"base_url": "http://x"}, TZ)
    xml = """<r25:event xmlns:r25="http://www.collegenet.com/r25">
      <r25:event_id>E1</r25:event_id>
      <r25:reservations><r25:reservation>
        <r25:event_start_dt>2026-06-10T09:00:00</r25:event_start_dt>
        <r25:event_end_dt>2026-06-10T10:00:00</r25:event_end_dt>
        <r25:setup_dt>2026-06-10T07:00:00</r25:setup_dt>
        <r25:takedown_dt>2026-06-10T12:00:00</r25:takedown_dt>
        <r25:space_id>1</r25:space_id>
      </r25:reservation></r25:reservations>
    </r25:event>"""
    sm = {"1": space(1, "room", "B/Rm1")}
    sm["1"].pre_condition_minutes = 30
    sm["1"].post_buffer_minutes = 15
    ev = client._parse_event(ET.fromstring(xml), sm)[0]
    assert ev.start == dt(7, day=10) and ev.end == dt(12, day=10), (ev.start, ev.end)
    client.close()


def test_parse_event_filters_by_state_client_side():
    """The state filter is applied to the response too, so `state_param_style:
    none` still only syncs confirmed events."""
    import xml.etree.ElementTree as ET

    from bassync.collegenet import CollegeNetClient
    client = CollegeNetClient({"base_url": "http://x", "include_states": [2]}, TZ)
    xml = """<r25:event xmlns:r25="http://www.collegenet.com/r25">
      <r25:event_id>E1</r25:event_id><r25:state>1</r25:state>
      <r25:reservations><r25:reservation>
        <r25:event_start_dt>2026-06-10T09:00:00</r25:event_start_dt>
        <r25:event_end_dt>2026-06-10T10:00:00</r25:event_end_dt>
        <r25:space_id>1</r25:space_id>
      </r25:reservation></r25:reservations></r25:event>"""
    assert client._parse_event(ET.fromstring(xml),
                              {"1": space(1, "room", "B/Rm1")}) == []
    client.close()


def test_html_error_page_is_reported_clearly():
    """A login page where XML was expected must say so, not raise a bare
    ParseError from deep in the stdlib."""
    from bassync.collegenet import CollegeNetClient, CollegeNetError
    client = CollegeNetClient({"base_url": "http://x"}, TZ)
    # Real HTML, with the unclosed tags that make it invalid XML.
    page = ('<!DOCTYPE html><html><head><meta charset="utf-8">'
            "<title>Sign in</title></head><body>Please sign in<br></body></html>")
    try:
        client._parse_xml(page, "fetching events")
    except CollegeNetError as exc:
        assert "not valid XML" in str(exc) and "Sign in" in str(exc), exc
        return
    finally:
        client.close()
    raise AssertionError("an HTML error page should raise CollegeNetError")


def test_wellformed_but_wrong_xml_is_caught_by_validate():
    """An SSO login page that happens to be valid XHTML parses fine and then
    yields zero events — which a live run would read as an empty campus. The
    connection check names the real cause instead."""
    from bassync.collegenet import CollegeNetClient
    client = CollegeNetClient({"base_url": "http://x"}, TZ)

    class FakeResponse:
        status_code = 200
        text = "<html><body>Please sign in with your NetID</body></html>"

    client.session.get = lambda *a, **kw: FakeResponse()
    ok, detail = client.check_connection()
    assert not ok, detail
    assert "SSO" in detail and "<html>" in detail, detail
    client.close()


# ── paging, de-duplication, cancellations, the fetch window ──────────────────

NOW = datetime(2026, 10, 7, 2, 0, tzinfo=TZ)          # a nightly run at 2 AM


def _event_xml(eid, space_id="1", start="2026-10-07T09:00:00",
               end="2026-10-07T10:00:00", reservation_state=None, state=2):
    rstate = (f"<r25:reservation_state>{reservation_state}</r25:reservation_state>"
              if reservation_state is not None else "")
    return (f"<r25:event><r25:event_id>{eid}</r25:event_id><r25:state>{state}</r25:state>"
            f"<r25:reservations><r25:reservation>{rstate}"
            f"<r25:event_start_dt>{start}</r25:event_start_dt>"
            f"<r25:event_end_dt>{end}</r25:event_end_dt>"
            f"<r25:space_reservation><r25:space_id>{space_id}</r25:space_id>"
            f"</r25:space_reservation></r25:reservation></r25:reservations></r25:event>")


def _doc(events):
    return f'<r25:events xmlns:r25="http://www.collegenet.com/r25">{"".join(events)}</r25:events>'


class FakeSession:
    """Answers events.xml from a function of the query parameters."""

    def __init__(self, respond):
        self.respond = respond
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(dict(params or {}))
        text = self.respond(params or {})

        class Resp:
            status_code = 200

            def raise_for_status(self):
                pass
        r = Resp()
        r.text = text
        return r

    def close(self):
        pass


def _client(respond, **cfg):
    from bassync.collegenet import CollegeNetClient
    c = CollegeNetClient({"base_url": "http://x", "include_states": [2], **cfg}, TZ)
    c.session = FakeSession(respond)
    return c


def _spaces(*ids):
    return {str(i): space(i, "room", f"T{i}") for i in ids}


def test_server_that_ignores_paging_is_fetched_once_without_duplicates():
    """Regression: 150 events used to become 1,000 requests and 150,000
    duplicate events, which also defeated the min_events safety check."""
    everything = _doc([_event_xml(i) for i in range(150)])
    c = _client(lambda p: everything)
    events = c.fetch_events(_spaces(1), now=NOW)
    assert len(c.session.calls) == 1
    assert len(events) == 150


def test_server_that_ignores_the_offset_fails_loudly():
    """page_size honored, page_offset ignored: the first 100 come back
    forever. Silently stopping would drop everything after event 100."""
    import pytest

    from bassync.collegenet import CollegeNetError
    first_page = _doc([_event_xml(i) for i in range(100)])
    c = _client(lambda p: first_page)
    with pytest.raises(CollegeNetError, match="page_offset"):
        c.fetch_events(_spaces(1), now=NOW)


def test_honored_paging_reads_every_page():
    def respond(p):
        off = int(p["page_offset"])
        return _doc([_event_xml(i) for i in range(off, min(off + 100, 150))])
    c = _client(respond)
    assert len(c.fetch_events(_spaces(1), now=NOW)) == 150
    assert [int(call["page_offset"]) for call in c.session.calls] == [0, 100]


def test_cancelled_occurrence_is_skipped():
    """One cancelled meeting of a confirmed recurring event must not
    condition the room."""
    doc = _doc([_event_xml("A", reservation_state=99),
                _event_xml("B", reservation_state=1)])
    events = _client(lambda p: doc).fetch_events(_spaces(1), now=NOW)
    assert [e.event_id for e in events] == ["B"]


def test_fetch_starts_yesterday_and_clips_to_today():
    """An overnight event that began last night is still running at 2 AM:
    it must be fetched and clipped to today's midnight, not dropped."""
    doc = _doc([
        _event_xml("overnight", start="2026-10-06T22:00:00", end="2026-10-07T06:00:00"),
        _event_xml("finished", start="2026-10-06T09:00:00", end="2026-10-06T10:00:00"),
    ])
    c = _client(lambda p: doc)
    events = c.fetch_events(_spaces(1), now=NOW)
    assert c.session.calls[0]["start_dt"] == "2026-10-06T00:00:00"
    assert [e.event_id for e in events] == ["overnight"]
    assert events[0].start == datetime(2026, 10, 7, 0, 0, tzinfo=TZ)


def test_event_in_two_space_batches_is_counted_once():
    """A multi-room event whose rooms fall in different 50-id batches is
    returned by both queries."""
    from bassync import collegenet
    ids = list(range(1, 61))                          # two batches: 50 + 10
    shared = _event_xml("shared", space_id="1")
    doc = _doc([shared])
    c = _client(lambda p: doc)
    events = c.fetch_events(_spaces(*ids), now=NOW)
    assert len(c.session.calls) == 2
    assert collegenet.SPACE_IDS_PER_REQUEST == 50
    assert len(events) == 1


def test_space_style_encodes_like_space_ids():
    import requests
    c = _client(lambda p: "", include_states=[2, 4], state_param_style="space")
    url = requests.Request("GET", "http://x/events.xml",
                           params=c._state_params()).prepare().url
    assert url.endswith("state=2+4"), url


def test_probe_reports_which_state_style_returns_events():
    doc = _doc([_event_xml("A")])

    def respond(p):
        return doc if p.get("state") == "2,4" else _doc([])
    c = _client(respond, include_states=[2, 4])
    counts = c.probe_state_styles(_spaces(1))
    assert counts["comma"] == 1 and counts["plus"] == 0 and counts["space"] == 0
    assert c.state_param_style == "plus"               # restored afterwards


# ── discovery ────────────────────────────────────────────────────────────────

def _discover_doc():
    """Three spaces, as different instances describe them: a nested <space>
    with details, a formal name only, and a building in a nested element."""
    return _doc([
        "<r25:event><r25:event_id>1</r25:event_id><r25:state>2</r25:state>"
        "<r25:reservations><r25:reservation><r25:space_reservation>"
        "<r25:space_id>101</r25:space_id><r25:space><r25:space_id>101</r25:space_id>"
        "<r25:space_name>SCI 101</r25:space_name>"
        "<r25:formal_name>Science Hall 101</r25:formal_name>"
        "<r25:max_capacity>40</r25:max_capacity>"
        "<r25:building_name>Science Hall</r25:building_name></r25:space>"
        "</r25:space_reservation></r25:reservation>"
        "<r25:reservation><r25:reservation_state>99</r25:reservation_state>"
        "<r25:space_reservation><r25:space_id>101</r25:space_id>"
        "</r25:space_reservation></r25:reservation>"
        "<r25:reservation><r25:space_reservation><r25:space_id>101</r25:space_id>"
        "</r25:space_reservation></r25:reservation></r25:reservations></r25:event>",
        "<r25:event><r25:event_id>2</r25:event_id><r25:state>2</r25:state>"
        "<r25:reservations><r25:reservation><r25:space_reservation>"
        "<r25:space_id>102</r25:space_id><r25:formal_name>Room B Formal</r25:formal_name>"
        "<r25:building><r25:building_name>Art Center</r25:building_name></r25:building>"
        "</r25:space_reservation></r25:reservation></r25:reservations></r25:event>",
        # Tentative, and the sync includes confirmed only: listed, not counted.
        "<r25:event><r25:event_id>3</r25:event_id><r25:state>1</r25:state>"
        "<r25:reservations><r25:reservation><r25:space_reservation>"
        "<r25:space_id>103</r25:space_id>"
        "</r25:space_reservation></r25:reservation></r25:reservations></r25:event>",
    ])


def test_discover_describes_each_space_and_counts_its_bookings():
    c = _client(lambda p: _discover_doc())
    spaces, every = c.discover_spaces(30, every_space=False)
    assert every is False
    found = {s["space_id"]: s for s in spaces}
    assert found["101"] == {"space_id": "101", "space_name": "SCI 101",
                            "formal_name": "Science Hall 101", "capacity": 40,
                            "building": "Science Hall", "bookings": 2}
    assert found["102"]["space_name"] == "Room B Formal"
    assert found["102"]["building"] == "Art Center"
    assert found["102"]["capacity"] is None and found["102"]["bookings"] == 1
    assert found["103"]["space_name"] == "103" and found["103"]["bookings"] == 0
    # Sorted by name, and asked of every space (no space_id filter).
    assert [s["space_id"] for s in spaces] == ["103", "102", "101"]
    assert "space_id" not in c.session.calls[0]


def test_discover_merges_what_each_page_says_about_a_space():
    """A name that only appears on a later page still reaches the space."""
    pages = {0: _doc([_event_xml(i, space_id="7") for i in range(100)]),
             100: _doc(["<r25:event><r25:event_id>x</r25:event_id><r25:reservations>"
                        "<r25:reservation><r25:space_reservation><r25:space_id>7</r25:space_id>"
                        "<r25:space_name>Late Name</r25:space_name></r25:space_reservation>"
                        "</r25:reservation></r25:reservations></r25:event>"])}
    c = _client(lambda p: pages[int(p["page_offset"])])
    [only], _every = c.discover_spaces(30, every_space=False)
    assert only["space_name"] == "Late Name" and only["bookings"] == 101


class RoutedSession(FakeSession):
    """Answers each endpoint from its own function; one that raises is an
    HTTP error from that endpoint."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, params=None, timeout=None):
        endpoint = url.rsplit("/", 1)[-1]
        self.calls.append({"_endpoint": endpoint, **dict(params or {})})
        answer = self.routes[endpoint](params or {})

        class Resp:
            status_code = 200 if isinstance(answer, str) else answer

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise requests.HTTPError(f"{self.status_code} Client Error")
        r = Resp()
        r.text = answer if isinstance(answer, str) else ""
        return r


def _spaces_doc(*spaces):
    return ('<r25:spaces xmlns:r25="http://www.collegenet.com/r25">'
            + "".join(f"<r25:space><r25:space_id>{sid}</r25:space_id>"
                      f"<r25:space_name>{name}</r25:space_name>"
                      f"<r25:max_capacity>{cap}</r25:max_capacity></r25:space>"
                      for sid, name, cap in spaces)
            + "</r25:spaces>")


def test_discover_lists_every_space_even_without_bookings():
    c = _client(lambda p: "")
    c.session = RoutedSession({
        "spaces.xml": lambda p: _spaces_doc(("101", "SCI 101", 40), ("900", "Storage 9", 2)),
        "events.xml": lambda p: _discover_doc(),
    })
    spaces, every = c.discover_spaces(30)
    found = {s["space_id"]: s for s in spaces}
    assert every is True
    assert found["900"] == {"space_id": "900", "space_name": "Storage 9", "formal_name": "",
                            "capacity": 2, "building": "", "bookings": 0}
    assert found["101"]["bookings"] == 2 and found["101"]["capacity"] == 40
    assert set(found) == {"101", "102", "103", "900"}      # booked ones merge in
    listing = c.session.calls[0]
    assert listing["_endpoint"] == "spaces.xml"
    assert "start_dt" not in listing and "state" not in listing and "scope" not in listing


def test_a_listing_that_fails_falls_back_to_the_booked_spaces(caplog):
    c = _client(lambda p: "")
    c.session = RoutedSession({"spaces.xml": lambda p: 403,
                               "events.xml": lambda p: _discover_doc()})
    spaces, every = c.discover_spaces(30)
    assert every is False and {s["space_id"] for s in spaces} == {"101", "102", "103"}
    assert "Couldn't list every space" in caplog.text


def test_a_listing_that_ignores_the_offset_falls_back_rather_than_truncating():
    first = _spaces_doc(*[(str(i), f"Room {i}", 1) for i in range(100)])
    c = _client(lambda p: "")
    c.session = RoutedSession({"spaces.xml": lambda p: first,
                               "events.xml": lambda p: _discover_doc()})
    spaces, every = c.discover_spaces(30)
    assert every is False and len(spaces) == 3


def test_a_listing_is_paged_like_the_events():
    def page(p):
        off = int(p["page_offset"])
        return _spaces_doc(*[(str(i), f"Room {i}", 1) for i in range(off, min(off + 100, 130))])
    c = _client(lambda p: "")
    c.session = RoutedSession({"spaces.xml": page, "events.xml": lambda p: _doc([])})
    spaces, every = c.discover_spaces(30)
    assert every is True and len(spaces) == 130
    offsets = [int(call["page_offset"]) for call in c.session.calls
               if call["_endpoint"] == "spaces.xml"]
    assert offsets == [0, 100]


def test_spaces_nested_in_a_space_do_not_make_a_page_look_complete():
    """A page of 100 spaces, each naming a related space inside it, is still
    a full page: the next one is asked for."""
    def page(p):
        off = int(p["page_offset"])
        items = [f"<r25:space><r25:space_id>{i}</r25:space_id><r25:space_name>R{i}</r25:space_name>"
                 f"<r25:related><r25:space_id>{i + 5000}</r25:space_id></r25:related></r25:space>"
                 for i in range(off, min(off + 100, 150))]
        return '<r25:spaces xmlns:r25="http://www.collegenet.com/r25">' + "".join(items) + "</r25:spaces>"
    c = _client(lambda p: "")
    c.session = RoutedSession({"spaces.xml": page, "events.xml": lambda p: _doc([])})
    spaces, every = c.discover_spaces(30)
    assert every is True
    assert [int(call["page_offset"]) for call in c.session.calls
            if call["_endpoint"] == "spaces.xml"] == [0, 100]
    assert {str(i) for i in range(150)} <= {s["space_id"] for s in spaces}
