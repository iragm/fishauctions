# Help system campaign

Tracks building the public help at `/help/`, which replaces the FAQ and the per-auction help page.
The mechanics are in the `auctions/help_guides.py` docstring. This file holds the plan, the voice,
and what's left. Delete sections once they're done.

## Status

- **Built:** `/help/` index with search, one page per guide, the account-style sidebar, personal tips,
  `sitemap.xml`, footer/navbar/app-menu links, `/auctions/<slug>/help/` redirecting to the right
  guide, `search_help` (palette + MCP) searching the guides first, and `test_help.py` failing the
  build when a page or auction rule is in no guide.
- **Guides:** nineteen: the eight planned, plus `mobile-app`, `payments` (Square and PayPal),
  `scanning`, `bagging-fish` (from the "Transporting fish" blog post) and `ai-agents`, and the club
  guides split out of `clubs`: `club-membership`, `club-email`, `club-events`,
  `breeder-award-programs`, `club-donations` and `club-money`. Every page, every auction rule and
  every club setting is in a guide or excused, so all three backlogs are empty.
- **About page:** `/about/` redirects to `/help/`. `PromoSite` is still the signed-out landing page
  when `ENABLE_PROMO_PAGE` is on.
- **Pages that became guides:** `/square/`, `/paypal/` and `/ai/` redirect into `payments` and
  `ai-agents`, keeping their URLs. The reader's own connections are drawn there by `square_account`,
  `paypal_account` and `ai_connections` in `help_tags`; `/ai/` still takes the key and disconnect POSTs.
- **Dynamic:** `HelpContext` quotes the reader back to themselves: their auction's fees in a worked
  example (`{% fee_example %}`), a "Yours:" badge on every rule for its admins, what their labels
  print, whether their in-person auction takes online bids, an account checklist, and numbers from
  the last auction they ran once it's pretty much over (`help_stats.auction_facts`). Site-wide
  numbers (photo sell rates, late bids, unsold rates) are `help_stats.site_stats`, counted daily. What a photo is worth at big in-person auctions is `help_stats.in_person_photos`, which replaced the images chart on the stats page.
- **AI tip:** every help page tells a reader with no agent connected to connect one.
- **Setting adoption:** moved off the usability dashboard. `{% rule %}` inside the rules guide shows
  `FieldAdoption.usage` as a badge ("Used by 55% of auctions"). The dashboard keeps a one-line pointer.
- **Source material:** the email threads (795), the FAQ and the three video transcripts were all
  written into the guides, so they're gone from here. The scrubbed per-thread notes are still at
  `~/.claude/projects/-workspace/help-email-notes.md`, outside the repo; the raw threads are deleted.
- **FAQ:** still live at `/faq/`, out of the navbar and app menu. Everything in it is now in a guide.

## What was asked for

1. Link the help in the footer, next to the DMCA link. **Done.**
2. Link relevant guides from emails. `help_guides.guide_url(slug, auction)` builds the link; no
   email uses it yet.
3. Replace the per-auction help, and keep the three tutorial videos. **Done.** Quick checkout and
   Square, which the videos don't cover, are written up in the running guides.
4. Remove the FAQ, after moving its content into the guides. **Content moved.** Removal is a
   migration plus `FAQ.include_in_auctiontos_confirm_email`, the `help://faq` MCP resource and the
   palette seed rows in `0307`.
5. Make the help work over MCP. **Done.**
6. Make the help follow the reader's last auction. **Done.**
7. Content. **Done**, all eight topics plus the phone app.
8. Show an example of every feature being used. **Done** as far as the guides go; worked tables for
   fees, the pre-registration discount and proxy bidding.
9. Public and findable on Google. **Done.**
10. Use the same menu as the account pages. **Done.**
11. Searchable over MCP. **Done.**
12. A test that keeps the help up to date. **Done.**

## Voice

Written for someone who knows fish and is moderately familiar with auctions, but has never used
this website. Say what a runner or a bump is once; never explain what a bag of guppies is.

- **Short and direct.** One idea per sentence. Say "click", not "navigate to". Name buttons
  exactly as the page does.
- **Step by step.** Numbered steps for anything done in order. Every guide starts with the big
  picture in three sentences.
- **An example, not a definition.** Not "The unsold lot fee is charged for lots that don't sell."
  Instead: "Set a $1 unsold lot fee and the guy who lists a pair of common guppies at $40 thinks
  twice."
- **Mike.** Mike is a fictional club member: clueless, well-meaning, and in every club. Use him for
  the mistake the section prevents, in one or two sentences. Make him funny and recognizable, never
  mean. Use `{% mike %}` (it draws the site's fish), and no more than one Mike story per section.
- **Tips.** Advice worth pulling out of the text goes in `{% tip %}`: the midnight end time, "don't
  put dates in the rules text". Not a paragraph with a box round it.
- **Formatting.** Short paragraphs, lists, tables for anything with two columns, cards for side by
  side. Nothing bigger than h5.
- **No explaining things that just work.** See the "Silence is the design" memory. The time zone is
  set from the browser, so no guide mentions it.

## Writing a guide

- Name every page with `{% page "url_name" %}`, and pass the words the page itself uses:
  `{% page "auction_tos_list" "Users" %}`. The palette labels are written for a model, not a reader.
- Explain every rule with `{% rule "field" %}`, once per guide (it's an anchor). The label comes from
  the form's `LABELS`, so it matches the rules page.
- Start each section with `<h2 class="h5 mt-4" id="...">`, subsections `<h3 class="h6 mt-3">`.
  Search and MCP results link to the h2 anchors.
- Put anything about the reader's own auction in `{% helptip %}`, inside an `{% if %}`.

## What's next

1. Add `guide_url` links to the emails: the invoice email, the welcome email, the label reminder
   and the admin setup emails.
2. Remove the FAQ (item 4 above).
3. `help_stats.site_stats` has only run on dev data. Check its query time against prod before relying
   on the daily cache.
4. When the app is in the App Store and on Google Play, replace the pre-release note at the top of
   `mobile-app.html` with where to get it.

## Found while writing

- **Fixed:** moving an online auction's end now moves its unsold lots, until the last hour before the
  old end (`signals.on_save_auction`), and the rules page refuses an end less than an hour away.
- **Fixed:** the palette's `feedback` route was "Leave feedback about the site"; it's feedback on lots
  bought and sold.
- **Fixed:** the paid invoice email said "You owe a total of $X" (migration 0472).
- **Removed:** `set_lot_winners_url` and its autocomplete and presentation modes (migration 0473).
- **Left alone, and kept out of the guides on purpose:** `preferred_bidder_number`.
- **Fixed in the labels guide:** the seller's name is on by default (unsold labels only), "Origin"
  was a custom field, not a choice, and a sold label names its destination only with several
  locations.
- **Added:** `move_queued_lot` and `step_queue` MCP tools, so an agent can bump a lot and walk the
  queue; `reorder_queue` in `views/selling.py` is the queue page's drag, shared.
- **Not changed:** the club settings page still offers PayPal's OAuth connect to anyone whose
  `paypal_enabled` is on; the guides only describe the credentials route.
- **Not changed:** `advanced_lot_adding` is on no form. The Selling page says "It's free, with or
  without an auction", but independent lots are off with no plans to turn them back on.
