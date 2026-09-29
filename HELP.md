# Help system campaign

Tracks building the public help at `/help/`, which replaces the FAQ and the per-auction help page.
The mechanics are in the `auctions/help_guides.py` docstring. This file holds the plan, the voice,
and the raw material the guides get written from. Delete sections once they've been turned into guides.

## Status

- **Built:** `/help/` index with search, one page per guide, the account-style sidebar, personal tips,
  `sitemap.xml`, footer/navbar/app-menu links, `/auctions/<slug>/help/` redirecting to the right
  guide, `search_help` (palette + MCP) searching the guides first, and `test_help.py` failing the
  build when a page or auction rule is in no guide.
- **Guides:** eight outlines, headings only. One worked example of every mechanism is in
  `auction-rules.html` under "Charging some sellers less".
- **Email material:** not gathered. See [Source material: email](#source-material-email).
- **FAQ:** still live at `/faq/`, now out of the navbar and app menu. Remove it once the guides hold
  everything below. `FAQ.include_in_auctiontos_confirm_email`, the `help://faq` MCP resource and the
  palette seed rows in `0307` all point at it, so its removal is a migration plus those three.

## What was asked for

1. Link the help in the footer, next to the DMCA link. **Done.**
2. Link relevant guides from emails. `help_guides.guide_url(slug, auction)` builds the link; no
   email uses it yet.
3. Replace the per-auction help, and keep the three tutorial videos. **Done.** The videos are
   right, but they don't cover quick checkout or Square.
4. Remove the FAQ, after moving its content into the guides. Its content is below.
5. Make the help work over MCP. **Done.** `search_help` searches the guides.
6. Make the help follow the reader's last auction. **Done.** Personal text only goes in a
   `help-tip` div. Examples of what those tips do:
   - On the in-person guide: "The summer auction is an online auction. Running an online auction
     is probably the one you want."
   - On an admin guide, read by a bidder: "This one is mostly for the people running the auction.
     Taking part in an online auction is probably the one you want."
   - In the rules guide, next to alternate split: "The spring auction uses the label “Club Member”
     for this." (Admins only.)
7. Content:
   1. running an online auction
   2. running an in-person auction
   3. taking part in an online auction
   4. taking part in an in-person auction
   5. what the auction rules do
   6. labels, all of it
   7. everything clubs can do
   8. every page covered somewhere, without a guide per page. Step-by-step is what helps most.
8. Show an example of every feature being used, not just a description of it.
9. Public and findable on Google. Each guide has a canonical URL, a meta description and a sitemap
   entry.
10. Use the same menu as the account pages. **Done.**
11. Searchable over MCP. **Done.**
12. A test that keeps the help up to date as features are added. **Done.** `test_help.py`, working
    like the module-map test.

## Voice

Written for a 60-year-old who has kept fish all their life and has run the club's auction table
more times than they can count. They know fish and they know auctions. What they don't know is this
website.

- **Short and direct.** One idea per sentence. Say "click", not "navigate to". Name buttons
  exactly as the page does.
- **Step by step.** Numbered steps for anything done in order. Every guide starts with the big
  picture in three sentences.
- **An example, not a definition.** Not "The unsold lot fee is charged for lots that don't sell."
  Instead: "Set a $1 unsold lot fee and the guy who lists a pair of common guppies at $40 thinks
  twice."
- **Mike.** Mike is a fictional club member: clueless, well-meaning, and in every club. Use him for
  the mistake the section prevents, in one or two sentences. Make him funny and recognizable, never
  mean. He shows up at 11:59 with twenty leaking bags. He lists his ten-year-old plecos as "juvenile".
  He bids against himself. The old FAQ already had him ("Mike is kind of an idiot like that"), and so
  does the in-person video. Use `{% mike %}`, and no more than one Mike story per section.
- **No explaining things that just work.** See the "Silence is the design" memory. If the site
  links accounts by email automatically, the guide doesn't mention it.

## Writing a guide

- Name every page with `{% page "url_name" %}`. That links into the reader's own auction or club
  when it can, and it takes the page off `NOT_YET_DOCUMENTED`. The test says which names to remove.
- Explain every rule with `{% rule "field" %}`. `{% rule_value "field" %}` shows the admin how their
  own auction is set.
- Start each section with `<h2 class="h5 mt-4" id="...">`. Search and MCP results link to that
  anchor.
- Put anything about the reader's own auction in `{% helptip %}`, inside an `{% if %}`.

## Source material: email

**Not gathered yet.** Gmail was searched (support questions, contact-form messages, GitHub issue
notifications, club-admin conversations). That found about 1,850 threads, about 675 of which look
like real help material. Reading those threads in full and writing up summaries with the personal
details removed was blocked by the session's permission check for bulk handling of personal data.
No email content is in this file. To go ahead, allow that step, or export the threads yourself and
point me at the export.

When it's gathered, each entry should record: the role of the person writing (admin, bidder,
seller, prospective club), what they were trying to do, the words they used, what the answer was,
and which guide section would have saved them the email. No names, clubs, auctions, addresses or
contact details.

