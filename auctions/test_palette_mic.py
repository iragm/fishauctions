"""Guards for the command palette's microphone, which no other test can reach.

The mic lives entirely in ``static/js/command_palette.js`` and only comes alive inside the phone
app, so the two things most recently wrong with it are invisible to the Python suite and to a
browser on a desk. Both are checked here by reading the file:

* Android's System WebView **defines** ``webkitSpeechRecognition`` and cannot use it — nothing is
  wired to a recognition service and the shell denies the page's own microphone besides. Feature
  detection therefore answers "yes" and the button does nothing when tapped, so the app bridge has
  to be asked *before* the engine is believed. Presence is not capability.
* An error from the recognizer used to be swallowed, which is why a refused microphone and a broken
  button looked identical. The message the app writes ("Microphone access is off for this app…")
  has to end up on screen.
"""

import re
from collections import namedtuple
from pathlib import Path

from django.test import SimpleTestCase

PALETTE_JS = Path(__file__).resolve().parent / "static" / "js" / "command_palette.js"

Branch = namedtuple("Branch", ["is_else", "condition"])

# `if (assistEnabled && …) {` / `} else if (assistEnabled && …) {`: the chain that decides which
# recognizer -- if any -- gets the mic button.
BRANCH_RE = re.compile(r"^(?P<else>\} else )?if \((?P<condition>assistEnabled &&.*)\) \{$")

# The listener buildRecognition() attaches for a refused or failed microphone.
ERROR_HANDLER_RE = re.compile(
    r"""speech\.addEventListener\("error", function \((?P<argument>[^)]*)\) \{(?P<body>.*?)\n      \}\);""",
    re.DOTALL,
)


def mic_branches(source):
    """Every branch of the "who gets the mic" chain, in the order the browser evaluates them.

    Keyed on ``paletteMic``: ``assistEnabled`` guards other things too (Enter with nothing
    selected, for one) and those are not part of this decision.
    """
    branches = []
    for line in source.splitlines():
        match = BRANCH_RE.match(line.strip())
        if match and "paletteMic" in match.group("condition"):
            branches.append(Branch(bool(match.group("else")), match.group("condition")))
    return branches


class PaletteMicSourceTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.source = PALETTE_JS.read_text(encoding="utf-8")

    def test_the_file_this_reads_is_the_one_with_the_mic_in_it(self):
        """A guard pointed at the wrong file, or a renamed one, would pass for ever in silence."""
        self.assertIn("command-palette-mic", self.source)
        self.assertIn("dictateGetState", self.source)

    def test_the_app_bridge_is_asked_before_the_web_speech_api_is_believed(self):
        branches = mic_branches(self.source)
        self.assertEqual(len(branches), 2, f"expected a two-branch mic chain, found {branches}")
        bridge, detection = branches
        self.assertFalse(bridge.is_else, "the bridge must be the first branch, not the fallback")
        self.assertIn("appBridge()", bridge.condition)
        self.assertNotIn(
            "SpeechRecognition",
            bridge.condition,
            "inside the app the phone's recognizer wins whatever the WebView claims to have",
        )
        self.assertTrue(detection.is_else, "feature detection is the fallback for real browsers")
        self.assertIn("SpeechRecognition", detection.condition)

    def test_a_dictation_error_is_shown_rather_than_swallowed(self):
        match = ERROR_HANDLER_RE.search(self.source)
        self.assertIsNotNone(match, "buildRecognition() no longer listens for `error`")
        self.assertTrue(
            match.group("argument").strip(),
            "the error handler takes no argument, so it cannot be reading the message off it",
        )
        body = match.group("body")
        self.assertIn("setListening(false)", body, "the button still has to come back out of its listening state")
        self.assertIn("showMicError(micErrorMessage(", body)

    def test_the_message_lands_in_the_results_pane_as_a_failure(self):
        self.assertIn('renderNote(message, "danger", "bi-exclamation-triangle-fill")', self.source)

    def test_the_app_s_own_wording_is_preferred_to_ours(self):
        """The app writes a sentence naming the phone's settings; ours can't be that specific."""
        self.assertIn("return event.message;", self.source)

    def test_a_stop_we_asked_for_is_not_reported_as_a_failure(self):
        """`stopListening()` runs on every keystroke while the mic is on -- silently, or it's noise."""
        self.assertIn("aborted", self.source)
        self.assertIn("no-speech", self.source)
        silent = re.search(r"var MIC_ERRORS_NOT_WORTH_SAYING = \{(?P<codes>[^}]*)\}", self.source)
        self.assertIsNotNone(silent, "the list of errors not worth reporting is gone")
        for code in ("aborted", "no-speech"):
            self.assertIn(code, silent.group("codes"))
