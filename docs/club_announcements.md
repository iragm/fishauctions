# Club announcements

A club has four ways to reach its members and, until now, no way to use them together: a Discord
server, a set of phones with the app on them, its own mailing list at Mailchimp or Brevo, and its
own website. An **announcement** is one short message plus a set of ticked channels — the "and".

* Permission: `ClubMember.permission_send_announcements`
* Model: `ClubAnnouncement` in `auctions/models.py`
* Delivery: `auctions/announcements.py`
* Page: `/clubs/<slug>/announcements/` — write one, and see where the last ones went
* Embed: `/clubs/<slug>/announcements-embed/`, listed on **Website integration**

## The shape of it

An announcement is deliberately not an event, a blog post or an email campaign. It is a sentence
or two that a club wants in front of members *now* — "bring a plant to Saturday's meeting". If it
has a date it belongs on the calendar instead, where it also reaches people's own calendars.

Channels are stored **as they were chosen**, not as they are configured now. A club that later
disconnects Discord must not have its history rewritten into "this never went to Discord".

**Every channel carries the whole announcement and nothing else.** There is no page of its own and
no "read the rest on our website" link, in the email, in Discord or anywhere: an announcement is a
sentence or two by design, so a link would only lead somewhere that repeats it. The Discord post is
the club's name in bold and the text; the push notification body is the entire announcement, which
is what `MAX_LENGTH` is sized for; tapping the notification opens the club's page, because that is
a destination rather than a continuation.

That is also why there is no invented "seen" number. Only one channel here can report a real
**read**, and it is the one somebody else runs. The website has the next best thing, which is a
count of how many times it was put on a page — an impression, labelled as one:

| Icon | Number | What it is |
|---|---|---|
| Discord | none | Discord has no read receipts, and there is no link to instrument either. A red warning triangle means the post was refused. |

| Bell | `push_recipients` | How many members' phones the push was **handed to**. Not readership. |
| Envelope | `email_opens` | Unique opens on the email campaign, as the provider reports them. The only real read receipt any channel has, and it arrives hours late. |
| Globe | `website_views` | How many times it was **rendered** — on the club page here, or by any format of the announcements embed. A render, not a read: it answers "is the snippet on my site actually showing this", which is the question clubs ask about that channel. Admins viewing their own club page are not counted. |

## Discord

`/announcements_here`, modelled on `/auctions_here`: run it in the channel you want announcements
in, needs Manage Server, writes `Club.announcement_channel_id` and a `ClubHistory` row.

It is a **second channel** rather than a reuse of `auction_channel_id` on purpose — an auction
announcement is news for everybody and a club announcement is often for members only, so on most
servers the two land in different rooms.

Register it with `manage.py register_discord_commands`.

## Push

Category `CATEGORY_CLUB_ANNOUNCEMENT`, in `PUSH_ONLY_CATEGORIES`. That last part matters: every
other category falls back to email when no device can be reached, and here that would be a fourth
kind of mail nobody ticked a box for. The club picks its channels one at a time on the form; a
member who didn't get the push is reached by Discord or the club's website.

Recipients are club members who are linked to a site account, have a push-enabled device with a
live FCM token, and are not `do_not_contact`. "No non-essential emails" is *not* a bar — it is
about email, and a member who installed the app and left notifications on has opted in to exactly
this.

Sends are queued (`send_push_to_user.delay`), one task per member, so a club with 400 members
doesn't hold a form POST open for 400 FCM calls.

---

# Email: Mailchimp and Brevo

**Shipped.** An announcement can also go out as an **email campaign** through the club's own
Mailchimp or Brevo account. Never through this site's mail server: the provider owns the
unsubscribe list, and mail sent from here would reach the people who left it — which is the whole
reason the two integrations are one-way.

## Two checkboxes, and only one of them may be ticked

`send_to_mailchimp` and `send_to_brevo` are two columns rather than one `send_to_email` flag so the
row records **which** provider carried it, which is the question asked a month later. But a given
announcement may only go through one of them, and `ClubAnnouncementForm.clean` refuses the pair
outright: this site syncs every member to whichever provider lists the club has connected, so a
club with both has the **same people on both**, and ticking both would put two copies of the same
message in the same inbox. Both connected is a club part-way through moving from one to the other —
a reason to have two accounts configured, not a reason to send to both at once. When both are
ready, each checkbox says so: *your members are synced to both, so pick the one you want this to go
out through — not both.*

