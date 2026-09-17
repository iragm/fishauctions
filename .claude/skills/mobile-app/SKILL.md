---
name: mobile-app
description: The mobile app's navigation drawer and the OAuth connect flows that throw a user out to /login/. Use when touching auctions/mobile/, the /api/mobile/config/ payload, app links, or anything a user reaches from inside the app.
---

# The app

## Drawer

`GET /api/mobile/config/` carries `menu`, built per user by `auctions/mobile/menu.py`, so the drawer
changes without an app release.

- Not rendered from `base.html`. `test_mobile_menu.py` renders the navbar and fails if a link is
  missing from the payload. Web-only links go in `WEB_ONLY_PATHS` with a reason.
- Sign out, offline mode, Tap to Pay and clubs are the app's own rows, merged by section `id`.
- `menu` is the only per-user block, so `MobileConfigView` authenticates optionally. Anonymous gets a
  200. **Never `cache_page` it.**
- Paths are site-relative; query strings matter. Off-host URLs are dropped.

## Connect flows

Connecting Square, PayPal, Mailchimp, Google Calendar or Discord from the app lands on `/login/`. The
fix is app-side: mint `POST /api/mobile/auth/web-session/`, open `handoff_url + "&next=<connect url>"`.
Server halves done: callbacks in `app_links.IOS_EXCLUDED_PATHS`, `HANDOFF_TTL_SECONDS` = 300.
`docs/app_oauth_connect_flows.md` has the open items.
