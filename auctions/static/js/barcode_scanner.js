// Shared barcode scan pipeline for auction admin pages.
//
// Input arrives from two sources:
//   1. A USB barcode scanner in HID (keyboard wedge) mode, configured to send a
//      prefix keystroke (default F9) before the barcode and a CR (Enter) after it.
//      A document-level keydown listener captures everything between the two.
//   2. Camera scanning pages, which call window.auctionBarcodeScanner.handleCode().
//
// Configure by setting window.AUCTION_BARCODE_CONFIG before loading this file
// (see templates/auctions/barcode_scanner.html):
//   { scanUrl, csrfToken, prefixKey, checkInOnly }
//
// Feedback uses the site-wide $.toast from base.html: each new scan clears any
// earlier scan toasts, and toasts stick around for 15 seconds.
(function () {
  if (window.auctionBarcodeScanner) {
    return; // already initialized (this file is included defensively on some pages)
  }
  var config = window.AUCTION_BARCODE_CONFIG || {};
  var scanUrl = config.scanUrl || "";
  var csrfToken = config.csrfToken || "";
  var prefixKey = config.prefixKey || "F9";
  var checkInOnly = !!config.checkInOnly;
  var TOAST_DELAY = 15000;

  // Barcodes that don't immediately resolve to a member card set pending state that
  // is applied to the next member card scan (assign a bidder number, adjust an invoice).
  var pendingBidderNumber = "";
  var pendingAdjustment = null;

  function beep(type) {
    try {
      var audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      function playTone(freq, duration, startOffset) {
        var osc = audioCtx.createOscillator();
        var gain = audioCtx.createGain();
        osc.connect(gain);
        gain.connect(audioCtx.destination);
        osc.type = "sine";
        osc.frequency.value = freq;
        var t = audioCtx.currentTime + (startOffset || 0);
        gain.gain.setValueAtTime(0.15, t);
        gain.gain.exponentialRampToValueAtTime(0.001, t + duration);
        osc.start(t);
        osc.stop(t + duration);
      }
      if (type === "scan") {
        playTone(1000, 0.12);
        setTimeout(function () { audioCtx.close(); }, 400);
      } else if (type === "checkin") {
        playTone(1200, 0.08, 0);
        playTone(1200, 0.08, 0.15);
        setTimeout(function () { audioCtx.close(); }, 600);
      } else if (type === "error") {
        playTone(380, 0.45);
        setTimeout(function () { audioCtx.close(); }, 800);
      }
    } catch (e) {
      // audio not available; ignore
    }
  }

  function showToast(message, type) {
    // New scans clear old toasts so a wall of stale results never builds up
    var wrapper = document.getElementById("toast-wrapper");
    if (wrapper) {
      wrapper.innerHTML = "";
    }
    window.jQuery.toast({ title: message, type: type || "success", delay: TOAST_DELAY });
  }

  function parsePaddleBarcode(value) {
    if (!value.startsWith("11111") || value.length <= 5) {
      return "";
    }
    return value.slice(5);
  }

  function parseAdjustmentBarcode(value) {
    // 010{amount}{label} = charge extra, 000{amount}{label} = discount
    var m = value.match(/^(010|000)([\d.]+)([^0-9.].*)$/);
    if (!m) return null;
    return {
      adjustmentType: m[1] === "010" ? "ADD" : "DISCOUNT",
      amount: m[2],
      label: m[3].trim(),
    };
  }

  function announce(detail) {
    document.dispatchEvent(new CustomEvent("auction-barcode-scan", { detail: detail }));
  }

  async function postScan(barcode) {
    var assignedBidderNumber = pendingBidderNumber;
    var formData = new FormData();
    formData.append("barcode", barcode);
    if (checkInOnly) {
      formData.append("check_in_only", "1");
    } else {
      if (pendingBidderNumber) {
        formData.append("assign_bidder_number", pendingBidderNumber);
      }
      if (pendingAdjustment) {
        formData.append("adjustment_type", pendingAdjustment.adjustmentType);
        formData.append("adjustment_amount", pendingAdjustment.amount);
        formData.append("adjustment_label", pendingAdjustment.label);
      }
    }
    var payload = null;
    try {
      var response = await fetch(scanUrl, {
        method: "POST",
        headers: { "X-CSRFToken": csrfToken },
        body: formData,
      });
      payload = await response.json();
    } catch (e) {
      payload = null;
    }
    if (!payload || !payload.ok) {
      beep("error");
      var message = (payload && payload.message) || "Scan failed -- check your connection and try again.";
      showToast(message, "danger");
      announce({ ok: false, message: message });
      return;
    }
    beep("checkin");
    pendingBidderNumber = "";
    pendingAdjustment = null;
    if (checkInOnly) {
      // the self check-in kiosk shows its own full-screen welcome instead of a toast
      announce({ ok: true, payload: payload });
      return;
    }
    var suffix = assignedBidderNumber ? " and assigned bidder number " + assignedBidderNumber : "";
    var adjSuffix = payload.adjustment_desc ? " | " + payload.adjustment_desc + " applied" : "";
    var verb = payload.verb || "Checked in";
    showToast(verb + " " + payload.name + suffix + adjSuffix, "success");
    announce({ ok: true, payload: payload });
  }

  async function handleCode(rawValue) {
    var value = String(rawValue || "").trim();
    if (!value) {
      return;
    }
    if (checkInOnly) {
      // Kiosk mode: membership cards only. Paddle and adjustment barcodes are refused
      // here, and the server independently ignores their side effects.
      if (parsePaddleBarcode(value) || parseAdjustmentBarcode(value) || !/^\d+$/.test(value)) {
        beep("error");
        showToast("Unrecognized barcode", "danger");
        announce({ ok: false, message: "Unrecognized barcode" });
        return;
      }
      await postScan(value);
      return;
    }
    var paddleBidderNumber = parsePaddleBarcode(value);
    if (paddleBidderNumber) {
      beep("scan");
      pendingBidderNumber = paddleBidderNumber;
      showToast("Scan the user to assign bidder number " + pendingBidderNumber + " to.", "warning");
      return;
    }
    var adjustment = parseAdjustmentBarcode(value);
    if (adjustment) {
      beep("scan");
      pendingAdjustment = adjustment;
      var sign = adjustment.adjustmentType === "ADD" ? "+" : "-";
      showToast("Scan the member card to apply " + sign + "$" + adjustment.amount + " " + adjustment.label + " to their invoice.", "warning");
      return;
    }
    await postScan(value);
  }

  // --- USB HID (keyboard wedge) listener -----------------------------------
  // The prefix keystroke starts capture; every printable key is buffered (and kept
  // out of whatever input is focused) until Enter fires the scan. Scanners type with
  // near-zero delay between keys, so a pause means a human is at the keyboard and
  // capture is silently abandoned.
  var capturing = false;
  var buffer = "";
  var interKeyTimer = null;
  var INTER_KEY_TIMEOUT_MS = 500;

  function stopCapture() {
    capturing = false;
    buffer = "";
    if (interKeyTimer) {
      clearTimeout(interKeyTimer);
      interKeyTimer = null;
    }
  }

  function armInterKeyTimer() {
    if (interKeyTimer) {
      clearTimeout(interKeyTimer);
    }
    interKeyTimer = setTimeout(stopCapture, INTER_KEY_TIMEOUT_MS);
  }

  document.addEventListener(
    "keydown",
    function (event) {
      if (event.key === prefixKey && !event.ctrlKey && !event.altKey && !event.metaKey) {
        event.preventDefault();
        event.stopPropagation();
        capturing = true;
        buffer = "";
        armInterKeyTimer();
        return;
      }
      if (!capturing) {
        return;
      }
      if (event.key === "Enter") {
        event.preventDefault();
        event.stopPropagation();
        var value = buffer;
        stopCapture();
        handleCode(value);
        return;
      }
      if (event.key.length === 1 && !event.ctrlKey && !event.altKey && !event.metaKey) {
        event.preventDefault();
        event.stopPropagation();
        buffer += event.key;
        armInterKeyTimer();
      }
      // non-printable keys (e.g. Shift held by the scanner for symbols) are ignored without aborting
    },
    true // capture phase, so buffered keys never reach focused inputs or other shortcut handlers
  );

  window.auctionBarcodeScanner = {
    handleCode: handleCode,
    showToast: showToast,
    beep: beep,
    prefixKey: prefixKey,
    checkInOnly: checkInOnly,
  };
})();