Each box is offered honestly, the same way the Discord one is: disabled with the fix in its help
text when the provider isn't connected or has no list chosen, and otherwise carrying the number of
subscribed contacts it would reach. That number is counted from `ClubMember` rather than asked of
the provider — two API calls per page load would be paid by every admin who never emails — so it is
an estimate of the list, not the list itself.

## Who does it send as

Nobody types an address. Both providers already hold one, and one this site invented is one they
would refuse at send time:

* **Mailchimp** — the audience's `campaign_defaults` (`mailchimp.verified_sender`), which is what
  Mailchimp itself prefills on every campaign the club sends by hand.
* **Brevo** — the first active sender on the account (`brevo.default_sender`), or
  `Club.brevo_sender_id` when the club has more than one and has chosen.

The same read fills in **`Club.donation_mailing_address`** when it is blank
(`views._prefill_donation_address`): both providers make a club type a real postal address at
signup, because US bulk commercial email has to carry one, and that is the same address printed
under the sign-off of every donation letter. Only ever fills a blank — an address the club typed
itself is the club's, and a reconnect must not quietly rewrite the return address on its mail.

## The message

One template (`auctions/templates/auctions/announcements/email.html` and its `.txt` twin), no
editor, no per-club HTML: the same sentence that went to Discord, the club's icon and name under a
rule, and nothing else. No link: the announcement is in the email in full. The greeting is the
**provider's own merge tag**, marked safe on the way in so `{{ }}` and `*| |*`
reach the provider instead of being escaped into text:

| | Greeting |
|---|---|
| Mailchimp | `*\|IF:FNAME\|*Hi *\|FNAME\|*,*\|ELSE:\|*Hi there,*\|END:IF\|*` |
| Brevo | `Hi {{ contact.FIRSTNAME \| default : "there" }},` |

**No unsubscribe link of our own.** Mailchimp is asked for `auto_footer: True` and Brevo appends
its own, both wired to the list the campaign actually went to. Ours would be a second link
unsubscribing people from something else — and with the footer off, Mailchimp refuses any content
with no `*|UNSUB|*` tag in it anyway.

**Nobody types a subject.** `ClubAnnouncement.email_subject` is always `"<Club> announcement"`.
There used to be a box, and what clubs put in it was the announcement — which is one sentence, and
was already the whole body, so the inbox showed the same words twice. `<Club> announcement` is the
line that is useful in a list of unread mail: who it is from, and that it will be short. The
`subject` column is still there for the rows written while the box existed; nothing reads it.

**Only the provider a club actually has is offered.** With one of the two connected, the other
checkbox is dropped from the form entirely rather than shown permanently disabled — a club that has
Mailchimp is not shopping for Brevo, and a dead box beside a live one can only ever be wrong. Both
are offered while *neither* is connected, because there the pair is a menu.

**Nothing is ticked when the form opens**, the website box included, even though
`show_on_website` defaults to True on the model — a row created any other way should still reach the
club's page, but a pre-ticked box on this form is a channel nobody chose. `clean()` already refuses
a send with no channel at all, so forgetting costs an error message rather than a silent publish.

## Sending, and what comes back

Sending moves **out of the request** (`tasks.send_announcement_emails`): "create campaign, set
content, send" is four round trips to somebody else's API per provider, and an admin should not
watch a spinner while Mailchimp thinks about it. Discord and push stay in the request — one call
and N enqueues — so the immediate "Discord did not accept this" feedback survives.

The task still handles the two providers independently — one failing cannot stop the other, and
each records its own `mailchimp_campaign_id` / `brevo_campaign_id` — even though the form only ever
lets one of them be ticked. Anything that goes wrong is written to `email_error` and shown on the
announcement's history row. That matters more here than elsewhere, because the send happens long
after the person who wrote it has left the page.

**Opens** (`email_opens`) are the one real read receipt any channel has — Discord has none and a
delivered push is not a read one — but they arrive hours later. They are pulled in the background
(`tasks.refresh_announcement_opens`, queued for at most the five most recent email announcements
from the last 30 days when the page is opened) and only ever *displayed* from the stored number.
A provider answering `None` means "no report yet", which is not the same as nobody opening it, so
the stored number survives.

## Scheduling, and the retract window

**Nothing is ever delivered in the request.** An announcement with no time on it gets
`scheduled_for = now + announcements.GRACE_SECONDS` (30), and the form says so: *"Going to Discord
and your website in 30 seconds. Read it back — Retract now and nobody sees it."* The mistake clubs
make is not ticking the wrong box, it is the wrong date in the sentence, and they see it the
instant the page reloads and shows it back to them. Half a minute is long enough to read what you
wrote and press Retract, short enough that nobody thinks it is broken, and it makes Retract mean
something for the two channels that can never be recalled.

