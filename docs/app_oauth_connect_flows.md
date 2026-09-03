# Why "connecting anything" signs you out of the app

The report: inside the app, tapping **Connect** on Square, PayPal, Mailchimp, Google Calendar or
Discord throws the user out to the web, where they are asked to sign in — and afterwards the app
behaves as though they were signed out.

Investigated 2026-09-02 against `ira-development`. Every claim below was read out of this repo and
the file:line references are the evidence.

**Status.** Two server changes are done (server items 6 and the first half of 9, marked **DONE**
below) — they are the half the app repo cannot make, and they are what stops the app-side fix being
flaky. Server item 5 is **rejected**, deliberately. Everything else is open, and the fix for the
reported symptom is app-side item 2.

## What is actually happening

### 1. The connect views require a session, and the app opens them somewhere that has none

`SquareConnectView` ([auctions/views/payments.py](../auctions/views/payments.py)) is `LoginRequiredMixin`, and its own
comment four lines down says the app opens this URL "in a browser view that carries no session of
ours". Both statements are true, which is the bug: `LoginRequiredMixin` runs in `dispatch()`, so the
`?return_to_app=1` handling underneath it is never reached by an anonymous request. What the browser
view gets is `302 → /login/?next=…` — the site's sign-in page, opened from inside a signed-in app.

The same shape holds for every connect flow:

| Flow | Connect view | Callback | Off-host authorize URL |
|---|---|---|---|
| Square | `SquareConnectView` | `SquareCallbackView` | `connect.squareup.com/oauth2/authorize` |
| PayPal | `PayPalConnectView` | `PayPalCallbackView` | PayPal `action_url` |
| Mailchimp | `MailchimpConnectView` | `MailchimpCallbackView` | `login.mailchimp.com/oauth2/authorize` |
| Google Calendar | `GoogleCalendarConnectView` | `GoogleCalendarCallbackView` | `accounts.google.com/o/oauth2/v2/auth` |
| Discord bot | `ClubDiscordConfigView` | — | `discord.com/oauth2/authorize` |

The Square and PayPal pairs are in `auctions/views/payments.py`; Mailchimp and Google Calendar are in
`auctions/views/club_integrations.py`; the Discord one is in `auctions/views/discord.py`. Views are
named rather than given line numbers on purpose -- a line number in a document is wrong by the next
commit, and `docs/module_map.md` will find any of these by name.

`?return_to_app=1`, `session_opened_by_app` and the `fishauctions-oauth://` exit exist for **Square
only** (`views/club_integrations.py`, `views/club_integrations.py`, `square_connected_app.html`). The other four have none of
it, so even when they work they end on a web page the merchant has no way out of.

### 2. All the OAuth state lives in the Django session, so a second cookie jar cannot finish the trip

`GOOGLE_CALENDAR_OAUTH_CLUB_SESSION_KEY` + a per-attempt `state` nonce (`views.py:12869-12872`),
`MAILCHIMP_OAUTH_CLUB_SESSION_KEY` (`views/club_integrations.py`), `_stash_club_for_payment_oauth`
(`views.py:922-936`). Signing in again inside the browser view does not help: the state was written
to the *other* session. The callback then takes the "Your Google Calendar connection session
expired. Please try again." branch (`views.py:12886-12889`, `views.py:12547-12550`) and redirects to
`home` — which reads as "it signed me out and dumped me on the front page", and loops forever.

### 3. The site claims every path as a Universal Link, so the OAuth *return* can be stolen back

`IOS_EXCLUDED_PATHS` was `["/.well-known/*"]` then a `/*` catch-all (`app_links.py`), and
`handle_all_urls` on Android (`app_links.py:69`). The module docstring weighs exactly this risk and
concludes "there is nothing today" — that was true before these callbacks existed. If the app opens
the flow in anything other than `ASWebAuthenticationSession` (a plain `url_launcher`, a Custom Tab
without path exclusions), the OS hands `/square/onboard/success/`, `/paypal/onboard/success/`,
`/mailchimp/callback/` or `/google-calendar/callback/` to the app and the browser session dies
mid-flow.

### 4. Google refuses to authenticate in an embedded WebView

The codebase already knows: `disallowed_useragent` is why social login is hidden for the app's user
agent in `account/login.html:12-18` and `socialaccount/connections.html:47-52`. Google **Calendar**
connect has no such guard — `club_google_calendar_settings.html:26` draws the button
unconditionally, as do `club_mailchimp_settings.html:22`, `square_seller.html:47,57,132`,
`club_membership_settings.html:96` and `auction_ribbon.html:85`.

