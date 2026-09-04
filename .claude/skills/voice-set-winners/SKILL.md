---
name: voice-set-winners
description: The voice grammar for setting lot winners: what is data in VoiceGrammar rather than app code, and the page-side fallback matcher. Use when touching auctions/voice.py, dynamic_set_lot_winner.html, or the mobile voice endpoints.
---

# Voice-driven set winners

The app does the listening (iOS `WKWebView` has no Web Speech API), but the **grammar is data**: in
`auctions/voice.py` and the single `VoiceGrammar` row, so "the auctioneer says 'hammer' where we
expected 'sold'" is an admin edit rather than an app release.

- `GET /api/mobile/config/` serves the grammar; the app merges it over what it shipped with.
- `GET /api/mobile/auctions/<slug>/voice/vocabulary/` serves the lot and bidder numbers legal **in
  this auction**. Both sides match the utterance against values that actually exist rather than
  transcribing freely and repairing the text.
- `voice.page_config` also sends the grammar and vocabulary down with the page;
  `voiceParse` / `voiceMatchLocally` in `auctions/templates/auctions/dynamic_set_lot_winner.html`
  match the transcript themselves after the app has had `voiceUnmatchedGraceMs` (1200 ms) to answer.
  A build that does match is never second-guessed, and everything the fallback produces goes through
  `voiceHandleCommand` (same green/amber threshold, same `VoiceCommandLog` row, same Confirm
  button).
- The matcher never invents a value and never guesses which slot a bare number belongs to. Both
  readings of a run of number words are tried and the vocabulary picks between them. Two matches
  means an amber field offering both (`VoiceGrammar.homophones`). Price is the one field with no
  list; a currency symbol in front of a number is the price anchor.
- When it matches nothing it says why (`heard "lot one" — no lot like that in this auction`) and
  deliberately does not repeat the number back. A late `command` for an utterance the page has
  already handled is dropped by exact transcript text within three grace windows.
- Fix voice problems on the page, not in the app: the app ships through two app stores.