## Source material: the current FAQ

Scraped from the public `/faq/`, with club names removed. Grouped by the guide it belongs in.

**About the site** → index / promo

- The site lets fish clubs run auctions of fish, reptiles, amphibians, food cultures and supplies.
  It's free. It's open source. There are no person-to-person sales: sell through a club auction.
- Getting started: make a test auction. Untick "promote this auction" and set the end date to
  tomorrow. Online auctions send a series of emails that walk the creator through setup. Most
  people find online easier than in-person.
- "I bought fish and they were dead": the site doesn't handle disputes. Contact the seller, then
  the auction's organizer. Leave feedback, or charge back if your payment method allows it. Club
  admins can refund a lot, fully or partly, from the lot's page.
- Nothing illegal may be sold. Report it and the lot is removed. The FAQ's example: the one person
  who did it didn't know the item was illegal ("Mike is kind of an idiot like that").

**Taking part** → online-auctions, in-person-auctions

- Adding a lot to a club auction: make an account, find the auction, join it (the green button at
  the bottom of its page), then create a lot and choose the auction. If the auction isn't in the
  list, you haven't joined, or lot submission is closed.
- Videos on lots: put a YouTube link in the reference link field and it's embedded next to the
  photos.
- Proxy bidding, the FAQ's example: John bids $2 on guppies. Anne bids $50, and the high bid
  becomes $3. John bids $10, and it becomes $11. Anne wins at $11. (Recast with Mike.)
- There's no countdown timer, because a countdown encourages sniping. A chat message appears one
  minute before the end.
- Dynamic endings: a bid in the last 15 minutes moves that lot's end to 15 minutes after the bid.
  These don't extend it: raising your own proxy bid, buy now, or the first bid on a lot. There's a
  hard stop one hour after the auction ends.
- Recommended lots are driven by the categories you view and bid on. Bids count 10 times as much as
  views, with some randomness added. The data isn't sold.

**Running an in-person auction** → run-an-in-person-auction

- Adding people: they join through the site themselves (pre-registration), or an admin adds them
  on the Users tab. Both can be used in one auction.
- Extra admins: click a user, then tick "Grant admin permissions". It matches by email, so if it
  doesn't work, check which email they signed up with.
- Admins add lots for anybody: Users tab → the person → Add lots.
- Bidder numbers can be changed any time, and lots already added stay on the right invoice. Lot
  numbers never change, because the labels are already printed.
- Setting winners: More → Set lot winners, and fill it in for every lot. Online auctions need
  nothing.
- Cutting typos: 5% of lots can be recorded wrong, and just over 1% is the average. Have two admins
  record the same sales on Set lot winners. The second entry either confirms the first or warns
  that it differs, and the warning comes with an undo. It works after the fact from paper slips
  too.
