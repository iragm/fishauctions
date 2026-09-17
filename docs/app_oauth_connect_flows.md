# Connecting an account from the app signs you out

Tapping **Connect** in the app (Square, PayPal, Mailchimp, Google Calendar, Discord) lands on
`/login/`, and the app then acts signed out. Diagnosed 2026-09-02.

## Cause

- The connect views are `LoginRequiredMixin`. The app opens them in an auth session
  (`ASWebAuthenticationSession` / Chrome Auth Tab) that has no cookie of ours, so they 302 to
  `/login/`.
- OAuth state lives in the Django session. Signing in inside the auth session writes a different
  session, so the callback hits "connection session expired" and redirects home.
- Only Square has the `?return_to_app=1` / `session_opened_by_app` / `fishauctions-oauth://` exit.
- Google refuses embedded WebViews (`disallowed_useragent`); the Calendar connect button has no guard.
- Refresh rotation + blacklisting: two refreshes on resume = one 401. The refresh throttle is per IP.

Ruled out: `SESSION_COOKIE_SAMESITE="Lax"` (callbacks are top-level GETs — don't loosen it), `/o/`,
allauth.

## App side (open)

1. Open authorize URLs in an auth session, never the WebView.
2. **The fix:** `POST /api/mobile/auth/web-session/`, then open
   `handoff_url + "&next=" + urlencode("/square/connect/?return_to_app=1")`. The handoff lives
   5 minutes and marks the session as the app's.
3. Do it for all five: `/square/connect/`, `/paypal/connect/`, `/mailchimp/connect/<slug>/`,
   `/google-calendar/connect/<slug>/`, `/clubs/<slug>/discord/`.
4. Exclude the four callback paths from Android intent filters.
5. Landing on `/login/` means re-mint the handoff, never sign out. Only a completed `POST /logout/`,
   account deletion, or a refresh 401 that wasn't a concurrent rotation is a sign-out.
6. Single-flight token refresh; back off on 429 with jitter.
7. Refresh before refetching `/api/mobile/config/` on resume (a stale token gets the anonymous drawer).
8. Never send the app User-Agent from the auth session.

## Server side

Done: callback paths in `app_links.IOS_EXCLUDED_PATHS` (checked against the URLconf by
`test_app_links`); `HANDOFF_TTL_SECONDS` 60 → 300.

Rejected: moving OAuth state out of the Django session. Revisit only if a flow must work in a plain
external browser.

Open:
1. Anonymous `?return_to_app=1` on a connect view renders "open this from the app" instead of `/login/`.
2. Share Square's `session_opened_by_app` check and `SquareCallbackView._done` exit with the other four.
3. "Session expired" callbacks redirect to the club's config page, not `home`.
4. `?return_to_app=1` on in-app connect buttons.
5. Own throttle scope for `MobileTokenRefreshView`.
6. Expired handoff redirects to `/login/?handoff=expired`.
7. Maybe `"authenticated": false` on `/api/mobile/config/`.
8. Test an **anonymous** `/square/connect/?return_to_app=1`; every current test uses `force_login`.
