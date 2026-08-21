// Shared camera barcode scanner used by the quick check-in, quick checkout and lot queue pages.
//
// One place owns the actual barcode-reading logic (native BarcodeDetector with a ZXing
// fallback, plus per-frame duplicate suppression) so future improvements to how a
// barcode is read don't drift between the pages. Each page instantiates its own
// controller with its <video> element and a callback that decides what a decoded value
// means (check a member in, or pull up an invoice).
//
//   const scanner = window.createCameraScanner({
//     video: document.getElementById("scanner-video"),
//     onCode: async (value) => { ... },     // called with each newly-decoded value
//     onStatus: (message, level) => { ... } // optional; camera state changes
//   });
//   await scanner.start();  // getUserMedia + decode loop
//   await scanner.stop();   // releases the camera
//   scanner.resetDuplicate(); // forget the last value so the same code can fire again
//
// onCode is awaited, so a slow handler won't be re-entered for the same frame.
//
// iPhones take a completely different path through this file than Android does: Safari has
// never shipped BarcodeDetector, so every iOS device falls back to decoding in JavaScript with
// ZXing, while Android Chrome decodes natively and never touches that code. Anything below
// marked "fallback" is therefore effectively iOS-only, and iOS-only bugs live there.
// window.cameraScannerDiagnostics() dumps what this device actually supports -- see the
// "Camera not working?" panel on the quick check-in page.
(function () {
  if (window.createCameraScanner) {
    return;
  }

  // Self-hosted (see VENDOR_LIBRARIES.md): loading this from a CDN meant iPhones -- the only
  // devices that need it -- couldn't scan at all on the flaky guest wifi typical of an auction
  // venue, while Android carried on working off the native detector. Resolved relative to this
  // script's own URL so it follows STATIC_URL without needing anything from the template.
  var ZXING_SRC = (function () {
    var tag = document.currentScript;
    if (tag && tag.src) {
      return tag.src.replace(/camera_scanner\.js(\?.*)?$/, "vendor/zxing.min.js");
    }
    return "/static/js/vendor/zxing.min.js";
  })();
  // Formats a membership card / bidder-number / paddle barcode might use.
  var FORMATS = ["code_128", "qr_code", "ean_13", "ean_8", "upc_a", "upc_e"];

  var userAgent = navigator.userAgent || "";
  var IS_IOS =
    /iPad|iPhone|iPod/.test(userAgent) || (userAgent.indexOf("Macintosh") !== -1 && navigator.maxTouchPoints > 1);
  // Set by whichever scanner last failed, so the diagnostics panel can report it.
  var lastFailure = "";

  // Camera access inside an embedded WebView is up to the host app, and most hosts say no. Only
  // used to make the error message actionable ("open this in Safari"), never to block a scan --
  // a WebView that does grant the camera still works fine.
  function embeddedBrowserName() {
    // The app appends its own token to the WebView's User-Agent (see MobileAppMiddleware); the JS
    // bridge is the backstop for a WebView that didn't get the custom agent set.
    if (/FishAuctionsApp/.test(userAgent) || window.flutter_inappwebview) {
      return "the auction.fish app";
    }
    if (/FBAN|FBAV|FB_IAB/.test(userAgent)) {
      return "the Facebook app";
    }
    if (/Instagram/.test(userAgent)) {
      return "the Instagram app";
    }
    if (/\bLine\/|LinkedInApp|Snapchat|Pinterest|GSA\//.test(userAgent)) {
      return "an in-app browser";
    }
    // iOS WKWebView: an iOS user agent with no browser marker of its own.
    if (IS_IOS && !/Safari\//.test(userAgent) && !/CriOS|FxiOS|EdgiOS/.test(userAgent)) {
      return "an in-app browser";
    }
    return "";
  }

  // Reasons the camera can't even be attempted. Checked before getUserMedia so the operator gets
  // "open this over https" instead of "undefined is not an object".
  function unsupportedReason() {
    if (window.isSecureContext === false) {
      return "The camera only works over a secure (https) connection. Open this page with https:// and try again.";
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      var app = embeddedBrowserName();
      if (app) {
        return (
          "Opening this page in " +
          app +
          " doesn't allow camera access. Open it in Safari or Chrome instead, or use a USB barcode scanner."
        );
      }
      return "This browser can't use the camera. Use a USB barcode scanner, or type the number in.";
    }
    return "";
  }

  // getUserMedia's errors are all phrased for developers; translate the ones an operator standing
  // at a check-in table can actually act on.
  function cameraErrorMessage(error) {
    var name = (error && error.name) || "";
    var app = embeddedBrowserName();
    if (name === "NotAllowedError" || name === "SecurityError" || name === "PermissionDeniedError") {
      if (app) {
        return "Camera access is blocked in " + app + ". Open this page in Safari or Chrome and try again.";
      }
      if (IS_IOS) {
        // Safari remembers a "Don't Allow" per site, and never asks again -- so this looks to the
        // operator like the camera is simply broken until they clear it here.
        return "Safari is blocking the camera for this site. Tap “aA” at the left of the address bar, choose Website Settings, set Camera to Allow, then reload this page.";
      }
      return "Camera access was denied. Allow the camera for this site in your browser settings, then reload this page.";
    }
    if (name === "NotFoundError" || name === "OverconstrainedError" || name === "DevicesNotFoundError") {
      return "No camera was found on this device. Use a USB barcode scanner, or type the number in.";
    }
    if (name === "NotReadableError" || name === "TrackStartError" || name === "AbortError") {
      return "Another app is using the camera. Close it (and any other tab with the camera open), then try again.";
    }
    var detail = (error && error.message) || error || "unknown error";
    return "Unable to start the camera: " + detail + (name ? " (" + name + ")" : "");
  }

  function loadZxing() {
    if (window.ZXing) {
      return Promise.resolve(window.ZXing);
    }
    return new Promise(function (resolve, reject) {
      var script = document.createElement("script");
      script.src = ZXING_SRC;
      script.onload = function () {
        if (window.ZXing) {
          resolve(window.ZXing);
        } else {
          reject(new Error("The barcode reader loaded but did not start up."));
        }
      };
      // Script errors carry no useful message of their own, so supply one. It's served from this
      // site, so a failure here is a bad connection to us or a missing collectstatic, not a CDN.
      script.onerror = function () {
        reject(new Error("Couldn't load the barcode reader. Reload the page and try again."));
      };
      document.head.appendChild(script);
    });
  }

  // Enough for the reporter of an unreproducible "the camera doesn't work" to screenshot.
  window.cameraScannerDiagnostics = function () {
    return [
      "userAgent: " + userAgent,
      "secure context: " + (window.isSecureContext !== false ? "yes" : "no"),
      "camera API: " + (navigator.mediaDevices && navigator.mediaDevices.getUserMedia ? "available" : "MISSING"),
      "native detector: " + ("BarcodeDetector" in window ? "yes" : "no (using the ZXing fallback)"),
      "barcode reader loaded: " + (window.ZXing ? "yes" : "not yet"),
      "embedded browser: " + (embeddedBrowserName() || "no"),
      "last camera error: " + (lastFailure || "none"),
    ].join("\n");
  };

  window.createCameraScanner = function (options) {
    options = options || {};
    var video = options.video;
    var onCode = options.onCode || function () {};
    var onStatus = options.onStatus || function () {};
    // How long the same decoded value is suppressed after a *successful* read, so the camera
    // (which decodes the same code on every frame) doesn't fire it dozens of times a second.
    var DUP_WINDOW_MS = options.duplicateWindowMs || 2500;
    // Shorter window used after an *invalid* read: if onCode returns false, the same card can be
    // re-tried this quickly (a fresh, hopefully cleaner frame) instead of being locked out for the
    // full DUP_WINDOW_MS. Fast enough to feel responsive, slow enough not to spam error beeps.
    var RETRY_WINDOW_MS = options.retryWindowMs || 700;
    // Gap between ZXing decode attempts. ZXing decodes synchronously on the main thread, so with no
    // gap at all the phone has no time left to paint the preview or handle taps.
    var FALLBACK_INTERVAL_MS = options.fallbackIntervalMs || 120;

    var stream = null;
    var detector = null;
    var animationFrameId = null;
    var fallbackTimerId = null;
    var zxingReader = null;
    var zxingLib = null;
    var isScanning = false;
    var lastValue = "";
    var lastValueTime = 0;
    var tapToPlayHandler = null;

    async function handleCode(rawValue) {
      var value = String(rawValue || "").trim();
      if (!value) {
        return;
      }
      var now = Date.now();
      // Suppress rapid repeats of the same value within the active window.
      if (value === lastValue && now - lastValueTime < DUP_WINDOW_MS) {
        return;
      }
      lastValue = value;
      lastValueTime = now;
      var result = await onCode(value);
      // onCode returns false when the value was invalid/unrecognized; back-date the timestamp so the
      // remaining suppression is only RETRY_WINDOW_MS and the operator can immediately re-present the
      // card. A successful read keeps the full DUP_WINDOW_MS.
      if (result === false) {
        lastValueTime = now - (DUP_WINDOW_MS - RETRY_WINDOW_MS);
      }
    }

    // Ask the camera for continuous autofocus once the track is live. Small barcodes and phone-screen
    // membership cards read far more reliably when the lens keeps refocusing; capabilities vary by
    // device, so every hint is best-effort and failures are ignored.
    function applyTrackEnhancements() {
      if (!stream) {
        return;
      }
      var track = stream.getVideoTracks()[0];
      if (!track || !track.getCapabilities) {
        return;
      }
      var caps = track.getCapabilities();
      var advanced = [];
      if (caps.focusMode && caps.focusMode.indexOf("continuous") !== -1) {
        advanced.push({ focusMode: "continuous" });
      }
      if (advanced.length) {
        track.applyConstraints({ advanced: advanced }).catch(function () {});
      }
    }

    // Prefer a higher-resolution stream so small/screen barcodes carry enough detail to decode.
    // `ideal` degrades gracefully on cameras that can't hit it.
    var NATIVE_VIDEO_CONSTRAINTS = { facingMode: "environment", width: { ideal: 1920 }, height: { ideal: 1080 } };
    // The fallback decodes every frame in JavaScript, and the work scales with the pixel count: a
    // 1080p frame costs over four times what a 720p one does, which is the difference between a
    // usable scanner and a wedged phone. 720p still resolves Code 128 and QR at arm's length.
    var FALLBACK_VIDEO_CONSTRAINTS = { facingMode: "environment", width: { ideal: 1280 }, height: { ideal: 720 } };

    // Cameras vary wildly in what resolutions they'll agree to, so step down rather than give up.
    // Permission and hardware failures are re-thrown immediately -- relaxing constraints can't fix
    // a camera the user said no to, or one that isn't there.
    async function openCamera(constraints) {
      var attempts = [
        { video: constraints, audio: false },
        { video: { facingMode: "environment" }, audio: false },
        { video: true, audio: false },
      ];
      var failure = null;
      for (var i = 0; i < attempts.length; i++) {
        try {
          return await navigator.mediaDevices.getUserMedia(attempts[i]);
        } catch (error) {
          failure = error;
          var name = error && error.name;
          if (name === "NotAllowedError" || name === "SecurityError" || name === "NotFoundError") {
            throw error;
          }
        }
      }
      throw failure;
    }

    function attachStream() {
      // iOS will only play a camera stream inline if the element is muted and playsinline. The
      // templates set both, but set them here too so a page that builds its <video> dynamically
      // (or a future caller) can't silently lose them -- and set the muted *property*, since the
      // attribute alone only supplies a default.
      video.muted = true;
      video.setAttribute("muted", "");
      video.setAttribute("playsinline", "");
      video.setAttribute("autoplay", "");
      video.srcObject = stream;
    }

    async function tryPlay() {
      try {
        await video.play();
        return true;
      } catch (error) {
        return false;
      }
    }

    // iOS refuses to autoplay video in Low Power Mode even when it's muted, inline and camera-backed,
    // and it reports this as nothing more than a rejected play() promise. The stream is live and the
    // permission was granted, so don't tear the session down -- a single tap starts it.
    function offerTapToPlay() {
      onStatus("Tap the camera preview to start scanning.", "warning");
      tapToPlayHandler = async function () {
        if (!isScanning) {
          return;
        }
        if (await tryPlay()) {
          removeTapToPlay();
          onStatus("Scanning...", "info");
        }
      };
      video.addEventListener("click", tapToPlayHandler);
    }

    function removeTapToPlay() {
      if (tapToPlayHandler) {
        video.removeEventListener("click", tapToPlayHandler);
        tapToPlayHandler = null;
      }
    }

    async function startPlaying() {
      attachStream();
      if (!(await tryPlay())) {
        offerTapToPlay();
        return;
      }
      onStatus("Scanning...", "info");
    }

    // A phone call, the screen locking, or switching apps interrupts the camera. iOS often comes back
    // with the preview paused (looking frozen rather than broken) and sometimes with the track ended.
    async function onVisibilityChange() {
      if (!isScanning || document.hidden || !video) {
        return;
      }
      if (video.paused && !(await tryPlay()) && !tapToPlayHandler) {
        offerTapToPlay();
      }
    }

    function onTrackEnded() {
      if (isScanning) {
        onStatus("The camera was interrupted. Turn it back on to keep scanning.", "danger");
      }
    }

    function watchStream() {
      document.addEventListener("visibilitychange", onVisibilityChange);
      var track = stream && stream.getVideoTracks()[0];
      if (track) {
        track.addEventListener("ended", onTrackEnded);
      }
    }

    // Both decoders need a frame with real dimensions; asking before then throws, and on iOS the
    // gap between "playing" and "has dimensions" is long enough to matter.
    function frameIsReady() {
      return video && video.readyState >= 2 && video.videoWidth > 0;
    }

    async function startNativeScanner() {
      detector = new BarcodeDetector({ formats: FORMATS });
      stream = await openCamera(NATIVE_VIDEO_CONSTRAINTS);
      applyTrackEnhancements();
      await startPlaying();
      watchStream();
      var scanFrame = async function () {
        if (!isScanning) {
          return;
        }
        try {
          if (frameIsReady()) {
            var barcodes = await detector.detect(video);
            if (barcodes.length) {
              await handleCode(barcodes[0].rawValue);
            }
          }
        } catch (error) {
          console.error(error);
        }
        if (isScanning) {
          animationFrameId = requestAnimationFrame(scanFrame);
        }
      };
      animationFrameId = requestAnimationFrame(scanFrame);
    }

    function buildZxingHints(ZXing) {
      var hints = new Map();
      // Spend more effort per frame -- worth it for glare/screen reads on iOS.
      hints.set(ZXing.DecodeHintType.TRY_HARDER, true);
      var formatMap = {
        code_128: "CODE_128",
        qr_code: "QR_CODE",
        ean_13: "EAN_13",
        ean_8: "EAN_8",
        upc_a: "UPC_A",
        upc_e: "UPC_E",
      };
      var possible = [];
      FORMATS.forEach(function (name) {
        var zx = formatMap[name];
        if (zx && ZXing.BarcodeFormat[zx] !== undefined) {
          possible.push(ZXing.BarcodeFormat[zx]);
        }
      });
      if (possible.length) {
        hints.set(ZXing.DecodeHintType.POSSIBLE_FORMATS, possible);
      }
      return hints;
    }

    function isNotFound(error) {
      // "No barcode in this frame", thrown on nearly every frame -- not worth logging.
      if (zxingLib && error instanceof zxingLib.NotFoundException) {
        return true;
      }
      return !!(error && typeof error.getKind === "function" && error.getKind() === "NotFoundException");
    }

    // This drives the decode loop itself instead of handing the camera to ZXing's
    // decodeFromConstraints()/decodeContinuously(), all three of whose behaviours are only ever
    // hit on iOS and each of which reads to an operator as "the camera doesn't work":
    //  * decodeFromConstraints() resolves only once the <video> fires `playing`, and it swallows a
    //    refused play(). On a phone that won't autoplay, the promise never settles at all, so
    //    start() hangs forever with a black box and no error.
    //  * decodeContinuously() re-arms its loop only for a "no barcode here" error. Any other throw
    //    -- an interrupted camera, a frame captured before the video has dimensions -- ends
    //    scanning permanently while the preview keeps running, so the page looks alive but is dead.
    //  * both of its delays default to 0ms, so it decodes flat out on the main thread. (The
    //    `delayBetweenScanAttempts` option that used to be passed here belongs to the separate
    //    @zxing/browser package; @zxing/library's second constructor argument is a plain number of
    //    milliseconds, so it was being read as NaN and throttling nothing.)
    async function scanFallbackFrame() {
      if (!isScanning) {
        return;
      }
      if (frameIsReady()) {
        try {
          var result = zxingReader.decode(video);
          if (result) {
            await handleCode(result.getText());
          }
        } catch (error) {
          if (!isNotFound(error)) {
            console.error(error);
          }
        }
      }
      if (isScanning) {
        fallbackTimerId = setTimeout(scanFallbackFrame, FALLBACK_INTERVAL_MS);
      }
    }

    // Safari has no BarcodeDetector, on any Apple device or version, so this is the iPhone path.
    async function startFallbackScanner() {
      zxingLib = await loadZxing();
      zxingReader = new zxingLib.BrowserMultiFormatReader(buildZxingHints(zxingLib));
      stream = await openCamera(FALLBACK_VIDEO_CONSTRAINTS);
      applyTrackEnhancements();
      await startPlaying();
      watchStream();
      scanFallbackFrame();
    }

    async function start() {
      if (isScanning) {
        return;
      }
      var blocked = unsupportedReason();
      if (blocked) {
        lastFailure = blocked;
        onStatus(blocked, "danger");
        throw new Error(blocked);
      }
      isScanning = true;
      onStatus("Starting the camera...", "info");
      try {
        // Some Android WebViews expose BarcodeDetector but can't actually build one for these
        // formats; fall back rather than losing the camera entirely.
        var useNative = false;
        if ("BarcodeDetector" in window) {
          try {
            new BarcodeDetector({ formats: FORMATS });
            useNative = true;
          } catch (error) {
            useNative = false;
          }
        }
        if (useNative) {
          await startNativeScanner();
        } else {
          await startFallbackScanner();
        }
      } catch (error) {
        console.error(error);
        await stop();
        var message = cameraErrorMessage(error);
        lastFailure = ((error && error.name) || "Error") + ": " + ((error && error.message) || error);
        onStatus(message, "danger");
        throw error;
      }
    }

    async function stop() {
      isScanning = false;
      removeTapToPlay();
      document.removeEventListener("visibilitychange", onVisibilityChange);
      if (animationFrameId) {
        cancelAnimationFrame(animationFrameId);
        animationFrameId = null;
      }
      if (fallbackTimerId) {
        clearTimeout(fallbackTimerId);
        fallbackTimerId = null;
      }
      if (zxingReader) {
        zxingReader.reset();
        zxingReader = null;
      }
      if (stream) {
        stream.getTracks().forEach(function (track) {
          track.removeEventListener("ended", onTrackEnded);
          track.stop();
        });
        stream = null;
      }
      if (video) {
        video.srcObject = null;
      }
      onStatus("Camera is off.", "secondary");
    }

    return {
      start: start,
      stop: stop,
      resetDuplicate: function () {
        lastValue = "";
        lastValueTime = 0;
      },
      isScanning: function () {
        return isScanning;
      },
    };
  };
})();