- A seller leaving early: open their invoice. If they owe money, take it, then add an adjustment
  (discount = amount paid, reason "partial payment before auction ends") and leave the invoice open.
- Importing members: a CSV with the columns Name, Email, Bidder number, Address, Phone and Club
  Member (only email is required; "Yes" or "Member" marks a member). Users → Bulk add users →
  Import from CSV. Importing again updates people, and the CSV wins.
- Running an auction without people knowing about the site (in-person only): add users manually,
  untick "promote this auction" and invoice notifications, and turn the QR code off the labels.

**Hybrid** → run-an-in-person-auction#online-bidding

- Make an in-person auction and allow online bidding. Not recommended for new clubs.
- A silent-auction example: enter the donations under a user whose bidder number is "donations".
  Print their labels and put the bags on a table. People bid by scanning the QR code on the label.
  At the end, filter the lot admin list for "donations" and click "Sell lots to online high bidder".
  The same works with a custom checkbox field set to "Silent auction".
- If a lot was sold by mistake, you get an error. Save with "Ignore errors" from the arrow next to
  the save button.

**Rules** → auction-rules

- "Only approved sellers" and "Only approved bidders" (bidders is online only). Approve people by
  clicking their name. The filter words "no bid" and "no sell" find the ones who can't.
- Custom field: Optional or Required, with any name ("Scientific name", "Collection location").
- A custom checkbox for CARES species. It shows on labels, lot pages and the CSV, and admins can
  filter the lot list by it.
- Breeder points: a checkbox on lots, when "Use breeder points" is on. It shows a "Bred by this
  user" badge, and the CSVs carry a per-user count for the club's BAP records.
- Multiple pickup locations are online auctions only.
- Sales tax: you probably owe it. There's a rule for it.

**Invoices and payment** → both running guides

- The site makes the invoices. Payment is up to the club: Square (approved accounts), a PayPal
  batch-invoice CSV export, or cash. Paying sellers is up to the club; some mail checks to avoid
  PayPal fees.

**Emails** → running guides

- People who join through the site get a welcome email with the date, time and a map link. If
  they've added lots, a label reminder the day before. An invoice link once the invoice is ready
  or paid. People an admin added get only the invoice email. "Invoice notifications" in the rules
  controls that email.
- A reminder to join goes to anyone who has an account, looked at the auction, didn't join, and
  lives nearby. Replies go to the auction's creator.
- The envelope icons on the Users tab: no envelope means the address isn't verified yet; white means
  it's good; red means mail to it has bounced. Addresses are verified by the invoice email, so a
  bad one shows up at the next auction.

**Technical** → probably nowhere

- Load-tested to 200–300 people bidding at the same time. In-person auctions have effectively no
  limit, because only admins use the site.

## Source material: the three videos

Auto-captions, fetched from YouTube. Club names removed. Everything below is still true unless it's
marked otherwise.

### Running an online auction (20 min)

1. **The big picture.** Create the auction. Members add lots and bid until it ends. Invoices go
   out. Everyone meets at the exchange location.
2. **Account.** Create account, then confirm by email. With Gmail it's one click. Everybody in an
   online auction needs an account.
3. **Create.** Auctions → Create a new auction, enter the start date and name, then "Create online
   auction".
4. **Pickup location.** Usually a couple of days after the end. "No pickup location" means lots
   are mailed, and then people must give an address to join. Notes and directions go here too. A
   checklist at the top of the page tracks what's left to do.
5. **Rules.** Put links to the club's website and Facebook page in the description. Lot submission
   dates (the defaults are fine). Bidding dates (a week is good). Unsold lot fee ($1–2 stops
   ridiculous prices). Lot entry fee (flat, per lot). Club cut (a percentage, so "$2 + 20%").
