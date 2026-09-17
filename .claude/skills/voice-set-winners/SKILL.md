---
name: voice-set-winners
description: The voice grammar for setting lot winners: what is data in VoiceGrammar rather than app code, and the page-side fallback matcher. Use when touching auctions/voice.py, dynamic_set_lot_winner.html, or the mobile voice endpoints.
---

# Voice set winners

The app listens (WKWebView has no Web Speech API); the **grammar is data** in `auctions/voice.py` and
the `VoiceGrammar` row, so a new word is an admin edit, not an app release. Fix voice problems here.

- `GET /api/mobile/config/` serves the grammar. `…/auctions/<slug>/voice/vocabulary/` serves the lot
  and bidder numbers that exist in this auction; matching is against those, never free text.
- `voice.page_config` sends both to the page. `voiceParse` / `voiceMatchLocally` in
  `dynamic_set_lot_winner.html` match if the app hasn't answered in `voiceUnmatchedGraceMs` (1200).
  Everything goes through `voiceHandleCommand`.
- Never invent a value or guess a bare number's slot. Two matches → amber, both offered
  (`VoiceGrammar.homophones`). A currency symbol anchors the price.
- No match says why and doesn't repeat the number. A late duplicate transcript is dropped.
