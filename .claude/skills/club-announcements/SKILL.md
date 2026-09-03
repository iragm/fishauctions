---
name: club-announcements
description: Club announcements and the website integration: delivery channels, the five embeds, calendar links and generated event wording. Use when touching auctions/announcements.py, auctions/club_events.py, auctions/google_calendar.py, auctions/views/embeds.py or the club website page.
---

# Club announcements and website integration

`auctions/announcements.py` delivers; `/clubs/<slug>/announcements/` is where an admin writes one,
behind `permission_send_announcements`. Channels: Discord, push, an email campaign through the
club's own Mailchimp or Brevo, and the club's website. The Discord channel is set in Discord with
`/announcements_here` (a second channel from `/auctions_here`).

- **Every channel carries the whole announcement.** It has no page of its own and nothing links to
  one.
- `ClubAnnouncement.website_views` counts **renders** (the club page here plus every format of the
  embed, admins excluded) — an impression, not a read.
- Email always goes as a **campaign** addressed to the provider's list, from a Celery task, never
  through this site's mail server. Nobody types a from address (Mailchimp's `campaign_defaults` /
  Brevo's verified senders; the same read fills in `Club.donation_mailing_address` when blank) and
  nobody types a subject — it is always `"<Club> announcement"`. Mailchimp and Brevo are two
  checkboxes but **only one may be ticked**. Only a connected provider is offered. The form opens
  with nothing ticked, the website box included.
- **Nothing is delivered in the request.** One with no time on it is scheduled
  `announcements.GRACE_SECONDS` (30) out; an explicit schedule is the same path with a longer wait.
  `sent_at`, not `scheduled_for`, is the column everything public filters on. Retracting stops one
  that hasn't gone, deletes the Discord post, takes it off the website, then says which channels it
  could not reach. Send and retraction each write a `ClubHistory` row under `ANNOUNCEMENTS`.
- `docs/club_announcements.md` has the whole design.

## The website page and embeds

`/clubs/<slug>/website/` holds everything a club can put on its own site: the event calendar, past
events, the current auction, the latest announcement, the BAP leaderboard, plus a Calendar links
card. Snippets are listed whether or not the feature behind them is switched on, with a note.

- The five embeds share one shell (`auctions/templates/auctions/embeds/`); each has a styled
  template and an `_unstyled` one. `embed_mode_from_request` / `embed_response` in `views.py` are
  the one reader of `?format=`.
- `ClubPastEventsEmbedView` subclasses `ClubEventsEmbedView` and changes three class attributes —
  deliberately the same `events.html`, row shape and `_club_events_embed_rows`.
- **The embed measures itself and the snippet listens.** Every styled embed posts
  `{clubEmbed: "height", height: N}` to `window.parent` (on load, on resize, through a
  `ResizeObserver`); `website_snippet.html` hands over the listener *inside the same `<pre>`*. The
  listener checks `event.origin` against this site and matches frames on `event.source`. The
  `height` in the snippet is only the starting size.
- Calendar links is **not** an embed: two plain addresses following `Club.calendar_subscribe_url` /
  `.calendar_feed_url` — **the club's Google calendar when it is shared, ours when it isn't**. The
  same rule picks the Google button on the club page and the "Add our calendar" link in membership
  emails. The subscribe link is `webcal://` when it falls back to us (an `https` `.ics` is a
  download, which most calendar apps import as a frozen snapshot).
- **Whether that calendar is shared is read, never asked.** `google_calendar.refresh_public_flag`
  fetches the calendar's public `.ics` anonymously (200 = really shared) at the end of every
  `sync_club`, at most hourly (`PUBLIC_CHECK_INTERVAL`, stamped in
  `google_calendar_public_checked`); **Sync now** forces it, `disconnect()` forgets it, and failing
  to reach Google leaves the flag alone. We cannot *set* sharing (needs the sensitive
  `calendar.acls` scope).

## Generated event wording

`ClubEvent.title_is_custom` / `description_is_custom` stop `sync_one_auction_event` and
`sync_pickup_events` overwriting a hand-typed field; `title` and `description` still hold the value
everything displays. `_apply_event_item` refuses Google-side edits to automatic events.
`club_events.generated_wording` recomputes what the site would have written (help text and reset).
`ClubEventForm` narrows itself to those two fields when `instance.is_automatic`, so dates, location,
cancellation and delete stay with the auction (`is_editable` gates delete; `details_are_editable`
guards the form). `docs/club_event_details.md` has the whole design.

- `Club.events_website_views` / `events_website_last_view` count renders of the events embed (every
  `?format=` including JSON); `Club.embeds_events_on_website` is a render inside
  `EVENTS_EMBED_ACTIVE_DAYS` (90). Counted on the **club**, not on a row; the club page here is not
  counted, and an admin's own view is not counted.
- `Auction.event_needing_custom_wording` is the one reader and puts a banner beside the setup
  checklist (outside its if/else). Dismissing writes `Auction.dismissed_customize_event_banner`,
  deliberately not in `AUCTION_FIELDS_TO_CLONE`.
- There is deliberately **no per-event "add this to my calendar" link** on the club page's event
  list. The pickup-time buttons on the auction page are a different thing and stay.
