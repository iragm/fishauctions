# SES Email Setup

This guide covers setting up Amazon SES for both outbound sending and inbound email routing. Inbound routing lets replies sent to `auction-slug@yourdomain.com`, `club-slug-auctions@yourdomain.com`, and `club-slug-contact@yourdomain.com` reach the right club member automatically, and records replies to `club-slug-donations-<digits>@yourdomain.com` against the donation vendor who sent them.

---

## Part 1: SES Identity & Sending

### 1. Verify your domain in SES

- Console → SES → *Verified identities* → *Create identity* → Domain
- Enter your domain (e.g. `yourdomain.com`)
- SES gives you three DKIM CNAME records and one verification TXT record — add all four to DNS (see [DNS Records](#dns-records) below)
- Status turns *Verified* within a few minutes once DNS propagates

### 2. Request production access (if not already done)

New SES accounts are in *sandbox* mode and can only send to verified addresses. To send to real users:

- Console → SES → *Account dashboard* → *Request production access*
- Describe your use case; approval takes 24–48 h

### 3. Create a Configuration Set (recommended)

Enables bounce and complaint tracking:

- SES → *Configuration sets* → *Create*
- Name it (e.g. `fishauctions-prod`)
- Set `AWS_SES_CONFIGURATION_SET="fishauctions-prod"` in your `.env`

---

## Part 2: Inbound Email Routing

To receive replies sent to your sender aliases and forward them to the right club member, you need an SNS topic, a Lambda function, and an SES receipt rule.

### 4. Create the SNS topic

- Console → SNS → *Topics* → *Create topic*
- Type: **Standard** (not FIFO)
- Name: `ses-inbound-router`
- No encryption or special configuration needed
- Note the Topic ARN

### 5. Create the Lambda function

- Console → Lambda → *Create function* → *Author from scratch*
- Name: `ses-inbound-router`
- Runtime: **Python 3.12**
- Architecture: x86_64
- Click *Create function*

### 6. Set Lambda environment variables

Lambda → *Configuration* → *Environment variables*:

| Key | Example value | Notes |
|---|---|---|
| `DJANGO_API_URL` | `https://yourdomain.com/api/v1/email-routing/resolve/` | Full URL including trailing slash |
| `DJANGO_DONATION_API_URL` | `https://yourdomain.com/api/v1/email-routing/donation/` | Optional. Where vendor replies are posted so they're recorded against the vendor. Defaults to `donation/` beside `DJANGO_API_URL` |
| `INBOUND_ROUTING_SECRET` | *(see below)* | Must match `INBOUND_ROUTING_SECRET` in Django `.env` |
| `RELAY_SENDER` | `relay@yourdomain.com` | Address used as From when forwarding |
| `RELAY_DISPLAY_NAME` | `Club Relay` | Display name in forwarded From field (optional) |
| `FALLBACK_RECIPIENT` | `info@yourdomain.com` | Where to send mail if Django is unreachable. Note this is normally an address on the same domain SES receives for, so the forward comes back in through the Lambda — the loop guard below is what stops that repeating |
| `RELAY_CONFIGURATION_SET` | `fishauctions-prod` | SES configuration set for relay sends (optional — omit unless your account enforces one; if you see `ConfigurationSetDoesNotExist` errors, either set this or clear the account-level default in SES → Account dashboard) |

**Generate the secret** — use a minimum 40-character random string:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(40))"
```

Copy the output into both the Lambda env var and `INBOUND_ROUTING_SECRET` in your Django `.env`. The secret is the only thing preventing anyone on the internet from querying your routing API, so make it long.

### 7. Paste the Lambda handler

Lambda → *Code* tab → replace the contents of `lambda_function.py` with the code below → *Deploy*

```python
import base64
import email
import email.mime.multipart
import email.mime.text
import email.utils
import json
import os
import urllib.error
import urllib.parse
import urllib.request

import boto3

SES = boto3.client("ses")

DJANGO_API_URL = os.environ["DJANGO_API_URL"]
# Donation replies are posted here as well as forwarded, so the message body is recorded
# against the vendor who sent it. Defaults to "donation/" beside the resolve endpoint.
DJANGO_DONATION_API_URL = os.environ.get("DJANGO_DONATION_API_URL", "").strip() or (
    DJANGO_API_URL.rstrip("/").rsplit("/", 1)[0] + "/donation/"
)
ROUTING_SECRET = os.environ["INBOUND_ROUTING_SECRET"]
RELAY_SENDER = os.environ["RELAY_SENDER"]
RELAY_DISPLAY_NAME = os.environ.get("RELAY_DISPLAY_NAME", "Club Relay")
FALLBACK_RECIPIENT = os.environ["FALLBACK_RECIPIENT"]
# Optional: set to your SES configuration set name if your account requires one.
# Leave unset (or empty) to send without a configuration set.
RELAY_CONFIGURATION_SET = os.environ.get("RELAY_CONFIGURATION_SET", "").strip()

# Sent on both calls to Django.  urllib's default ("Python-urllib/3.x") is one of the signatures
# Cloudflare's Browser Integrity Check blocks outright -- and it blocks the POST while letting the
# GET through, so resolution succeeds, the reply is never recorded, and nothing anywhere reports an
# error.  Any honest identifier avoids it.  See "When a reply goes nowhere" below.
USER_AGENT = "ses-inbound-router"

# Refuse to parse messages larger than this before even touching the MIME tree.
# SNS caps delivery at 150 KB, so anything larger means SNS truncated the body;
# the hard cap here guards against pathological payloads.
_MAX_RAW_BYTES = 200_000  # 200 KB

# Sentinel: address is valid but should be dropped (not a network error).
_DROP = object()

# Auto-reply header values that should cause a message to be dropped.
# Forwarding auto-replies back to senders causes mail loops and annoys people.
_AUTOREPLY_AUTO_SUBMITTED = {"auto-replied", "auto-generated", "auto-notified"}

# Loop guard.  FALLBACK_RECIPIENT is normally an address on the same domain SES
# receives for, so a forward to it comes straight back in through this Lambda.
# When Django is unreachable every pass resolves to the fallback again and the
# message ping-pongs until somebody notices the SES bill.  Every forward carries
# a hop count; past this many, drop.
_HOP_HEADER = "X-Club-Relay-Hops"
_MAX_HOPS = 3

# SES scans inbound mail before it ever reaches us and puts the result in the
# notification.  Relaying a FAIL out of our own DKIM-signed domain is how a
# domain's sending reputation dies, so these are dropped rather than forwarded.
_VERDICTS_TO_DROP = ("spamVerdict", "virusVerdict")


def failed_verdict(notification):
    """Return the name of the SES scan this message failed, or None."""
    receipt = notification.get("receipt") or {}
    for name in _VERDICTS_TO_DROP:
        if (receipt.get(name) or {}).get("status") == "FAIL":
            return name
    return None


def is_valid_recipient(address):
    """True if *address* is safe to put in a header and hand to SES.

    Django is trusted, but it is still a remote service answering over the network, and
    ``msg["To"] = value`` does no validation: a value with a newline in it would be written
    straight into the message as a second header.  One cheap check closes that off.
    """
    address = (address or "").strip()
    if not address or "@" not in address:
        return False
    return not any(char in address for char in "\r\n,;")


def hop_count(msg):
    """How many times this message has already been through the relay."""
    try:
        return int((msg.get(_HOP_HEADER) or "0").strip())
    except ValueError:
        return 0


def is_autoreply(msg):
    """Return True if the message looks like an automated reply or vacation notice."""
    # RFC 3834 — Auto-Submitted header
    auto_submitted = (msg.get("Auto-Submitted") or "").strip().lower()
    if auto_submitted and auto_submitted != "no":
        # "auto-replied", "auto-generated", etc.  "auto-forwarded" is fine to pass through.
        if auto_submitted in _AUTOREPLY_AUTO_SUBMITTED or auto_submitted.startswith("auto-replied"):
            return True

    # Non-standard but widely used autoreply markers
    if (msg.get("X-Autoreply") or "").strip().lower() == "yes":
        return True
    if (msg.get("X-Autorespond") or "").strip():
        return True

    # Precedence: bulk / junk / list are automated; "auto-reply" is explicit
    precedence = (msg.get("Precedence") or "").strip().lower()
    if precedence in {"bulk", "junk", "auto-reply", "auto_reply"}:
        return True

    # Some mailers use X-Auto-Response-Suppress on their *outbound* autoreplies
    if (msg.get("X-Auto-Response-Suppress") or "").strip():
        return True

    return False


def resolve_recipient(local_part):
    """Ask Django which address to forward this alias to.

    Returns a (recipient, display_name, kind) tuple, _DROP as the recipient if Django says 404
    (unknown alias), or (FALLBACK_RECIPIENT, None, "") if Django is unreachable
    or returns any other error so the email is never silently lost.

    ``kind`` is "donation" for a vendor reply to a donation address, and "" for everything else.
    Donation addresses are the one case where an *empty* recipient is a real answer rather than a
    missing one: the club may have chosen to have replies recorded on the site and forwarded to
    nobody, so an empty recipient must NOT be turned into FALLBACK_RECIPIENT here.
    """
    params = urllib.parse.urlencode({"address": local_part})
    req = urllib.request.Request(
        f"{DJANGO_API_URL}?{params}",
        headers={"X-Routing-Secret": ROUTING_SECRET, "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            kind = data.get("kind") or ""
            recipient = data.get("recipient") or ""
            if kind != "donation":
                recipient = recipient or FALLBACK_RECIPIENT
            return recipient, data.get("display_name"), kind
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            # Django says this alias doesn't exist — drop silently.
            return _DROP, None, ""
        # Any other HTTP error (401, 503, 500 …) — forward to fallback so
        # mail is never lost due to a misconfiguration or temporary outage.
        print(f"[ses-router] resolve_recipient HTTP error {exc.code} for {local_part!r}: {exc}")
        return FALLBACK_RECIPIENT, None, ""
    except Exception as exc:
        # Django unreachable (timeout, DNS failure, etc.) — forward to fallback.
        print(f"[ses-router] resolve_recipient failed for {local_part!r}: {exc}")
        return FALLBACK_RECIPIENT, None, ""


def extract_text_body(msg):
    """Return the readable body of a message as a string.

    Prefers text/plain; falls back to text/html, which Django strips down itself. Attachments and
    inline images are skipped — the record kept against a vendor is the words they wrote.
    """
    plain = []
    html = []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart":
            continue
        if "attachment" in (part.get_content_disposition() or "").lower():
            continue
        content_type = part.get_content_type()
        if content_type not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        (plain if content_type == "text/plain" else html).append(text)
    return "\n".join(plain).strip() or "\n".join(html).strip()


def post_donation_email(to_addr, msg):
    """Hand a donation vendor's reply to Django so it lands on the vendor's row.

    Best effort: a failure here must never stop the message being forwarded, and Django answers
    200 for anything it can't match so this never retries a message that will never land.
    """
    payload = json.dumps(
        {
            "address": to_addr,
            "from": msg.get("From", ""),
            "recipients": to_addr,
            "subject": msg.get("Subject", ""),
            "body": extract_text_body(msg),
            "message_id": msg.get("Message-ID", ""),
        }
    ).encode()
    req = urllib.request.Request(
        DJANGO_DONATION_API_URL,
        data=payload,
        headers={
            "X-Routing-Secret": ROUTING_SECRET,
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"[ses-router] donation reply posted: {resp.read()[:200]!r}")
    except Exception as exc:
        print(f"[ses-router] could not post donation reply for {to_addr!r}: {exc}")


def strip_attachments(msg):
    """Recursively remove all attachment and non-text parts from a MIME message."""
    if not msg.is_multipart():
        return

    def keep_part(part):
        disposition = (part.get_content_disposition() or "").lower()
        if "attachment" in disposition:
            return False
        ct = part.get_content_type()
        return ct.startswith("text/") or ct.startswith("multipart/")

    def clean(part):
        if not part.is_multipart():
            return
        kept = [p for p in part.get_payload() if keep_part(p)]
        for p in kept:
            clean(p)
        part.set_payload(kept or [email.mime.text.MIMEText("[Attachments removed]", "plain")])

    clean(msg)


def prefix_subject(msg, display_name):
    """Prepend [display_name] to the Subject if not already present."""
    if not display_name:
        return
    prefix = f"[{display_name}]"
    subject = msg.get("Subject", "")
    if not subject.startswith(prefix):
        del msg["Subject"]
        msg["Subject"] = f"{prefix} {subject}".strip()


def lambda_handler(event, context):
    """Handle every record SNS delivered.  One is the normal case; batches are allowed."""
    results = [handle_record(record) for record in event.get("Records") or []]
    if len(results) == 1:
        return results[0]
    return {"status": "batch", "results": results}


def handle_record(record):
    # SES delivers the full email (headers + body) via SNS for messages ≤ 150 KB.
    # Larger messages are truncated by SNS and won't have a usable body.
    notification = json.loads(record["Sns"]["Message"])

    # SES already scanned this.  Don't relay what it flagged.
    verdict = failed_verdict(notification)
    if verdict:
        print(f"[ses-router] dropping message that failed {verdict}")
        return {"status": "dropped", "reason": verdict}

    raw_content = notification.get("content")
    if not raw_content:
        # Message exceeded the 150 KB SNS limit (very unusual without attachments).
        print("[ses-router] dropping oversized message with no content")
        return {"status": "dropped", "reason": "no content"}

    # The SES receipt rule's SNS action must be configured with Base64 encoding —
    # UTF-8 mode silently corrupts non-ASCII bytes (encoded headers, quoted-printable
    # bodies, etc.), producing a "jumbled text" forward.
    try:
        raw_bytes = base64.b64decode(raw_content, validate=True)
    except (ValueError, TypeError) as exc:
        print(f"[ses-router] dropping message with non-base64 content: {exc}")
        return {"status": "dropped", "reason": "invalid content encoding"}

    # Guard against pathological payloads before handing to the MIME parser.
    if len(raw_bytes) > _MAX_RAW_BYTES:
        print(f"[ses-router] dropping message exceeding size cap ({len(raw_bytes)} bytes)")
        return {"status": "dropped", "reason": "message too large"}

    msg = email.message_from_bytes(raw_bytes)

    # Drop automated replies (out-of-office, vacation notices, delivery reports).
    # Forwarding these back causes mail loops and clutters the recipient's inbox.
    if is_autoreply(msg):
        print(f"[ses-router] dropping autoreply from {msg.get('From', '?')!r}")
        return {"status": "dropped", "reason": "autoreply"}

    # Our own forward, arriving back through the MX.  Either FALLBACK_RECIPIENT is an
    # address this domain receives for (it usually is) or two aliases point at each
    # other; both are loops, and both are silent until the bill arrives.
    hops = hop_count(msg)
    _, from_addr = email.utils.parseaddr(msg.get("From", ""))
    if from_addr.strip().lower() == RELAY_SENDER.strip().lower() or hops >= _MAX_HOPS:
        print(f"[ses-router] dropping relay loop from {from_addr!r} after {hops} hop(s)")
        return {"status": "dropped", "reason": "relay loop"}

    # Determine which alias received the message.
    # Prefer the envelope destination from SES metadata — more reliable than
    # the To header, which may be absent (BCC) or contain a different address.
    destinations = notification.get("mail", {}).get("destination", [])
    to_addr = destinations[0] if destinations else ""
    if not to_addr:
        _, to_addr = email.utils.parseaddr(msg.get("To", ""))
    original_sender = msg.get("From", "")
    if "@" in to_addr:
        local_part = to_addr.split("@")[0].strip().lower()
    else:
        local_part = to_addr.strip().lower()

    # Drop messages addressed to the relay sender itself — these are
    # misrouted replies that should instead use the Reply-To header.
    relay_local = RELAY_SENDER.split("@")[0].lower() if "@" in RELAY_SENDER else "relay"
    if local_part == relay_local:
        print(f"[ses-router] dropping message addressed to relay: {to_addr!r}")
        return {"status": "dropped", "reason": "relay address"}

    # Resolve the forwarding target; drop only if Django explicitly says 404.
    # Any other failure (Django down, 5xx, timeout) falls back to FALLBACK_RECIPIENT.
    forward_to, display_name, kind = (
        resolve_recipient(local_part) if local_part else (_DROP, None, "")
    )
    if forward_to is _DROP:
        print(f"[ses-router] dropping message to unrecognised alias: {to_addr!r}")
        return {"status": "dropped", "reason": "unknown alias"}
    # An empty recipient is only ever a real answer for a donation address (below).
    if forward_to and not is_valid_recipient(forward_to):
        print(f"[ses-router] dropping unusable recipient {forward_to!r} for {to_addr!r}")
        return {"status": "dropped", "reason": "invalid recipient"}

    # Donation vendor replies are recorded on the site as well as (optionally) forwarded. Post
    # before the headers below are rewritten, so Django sees the vendor's own From address.
    if kind == "donation":
        post_donation_email(to_addr, msg)
        if not forward_to:
            # The club chose to have replies recorded here and forwarded to nobody — which is the
            # recommended setup, because a reply sent from a personal inbox is never tracked.
            print(f"[ses-router] donation reply recorded, no forwarding recipient: {to_addr!r}")
            return {"status": "recorded", "to": None}

    # Remove all attachments before forwarding.
    strip_attachments(msg)

    # Prepend [Auction/Club Name] to the subject so the recipient knows it's forwarded.
    prefix_subject(msg, display_name)

    # Rewrite envelope headers; SES will re-sign with its own DKIM key.
    del msg["To"]
    del msg["From"]
    del msg["Reply-To"]
    del msg[_HOP_HEADER]
    while "DKIM-Signature" in msg:
        del msg["DKIM-Signature"]
    msg["To"] = forward_to
    msg["From"] = f"{RELAY_DISPLAY_NAME} <{RELAY_SENDER}>"
    msg["Reply-To"] = original_sender
    # Tell receiving mail servers this is an automated forward, not an original message.
    # This suppresses out-of-office autoreplies from the forwarding recipient.
    msg["Auto-Submitted"] = "auto-forwarded"
    msg["X-Auto-Response-Suppress"] = "All"
    # Survives the round trip if this forward comes back in through the MX; see _MAX_HOPS.
    msg[_HOP_HEADER] = str(hops + 1)

    send_kwargs = {
        "Source": RELAY_SENDER,
        "Destinations": [forward_to],
        "RawMessage": {"Data": msg.as_bytes()},
    }
    if RELAY_CONFIGURATION_SET:
        send_kwargs["ConfigurationSetName"] = RELAY_CONFIGURATION_SET

    try:
        SES.send_raw_email(**send_kwargs)
    except Exception as exc:
        print(f"[ses-router] SES.send_raw_email failed forwarding to {forward_to!r}: {exc}")
        # Attempt fallback delivery if the primary recipient wasn't already the fallback.
        if forward_to != FALLBACK_RECIPIENT:
            try:
                # del first: assigning to a header that is already set *appends* a second
                # one, and the recipient would see the address they were forwarded for.
                del msg["To"]
                msg["To"] = FALLBACK_RECIPIENT
                SES.send_raw_email(
                    Source=RELAY_SENDER,
                    Destinations=[FALLBACK_RECIPIENT],
                    RawMessage={"Data": msg.as_bytes()},
                )
                return {"status": "fallback", "to": FALLBACK_RECIPIENT, "error": str(exc)}
            except Exception as exc2:
                print(f"[ses-router] fallback delivery also failed: {exc2}")
        return {"status": "error", "error": str(exc)}

    return {"status": "forwarded", "to": forward_to}
```

### 8. Add IAM permissions to the Lambda execution role

Lambda → *Configuration* → *Permissions* → click the execution role link → IAM → *Add permissions* → *Create inline policy*:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": "ses:SendRawEmail",
    "Resource": "*"
  }]
}
```

Save the policy as `ses-send-raw`.

### 9. Subscribe Lambda to the SNS topic

- SNS → *Topics* → `ses-inbound-router` → *Create subscription*
- Protocol: **AWS Lambda**
- Endpoint: select the `ses-inbound-router` Lambda ARN
- *Create subscription*

Lambda → *Configuration* → *Triggers* should now show the SNS trigger.

### 10. Create the SES receipt rule

- SES → *Email receiving* → *Rule sets* → create or select a rule set
- *Create rule* → name it `route-all`
- **Recipients**: leave blank to catch all addresses for your domain
- **Actions**: add a single **SNS** action → select `ses-inbound-router`
  → set **Encoding: Base64**
  (the default UTF-8 setting silently corrupts non-ASCII bytes and produces
  jumbled forwarded text — the Lambda expects Base64)
- Enable the rule and make sure the rule set itself is **Active** (rule sets have a separate active/inactive toggle)

---

## DNS Records

Add all of these to your domain DNS. The DKIM and verification values come from the SES console after you create the identity.

```
# Inbound — SES receives mail for your domain
# Replace us-east-1 with your actual SES region if different
yourdomain.com.    MX  10  inbound-smtp.us-east-1.amazonaws.com.

# Outbound SPF — authorise SES to send on your behalf
yourdomain.com.    TXT  "v=spf1 include:amazonses.com ~all"

# DKIM — three CNAMEs from the SES console
# SES → Verified identities → yourdomain.com → DKIM tab
<token1>._domainkey.yourdomain.com.  CNAME  <token1>.dkim.amazonses.com.
<token2>._domainkey.yourdomain.com.  CNAME  <token2>.dkim.amazonses.com.
<token3>._domainkey.yourdomain.com.  CNAME  <token3>.dkim.amazonses.com.

# Domain verification TXT (value shown in SES console)
_amazonses.yourdomain.com.  TXT  "<value-from-ses-console>"

# DMARC (recommended) — tells receivers what to do with failures
_dmarc.yourdomain.com.  TXT  "v=DMARC1; p=quarantine; rua=mailto:dmarc@yourdomain.com"
```

> **Note:** SES inbound is only available in certain regions (us-east-1, us-west-2, eu-west-1, and a few others). Your MX endpoint must match the region you chose for email receiving. The exact endpoint is shown in SES → *Email receiving*.

---

## Django `.env` additions

```bash
POST_OFFICE_EMAIL_BACKEND="django_ses.SESBackend"
AWS_ACCESS_KEY_ID="your-key"
AWS_SECRET_ACCESS_KEY="your-secret"
AWS_SES_REGION_NAME="us-east-1"
AWS_SES_REGION_ENDPOINT="email.us-east-1.amazonaws.com"
AWS_SES_CONFIGURATION_SET="fishauctions-prod"   # if you created one
SITE_DOMAIN="yourdomain.com"
INBOUND_ROUTING_SECRET="<same value you put in Lambda>"
```

Once `POST_OFFICE_EMAIL_BACKEND=django_ses.SESBackend` and `SITE_DOMAIN` are set, `SES_ROUTE_EMAILS_ENABLED` activates automatically and outbound mail sends from `info@yourdomain.com`.

> **Note on `DEFAULT_FROM_EMAIL`:** When SES routing is enabled the app automatically uses `info@SITE_DOMAIN` as the *default* sender — what a message goes out as when the code doesn't name one. Any `DEFAULT_FROM_EMAIL` value in your `.env` is intentionally ignored — the domain-based address ensures DKIM signing works correctly. If you were previously using a custom `DEFAULT_FROM_EMAIL`, verify that `info@yourdomain.com` is authorised in SES before deploying.

> **Never set `AWS_SES_FROM_EMAIL`.** django-ses passes it to the SES API as `FromEmailAddress` (`Source` on the v1 path), and that parameter *overrides the From header of the message*. Setting it pins every email the site sends to one address, so the per-auction, per-club and per-vendor aliases below are written into the message and then discarded on the way out — the From line reads "info", and replies come back to the site admin instead of the club. There is a regression test (`auctions.tests.SesSendsTheMessagesOwnFromAddressTests`) and a comment in `settings.py`; both exist because this failure is invisible from inside Django — post_office stores the right From either way.

---

## Verification checklist

1. Send a test email to `info@yourdomain.com` from an external account — confirm it arrives at your admin address
2. Send to `yourclub-auctions@yourdomain.com` — confirm it routes to the configured club member (or admin fallback)
3. Send to `yourclub-contact@yourdomain.com` — same check
4. Send to an unknown alias — confirm it is silently dropped (no bounce, nothing in your inbox)
5. With donation tracking on, reply to a donation request and confirm the reply appears in the vendor's panel under Donation Tracking
6. Stop Django (or point `DJANGO_API_URL` at a dead host) and send to an unknown club alias — confirm CloudWatch shows **one** delivery to `FALLBACK_RECIPIENT` followed by a `relay loop` drop, not a stream of them
7. Check Lambda *Monitor* → *Logs* in CloudWatch for any errors

### When a reply goes nowhere

**Every drop is a successful invocation.** The handler prints why and returns normally, so Lambda's
error count, the SES bounce rate and the SNS delivery metrics all stay at zero while mail quietly
disappears. Open the log group (`/aws/lambda/ses-inbound-router`) and read the `[ses-router]` lines
themselves — the metrics will not tell you anything.

| Log line | What happened | Fix |
|---|---|---|
| *(nothing at all for that minute)* | SES never invoked the Lambda | Receipt rule disabled, rule set not *Active*, or the rule is in a different region from the one your MX names |
| `dropping message with non-base64 content` | The SNS action is in UTF-8 mode | Set the receipt rule's SNS action **Encoding: Base64** |
| `dropping message that failed spamVerdict` / `virusVerdict` | SES's own scan flagged it | Nothing to fix here; check the sender's SPF/DKIM/DMARC |
| `dropping oversized message with no content` | Over the 150 KB SNS cap | Nothing to fix; ask the sender to reply without the attachment |
| `dropping autoreply from …` | Out-of-office or a mailer marked `Precedence: bulk` | Working as intended |
| `dropping relay loop from …` | `RELAY_SENDER`'s own mail, or 3 hops | Usually the tail of an earlier fallback loop; fix whatever made Django unreachable |
| `dropping message to unrecognised alias` | Django answered **404** | The alias doesn't resolve — vendor deleted, donation tracking off, or a mistyped key. Check with the `curl` below |
| `resolve_recipient HTTP error 401` | The secret doesn't match | `INBOUND_ROUTING_SECRET` in the Lambda ≠ the one in Django's `.env` |
| `resolve_recipient failed …` | Django unreachable from Lambda | DNS, TLS, a WAF rule, or the site being down |
| `could not post donation reply for …` | Resolution worked, **recording didn't** | Usually a CDN/WAF blocking the POST (see below), otherwise the same 401/unreachable causes as above on `DJANGO_DONATION_API_URL`. The reply is lost: this call is best-effort and never retried |
| `donation reply recorded, no forwarding recipient` | Everything worked | The reply is on the vendor's row; nobody was emailed a copy, which is the recommended setup |

Ask Django directly what it would have answered — this is the same call the Lambda makes:

```bash
curl -s -H "X-Routing-Secret: $INBOUND_ROUTING_SECRET" \
  "https://yourdomain.com/api/v1/email-routing/resolve/?address=yourclub-donations-1234567890"
# {"recipient": "", "display_name": "Your Club", "kind": "donation", "vendor_key": "1234567890"}
```

`401` means the secret is wrong, `404` means no live vendor owns those digits, and a `200` with no
`"kind"` means the site is running a build from before donation tracking. A reply that Django did
record leaves two marks a person can see without a log: the message on the vendor's panel, and a
`Received a donation reply from …` line in the club's history.

### If the site is behind Cloudflare

Cloudflare's **Browser Integrity Check** blocks a `POST` carrying urllib's default user agent while
letting the `GET` through — so the alias resolves, the forward goes out, and only the *recording*
of the reply is dropped. It shows up in Cloudflare's Security Events as `"source": "bic"` with
`"userAgent": "Python-urllib/3.x"`, and nowhere else. Reproduce it from any machine:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST -A 'Python-urllib/3.14' \
  -H 'Content-Type: application/json' -d '{}' https://yourdomain.com/api/v1/email-routing/donation/
# 403 = Cloudflare is eating it. 401 = it reached Django, which is what you want.
```

The `USER_AGENT` in the handler above is enough on its own. Belt and braces, exempt the two
routing endpoints as well — they authenticate on a 40-character shared secret and answer `401` to
everyone else, so there is nothing there for the WAF to protect:

1. Cloudflare dashboard → your zone → **Security → WAF → Custom rules → Create rule**
2. Name it `Allow SES inbound router`, and use the expression editor:
   `starts_with(http.request.uri.path, "/api/v1/email-routing/")`
   (add `and ip.src.asnum eq 16509` to narrow it to AWS, at the cost of breaking if you ever move the Lambda)
3. Action: **Skip**, then tick **Browser Integrity Check** — plus *Super Bot Fight Mode* and
   *Managed rules* if your plan shows them
4. **Deploy**, and drag it above any custom rule that could block first — custom rules run in order

*Configuration Rules* → **Browser Integrity Check: Off** on the same expression does the same job
if you would rather not spend a custom rule. Note that Bot Fight Mode on the Free plan is neither
skippable nor scopable — if that is what is blocking you, the user agent above is the only fix.

> **Already running an older copy of the handler?** Re-paste it. The version above adds the
> spam/virus verdict check and the `X-Club-Relay-Hops` loop guard — without them a Django outage
> can put the relay into a self-sustaining mail loop — and the `USER_AGENT` that keeps a WAF from
> silently eating the donation POST.

---

## How routing works

```
Inbound email
     │
     ▼
SES Receipt Rule (SNS action)
     │
     ▼
SNS Topic ──► Lambda
                │
                ├─ Calls Django API with X-Routing-Secret header
                │   GET /api/v1/email-routing/resolve/?address=<alias>
                │
                ├─ 200: forward to returned recipient, prefix subject with [Name]
                ├─ 200 + "kind": "donation": also POST the body to
                │   /api/v1/email-routing/donation/ so it is recorded against
                │   the vendor; forward only if "recipient" is non-empty
                ├─ 404: drop silently (unknown alias)
                └─ error/timeout: forward to FALLBACK_RECIPIENT
```

Dropped before any of that happens:

- anything SES's own scan marked `spamVerdict: FAIL` or `virusVerdict: FAIL`. Relaying those
  out of your DKIM-signed domain is how a sending reputation dies.
- anything sent *from* `RELAY_SENDER`, or carrying `X-Club-Relay-Hops: 3` or more. Every forward
  increments that header. `FALLBACK_RECIPIENT` is normally an address on the same domain SES
  receives for, so while Django is down each forward to it arrives back at the Lambda, resolves
  to the fallback again, and would otherwise ping-pong indefinitely at SES's per-message price.
- a recipient Django returned that isn't a usable single address (no `@`, or containing a newline
  or a comma). `msg["To"] = value` does no validation of its own.

Recognised alias patterns:

| Alias | Priority order | Final fallback |
|---|---|---|
| `info@yourdomain.com` | — | Site admin (`ADMINS[0]` or `DEFAULT_FROM_EMAIL`) |
| `<club-slug>-auctions@yourdomain.com` | Configured member → oldest non-admin auction manager → oldest admin | Site admin |
| `<club-slug>-contact@yourdomain.com` | Configured member → oldest non-admin membership manager → oldest admin | **Dropped** (no fallback) |
| `<club-slug>-donations-<10 digits>@yourdomain.com` | Configured donation contact only | **Recorded on the site, forwarded to nobody** |
| `<auction-slug>@yourdomain.com` | Club's non-admin auction manager → club admin → auction creator | Dropped if no creator email |
| anything else | — | Dropped |

**Priority notes:**
- "Configured member" means the specific club member selected on the Email Settings page; this takes precedence over the automatic fallback order.
- For `*-auctions` and auction slugs, non-admin members with the **Manage auctions** permission are preferred over admins, keeping auction replies away from full admins unless no specialist is available.
- For `*-contact`, non-admin members with the **Manage membership** permission are preferred. If no such member exists and there are no admins, the message is **dropped silently** — configure at least one member with admin or membership permissions to receive contact mail.
- `*-donations-*` is the one alias whose reply is worth keeping even when nobody is forwarded a copy: the trailing 10 digits identify a donation vendor, and the message body is stored against that vendor and summarized. It has **no fallback chain** by design — a reply that lands in an officer's personal inbox is one they will answer from that inbox, and the site never sees the rest of the conversation. Clubs are told to leave the donation contact unset for exactly this reason. Replies that don't match a live vendor (deleted vendor, tracking turned off, made-up digits) are dropped silently.
- When SES routing is active, outbound auction emails no longer set a `Reply-To` header. Replies naturally reach `<auction-slug>@yourdomain.com` (the `From` address) and are routed by Lambda, adding a `[Auction Name]` subject prefix so recipients know the context.
