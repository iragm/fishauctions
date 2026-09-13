# SES email setup

Outbound sending plus inbound routing: replies to `auction-slug@yourdomain.com`,
`club-slug-auctions@yourdomain.com`, `club-slug-contact@yourdomain.com` reach the right club member,
and replies to `club-slug-donations-<digits>@yourdomain.com` are recorded against the donation vendor
who sent them. Console click-paths aren't recorded here (AWS changes them); this is the wiring that's
ours — DNS records, env vars, the Lambda source, and the gotchas that cost hours to find.

## Inbound routing: SNS topic → Lambda → SES receipt rule

Create a **Standard** SNS topic (`ses-inbound-router`), a Python 3.12 Lambda function of the same
name subscribed to it, and an SES receipt rule with an SNS action pointed at that topic —
**Encoding: Base64** (the default UTF-8 setting silently corrupts non-ASCII bytes and produces
jumbled forwarded text; the Lambda expects Base64). Rule set must be **Active** (separate toggle from
the rule itself).

### Lambda environment variables

| Key | Example | Notes |
|---|---|---|
| `DJANGO_API_URL` | `https://yourdomain.com/api/v1/email-routing/resolve/` | full URL, trailing slash |
| `DJANGO_DONATION_API_URL` | *(optional)* | where vendor replies are posted; defaults to `donation/` beside `DJANGO_API_URL` |
| `INBOUND_ROUTING_SECRET` | *(see below)* | must match Django's `.env` value |
| `RELAY_SENDER` | `relay@yourdomain.com` | From address when forwarding |
| `RELAY_DISPLAY_NAME` | `Club Relay` | optional |
| `FALLBACK_RECIPIENT` | `info@yourdomain.com` | where mail goes if Django is unreachable — normally on the same domain SES receives for, so the forward comes back through the Lambda; the loop guard below is what stops that repeating |
| `RELAY_CONFIGURATION_SET` | `fishauctions-prod` | optional; set only if the account enforces one, or on `ConfigurationSetDoesNotExist` |

Generate the secret (40+ chars) and put the same value in both places:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(40))"
```

IAM: attach an inline policy allowing `ses:SendRawEmail` on `*` to the execution role.

### Lambda handler

Source: `docs/ses_inbound_router.py.txt`, pasted into the Lambda console as `lambda_function.py`
(Python 3.12). It is a `.txt` because it runs in AWS, not here -- the extension keeps it out of the
module map and the linter. Behavior that isn't obvious from the wiring above:

- Drops silently, before ever reaching Django: messages SES's own scan marked `spamVerdict` or
  `virusVerdict` FAIL (relaying those out of our DKIM-signed domain is how a sending reputation
  dies); autoreplies and vacation notices (`Auto-Submitted`, `X-Autoreply`, `Precedence: bulk`, …);
  anything over 200 KB decoded (SNS itself caps delivery at 150 KB); messages to the relay's own
  address (misrouted — should have used Reply-To).
- **Loop guard**: every forward carries `X-Club-Relay-Hops`; from `RELAY_SENDER` or ≥3 hops is
  dropped. `FALLBACK_RECIPIENT` is normally on the domain SES receives for, so while Django is down
  each forward to it comes straight back through the Lambda — without this it ping-pongs until
  someone notices the bill.
- **Recipient resolution** (`GET DJANGO_API_URL?address=<local-part>`): Django's 404 drops the
  message (unknown alias); any other error or timeout forwards to `FALLBACK_RECIPIENT` so mail is
  never silently lost. For a `"kind": "donation"` reply, an *empty* recipient is a real answer (the
  club chose record-but-don't-forward) and must not become the fallback.
- Donation replies are POSTed to `DJANGO_DONATION_API_URL` (best-effort, never retried, never blocks
  forwarding) **before** headers are rewritten, so Django sees the vendor's own `From`.
- Before forwarding: attachments stripped, `[ClubName]` prefixed to the subject, `To`/`From`/
  `Reply-To`/DKIM-Signature removed and rewritten (`Reply-To` becomes the original sender),
  `Auto-Submitted: auto-forwarded` set so the *recipient's* mail client doesn't fire its own
  out-of-office back.
- On a failed `SES.send_raw_email`, one fallback attempt to `FALLBACK_RECIPIENT` before giving up.
- `USER_AGENT = "ses-inbound-router"` on both outbound calls to Django — see "If the site is behind
  Cloudflare" below for why that string matters.

## DNS records

```
# Inbound -- replace us-east-1 with your actual SES region
yourdomain.com.    MX  10  inbound-smtp.us-east-1.amazonaws.com.

