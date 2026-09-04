---
name: mobile-app
description: The mobile app's navigation drawer and the OAuth connect flows that throw a user out to /login/. Use when touching auctions/mobile/, the /api/mobile/config/ payload, app links, or anything a user reaches from inside the app.
---

# The app's navigation drawer

`GET /api/mobile/config/` carries a `menu` block: the app's drawer, built by `auctions/mobile/menu.py`
and gated per user. It was a hand-copied mirror of the navbar compiled into the app, so it could only
follow a navbar change through an app-store release, and two things could never be in it at all --
the superuser **Admin** menu and the **About site** link, because *who may see them* is a server
question.

- Deliberately **not** rendered from `base.html`. The drawer and the navbar are different surfaces:
  no Clubs dropdown (the app builds that from `clubs/mine/`), no sign-in pair, an order chosen for a
  phone. `auctions/test_mobile_menu.py` is what keeps them in step instead -- it renders the real
  navbar for a user, pulls the account/admin/about dropdown links back out of the HTML and fails if
  one is missing from that user's payload. A deliberately web-only link goes in `WEB_ONLY_PATHS`
  there, with a reason.
- **Four rows are the app's and are never sent**: sign out (it clears the JWT pair, the WebView
  cookie jar, the cached profile, the offline files and the Square authorization), offline mode and
  Tap to Pay (native screens), and clubs. The app merges them into `main` and `account` by section
  `id`.
- `menu` is the **only per-user block** in that response, which is why `MobileConfigView` now
  authenticates *optionally* (`mobile/authentication.py`): a bearer token personalises it, an
  anonymous or stale-token fetch still gets a 200 and the public menu, because the app reads this
  endpoint before sign-in to wire up Square, Firebase and the social buttons. Never put a blanket
  `cache_page` on it.
- Paths are site-relative and query strings are load-bearing (`?days=30`). An absolute URL on
  another host is dropped, not followed -- the same rule `terms_url` lives under.

Connecting an outside account (Square, PayPal, Mailchimp, Google Calendar, Discord) from inside the
app throws the user out to `/login/` and reads as a sign-out: the app opens those connect URLs in an
auth session (`ASWebAuthenticationSession` / Chrome Auth Tab) that shares no cookies with its
WebView, so `LoginRequiredMixin` fires. **The fix is app-side** — mint a handoff
(`POST /api/mobile/auth/web-session/`) and open `handoff_url + "&next=<connect url>"`; consuming it
also sets `mark_session_opened_by_app`, which is what lights up the `fishauctions-oauth://` exit.
Two server halves are done: the OAuth callback paths are excluded from Universal Links
(`app_links.IOS_EXCLUDED_PATHS`, checked against the URLconf by `test_app_links`), and
`HANDOFF_TTL_SECONDS` is 300 rather than 60, because the OS draws its consent sheet *before*
fetching the handoff URL and an expired token redirects to `/login/` — the very symptom.
`docs/app_oauth_connect_flows.md` has the diagnosis, what is done, and what was rejected.