6. **Join your own auction.** It works the same as for everyone else: Rules page → "Yes, I will be
   at this auction" → Join. The site warns about mistakes it can spot, like a pickup time before the
   auction ends. Click the warning to fix it.
7. **Adding lots.** Selling dashboard → Create a new lot → choose the auction. The category fills
   itself in. The description, quantity, "I bred this fish" and minimum bid are optional. The
   creator can turn the minimum bid and buy now fields off in the rules. After saving, add an
   image; there are rotate and add-another controls. "Copy lot" on the selling dashboard reuses
   a lot in this auction or the next one.
8. **Why people can't bid.** Somebody who hasn't joined gets the reason when they click Place bid.
   Showing the error up front was ignored, and hiding it behind the button cut these emails by
   70–80%. People who look at an auction but don't join within 24 hours, and live near it, get a
   nudge email. Replies go to the creator.
9. **Bidding.** Outbid updates in real time, plus an email. Proxy bidding: bid your maximum and the
   site bids for you, but only 49% of people figure that out. So there are dynamic endings: bids in
   the last 15 minutes extend the lot, with a hard stop one hour after the auction's end.
10. **Invoices.** Users tab → click a person to see their invoice. Adjustments go at the top
    (membership fees, discounts, PayPal fees). "Set open invoices to ready" does everyone and emails
    them. Export → PayPal invoices CSV (greyed out if no invoice is ready), then PayPal's batch
    invoice page → upload template → review → send.
11. **People who don't pay.** Filter the Users tab for people who haven't seen their invoice. Sort
    by least engagement to find risky new accounts. Leave a few days between the end and the
    exchange. For a club that's worried, "Only approved bidders" means an admin approves each
    person before they can bid. Usually no more than one or two people per auction don't pay.
12. **Labels (online).** Sellers print their own, after the end, for sold lots only, and get an
    email when it's time. The label carries the winner's name so the lot goes to the right person.
13. **Pickup.** If payment went through PayPal, no money changes hands at the exchange.
14. **Stats** (More → Stats). The activity graph shows most views and bids on the last day, and lots
    added in two spikes, at the start and at the end. Lots added early do better, so get donations
    and the big sellers in on day one. A few must-have lots with low opening bids bring people in,
    and then they bid on other things. Three or four committed sellers make an auction work.
15. **Multi-location.** The distance graph shows fewer people join the farther they are from pickup.
    Neighboring clubs each run a pickup location. Sold lots are driven to a central spot, sorted by
    the winner's location, and driven back. More → Location → add a location with its own time and
    a checked map. Each location has "incoming" and "outgoing" lists; print them and use them as the
    checklist on the day.
16. **Support.** More → Help and support. Crashes report themselves. GitHub issues are for feature
    requests. *(Out of date: the help is now `/help/`.)*

### Running an in-person auction (25 min)

1. **Account.** Only the organizer needs one. Bidders don't even need an email address.
2. **Create.** Auctions → Create a new auction → start date and name → "Create in-person auction".
   The checklist at the top covers the next steps.
3. **Rules.** Description (website, Facebook, contacts). Lot submission defaults to the week before
   up to the start. The start time is the time people are told to show up, so get it right. Fees,
   for example "$3 + 30%".
4. **Location.** More → Location, with a map and notes.
5. **Join.** Rules page → tick "I will be at the auction" → Join.
6. **Adding people.** Users → Add user (only a name is needed; get the email if you can), or Bulk
   add users (bidder numbers are generated when left blank). People can also join themselves from
   the rules page.
7. **Admins.** Click a name, then tick "Grant admin permissions". Admins can do anything the creator
   can. People added before they have an account become admins when they sign up with that email.
8. **Adding lots.** Users → Add lots next to the person's name. Check the name before saving,
   because you can't move lots to someone else afterward. Edit a lot from the Lots tab, for
   example to tick "donation". Members can add their own lots from the rules page, and should.
   "That one guy" shows up at 11:59 with 20 leaking bags. The pre-registration discount (5–15%)
   gives people a reason to add lots themselves.
