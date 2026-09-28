[Documentation](README.md) › 25Live setup

# 25Live setup

The sync only ever **reads** from 25Live, through Series25 WebServices.

- Create a **local** service account (not SSO) with read access to the relevant
  events and locations, and enable Series25 WebServices for it. Its password is
  `BAS_25LIVE_PASSWORD`, or set it on the web UI's Connection page.
- Set `collegenet.instance` (CollegeNET-hosted) or `collegenet.base_url`
  (self-hosted).
- The request filters confirmed events by the numeric `state` parameter
  (`include_states`, default `[2]`, confirmed; add `4` for tentative).
  **Instances differ in how they want it encoded** — set
  `collegenet.state_param_style` to `plus` (default), `space`, `comma`,
  `repeat`, or `none` (filter client-side). Getting this wrong returns 200 with
  zero events; `--validate` catches that and names a style that works.
- Individually cancelled occurrences of a recurring event (reservation state
  99) are skipped; `collegenet.exclude_reservation_states` changes the list.
- The fetch starts at yesterday's midnight, so an overnight booking that began
  last night is still scheduled for the rest of it this morning.
- Paging is guarded: if an instance ignores the paging parameters, the sync
  either takes the single full response or — if it would silently truncate —
  fails the fetch with a message, instead of re-requesting the same page.
- Find a space's numeric `space_id` from its detail-page URL in 25Live, with
  *Discover spaces* on the web UI, or with `--discover`. The web UI's
  [Room map → From 25Live](web-ui.md#adding-rooms-from-25live) lists the
  spaces grouped by building, with each one's capacity and number of bookings,
  and adds them a building at a time.
- Discovery lists **every space** the service account can see from
  `spaces.xml`, booked or not, and counts each one's bookings from the same
  `events.xml` the sync reads. If your instance or account won't list spaces —
  or pages the list in a way the sync doesn't recognise — it falls back to the
  spaces with bookings in the window, and says so. What the account can see is
  set by its 25Live security; a space it can't see can still be added by its
  `space_id`. Where either response includes a space's building, it's used;
  otherwise the building is guessed from the space's name, for you to check.

A 25Live that returns nothing at all — an expired account, a changed `state`
parameter — looks exactly like an empty campus. The
[safety rails](safety.md) exist for that.