# Outbound SPF
yourdomain.com.    TXT  "v=spf1 include:amazonses.com ~all"

# DKIM -- three CNAMEs from SES console -> Verified identities -> yourdomain.com -> DKIM tab
<token1>._domainkey.yourdomain.com.  CNAME  <token1>.dkim.amazonses.com.
<token2>._domainkey.yourdomain.com.  CNAME  <token2>.dkim.amazonses.com.
<token3>._domainkey.yourdomain.com.  CNAME  <token3>.dkim.amazonses.com.

# Domain verification TXT (value from SES console)
_amazonses.yourdomain.com.  TXT  "<value-from-ses-console>"

# DMARC (recommended)
_dmarc.yourdomain.com.  TXT  "v=DMARC1; p=quarantine; rua=mailto:dmarc@yourdomain.com"
```

SES inbound is only available in some regions (us-east-1, us-west-2, eu-west-1, others) — the MX
endpoint must match the region chosen for email receiving.

## Django `.env`

```bash
POST_OFFICE_EMAIL_BACKEND="django_ses.SESBackend"
AWS_ACCESS_KEY_ID="your-key"
AWS_SECRET_ACCESS_KEY="your-secret"
AWS_SES_REGION_NAME="us-east-1"
AWS_SES_REGION_ENDPOINT="email.us-east-1.amazonaws.com"
AWS_SES_CONFIGURATION_SET="fishauctions-prod"   # if you created one
SITE_DOMAIN="yourdomain.com"
INBOUND_ROUTING_SECRET="<same value as Lambda>"
```

`SES_ROUTE_EMAILS_ENABLED` activates automatically once `POST_OFFICE_EMAIL_BACKEND` and `SITE_DOMAIN`
are set; outbound mail sends from `info@yourdomain.com`.

**`DEFAULT_FROM_EMAIL` is ignored when SES routing is on** — the app always sends as
`info@SITE_DOMAIN` so DKIM signs correctly. Verify that address is authorized in SES before deploying
if you were using a custom one.

**Never set `AWS_SES_FROM_EMAIL`.** django-ses passes it as `FromEmailAddress`/`Source`, which
overrides the message's own `From` header — every per-auction, per-club and per-vendor alias gets
written into the message and then discarded on the way out, and replies come back to the site admin
instead of the club. Invisible from inside Django (post_office stores the right From either way).
Guarded by `auctions.tests.SesSendsTheMessagesOwnFromAddressTests` and a comment in `settings.py`.

## Verification checklist

1. External → `info@yourdomain.com` → arrives at the admin address
2. External → `yourclub-auctions@yourdomain.com` → routes to the configured member (or admin fallback)
3. Same for `yourclub-contact@yourdomain.com`
4. Unknown alias → silently dropped, no bounce
5. Donation tracking on → reply to a donation request → appears on the vendor's row
6. Point `DJANGO_API_URL` at a dead host, send to an unknown club alias → CloudWatch shows **one**
   delivery to `FALLBACK_RECIPIENT` then a `relay loop` drop, not a stream of them
7. Check CloudWatch → Lambda → Monitor → Logs for errors

### When a reply goes nowhere

**Every drop is a successful invocation** — Lambda's error count, the SES bounce rate and SNS
delivery metrics all stay at zero while mail disappears. Read `/aws/lambda/ses-inbound-router`'s
`[ses-router]` lines; the metrics won't tell you anything.

| Log line | Meaning | Fix |
|---|---|---|
| *(nothing for that minute)* | SES never invoked the Lambda | Rule disabled, rule set not Active, or rule in the wrong region |
| `dropping message with non-base64 content` | SNS action is in UTF-8 mode | Set **Encoding: Base64** on the receipt rule |
| `dropping message that failed spamVerdict/virusVerdict` | SES's own scan flagged it | Check sender's SPF/DKIM/DMARC |
| `dropping oversized message with no content` | Over the 150 KB SNS cap | Nothing to fix; ask for a reply without the attachment |
| `dropping autoreply from …` | Out-of-office / `Precedence: bulk` | Working as intended |
| `dropping relay loop from …` | `RELAY_SENDER`'s own mail, or 3 hops | Usually the tail of a Django outage; fix that |
| `dropping message to unrecognised alias` | Django answered 404 | Alias doesn't resolve; check with the `curl` below |
| `resolve_recipient HTTP error 401` | Secret mismatch | `INBOUND_ROUTING_SECRET` differs between Lambda and Django |
| `resolve_recipient failed …` | Django unreachable | DNS, TLS, WAF, or the site is down |
| `could not post donation reply for …` | Resolved OK, recording failed | Usually CDN/WAF blocking the POST (below); the reply is lost, this call never retries |
| `donation reply recorded, no forwarding recipient` | Working as intended | Reply is on the vendor's row; nobody emailed a copy |

```bash
curl -s -H "X-Routing-Secret: $INBOUND_ROUTING_SECRET" \
  "https://yourdomain.com/api/v1/email-routing/resolve/?address=yourclub-donations-1234567890"