9. **Labels.** Optional, but do them. The video's club searches for each person on the Users tab
   as they arrive and prints their labels. Or use More → Print labels for everyone, or only the
   unprinted ones. Printing preferences: small or large Avery sheets, or a thermal printer; the
   output is always a PDF. Do as much of this as you can before the day.
10. **Selling.** Lots tab → Set lot winners. Enter the lot number (the photo shows), the price and
    the bidder number, then press enter. There's an undo for the last one. It catches lot numbers
    and bidder numbers that don't exist, and lots that are already sold (mark it unsold first, but
    let the registration table deal with that, not the recorder). Keep bidder numbers three
    digits so a mistyped price ($819) doesn't match a real bidder.
11. **Paying out.** Users tab → filter by name → click them. At the top is what they owe or are
    owed, and how many unsold lots to take home. "Paid" emails them their invoice. Adjustments
    (extra charges) can be made while the invoice is open. *(Out of date: quick checkout and
    Square now exist and aren't shown.)*
12. **The hall.** Two laptops with printers for check-in, one laptop for walk-in buyers (take their
    details, hand over a bidder card), and a projector laptop showing the lot's photo. The recorder
    sits next to the auctioneer. Lots are sorted onto ten tables by the last digit of the lot
    number.
13. **Images.** A photo raises the selling price by about 15–16% across auctions. There's an "Add
    image" button on the Lots tab.
14. **Club member split.** Rules → a separate split for club members (for example $1 + 20% against
    $3 + 30%). Click a person, then tick "Club member"; they get a badge, and their invoice notes
    the split.
15. **Changing a bidder number.** Any time, for example after coffee lands on the card. Lot numbers
    already assigned don't change. Future sales go to the new number.
16. **Stats.** The activity graph: a peak in views 5–7 days before (people deciding whether to
    come, so good lots early matter), another just before, and one after (invoices). Joins mostly
    happen on the day. New lots should peak early, not late. Attrition: prices fall over a long
    auction; an auction where every lot sold under $10 had nowhere to fall. Buy now helps. The
    seller sets the price, the buyer pays at the table, and fewer lots reach the auctioneer.
    Auctioneer speed: about two lots a minute is good, and a separate announcer helps. The "first
    auction?" chart: you want a mix of first-timers and veterans. Take stats with a grain of salt:
    "if Mike comes up to me at the end of an auction and says he wishes we'd done X differently,
    I'm changing X."
17. **Next year.** More → Copy to new auction keeps the rules. Bulk add users → Import from old
    auctions brings the people over with the same bidder numbers and already-verified emails.
    "Download marketing list" gives a CSV of everyone from your auctions, for Mailchimp or BCC.
    Advertise on Facebook; you can't over-advertise.

### Online bidding in an in-person auction (3 min)

- Watch both of the other videos first. It's confusing for admins and bidders alike.
- Allowing online bidding adds the online bidding start and end dates to the rules.
- Bidding online works exactly like an online auction. Admins can see the maximum bid, and
  deleting a bid leaves a visible note in the lot's history.
- Setting a winner below the online maximum is an error. There's a shortcut to sell to the online
  high bidder, and the lot admin dialog can override it.
- Why not to do it: online bids only push the price up, so experienced bidders bid in the room. When
  it does make sense: buy now set to required with bidding off. That's a pre-sale; those lots never
  reach the auctioneer and go straight onto the buyer's invoice.
- Getting lots to people who weren't there is the club's problem.

## What's next

1. Gather the email material (see above), then fill in the guides one at a time. Start with the
   two running-an-auction guides, since they get the most questions.
2. Write the "quick checkout" and "Square" sections. The videos don't cover them.
3. Add `guide_url` links to the emails. The invoice email, the welcome email, the label reminder
   and the admin setup emails are the obvious first ones.
4. Once the FAQ content is in the guides, remove the FAQ.
