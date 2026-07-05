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

    var stream = null;
    var detector = null;
    var animationFrameId = null;
    var zxingReader = null;
    var isScanning = false;
    var lastProcessedValue = "";

    function shouldIgnoreDuplicate(value) {
      // The camera decodes the same barcode on every frame; silently ignore repeats
      // of the same value until a new one is detected.
      if (value === lastProcessedValue) {
        return true;
      }
      lastProcessedValue = value;
      return false;
    }

    async function handleCode(rawValue) {
      var value = String(rawValue || "").trim();
      if (!value || shouldIgnoreDuplicate(value)) {
        return;
      }
      await onCode(value);
    }

    async function startNativeScanner() {
      detector = new BarcodeDetector({ formats: FORMATS });
      stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" }, audio: false });
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

    async function startFallbackScanner() {
      var ZXing = await loadZxing();
      zxingReader = new ZXing.BrowserMultiFormatReader();
      await zxingReader.decodeFromVideoDevice(null, video, function (result, error) {
        if (result) {
          handleCode(result.getText());
        } else if (error && !(error instanceof ZXing.NotFoundException)) {
          console.error(error);
        }
      });
      stream = video.srcObject;
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
        lastProcessedValue = "";
      },
      isScanning: function () {
        return isScanning;
      },
    };
  };
})();
