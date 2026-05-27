# SES Email Setup

This guide covers setting up Amazon SES for both outbound sending and inbound email routing. Inbound routing lets replies sent to `auction-slug@yourdomain.com`, `club-slug-auctions@yourdomain.com`, and `club-slug-contact@yourdomain.com` reach the right club member automatically.

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
| `INBOUND_ROUTING_SECRET` | *(see below)* | Must match `INBOUND_ROUTING_SECRET` in Django `.env` |
| `RELAY_SENDER` | `relay@yourdomain.com` | Address used as From when forwarding |
| `RELAY_DISPLAY_NAME` | `Club Relay` | Display name in forwarded From field (optional) |
| `FALLBACK_RECIPIENT` | `info@yourdomain.com` | Where to send mail if Django is unreachable |
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
ROUTING_SECRET = os.environ["INBOUND_ROUTING_SECRET"]
RELAY_SENDER = os.environ["RELAY_SENDER"]
RELAY_DISPLAY_NAME = os.environ.get("RELAY_DISPLAY_NAME", "Club Relay")
FALLBACK_RECIPIENT = os.environ["FALLBACK_RECIPIENT"]
# Optional: set to your SES configuration set name if your account requires one.
# Leave unset (or empty) to send without a configuration set.
RELAY_CONFIGURATION_SET = os.environ.get("RELAY_CONFIGURATION_SET", "").strip()

# Refuse to parse messages larger than this before even touching the MIME tree.
# SNS caps delivery at 150 KB, so anything larger means SNS truncated the body;
# the hard cap here guards against pathological payloads.
_MAX_RAW_BYTES = 200_000  # 200 KB

# Sentinel: address is valid but should be dropped (not a network error).
_DROP = object()

# Auto-reply header values that should cause a message to be dropped.
# Forwarding auto-replies back to senders causes mail loops and annoys people.
_AUTOREPLY_AUTO_SUBMITTED = {"auto-replied", "auto-generated", "auto-notified"}


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

    Returns a (recipient, display_name) tuple, _DROP if Django says 404
    (unknown alias), or (FALLBACK_RECIPIENT, None) if Django is unreachable
    or returns any other error so the email is never silently lost.
    """
    params = urllib.parse.urlencode({"address": local_part})
    req = urllib.request.Request(
        f"{DJANGO_API_URL}?{params}",
        headers={"X-Routing-Secret": ROUTING_SECRET},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            return data.get("recipient") or FALLBACK_RECIPIENT, data.get("display_name")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            # Django says this alias doesn't exist — drop silently.
            return _DROP, None
        # Any other HTTP error (401, 503, 500 …) — forward to fallback so
        # mail is never lost due to a misconfiguration or temporary outage.
        print(f"[ses-router] resolve_recipient HTTP error {exc.code} for {local_part!r}: {exc}")
        return FALLBACK_RECIPIENT, None
    except Exception as exc:
        # Django unreachable (timeout, DNS failure, etc.) — forward to fallback.
        print(f"[ses-router] resolve_recipient failed for {local_part!r}: {exc}")
        return FALLBACK_RECIPIENT, None


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
    # SES delivers the full email (headers + body) via SNS for messages ≤ 150 KB.
    # Larger messages are truncated by SNS and won't have a usable body.
    record = event["Records"][0]
    notification = json.loads(record["Sns"]["Message"])

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
    forward_to, display_name = resolve_recipient(local_part) if local_part else (_DROP, None)
    if forward_to is _DROP:
        print(f"[ses-router] dropping message to unrecognised alias: {to_addr!r}")
        return {"status": "dropped", "reason": "unknown alias"}

    # Remove all attachments before forwarding.
    strip_attachments(msg)

    # Prepend [Auction/Club Name] to the subject so the recipient knows it's forwarded.
    prefix_subject(msg, display_name)

    # Rewrite envelope headers; SES will re-sign with its own DKIM key.
    del msg["To"]
    del msg["From"]
    del msg["Reply-To"]
    while "DKIM-Signature" in msg:
        del msg["DKIM-Signature"]
    msg["To"] = forward_to
    msg["From"] = f"{RELAY_DISPLAY_NAME} <{RELAY_SENDER}>"
    msg["Reply-To"] = original_sender
    # Tell receiving mail servers this is an automated forward, not an original message.
    # This suppresses out-of-office autoreplies from the forwarding recipient.
    msg["Auto-Submitted"] = "auto-forwarded"
    msg["X-Auto-Response-Suppress"] = "All"

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
- **Actions**: add a single **SNS** action → select `ses-inbound-router` → set **Encoding: Base64** (the default UTF-8 setting silently corrupts non-ASCII bytes and produces jumbled forwarded text — the Lambda expects Base64)
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

> **Note on `DEFAULT_FROM_EMAIL`:** When SES routing is enabled the app automatically uses `info@SITE_DOMAIN` as the default sender. Any `DEFAULT_FROM_EMAIL` value in your `.env` is intentionally ignored — the domain-based address ensures DKIM signing works correctly. If you were previously using a custom `DEFAULT_FROM_EMAIL`, verify that `info@yourdomain.com` is authorised in SES before deploying.

---

## Verification checklist

1. Send a test email to `info@yourdomain.com` from an external account — confirm it arrives at your admin address
2. Send to `yourclub-auctions@yourdomain.com` — confirm it routes to the configured club member (or admin fallback)
3. Send to `yourclub-contact@yourdomain.com` — same check
4. Send to an unknown alias — confirm it is silently dropped (no bounce, nothing in your inbox)
5. Check Lambda *Monitor* → *Logs* in CloudWatch for any errors

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
                ├─ 404: drop silently (unknown alias)
                └─ error/timeout: forward to FALLBACK_RECIPIENT
```

Recognised alias patterns:

| Alias | Priority order | Final fallback |
|---|---|---|
| `info@yourdomain.com` | — | Site admin (`ADMINS[0]` or `DEFAULT_FROM_EMAIL`) |
| `<club-slug>-auctions@yourdomain.com` | Configured member → oldest non-admin auction manager → oldest admin | Site admin |
| `<club-slug>-contact@yourdomain.com` | Configured member → oldest non-admin membership manager → oldest admin | **Dropped** (no fallback) |
| `<auction-slug>@yourdomain.com` | Club's non-admin auction manager → club admin → auction creator | Dropped if no creator email |
| anything else | — | Dropped |

**Priority notes:**
- "Configured member" means the specific club member selected on the Email Settings page; this takes precedence over the automatic fallback order.
- For `*-auctions` and auction slugs, non-admin members with the **Manage auctions** permission are preferred over admins, keeping auction replies away from full admins unless no specialist is available.
- For `*-contact`, non-admin members with the **Manage membership** permission are preferred. If no such member exists and there are no admins, the message is **dropped silently** — configure at least one member with admin or membership permissions to receive contact mail.
- When SES routing is active, outbound auction emails no longer set a `Reply-To` header. Replies naturally reach `<auction-slug>@yourdomain.com` (the `From` address) and are routed by Lambda, adding a `[Auction Name]` subject prefix so recipients know the context.