An explicit `scheduled_for` is the same path with a longer wait, which is the point: there is one
way an announcement is sent, not two. `ClubAnnouncement.is_in_grace_period` is what tells them
apart for display — both are "scheduled", and *"going out in a moment, stop me"* and *"Friday at
9"* read nothing alike — and it does it by measuring the gap between `created_at` and
`scheduled_for` rather than storing a flag.

The view queues `send_scheduled_announcements` with a `countdown` for the exact moment. The beat
(every minute) is the **backstop**, not the timer: a lost countdown task costs a short delay,
never the announcement.

**`sent_at` is the column that matters**, not `scheduled_for`. It is what everything public filters
on — `latest_for_website` and the embed — because the row and its text exist from the moment
somebody writes it, and none of that may be readable before the club has actually said it. `ClubAnnouncement.save()` stamps it for any
announcement with no schedule, so a code path that makes one without going through the form can't
accidentally create something invisible.

`deliver()` writes `sent_at` **before touching a single channel**, and `send_due` claims each row
with the same UPDATE that marks it sent (`filter(sent_at__isnull=True).update(...)`, checking the
row count). Between them, two overlapping beat ticks can't both send the same announcement, and a
crash halfway through costs a channel rather than repeating the ones that already reached
everybody's phone.

Cancelling before it goes is the same Retract button, which knows the difference: it says
"Announcement cancelled. It was never sent." instead of listing what couldn't be recalled.

## Retracting

Three of the five channels can genuinely be taken back and two cannot, which is the whole reason
the button exists in the shape it does. `ClubAnnouncementRetractView` cancels a scheduled
announcement outright, deletes the Discord post, and drops it from the club's website and the
embed. It then **says what it could not take back** — a notification already on a lock screen, an
email already in an inbox, a Discord post the bot could not delete — rather than saying "retracted"
and letting the admin believe it was all undone.

The wrong-date Discord post is the case that earns it: an announcement in a channel stays there
being read for weeks, where a push and an email are read once in the hour they arrive and cannot be
recalled by anyone, us included.

## What lands in club history

`ClubHistory.applies_to="ANNOUNCEMENTS"`, its own category rather than `SETTINGS`, because sending
a message to every member is not a settings change:

| When | Row |
|---|---|
| It actually goes out (`send_due`) | `Announcement sent: <first line>` — owned by whoever wrote it, not by the beat that ran it |
| A club picked a date | `Announcement scheduled: <first line>`, at the moment they wrote it |
| Retracted or cancelled | `Announcement retracted: <first line>` |

An unscheduled announcement gets one row, written when it leaves; a scheduled one gets two.

The retract row is the one that has to be there: a retracted announcement is `is_deleted` and drops
off the club's own list, so club history becomes the only surviving record that it was ever said.

## Permission

Writing an announcement needs **`permission_send_announcements`**, its own club permission rather
than the auction-management one it originally rode on. One press reaches Discord, every member's
phone and the club's mailing list with nobody in between, which is a bigger blast radius than
adding an event to the calendar. Migration 0399 grants it to everyone who held
`permission_manage_auctions` when it landed, so nobody lost the page mid-week.

## What not to do

* **Don't send through the site's own SES.** It bypasses the provider's unsubscribe list, which is
  the entire reason the integrations are one-way. Connecting by OAuth or by API key is what buys
  native sending: the campaign is created in the club's own account and their provider sends it,
  from their own sender, against their own suppression list.
* **Don't ask the club to type a from address.** It is a field they will get wrong, and both
  providers already hold the right answer. The only thing worth asking is *which* sender, and only
  on the accounts that have more than one.
* **Don't deliver anything in the request.** The retract window is the only thing standing
  between a club and an un-recallable email with the wrong date in it, and it only exists because
  the send happens from a task.
* **Don't build a template editor.** The moment an announcement has formatting it stops being the
  same message that went to Discord, and the club is better served by its newsletter tool.
* **Don't add a "just email everyone" channel** for clubs with neither provider connected. It
  reintroduces the unsubscribe problem the integrations were built to avoid.
* **Don't use transactional send** on either provider. A campaign is addressed to the list, which
  is what applies the blocklist; a transactional send goes to whoever we name, unsubscribes and all.
