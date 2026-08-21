# Editing an auction event's title and description

**Status: built** (Aug 2026, migration `0406`). Kept because the shape was the whole decision and
the reasoning behind it is not visible in the diff.

## The problem it solved

A club's monthly meeting *is* the auction. The calendar entry for it read:

    Spring Auction
    In-person auction.

…because `club_events.sync_one_auction_event` writes `auction.title` into `ClubEvent.title` and
`_auction_description(auction)` into `ClubEvent.description`, and rewrites both every time the
auction is saved and again on the periodic backstop. What the club wants it to read is:

    Spring Auction — April meeting
    Doors at 6:30, auction at 7. Bring a dish. Speaker beforehand: Jane on rainbowfish.

There was nowhere to type that, and the three places somebody tries all failed:

* **Google Calendar.** `google_calendar._apply_event_item` still returns early on
  `existing.is_automatic` — "generated events are owned by the auction" — so the edit is dropped
  on the next pull, and the next push writes ours back over it. This is the one the club actually
  tried.
* **The event edit form.** `ClubEventUpdateView.dispatch` used to raise `Http404` when
  `not self.event.is_editable`. This is the one that changed: the guard is now
  `details_are_editable`, and the form narrows itself instead of slamming the door.
* **The auction's rules.** Wrong document. The rules are a standing contract about how the auction
  runs; the meeting details change every month, and nobody wants a version of the rules per
  meeting.

## The shape: two "this one is ours" flags, not two override columns

On `ClubEvent`:

```python
title_is_custom = models.BooleanField(default=False)
description_is_custom = models.BooleanField(default=False)
```

`title` and `description` keep meaning exactly what they mean now — **the value everything
displays**. The flags only tell `sync_one_auction_event` to keep its hands off:

```python
changed = (
    (not event.title_is_custom and event.title != auction.title)
    or (not event.description_is_custom and event.description != description)
    or event.date_start != start
    ...
)
```

…and the assignment block skips a flagged field the same way.

The obvious alternative is `title_override` / `description_override` columns with the effective
value behind a property. **Don't.** It moves every reader onto `event.display_title`, and there are
eight of them: the club page list, the events embed rows, `ClubEventsICalView`,
`google_calendar._event_body` (twice — `summary` and the `source.title`), `discord_events`,
`tasks.next_event_fragment`, `palette_actions._club_events`, and `__str__`. Miss one and the
club page says "Spring Auction — April meeting" while the .ics the members subscribed to says
"Spring Auction", which is a bug nobody notices for a month. With flags, every read site is
untouched and correct by construction; the only code that learns anything new is the one function
that was doing the overwriting.

The cost is that "what would the automatic value be?" has to be recomputed rather than read. That
is `auction.title` and `club_events._auction_description(auction)` — two cheap calls, and only the
form needs them.

## Where the edit happens

Reuse `club_event_edit`. Adding a URL costs two catalogue entries (`palette_routes` /
`palette_actions`) and this is the same act on the same object.

* `ClubEvent.is_editable` stays as it is — it means "everything about this event is yours", and for
  an auction event it is still false.
* `ClubEvent.details_are_editable` is `True` for every event, i.e. the guard in
  `ClubEventUpdateView.dispatch` asks "can I edit *anything* here", which is always yes for an
  event this club owns. `is_editable` still gates delete, so a generated event's Delete button is
  gone from the template *and* the POST branch 404s — the auction is what put it there, and
  deleting the row would only mean the next sync rebuilt it.
* `ClubEventForm.__init__` narrows itself when `instance.is_automatic`: fields are `title` and
  `description` only. No dates, no location, no cancel, no Delete button — those belong to the
  auction, and an event whose date disagrees with its auction is worse than no feature. `save()`
  sets each flag only when the value actually differs from the generated one, so typing the
  auction's own words back in is not a custom value and does not silently stop the event
  following a later rename.
* Each field has a **Reset** checkbox — "use the auction's title instead" — that clears the flag
  and restores the generated wording *in the same save*, rather than waiting up to fifteen minutes
  for the next sync to do it. Reset beats anything typed in the box above it: ticking it and
  editing the text in one save says two things, and reset is the one there is no other route to.
* Help text under each field shows the generated value verbatim, so somebody knows what they are
  replacing.

On the club page's event list, an automatic event currently gets one button, "Edit auction". It
gets two: **Edit details** (this form) and **Edit auction** (dates, location, promotion). Say which
is which in the tooltip — "the name and the notes on the calendar" versus "when and where it is".

## What stays owned by the auction, and why

Dates, location, cancellation, and existence. An auction that moves has to move its calendar entry,
an auction that is unpromoted has to lose it, and a club that could pin a date here would have a
calendar that disagrees with the auction page, the pickup times and the invoice deadlines. The
feature is "say more about it", not "detach it".

## Pickup events

They get the same two flags for free, and should be allowed to use them — "Pickup — swap table
open" is a real thing a club wants to say. The button appears on them for the same reason.

## Interaction with Google

Once a field is flagged, the pull path is *still* the right place to stop: leave
`_apply_event_item`'s `is_automatic` early return alone. Editing in Google would then set the value
without a person ever having decided the field is custom, so an accidental drag or a typo becomes
permanent with no obvious way back. The form is the place, and the form should say so in one line:
*"Edits made in Google Calendar to an auction's event don't stick — make them here."*

## Recurring meetings

Out of scope, and worth saying so to whoever asks next: if the meeting is monthly and the auction
is one-off, this only dresses up the month the auction is in. Next month's meeting is an ordinary
manual (or Google) event, which is what those are for.

## Tests

In `auctions/test_club_events.py`, split between `AuctionMirroringTests` (the sync half) and
`ClubEventViewTests` (the form half).

* Renaming the auction leaves a custom title alone, and still moves the date.
* Clearing the flag restores the auction's title on the next sync.
* The same, through the periodic backstop (`sync_auction_events`) and not just the post_save signal.
* The custom title reaches Google (`_event_body`), the .ics feed, the embed and the membership
  email — the read sites that would have broken under the override-column design.
* An automatic event's edit form offers no date, location, cancel or delete.
