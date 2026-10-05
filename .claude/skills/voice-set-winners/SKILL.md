---
name: voice-set-winners
description: Voice on set lot winners: the server reading the auctioneer's speech as sales, the page that listens (app bridge or OpenAI in a browser), and the grammar in VoiceGrammar. Use when touching auctions/voice_interpreter.py, auctions/voice.py, auctions/views/voice.py, dynamic_set_lot_winner.html, or the mobile voice endpoints.
---

# Voice set winners

Passive: the phone or laptop sits by the auctioneer, who calls the auction as usual, and voice replaces
the bid recorder. **The server reads; nothing else does.** Fix reading problems in
`auctions/voice_interpreter.py`, with a case in `test_voice_interpreter.py`.

- The page keeps a *window* (transcripts since the lot on the block came up) and posts it to
  `VoiceInterpretView`. The answer is commands for the three fields plus `sold`/`unsold`/`undo`, and
  `carry` -- the window after the close, applied only once the save lands. One request at a time.
- Listening: in the app, the app's recognizer pushes `transcript` events (a `state` re-arm ends a phrase);
  its `command` events are ignored. In a browser, the page streams the mic to OpenAI over WebRTC with
  a key from `VoiceCloudSessionView` (`VoiceGrammar.cloud_model`; the live model's turns are the page's
  to commit).
- Never invent a value: lots and bidders come from `build_vocabulary`; prices from the close or the
  last bid the auctioneer *had*. "For", "to" and "won" are never digits. A lot named after "sold" is
  the next lot's.
- A close missing a piece waits; a new lot or new round of bidding first makes it a `missed` row
  ("not recorded" on the voice log page), never a guess. `previous` stops a repeated close being sold
  to the next lot.
- End to end: OpenAI TTS of an auctioneer's script as a WAV, played as Chrome's microphone
  (`--use-fake-device-for-media-stream --use-file-for-fake-audio-capture=auction.wav%noloop`) in
  Selenium against the local site, sells real lots through the real OpenAI session.
