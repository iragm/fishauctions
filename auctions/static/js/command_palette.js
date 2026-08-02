/* Command palette: cross-site search + jump-to-page, opened from the navbar brand or Ctrl/Cmd+K.
 * Talks to the JSON endpoints named `command_palette` (results) and `command_palette_log` (search log).
 * Search logging keeps a single row per session (writes are serialized so refinements share a row):
 * the query is recorded as soon as it is typed — before results load — so a search isn't lost when the
 * user navigates away first. It is then refined to "bounce" if nothing matched, "clicked" when a result
 * is opened, or "abandoned" when the box is cleared / the palette is closed / the page is left. */
(function () {
  "use strict";

  function ready(fn) {
    if (document.readyState !== "loading") {
      fn();
    } else {
      document.addEventListener("DOMContentLoaded", fn);
    }
  }

  ready(function () {
    var modalEl = document.getElementById("command-palette-modal");
    if (!modalEl || typeof bootstrap === "undefined") {
      return; // not authenticated, or bootstrap not loaded
    }

    var input = document.getElementById("command-palette-input");
    var results = document.getElementById("command-palette-results");
    var searchUrl = modalEl.dataset.searchUrl;
    var logUrl = modalEl.dataset.logUrl;
    var assistUrl = modalEl.dataset.assistUrl;
    var executeUrl = modalEl.dataset.executeUrl;
    var assistEnabled = modalEl.dataset.assistEnabled === "1";
    var csrfToken = modalEl.dataset.csrf;
    var modal = bootstrap.Modal.getOrCreateInstance(modalEl);

    // Recent exchanges, so "print that label" knows which lot we just made. Kept in
    // sessionStorage (per tab, cleared when the tab closes) and capped server-side too.
    var CONTEXT_KEY = "cp_assist_context";
    var CONTEXT_MAX = 5;
    var assistInFlight = false;
    var countdownTimer = null;

    var DEBOUNCE_MS = 300;
    var debounceTimer = null;
    var currentSearchId = null; // pk of the in-progress CommandPaletteSearch row
    var lastQuery = ""; // last query we ran a search for
    var lastQueryEmpty = false; // did the most recent executed query return zero results?
    var navigatedByClick = false; // suppress the abandon-on-close beacon after a click
    var finalized = false; // has the current query already been finalized (abandoned/bounce)?
    var logChain = Promise.resolve(); // serialize log writes so refinements share one row
    var items = []; // flat list of rendered result elements for keyboard nav
    var activeIndex = -1;

    function open() {
      modal.show();
    }

    // --- Logging -------------------------------------------------------------

    function logBody(fields) {
      var body = new URLSearchParams();
      body.set("csrfmiddlewaretoken", csrfToken);
      if (currentSearchId) {
        body.set("id", currentSearchId);
      }
      Object.keys(fields).forEach(function (key) {
        if (fields[key] !== null && fields[key] !== undefined) {
          body.set(key, fields[key]);
        }
      });
      return body;
    }

    // Serialize log writes through one promise chain: the first POST creates the row and sets
    // currentSearchId before the next refinement runs, so a refined query updates the same row
    // instead of racing several requests (each unaware of the id) into duplicate rows. keepalive
    // lets a write that's still in flight survive the navigation a command palette usually causes.
    function postLog(fields) {
      logChain = logChain.then(function () {
        return fetch(logUrl, {
          method: "POST",
          headers: { "X-CSRFToken": csrfToken },
          body: logBody(fields),
          credentials: "same-origin",
          keepalive: true,
        })
          .then(function (resp) {
            return resp.json();
          })
          .then(function (data) {
            if (data && data.id) {
              currentSearchId = data.id;
            }
          })
          .catch(function () {});
      });
      return logChain;
    }

    // result is "pending" while results exist, or "bounce" when the query returned nothing
    // (bounces let us mine common typos/missing phrases and add them as synonyms later).
    function logSearchState(query, result) {
      return postLog({ search: query, result: result || "pending" });
    }

    // Finalize the session when the box is cleared, the palette closes, or the page is left. A
    // query that ended with no results is recorded as a "bounce" (mined later for missing
    // shortcuts); otherwise the user looked but didn't pick anything: "abandoned". The row is
    // already recorded (logged as soon as the query was typed), so even if currentSearchId hasn't
    // come back yet we still send the final state — without an id the server records a fresh row
    // rather than dropping the search, which is what used to happen on a quick navigation away.
    function logFinal(query) {
      if (finalized || !query) {
        return;
      }
      finalized = true;
      var result = lastQueryEmpty ? "bounce" : "abandoned";
      // sendBeacon survives page unload / modal close; csrf travels in the form body.
      var body = logBody({ search: query, result: result });
      if (navigator.sendBeacon) {
        navigator.sendBeacon(logUrl, body);
      } else {
        fetch(logUrl, { method: "POST", body: body, credentials: "same-origin", keepalive: true }).catch(function () {});
      }
      currentSearchId = null;
    }

    // Best-effort finalize when the user leaves the page (clicked a normal link, back button,
    // closed the tab). The modal's hidden event does not fire on a full-page navigation, so without
    // this a search ended by navigating away was never finalized.
    function flushFinal() {
      if (navigatedByClick) {
        return;
      }
      logFinal(input.value.trim());
    }

    function logClickAndGo(item) {
      navigatedByClick = true;
      finalized = true;
      var query = input.value.trim();
      var body = logBody({
        search: query,
        result: "clicked",
        result_type: item.type || "",
        result_url: item.url || "",
        result_object_id: item.id || "",
      });
      var go = function () {
        window.location.href = item.url;
      };
      fetch(logUrl, {
        method: "POST",
        headers: { "X-CSRFToken": csrfToken },
        body: body,
        credentials: "same-origin",
        keepalive: true,
      })
        .then(go)
        .catch(go);
    }

    // --- Rendering -----------------------------------------------------------

    function makeItem(data) {
      var el = document.createElement(data.url ? "a" : "button");
      el.className = "cp-item list-group-item list-group-item-action d-flex align-items-start gap-2 border-0 rounded";
      if (data.url) {
        el.href = data.url;
      } else {
        el.type = "button";
      }
      el.dataset.type = data.type || "";
      var icon = document.createElement("i");
      icon.className = "bi " + (data.icon || "bi-arrow-right-short") + " fs-5 flex-shrink-0";
      el.appendChild(icon);
      var textWrap = document.createElement("span");
      textWrap.className = "d-flex flex-column text-truncate";
      var title = document.createElement("span");
      title.className = "cp-item-title text-truncate";
      title.textContent = data.title || "";
      textWrap.appendChild(title);
      if (data.subtitle) {
        var sub = document.createElement("small");
        sub.className = "text-muted text-truncate";
        sub.textContent = data.subtitle;
        textWrap.appendChild(sub);
      }
      el.appendChild(textWrap);
      el.addEventListener("click", function (event) {
        event.preventDefault();
        activate(data);
      });
      return el;
    }

    function activate(data) {
      if (data.type === "search") {
        // Re-run a recent search inside the palette rather than navigating away.
        input.value = data.title;
        input.focus();
        runSearch();
        return;
      }
      if (data.url) {
        logClickAndGo(data);
      }
    }

    function render(groups) {
      results.innerHTML = "";
      items = [];
      activeIndex = -1;
      if (!groups || !groups.length) {
        var empty = document.createElement("p");
        empty.className = "text-muted small px-2 py-3 mb-0";
        empty.textContent = input.value.trim() ? "No results found." : "Start typing to search.";
        results.appendChild(empty);
        return;
      }
      groups.forEach(function (group) {
        var label = document.createElement("div");
        label.className = "cp-group-label text-muted px-2 pt-3 pb-1";
        label.textContent = group.label;
        results.appendChild(label);
        var list = document.createElement("div");
        list.className = "list-group list-group-flush";
        group.items.forEach(function (data) {
          var el = makeItem(data);
          el._cpData = data;
          list.appendChild(el);
          items.push(el);
        });
        results.appendChild(list);
      });
    }

    function setActive(index) {
      if (!items.length) {
        return;
      }
      if (activeIndex >= 0 && items[activeIndex]) {
        items[activeIndex].classList.remove("active");
      }
      activeIndex = (index + items.length) % items.length;
      var el = items[activeIndex];
      el.classList.add("active");
      el.scrollIntoView({ block: "nearest" });
    }

    // --- Fetching ------------------------------------------------------------

    function countItems(groups) {
      return groups.reduce(function (total, group) {
        return total + group.items.length;
      }, 0);
    }

    function fetchResults(query, onCount) {
      fetch(searchUrl + "?q=" + encodeURIComponent(query), {
        headers: { "X-Requested-With": "XMLHttpRequest" },
        credentials: "same-origin",
      })
        .then(function (resp) {
          return resp.json();
        })
        .then(function (data) {
          var groups = data.groups || [];
          render(groups);
          if (onCount) {
            onCount(countItems(groups));
          }
        })
        .catch(function () {
          render([]);
        });
    }

    function runSearch() {
      var query = input.value.trim();
      if (query === lastQuery) {
        return;
      }
      // Cleared the box after a search: finalize the old session before starting fresh.
      if (!query && lastQuery) {
        logFinal(lastQuery);
      }
      lastQuery = query;
      if (!query) {
        lastQueryEmpty = false;
        fetchResults(query);
        return;
      }
      // Record the query as soon as it's typed, before results load. The palette's job is to send
      // the user elsewhere, so they often navigate away before the results request round-trips;
      // logging up front (and refining to a bounce once we know the count) is what keeps those
      // searches from going unrecorded.
      finalized = false;
      logSearchState(query, "pending");
      fetchResults(query, function (count) {
        lastQueryEmpty = count === 0;
        if (count === 0) {
          logSearchState(query, "bounce");
        }
      });
    }

    // --- Natural-language assist ---------------------------------------------

    function loadContext() {
      try {
        var raw = window.sessionStorage.getItem(CONTEXT_KEY);
        var parsed = raw ? JSON.parse(raw) : [];
        return Array.isArray(parsed) ? parsed.slice(-CONTEXT_MAX) : [];
      } catch (err) {
        return [];
      }
    }

    function rememberExchange(query, summary, action, data) {
      try {
        var entries = loadContext();
        entries.push({ query: query, result: summary || "", action: action || "", data: data || {} });
        window.sessionStorage.setItem(CONTEXT_KEY, JSON.stringify(entries.slice(-CONTEXT_MAX)));
      } catch (err) {
        /* sessionStorage can be unavailable (private mode); context is a nicety, not required */
      }
    }

    function clearNav() {
      items = [];
      activeIndex = -1;
    }

    // Stop a pending countdown without running it. Safe to call at any time.
    function cancelCountdown() {
      if (countdownTimer) {
        window.clearInterval(countdownTimer);
        countdownTimer = null;
      }
    }

    function showThinking() {
      results.innerHTML = "";
      clearNav();
      var wrap = document.createElement("div");
      wrap.className = "cp-thinking text-muted d-flex align-items-center gap-2 px-2 py-3";
      var spinner = document.createElement("span");
      spinner.className = "spinner-border spinner-border-sm";
      spinner.setAttribute("role", "status");
      wrap.appendChild(spinner);
      var label = document.createElement("span");
      label.textContent = "Working out what you mean…";
      wrap.appendChild(label);
      results.appendChild(wrap);
    }

    // A plain message block. `type` follows the site's message-type standard: info for neutral
    // facts, danger for failures, warning for cautions.
    function renderNote(message, type, icon) {
      var note = document.createElement("div");
      note.className = "alert alert-" + (type || "info") + (type === "warning" ? " text-dark" : "") + " mb-2";
      var i = document.createElement("i");
      i.className = "bi " + (icon || "bi-info-circle") + " me-2";
      note.appendChild(i);
      var span = document.createElement("span");
      span.textContent = message;
      note.appendChild(span);
      return note;
    }

    function renderFollowups(followups) {
      if (!followups || !followups.length) {
        return null;
      }
      var list = document.createElement("div");
      list.className = "list-group list-group-flush";
      followups.forEach(function (followup) {
        if (!followup || !followup.url) {
          return;
        }
        var el = makeItem({ title: followup.label, url: followup.url, icon: "bi-arrow-right-short", type: "assist" });
        el._cpData = { title: followup.label, url: followup.url, type: "assist" };
        list.appendChild(el);
        items.push(el);
      });
      return list;
    }

    // Options from a clarify response: clicking one appends it to the context and re-asks, so
    // "which bob?" is answered by clicking rather than retyping the whole command.
    function renderOptions(query, message, options) {
      var list = document.createElement("div");
      list.className = "list-group list-group-flush";
      options.forEach(function (option) {
        var el = makeItem({ title: option, icon: "bi-check2-circle", type: "assist-option" });
        el._cpData = { title: option, type: "assist-option" };
        el.addEventListener("click", function (event) {
          event.preventDefault();
          rememberExchange(query, "Asked: " + message, "clarify", {});
          input.value = option;
          submitAssist(option);
        });
        list.appendChild(el);
        items.push(el);
      });
      return list;
    }

    // The 5 second window before a database change. Cancel stops it; "Go now" skips the wait.
    // Either way the server re-checks permissions when execute is called — this is UX only.
    function renderCountdown(query, response) {
      results.innerHTML = "";
      clearNav();
      var card = document.createElement("div");
      card.className = "cp-countdown card mb-2";
      var body = document.createElement("div");
      body.className = "card-body";

      var heading = document.createElement("div");
      heading.className = "d-flex align-items-center gap-2 mb-2";
      var spinner = document.createElement("span");
      spinner.className = "spinner-border spinner-border-sm text-primary";
      spinner.setAttribute("role", "status");
      heading.appendChild(spinner);
      var summary = document.createElement("strong");
      summary.textContent = response.summary || "About to make a change";
      heading.appendChild(summary);
      body.appendChild(heading);

      var hint = document.createElement("p");
      hint.className = "text-muted small mb-2";
      hint.textContent = "Starting in a moment — cancel if that's not what you meant.";
      body.appendChild(hint);

      var progressWrap = document.createElement("div");
      progressWrap.className = "progress mb-3";
      var bar = document.createElement("div");
      bar.className = "progress-bar";
      bar.style.width = "0%";
      progressWrap.appendChild(bar);
      body.appendChild(progressWrap);

      var buttons = document.createElement("div");
      buttons.className = "d-flex gap-2";
      var go = document.createElement("button");
      go.type = "button";
      go.className = "btn btn-success text-dark";
      go.textContent = "Go now";
      var cancel = document.createElement("button");
      cancel.type = "button";
      cancel.className = "btn btn-secondary";
      cancel.textContent = "Cancel";
      buttons.appendChild(go);
      buttons.appendChild(cancel);
      body.appendChild(buttons);
      card.appendChild(body);
      results.appendChild(card);

      var delay = response.delay_ms || 5000;
      var started = Date.now();
      var done = false;

      function fire() {
        if (done) {
          return;
        }
        done = true;
        cancelCountdown();
        runExecute(query, response);
      }

      countdownTimer = window.setInterval(function () {
        var elapsed = Date.now() - started;
        var pct = Math.min(100, (elapsed / delay) * 100);
        bar.style.width = pct + "%";
        if (elapsed >= delay) {
          fire();
        }
      }, 100);

      go.addEventListener("click", fire);
      cancel.addEventListener("click", function () {
        done = true;
        cancelCountdown();
        results.innerHTML = "";
        clearNav();
        results.appendChild(renderNote("Cancelled — nothing was changed.", "info", "bi-info-circle"));
      });
    }

    function runExecute(query, response) {
      showThinking();
      fetch(executeUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken },
        credentials: "same-origin",
        body: JSON.stringify({ action: response.action, params: response.params }),
      })
        .then(function (resp) {
          return resp.json();
        })
        .then(function (data) {
          renderAssist(query, data);
        })
        .catch(function () {
          renderAssist(query, { kind: "error", message: "That didn't go through. Please try again." });
        });
    }

    function renderAssist(query, response) {
      var kind = response && response.kind;
      if (kind === "results") {
        render(response.groups || []);
        return;
      }
      if (kind === "navigate" && response.url) {
        rememberExchange(query, response.message || "Opened a page", "navigate", {});
        navigatedByClick = true;
        finalized = true;
        window.location.href = response.url;
        return;
      }
      if (kind === "countdown") {
        renderCountdown(query, response);
        return;
      }
      results.innerHTML = "";
      clearNav();
      if (kind === "clarify") {
        results.appendChild(renderNote(response.message || "Which one did you mean?", "info", "bi-question-circle"));
        if (response.options && response.options.length) {
          results.appendChild(renderOptions(query, response.message, response.options));
        }
        return;
      }
      if (kind === "done") {
        rememberExchange(query, response.message || "Done", response.action || "", response.data || {});
        results.appendChild(renderNote(response.message || "Done.", "success", "bi-check-circle-fill"));
        var followups = renderFollowups(response.followups);
        if (followups) {
          results.appendChild(followups);
        }
        return;
      }
      results.appendChild(
        renderNote(
          (response && response.message) || "I couldn't do that.",
          "danger",
          "bi-exclamation-triangle-fill"
        )
      );
    }

    // The single entry point for "act on what's typed": Enter with nothing selected, and the
    // final speech transcript, both land here so voice and typing share one path.
    function submitAssist(query) {
      query = (query === undefined ? input.value : query).trim();
      if (!assistEnabled || !query || assistInFlight) {
        return;
      }
      assistInFlight = true;
      finalized = true; // this query ends in an assist result, not an abandoned search
      showThinking();
      fetch(assistUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken },
        credentials: "same-origin",
        body: JSON.stringify({ q: query, context: loadContext() }),
      })
        .then(function (resp) {
          return resp.json();
        })
        .then(function (data) {
          renderAssist(query, data);
        })
        .catch(function () {
          renderAssist(query, { kind: "error", message: "I couldn't reach the assistant just now." });
        })
        .then(function () {
          assistInFlight = false;
        });
    }

    // --- Speech to text ------------------------------------------------------

    var SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    var recognition = null;
    var listening = false;
    var micButtons = [];
    var paletteMic = document.getElementById("command-palette-mic");
    var navbarMic = document.getElementById("navbar-mic");

    function setListening(state) {
      listening = state;
      micButtons.forEach(function (button) {
        button.classList.toggle("listening", state);
        button.setAttribute("aria-pressed", state ? "true" : "false");
      });
    }

    function stopListening() {
      if (recognition && listening) {
        try {
          recognition.stop();
        } catch (err) {
          /* already stopped */
        }
      }
      setListening(false);
    }

    function startListening() {
      if (!recognition || listening) {
        return;
      }
      try {
        recognition.start();
        setListening(true);
      } catch (err) {
        setListening(false);
      }
    }

    function buildRecognition() {
      var speech = new SpeechRecognition();
      speech.continuous = false;
      speech.interimResults = true;
      speech.lang = document.documentElement.lang || "en-US";

      speech.addEventListener("result", function (event) {
        var transcript = "";
        var isFinal = false;
        for (var i = event.resultIndex; i < event.results.length; i++) {
          transcript += event.results[i][0].transcript;
          if (event.results[i].isFinal) {
            isFinal = true;
          }
        }
        transcript = transcript.trim();
        if (!transcript) {
          return;
        }
        // Interim words stream into the box so the user watches it being typed for them, and
        // the ordinary debounced search runs on them exactly as if they had typed it.
        input.value = transcript;
        if (isFinal) {
          setListening(false);
          submitAssist(transcript);
        } else {
          clearTimeout(debounceTimer);
          debounceTimer = setTimeout(runSearch, DEBOUNCE_MS);
        }
      });
      speech.addEventListener("end", function () {
        setListening(false);
      });
      speech.addEventListener("error", function () {
        setListening(false);
      });
      return speech;
    }

    if (assistEnabled && SpeechRecognition) {
      recognition = buildRecognition();
      [paletteMic, navbarMic].forEach(function (button) {
        if (!button) {
          return;
        }
        micButtons.push(button);
        button.classList.remove("d-none");
      });
      if (paletteMic) {
        paletteMic.addEventListener("click", function () {
          if (listening) {
            stopListening();
          } else {
            startListening();
          }
        });
      }
      if (navbarMic) {
        // From the navbar: open the palette first so the user can see the words appear.
        navbarMic.addEventListener("click", function (event) {
          event.preventDefault();
          if (listening) {
            stopListening();
            return;
          }
          open();
          window.setTimeout(startListening, 250);
        });
      }
    }

    // --- Events --------------------------------------------------------------

    input.addEventListener("input", function () {
      // Typing means the user has moved on: never let a countdown started a moment ago fire
      // against a query they are already editing.
      cancelCountdown();
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(runSearch, DEBOUNCE_MS);
    });
    input.addEventListener("blur", function () {
      clearTimeout(debounceTimer);
      runSearch();
    });
    input.addEventListener("keydown", function (event) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        setActive(activeIndex + 1);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        setActive(activeIndex - 1);
      } else if (event.key === "Enter") {
        if (activeIndex >= 0 && items[activeIndex]) {
          event.preventDefault();
          activate(items[activeIndex]._cpData);
        } else if (assistEnabled && input.value.trim()) {
          // Enter with nothing selected used to do nothing at all; it is now the assist
          // trigger, so no existing keyboard behaviour changes.
          event.preventDefault();
          submitAssist();
        }
      }
    });

    modalEl.addEventListener("shown.bs.modal", function () {
      input.focus();
      input.select();
    });
    modalEl.addEventListener("show.bs.modal", function () {
      // Fresh session each time the palette opens.
      navigatedByClick = false;
      finalized = false;
      currentSearchId = null;
      lastQuery = "";
      input.value = "";
      fetchResults("");
    });
    modalEl.addEventListener("hidden.bs.modal", function () {
      // Never leave the microphone running, or a countdown pending, once the palette is hidden.
      stopListening();
      cancelCountdown();
      flushFinal();
    });

    // Leaving the page (normal link, back button, tab close) does not fire the modal's hidden
    // event, so finalize here too. pagehide fires on navigation and on bfcache unload.
    window.addEventListener("pagehide", flushFinal);

    // Navbar brand opens the palette instead of navigating to the landing page.
    var brand = document.querySelector(".navbar-brand");
    if (brand) {
      brand.addEventListener("click", function (event) {
        event.preventDefault();
        open();
      });
    }

    // Ctrl/Cmd+K opens it from anywhere.
    document.addEventListener("keydown", function (event) {
      if ((event.metaKey || event.ctrlKey) && (event.key === "k" || event.key === "K")) {
        event.preventDefault();
        open();
      }
    });
  });
})();