# {"recipient": "", "display_name": "Your Club", "kind": "donation", "vendor_key": "1234567890"}
```

`401` = wrong secret, `404` = no live vendor owns those digits, `200` with no `"kind"` = build predates
donation tracking. A recorded reply leaves two marks: the message on the vendor's panel, and a
`Received a donation reply from …` line in club history.

### If the site is behind Cloudflare

Cloudflare's Browser Integrity Check blocks a `POST` carrying urllib's default user agent while
letting the `GET` through, so resolution works but the reply is never recorded. Shows up in Security
Events as `"source": "bic"`, `"userAgent": "Python-urllib/3.x"`. Reproduce:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST -A 'Python-urllib/3.14' \
  -H 'Content-Type: application/json' -d '{}' https://yourdomain.com/api/v1/email-routing/donation/
# 403 = Cloudflare is eating it. 401 = reached Django, which is what you want.
```

The handler's `USER_AGENT` fixes this on its own. Belt and braces: WAF custom rule, expression
`starts_with(http.request.uri.path, "/api/v1/email-routing/")` (optionally
`and ip.src.asnum eq 16509` to scope to AWS), action **Skip** → Browser Integrity Check (+ Super Bot
Fight Mode / Managed rules if applicable) — these endpoints authenticate on the 40-char secret and
answer 401 to everyone else, so there's nothing for the WAF to protect. Or turn off Browser Integrity
Check under Configuration Rules for the same path. Bot Fight Mode on the Free plan can't be scoped —
the user agent fix is the only lever if that's what's blocking you.

## Alias routing

| Alias | Priority order | Final fallback |
|---|---|---|
| `info@yourdomain.com` | — | Site admin (`ADMINS[0]` or `DEFAULT_FROM_EMAIL`) |
| `<club-slug>-auctions@yourdomain.com` | Configured member → oldest non-admin auction manager → oldest admin | Site admin |
| `<club-slug>-contact@yourdomain.com` | Configured member → oldest non-admin membership manager → oldest admin | **Dropped** |
| `<club-slug>-donations-<10 digits>@yourdomain.com` | Configured donation contact only | **Recorded on the site, forwarded to nobody** |
| `<auction-slug>@yourdomain.com` | Club's non-admin auction manager → club admin → auction creator | Dropped if no creator email |
| anything else | — | Dropped |

- "Configured member" (the specific person chosen on the Email Settings page) always outranks the
  automatic fallback order.
- Non-admins with **Manage auctions** are preferred for `*-auctions`/auction-slug aliases; non-admins
  with **Manage membership** for `*-contact`. `*-contact` with no such member and no admins is
  **dropped silently** — configure at least one.
- `*-donations-*` has no fallback chain on purpose: a reply that lands in an officer's personal inbox
  is answered from there and the site never sees the rest of the conversation. Clubs are told to
  leave the donation contact unset for this reason. Non-matching digits (deleted vendor, tracking
  off) are dropped silently.
- With SES routing active, outbound auction email sets no `Reply-To` — replies reach
  `<auction-slug>@yourdomain.com` (the `From` address) directly and get the `[Auction Name]` subject
  prefix from the Lambda.
