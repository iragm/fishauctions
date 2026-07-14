// Shared camera barcode scanner used by the quick check-in and quick checkout pages.
//
// One place owns the actual barcode-reading logic (native BarcodeDetector with a ZXing
// CDN fallback, plus per-frame duplicate suppression) so future improvements to how a
// barcode is read don't drift between the two pages. Each page instantiates its own
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
(function () {
  if (window.createCameraScanner) {
    return;
  }

  var ZXING_SRC = "https://cdn.jsdelivr.net/npm/@zxing/library@0.21.3/umd/index.min.js";
  // Formats a membership card / bidder-number / paddle barcode might use.
  var FORMATS = ["code_128", "qr_code", "ean_13", "ean_8", "upc_a", "upc_e"];

  function loadZxing() {
    if (window.ZXing) {
      return Promise.resolve(window.ZXing);
    }
    return new Promise(function (resolve, reject) {
      var script = document.createElement("script");
      script.src = ZXING_SRC;
      script.onload = function () {
        resolve(window.ZXing);
      };
      script.onerror = reject;
      document.head.appendChild(script);
    });
  }

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

    var stream = null;
    var detector = null;
    var animationFrameId = null;
    var zxingReader = null;
    var isScanning = false;
    var lastValue = "";
    var lastValueTime = 0;

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
    var VIDEO_CONSTRAINTS = { facingMode: "environment", width: { ideal: 1920 }, height: { ideal: 1080 } };

    async function startNativeScanner() {
      detector = new BarcodeDetector({ formats: FORMATS });
      stream = await navigator.mediaDevices.getUserMedia({ video: VIDEO_CONSTRAINTS, audio: false });
      applyTrackEnhancements();
      video.srcObject = stream;
      await video.play();
      var scanFrame = async function () {
        if (!isScanning) {
          return;
        }
        try {
          var barcodes = await detector.detect(video);
          if (barcodes.length) {
            await handleCode(barcodes[0].rawValue);
          }
        } catch (error) {
          console.error(error);
        }
        animationFrameId = requestAnimationFrame(scanFrame);
      };
      animationFrameId = requestAnimationFrame(scanFrame);
    }

    function buildZxingHints(ZXing) {
      var hints = new Map();
      // Spend more effort per frame — worth it for glare/screen reads on iOS.
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

    async function startFallbackScanner() {
      var ZXing = await loadZxing();
      // delayBetweenScanAttempts defaults to 500ms; drop it so iOS scans several times a second.
      zxingReader = new ZXing.BrowserMultiFormatReader(buildZxingHints(ZXing), { delayBetweenScanAttempts: 150 });
      var onResult = function (result, error) {
        if (result) {
          handleCode(result.getText());
        } else if (error && !(error instanceof ZXing.NotFoundException)) {
          console.error(error);
        }
      };
      // decodeFromConstraints lets us request the same higher-resolution stream as the native path;
      // fall back to the default device picker if this build of ZXing lacks it.
      if (zxingReader.decodeFromConstraints) {
        await zxingReader.decodeFromConstraints({ video: VIDEO_CONSTRAINTS, audio: false }, video, onResult);
      } else {
        await zxingReader.decodeFromVideoDevice(null, video, onResult);
      }
      stream = video.srcObject;
      applyTrackEnhancements();
    }

    async function start() {
      if (isScanning) {
        return;
      }
      isScanning = true;
      onStatus("Scanning...", "info");
      try {
        if ("BarcodeDetector" in window) {
          await startNativeScanner();
        } else {
          await startFallbackScanner();
        }
      } catch (error) {
        console.error(error);
        await stop();
        onStatus("Unable to start the camera: " + (error.message || error), "danger");
        throw error;
      }
    }

    async function stop() {
      isScanning = false;
      if (animationFrameId) {
        cancelAnimationFrame(animationFrameId);
        animationFrameId = null;
      }
      if (zxingReader) {
        zxingReader.reset();
        zxingReader = null;
      }
      if (stream) {
        stream.getTracks().forEach(function (track) {
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
