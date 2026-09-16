"""Guards the iPhone code path through the camera barcode scanner.

Safari has never shipped ``BarcodeDetector``, so every iPhone decodes barcodes in JavaScript with
ZXing while Android Chrome uses the native detector. A change to the fallback breaks iOS only,
nobody on an Android test device notices, and it reaches an auction as "the camera doesn't work on
my phone".

These check the source rather than behaviour -- the real thing needs a camera, a permission prompt
and Safari -- but each rule has already cost iOS users a working scanner.
"""

import re
from pathlib import Path

from django.test import SimpleTestCase

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC_JS = REPO_ROOT / "auctions" / "static" / "js"
SCANNER = STATIC_JS / "camera_scanner.js"
VENDORED_ZXING = STATIC_JS / "vendor" / "zxing.min.js"


class CameraScannerSourceTests(SimpleTestCase):
    def setUp(self):
        self.source = SCANNER.read_text()
        # The comments in that file name the APIs being avoided; checks for something that must not
        # be there run against the code only, or those explanations would fail them.
        self.code = "\n".join(line for line in self.source.splitlines() if not line.lstrip().startswith("//"))

    def test_zxing_is_self_hosted(self):
        """Loading ZXing from a CDN fails on an iPhone on guest wifi at a venue, and Android never fetches it
        at all -- so a blocked CDN looks like an iPhone-only camera fault.
        """
        self.assertIn("vendor/zxing.min.js", self.source)
        for cdn in ("cdn.jsdelivr.net", "cdnjs.cloudflare.com", "unpkg.com", "ajax.googleapis.com"):
            self.assertNotIn(cdn, self.source, f"camera_scanner.js must not load anything from {cdn}")

    def test_vendored_zxing_is_present_and_looks_like_zxing(self):
        self.assertTrue(VENDORED_ZXING.exists(), "run ./download_vendor_resources.sh")
        blob = VENDORED_ZXING.read_text(errors="ignore")
        self.assertGreater(len(blob), 100_000, "the vendored bundle looks truncated")
        self.assertIn("BrowserMultiFormatReader", blob)
        self.assertIn("NotFoundException", blob)

    def test_download_script_keeps_the_vendored_copy_fresh(self):
        """The weekly dependency workflow re-runs this script; a library missing from it stops being updated."""
        self.assertIn("vendor/zxing.min.js", (REPO_ROOT / "download_vendor_resources.sh").read_text())

    def test_does_not_use_zxing_browser_only_options(self):
        """``delayBetweenScanAttempts`` is an option of the separate @zxing/browser package. The vendored
        @zxing/library build takes a plain number of milliseconds there, so the object read as NaN and
        throttled nothing -- the decoder ran flat out on the main thread on the one platform that uses it.
        """
        self.assertNotIn("delayBetweenScanAttempts", self.code)

    def test_does_not_hand_the_camera_to_zxings_own_loops(self):
        """``decodeFromConstraints``/``decodeFromStream`` only settle once the <video> fires ``playing`` and
        swallow a refused ``play()``, so on an iPhone that won't autoplay start() never returns and nothing
        raises. ``decodeContinuously`` re-arms only for a "no barcode here" error, so one interruption ends
        scanning while the preview keeps running.
        """
        for api in ("decodeFromConstraints", "decodeFromStream", "decodeContinuously", "decodeFromVideoDevice"):
            self.assertNotIn(api, self.code, f"{api} must not drive the scan loop -- see the comment above it")

    def test_scan_loop_is_throttled(self):
        """A ZXing decode is synchronous: with no gap the phone has no main-thread time to paint the preview or
        handle taps, which reads as a frozen camera.
        """
        self.assertIn("FALLBACK_INTERVAL_MS", self.source)

    def test_failures_reach_the_operator(self):
        """Every way the camera can fail has to produce a message on the page: the original report behind these
        tests was unreproducible precisely because failure was silent.
        """
        for phrase in ("isSecureContext", "NotAllowedError", "NotReadableError", "cameraScannerDiagnostics"):
            self.assertIn(phrase, self.source)

    def test_ios_inline_playback_attributes_are_set_in_js(self):
        """iOS plays a camera stream inline only when the element is muted and playsinline. The templates set
        both, but quick_checkout.html rebuilds its <video> on every htmx swap, so the scanner reasserts them.
        """
        self.assertIn("playsinline", self.source)
        self.assertIn("video.muted = true", self.source)


class ScannerTemplateTests(SimpleTestCase):
    """Every page with a live preview needs the two attributes iOS requires on the element itself."""

    TEMPLATES = ("quick_check_in_users.html", "quick_checkout.html", "lot_queue.html")

    def test_video_elements_are_muted_and_inline(self):
        for name in self.TEMPLATES:
            source = (REPO_ROOT / "auctions" / "templates" / "auctions" / name).read_text()
            tags = [tag for tag in re.findall(r"<video\b[^>]*>", source) if "id=" in tag]
            self.assertTrue(tags, f"{name}: expected a live camera preview element")
            for tag in tags:
                self.assertIn("playsinline", tag, f"{name}: iOS won't play this inline")
                self.assertIn("muted", tag, f"{name}: iOS won't autoplay an unmuted video")

    def test_check_in_page_offers_diagnostics(self):
        source = (REPO_ROOT / "auctions" / "templates" / "auctions" / "quick_check_in_users.html").read_text()
        self.assertIn("cameraScannerDiagnostics", source)
        self.assertIn("Camera not working?", source)


class QuickCheckoutCameraStartsOffTests(SimpleTestCase):
    """The checkout camera opens only when asked for, and stays as it was left.

    It used to come up live on every load, which is right if you were just scanning bidder cards and
    wrong for an auction that doesn't print barcodes -- there the cashier got a camera pointed at the
    room on each checkout, in front of the person they were serving.
    """

    def setUp(self):
        self.source = (REPO_ROOT / "auctions" / "templates" / "auctions" / "quick_checkout.html").read_text()

    def test_the_camera_is_gated_on_an_explicit_preference(self):
        # Not just the screen size: cameraEnabled() has to ask whether it was wanted.
        self.assertIn("smallScreen.matches && cameraWanted()", self.source)

    def test_the_default_is_off(self):
        # Anything but the stored "on" -- never set, unreadable, or off -- means off.
        self.assertIn("getItem(CAMERA_PREFERENCE_KEY) === 'on'", self.source)

    def test_storage_failures_leave_it_off_rather_than_breaking_the_page(self):
        """A private window (or blocked site data) throws on localStorage rather than returning null, and an
        uncaught throw would take the whole checkout script with it.
        """
        wanted = self.source.split("function cameraWanted()")[1].split("function rememberCameraPreference")[0]
        self.assertIn("catch", wanted)
        self.assertIn("return false", wanted)

    def test_there_is_a_button_to_turn_it_on(self):
        self.assertIn('id="checkout-camera-toggle"', self.source)
        self.assertIn("rememberCameraPreference", self.source)

    def test_the_choice_is_remembered_across_the_tap_to_pay_round_trip(self):
        # Scan a card, tap to pay, come back and scan the next: the camera has to be where it was
        # left, which is why the preference is stored rather than per page load.
        self.assertIn("setItem(CAMERA_PREFERENCE_KEY", self.source)
