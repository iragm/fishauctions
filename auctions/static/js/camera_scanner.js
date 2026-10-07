// Shared camera barcode scanner used by the quick check-in, quick checkout and lot queue pages.
//
// One place owns the barcode reading -- native BarcodeDetector with a ZXing fallback, plus
// per-value duplicate suppression -- so it can't drift between the pages. Each page makes its own
// controller with its <video> element and a callback that decides what a decoded value means:
// check a member in, or pull up an invoice.
//
//   const scanner = window.createCameraScanner({
//     video: document.getElementById("scanner-video"),
//     onCode: async (value) => { ... },     // called with each newly-decoded value
//     onStatus: (message, level) => { ... }, // optional; camera state changes
//     formats: ["qr_code"],                  // optional; fewer is faster, ZXing most of all
//   });
//   await scanner.start();  // getUserMedia + decode loop
//   await scanner.stop();   // releases the camera
//   scanner.resetDuplicate(); // forget recent values so the same code can fire again
//
// onCode is awaited, so a slow handler won't be re-entered -- and the camera reads nothing until it
// returns. A page that posts every read (the lot queue) returns at once and reports the answer when
// it lands.
//
// Only what the preview shows is read. The previews are object-fit: cover boxes, so most of each
// frame is cropped off screen, and a code out there is one the operator never aimed at.
//
// iPhones take a completely different path through this file: Safari has never shipped
// BarcodeDetector, so every iOS device decodes in JavaScript with ZXing while Android Chrome
// decodes natively and never touches that code. Anything marked "fallback" is therefore iOS-only,
// and so are its bugs. (The app gives its WebView a BarcodeDetector backed by the phone's own
// reader, so in the app an iPhone takes the native path.) window.cameraScannerDiagnostics() dumps
// what this device supports -- see the "Camera not working?" panel on the quick check-in page.
(function () {
  if (window.createCameraScanner) {
    return;
  }

  // Self-hosted (see VENDOR_LIBRARIES.md): from a CDN, iPhones -- the only devices that need it --
  // couldn't scan at all on the flaky guest wifi typical of a venue, while Android carried on off
  // the native detector. Resolved relative to this script's URL, so it follows STATIC_URL.
  var ZXING_SRC = (function () {
    var tag = document.currentScript;
    if (tag && tag.src) {
      return tag.src.replace(/camera_scanner\.js(\?.*)?$/, "vendor/zxing.min.js");
    }
    return "/static/js/vendor/zxing.min.js";
  })();
  // Formats a membership card / bidder-number / paddle barcode might use.
  var FORMATS = ["code_128", "qr_code", "ean_13", "ean_8", "upc_a", "upc_e"];
  var ZXING_FORMATS = {
    code_128: "CODE_128",
    qr_code: "QR_CODE",
    ean_13: "EAN_13",
    ean_8: "EAN_8",
    upc_a: "UPC_A",
    upc_e: "UPC_E",
  };

  var userAgent = navigator.userAgent || "";
  var IS_IOS =
    /iPad|iPhone|iPod/.test(userAgent) || (userAgent.indexOf("Macintosh") !== -1 && navigator.maxTouchPoints > 1);
  // Set by whichever scanner last failed, so the diagnostics panel can report it.
  var lastFailure = "";
  // Set when a native detector refused every frame and ZXing took over.
  var nativeFailure = "";

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

  function nativeDetectorDiagnosis() {
    if (!("BarcodeDetector" in window)) {
      return "no (using the ZXing fallback)";
    }
    return nativeFailure ? "yes, but it failed every read (" + nativeFailure + "), so ZXing took over" : "yes";
  }

  // Enough for the reporter of an unreproducible "the camera doesn't work" to screenshot.
  window.cameraScannerDiagnostics = function () {
    return [
      "userAgent: " + userAgent,
      "secure context: " + (window.isSecureContext !== false ? "yes" : "no"),
      "camera API: " + (navigator.mediaDevices && navigator.mediaDevices.getUserMedia ? "available" : "MISSING"),
      "native detector: " + nativeDetectorDiagnosis(),
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
    var formats = options.formats && options.formats.length ? options.formats.slice() : FORMATS;
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
    // Chrome on a phone without Google Play services builds a BarcodeDetector that rejects every
    // read, while the preview looks alive. This many failures in a row and ZXing takes over.
    var NATIVE_FAILURES_BEFORE_FALLBACK = 8;
    // The longest edge ZXing is handed; its work scales with the pixel count. What the preview shows
    // is usually smaller anyway, but a native stream that fell back is 1080p.
    var FALLBACK_MAX_EDGE = 1280;

    var stream = null;
    var detector = null;
    var animationFrameId = null;
    var fallbackTimerId = null;
    // A plain reader and a TRY_HARDER one, taking frames in turn.
    var zxingReaders = null;
    var zxingLib = null;
    var fallbackFrames = 0;
    var canvas = null;
    var canvasContext = null;
    var isScanning = false;
    // Bumped by every start() and stop(), so a loop or a camera still opening from a previous run
    // can tell it's been superseded instead of carrying on beside the new one.
    var run = 0;
    // When each value last fired. Per value, because one remembered value let two labels in view
    // take turns: A, B, A, each of them "new".
    var recent = new Map();
    var tapToPlayHandler = null;

    async function handleCode(rawValue) {
      var value = String(rawValue || "").trim();
      if (!value) {
        return;
      }
      var now = Date.now();
      // Suppress rapid repeats of the same value within the active window.
      if (recent.has(value) && now - recent.get(value) < DUP_WINDOW_MS) {
        return;
      }
      recent.forEach(function (firedAt, seen) {
        if (now - firedAt >= DUP_WINDOW_MS) {
          recent.delete(seen);
        }
      });
      recent.set(value, now);
      var result = await onCode(value);
      // onCode returns false when the value was invalid/unrecognized; back-date the timestamp so the
      // remaining suppression is only RETRY_WINDOW_MS and the operator can immediately re-present the
      // card. A successful read keeps the full DUP_WINDOW_MS.
      if (result === false) {
        recent.set(value, now - (DUP_WINDOW_MS - RETRY_WINDOW_MS));
      }
    }

    // The part of the frame the preview shows, in the video's own pixels. object-fit: cover scales
    // the frame to fill the box and centres it, and whatever overflows the box is cut off.
    function visibleRegion() {
      var width = video.videoWidth;
      var height = video.videoHeight;
      var region = { x: 0, y: 0, width: width, height: height };
      var boxWidth = video.clientWidth;
      var boxHeight = video.clientHeight;
      if (!boxWidth || !boxHeight || window.getComputedStyle(video).objectFit !== "cover") {
        return region;
      }
      var scale = Math.max(boxWidth / width, boxHeight / height);
      region.width = Math.min(width, boxWidth / scale);
      region.height = Math.min(height, boxHeight / scale);
      region.x = (width - region.width) / 2;
      region.y = (height - region.height) / 2;
      return region;
    }

    function inRegion(barcode, region) {
      var box = barcode.boundingBox;
      if (!box || !(box.width || box.height)) {
        return true;
      }
      var x = box.x + box.width / 2;
      var y = box.y + box.height / 2;
      return x >= region.x && x <= region.x + region.width && y >= region.y && y <= region.y + region.height;
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

    // Opens the camera for this run. Stopped before it opened (Stop tapped while the permission
    // prompt was up), the camera is let go at once rather than left running behind a page that
    // says it's off.
    async function openCameraFor(thisRun, constraints) {
      var opened = await openCamera(constraints);
      if (thisRun !== run) {
        opened.getTracks().forEach(function (track) {
          track.stop();
        });
        return false;
      }
      stream = opened;
      return true;
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

    async function startNativeScanner(thisRun) {
      detector = new BarcodeDetector({ formats: formats });
      if (!(await openCameraFor(thisRun, NATIVE_VIDEO_CONSTRAINTS))) {
        return;
      }
      applyTrackEnhancements();
      await startPlaying();
      watchStream();
      var failures = 0;
      var scanFrame = async function () {
        if (thisRun !== run) {
          return;
        }
        var barcodes = [];
        if (frameIsReady()) {
          try {
            barcodes = await detector.detect(video);
            failures = 0;
          } catch (error) {
            console.error(error);
            failures += 1;
            if (failures >= NATIVE_FAILURES_BEFORE_FALLBACK && thisRun === run) {
              nativeFailure = (error && error.name) || String(error);
              switchToFallback(thisRun);
              return;
            }
          }
        }
        // Every code in view, not just the first: with several labels in frame the first can be the
        // same one every time, and the others are never read.
        if (barcodes.length) {
          try {
            var region = visibleRegion();
            for (var i = 0; i < barcodes.length && thisRun === run; i++) {
              if (inRegion(barcodes[i], region)) {
                await handleCode(barcodes[i].rawValue);
              }
            }
          } catch (error) {
            console.error(error);
          }
        }
        if (thisRun === run) {
          animationFrameId = requestAnimationFrame(scanFrame);
        }
      };
      animationFrameId = requestAnimationFrame(scanFrame);
    }

    // Keeps the stream: the camera is already open and showing.
    async function switchToFallback(thisRun) {
      detector = null;
      try {
        await prepareZxing();
      } catch (error) {
        lastFailure = ((error && error.name) || "Error") + ": " + ((error && error.message) || error);
        onStatus((error && error.message) || "Couldn't load the barcode reader.", "danger");
        return;
      }
      scanFallbackFrame(thisRun);
    }

    function buildZxingHints(ZXing, tryHarder) {
      var hints = new Map();
      // Spend more effort per frame -- worth it for glare/screen reads on iOS. Present at all means on.
      if (tryHarder) {
        hints.set(ZXing.DecodeHintType.TRY_HARDER, true);
      }
      var possible = [];
      formats.forEach(function (name) {
        var zx = ZXING_FORMATS[name];
        if (zx && ZXing.BarcodeFormat[zx] !== undefined) {
          possible.push(ZXing.BarcodeFormat[zx]);
        }
      });
      if (possible.length) {
        hints.set(ZXing.DecodeHintType.POSSIBLE_FORMATS, possible);
      }
      return hints;
    }

    async function prepareZxing() {
      zxingLib = await loadZxing();
      zxingReaders = [false, true].map(function (tryHarder) {
        var reader = new zxingLib.MultiFormatReader();
        reader.setHints(buildZxingHints(zxingLib, tryHarder));
        return reader;
      });
    }

    function isNotFound(error) {
      // "No barcode in this frame", thrown on nearly every frame -- not worth logging.
      if (zxingLib && error instanceof zxingLib.NotFoundException) {
        return true;
      }
      return !!(error && typeof error.getKind === "function" && error.getKind() === "NotFoundException");
    }

    // The visible part of the frame, drawn small enough to decode quickly. TRY_HARDER roughly
    // doubles a frame's cost, so frames take turns with and without it, starting without.
    //
    // The luminance is built here, never by ZXing's browser reader: handed a <video>, that inverts
    // every other buffer it builds (a global toggle, for light-on-dark codes), and a TRY_HARDER frame
    // with nothing in it builds two. The toggle starts on "inverted", so with TRY_HARDER every frame
    // it read was the negative, and an iPhone never read a dark-on-light label at all.
    function decodeFallbackFrame() {
      var region = visibleRegion();
      var scale = Math.min(1, FALLBACK_MAX_EDGE / Math.max(region.width, region.height));
      var width = Math.max(1, Math.round(region.width * scale));
      var height = Math.max(1, Math.round(region.height * scale));
      if (!canvas) {
        canvas = document.createElement("canvas");
        // ZXing reads the pixels back on every frame.
        canvasContext = canvas.getContext("2d", { willReadFrequently: true });
      }
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
      }
      canvasContext.drawImage(video, region.x, region.y, region.width, region.height, 0, 0, width, height);
      var luminance = new zxingLib.HTMLCanvasElementLuminanceSource(canvas);
      var bitmap = new zxingLib.BinaryBitmap(new zxingLib.HybridBinarizer(luminance));
      var reader = zxingReaders[fallbackFrames % 2];
      fallbackFrames += 1;
      return reader.decodeWithState(bitmap);
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
    async function scanFallbackFrame(thisRun) {
      if (thisRun !== run) {
        return;
      }
      if (frameIsReady()) {
        try {
          var result = decodeFallbackFrame();
          if (result) {
            await handleCode(result.getText());
          }
        } catch (error) {
          if (!isNotFound(error)) {
            console.error(error);
          }
        }
      }
      if (thisRun === run) {
        fallbackTimerId = setTimeout(function () {
          scanFallbackFrame(thisRun);
        }, FALLBACK_INTERVAL_MS);
      }
    }

    // Safari has no BarcodeDetector, on any Apple device or version, so this is the iPhone path.
    async function startFallbackScanner(thisRun) {
      await prepareZxing();
      if (!(await openCameraFor(thisRun, FALLBACK_VIDEO_CONSTRAINTS))) {
        return;
      }
      applyTrackEnhancements();
      await startPlaying();
      watchStream();
      scanFallbackFrame(thisRun);
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
      run += 1;
      var thisRun = run;
      onStatus("Starting the camera...", "info");
      try {
        // Some Android WebViews expose BarcodeDetector but can't actually build one for these
        // formats; fall back rather than losing the camera entirely.
        var useNative = false;
        if ("BarcodeDetector" in window) {
          try {
            new BarcodeDetector({ formats: formats });
            useNative = true;
          } catch (error) {
            useNative = false;
          }
        }
        if (useNative) {
          await startNativeScanner(thisRun);
        } else {
          await startFallbackScanner(thisRun);
        }
      } catch (error) {
        console.error(error);
        if (thisRun !== run) {
          // Stopped while it was starting: the failure is about a camera nobody wants any more.
          return;
        }
        await stop();
        var message = cameraErrorMessage(error);
        lastFailure = ((error && error.name) || "Error") + ": " + ((error && error.message) || error);
        onStatus(message, "danger");
        throw error;
      }
    }

    async function stop() {
      isScanning = false;
      run += 1;
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
      detector = null;
      zxingReaders = null;
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
        recent.clear();
      },
      isScanning: function () {
        return isScanning;
      },
    };
  };
})();
