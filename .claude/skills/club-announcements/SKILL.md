---
name: club-announcements
description: Club announcements and the website integration: delivery channels, the five embeds, calendar links and generated event wording. Use when touching auctions/announcements.py, auctions/club_events.py, auctions/google_calendar.py, auctions/views/embeds.py or the club website page.
---

# Club announcements and website integration

`auctions/announcements.py` delivers; `/clubs/<slug>/announcements/` writes, behind
`permission_send_announcements`. Channels: Discord (`/announcements_here`, a separate channel from
`/auctions_here`), push, a Mailchimp or Brevo campaign, and the club's website.

- **Every channel carries the whole announcement.** No page of its own, no "read more" link.
- Channels are stored as chosen, so disconnecting Discord later doesn't rewrite history.
- Push is in `PUSH_ONLY_CATEGORIES`: no email fallback. Recipients skip `do_not_contact` but not
  "no non-essential emails".
- `website_views` counts renders, not reads, admins excluded. `email_opens` is the only real read
  receipt, pulled in the background.
- Email is always a **campaign** to the provider's list, from a Celery task. Never through our mail
  server and never a transactional send: both bypass the provider's unsubscribe list.
- Nobody types a from address (provider's own sender; the same read fills a blank
  `Club.donation_mailing_address`) or a subject (`"<Club> announcement"`). No unsubscribe link of
  ours. No template editor.
- Mailchimp and Brevo: only one may be ticked (members are synced to both). Only a connected provider
  is offered. The form opens with nothing ticked.
- **Nothing is delivered in the request.** Unscheduled means `GRACE_SECONDS` (30) out, so Retract
  works. `sent_at`, not `scheduled_for`, gates everything public. `deliver()` stamps `sent_at`
  before any channel, and `send_due` claims rows with a conditional UPDATE, so overlapping ticks
  can't double-send.
- Retract cancels, deletes the Discord post, drops it from the website, then says what it couldn't
  recall. Sent, scheduled and retracted each write a `ClubHistory` row under `ANNOUNCEMENTS`.

## The website page and embeds

`/clubs/<slug>/website/`: event calendar, past events, current auction, latest announcement, BAP
leaderboard, and calendar links. Snippets are listed even when the feature is off.

- The five embeds share `auctions/templates/auctions/embeds/`, each styled and `_unstyled`.
  `embed_mode_from_request` / `embed_response` are the one reader of `?format=`.
- `ClubPastEventsEmbedView` subclasses `ClubEventsEmbedView`, changing three attributes.
- **The snippet is a bare `<script src="…?format=js">`.** It replaced an iframe because WordPress
  rewrites `&&` to `&#038;&#038;`. Iframe formats are still served for old snippets.
- Calendar links follow `Club.calendar_subscribe_url` / `.calendar_feed_url`: the club's Google
  calendar when shared, ours (`webcal://`) when not.
- **Sharing is read, never asked.** `refresh_public_flag` fetches the public `.ics` anonymously,
  at most hourly. We can't set sharing (needs `calendar.acls`).

## Generated event wording

An auction's calendar event can have a custom title and description (migration 0406).

- `ClubEvent.title_is_custom` / `description_is_custom` stop the sync overwriting; `title` and
  `description` still hold what's displayed. **Not override columns:** eight readers would each
  need to learn a `display_title`.
- `ClubEventForm` narrows to those two fields when `is_automatic`. Dates, location, cancellation and
  delete stay with the auction. A flag is set only when the value differs from
  `club_events.generated_wording`. Reset beats text typed in the same save.
- `_apply_event_item` ignores Google-side edits to automatic events, on purpose.
- `Auction.event_needing_custom_wording` drives a banner; dismissal is not in
  `AUCTION_FIELDS_TO_CLONE`.
- `Club.events_website_views` counts events-embed renders on the club, not per row.
- No per-event "add to my calendar" link on the club page.