### 5. Two things that make it *look* worse than it is

- **Refresh rotation.** `ROTATE_REFRESH_TOKENS` + `BLACKLIST_AFTER_ROTATION` (`settings.py:1222`).
  Two concurrent refreshes on resume and the second is a 401. An OAuth detour is exactly the long
  background excursion that ages out the 60-minute access token, so everything 401s at once on
  return.
- **The refresh throttle is keyed by IP.** `MobileTokenRefreshView` uses the `mobile_auth` scope at
  10/min (`mobile/views.py:1182`, `settings.py:1189`), and simplejwt's `TokenViewBase` sets
  `authentication_classes = ()`, so `ScopedRateThrottle` falls back to the IP. One venue's Wi-Fi
  shares a single bucket with `auth/login/` and the handoff endpoints.

### Ruled out

- `SESSION_COOKIE_SAMESITE = "Lax"` (`settings.py:472`) is not implicated: all four callbacks are
  top-level cross-site **GET** navigations, which Lax allows. Do not loosen it to `None`.
- `/o/` (the MCP OAuth server) clobbers nothing — separate tables, and the app never initiates it.
- allauth is not doing anything hostile on connect. `ACCOUNT_LOGOUT_ON_PASSWORD_CHANGE` is unset and
  the only `/logout/` link on the site is a POST form in the navbar, hidden in-app.

## What the server already does right — do not reimplement it

- **The WebView handoff pair is complete and correct, and already supports `?next=`.**
  `POST /api/mobile/auth/web-session/` mints a single-use 60-second token
  (`mobile/services/web_session.py`); `GET …/consume/?t=…&next=…` logs the browsing context in and
  honours a same-host `next` (`mobile/views.py:1200-1245`). It is simply not being used on the
  connect leg. **The highest-value fix on this whole page needs no server change.**
- `session_opened_by_app` already solves "the browser view sends Safari's User-Agent"
  (`web_session.py:19-27`). Don't re-derive app-ness from the UA inside a connect flow.
- `?return_to_app=1` + `fishauctions-oauth://square-connected` is the right pattern. It needs
  generalising, not redesigning.

## App-side changes

1. **Never open an OAuth authorize URL in the app's WebView.** `ASWebAuthenticationSession` (iOS) /
   Chrome Auth Tab (Android). Google hard-fails an embedded WebView with `disallowed_useragent`, and
   `square_connected_app.html` already assumes an auth session is what is running.
2. **Mint a web session for that browser view before opening it.** `POST /api/mobile/auth/web-session/`,
   take `handoff_url`, then open `handoff_url + "&next=" + urlencode("/square/connect/?return_to_app=1")`.
   URL-encode the `next` value — an unencoded `&` inside it is eaten by the query parser. Without
   this the browser view has no cookie and `LoginRequiredMixin` bounces it to `/login/`. This is the
   fix for the headline symptom, and it works against the server as it stands today.

   Two things worth knowing. The token now lives **5 minutes**, not 60 seconds (`HANDOFF_TTL_SECONDS`,
   raised for exactly this: iOS draws its consent sheet before the URL is fetched). And consuming a
   handoff calls `mark_session_opened_by_app` itself, so `?return_to_app=1` is *redundant* once you
   do this — the Square callback's closing page and its `fishauctions-oauth://square-connected`
   redirect light up either way. Keep sending it anyway; it costs nothing and covers older paths.
3. **Do it for all five flows, not just Square:** `/square/connect/`, `/paypal/connect/`,
   `/mailchimp/connect/<slug>/`, `/google-calendar/connect/<slug>/`, and the Discord bot invite on
   `/clubs/<slug>/discord/`. The whole round trip — authorize *and* callback — has to stay in the
   one browser view that consumed the handoff, because the state is in that session.
4. **Stop the callback deep-linking back into the app.** Exclude `/square/onboard/success/`,
   `/paypal/onboard/success/`, `/mailchimp/callback/` and `/google-calendar/callback/` from the
   Android intent-filter path patterns, and confirm iOS is using `ASWebAuthenticationSession` (which
   does not follow Universal Links into its own app) rather than opening Safari.
5. **Treat landing on `/login/` as "re-mint the web session", never as "sign out".** The JWT pair is
   independent of the Django session cookie. Only a *completed* `POST /logout/` or account deletion
   is a real sign-out — and note allauth renders a confirmation page on GET, so intercepting the GET
   signs people out for merely visiting the URL.
6. **Never clear the JWT pair because a web page load returned 302, 401 or 429.** The only genuine
   sign-out signals are the two above plus `/api/mobile/auth/refresh/` returning 401 for a token
   that was not concurrently rotated.
7. **Single-flight the token refresh** and replay the queued requests behind it. With rotation and
   blacklisting both on, two concurrent refreshes on resume guarantee one 401.
8. **Back off on 429 with jitter** instead of signing out — the refresh bucket is per IP, so a room
   full of phones on one Wi-Fi can be throttled together.
9. **Refresh the access token before refetching `/api/mobile/config/` on resume.**
   `OptionalJWTAuthentication` (`mobile/authentication.py`) treats a stale token as anonymous and
   returns the signed-out drawer, so the app can otherwise draw signed-out chrome around a live
   session.
10. **Keep sending the app's User-Agent from the WebView and never from the browser view.** The
    split is deliberate (`web_session.py:19-27`); spoofing the app UA into the auth session would
    make the Square callback meta-refresh to `fishauctions-oauth://` in a context that cannot handle
    the scheme.

## Server-side changes

Ordered by value, none of them done yet. 5 is the one that makes the flow *correct* rather than
better-signposted; 1 and 6 are the ones that stop the reported symptom on their own.

1. Replace `LoginRequiredMixin` on the four connect views (`views/club_integrations.py`, `12008`, `12518`,
   `12853`) with a mixin that, for an anonymous request carrying `?return_to_app=1`, renders "open
   this from the app" (or bounces to a `fishauctions-oauth://reauth` scheme) instead of `/login/`.
2. Generalise `session_opened_by_app(request) or request.GET.get("return_to_app")` (`views/club_integrations.py`)
   into a shared helper and call it from the Mailchimp, Google Calendar, PayPal and Discord connect
   views. Only Square marks the session today.
3. Promote `SquareCallbackView._done` (`views/club_integrations.py`) into a shared "finish in the app" helper so
   each callback ends at its own `fishauctions-oauth://…-connected`. The other three leave the
   merchant on a web page holding a `messages.success` they will never see.
4. Make the "connection session expired" branches (`views/club_integrations.py`, `views/club_integrations.py`) redirect to
   the club's config page with actionable copy instead of `home`.
5. ~~**Move the OAuth state out of the Django session**~~ — **rejected 2026-09-02.** It would make
   this class of bug impossible rather than merely avoided, but it means hand-rolling state handling
   for four OAuth flows that get it from Django for free today, and the app-side handoff removes the
   cookie-jar split that makes it bite. Not worth the surface area. Revisit only if a connect flow
   ever has to work when opened in a plain external browser.
6. **DONE.** The four callback paths are in `IOS_EXCLUDED_PATHS` (`app_links.py`), ahead of the
   `/*` catch-all — order matters, iOS takes the first match. `test_app_links` matches the patterns
   against the *URLconf*, so renaming one of those routes fails the build rather than quietly
   un-excluding it, and a second test proves the patterns stay narrow enough that ordinary pages are
   still claimed. `/login/*` and `/logout/*` were deliberately left claimed — nothing about them is
   part of an OAuth round trip. Android has no per-path exclusion in `assetlinks.json`, so that half
   is still app-side (item 4 above).
7. Append `?return_to_app=1` under `{% if request.is_mobile_app %}` in the connect buttons:
   `club_google_calendar_settings.html:26`, `club_mailchimp_settings.html:22`,
   `square_seller.html:47/57/132`, `club_membership_settings.html:96`, `auction_ribbon.html:85`.
8. Give `MobileTokenRefreshView` its own throttle scope (`mobile/views.py:1182`, `settings.py:1189`).
9. **Half done.** `HANDOFF_TTL_SECONDS` is now **300**, up from 60: the OS draws its consent sheet
   before the handoff URL is ever fetched, so a user reading that sheet blew the old window — and an
   expired token redirects to `/login/`, which is byte-identical to the bug. The cost is that an
   *unused* token stays live for five minutes; it does not widen what one can do (256 bits, stored
   server-side, bound to one user, single-use by an atomic delete, and the consume view is
   throttled). **Still open:** give that redirect a marker like `/login/?handoff=expired`
   (`mobile/views.py:1235`) so the app can tell "re-mint silently" from "actually signed out".
10. Consider an explicit `"authenticated": false` on `/api/mobile/config/` so the app can tell "your
    token aged out" from "you are signed out". Today the anonymous drawer is the only signal.
11. `test_tap_to_pay.py:426` uses `force_login` for every connect test, so the actual failure — an
    *anonymous* browser view hitting `/square/connect/?return_to_app=1` — is untested. Add it.
