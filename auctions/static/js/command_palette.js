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
    var cancelUrl = modalEl.dataset.cancelUrl;
    var assistEnabled = modalEl.dataset.assistEnabled === "1";
    var csrfToken = modalEl.dataset.csrf;
    var modal = bootstrap.Modal.getOrCreateInstance(modalEl);

    // Recent exchanges, so "print that label" knows which lot we just made. Kept in
    // sessionStorage (per tab, cleared when the tab closes) and capped server-side too.
    var CONTEXT_KEY = "cp_assist_context";
    var CONTEXT_MAX = 5;
    var assistInFlight = false;
    var countdownTimer = null;
    // Bumped whenever we start an assist request. A plain search fired by an earlier keystroke is
    // still in flight at that point, and when it lands it calls render() -- which used to wipe the
    // progress strip and print "No results found." over the top of a request that was working fine.
    // Stale responses compare this and drop themselves instead.
    var renderGeneration = 0;

    var DEBOUNCE_MS = 300;
    // How long "Taking you to the lot list…" stays on screen before the page actually changes.
    // Long enough to read the destination, short enough that nobody would call it a delay.
    var NAVIGATE_DELAY_MS = 700;
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
        if (assistInFlight) {
          // Still working. Saying "no results" here is not just unhelpful, it's wrong: the answer
          // is on its way and this is the frame the user reads while waiting for it.
          empty.textContent = "Searching…";
        } else {
          empty.textContent = input.value.trim() ? "No results found." : "Start typing to search.";
        }
        results.appendChild(empty);
        return;
      }
      renderInto(groups);
    }

    // Append result groups to whatever is already on screen, registering each one for keyboard
    // navigation. Used on its own to put clickable results *underneath* an answer or a question,
    // where clearing the box first would throw away the thing they are attached to.
    function renderInto(groups) {
      (groups || []).forEach(function (group) {
        if (!group.items || !group.items.length) {
          return;
        }
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
      var generation = renderGeneration;
      // An assist request started after this search did owns the screen; a late search result must
      // not paint over its progress strip or its answer.
      function isStale() {
        return generation !== renderGeneration;
      }
      fetch(searchUrl + "?q=" + encodeURIComponent(query), {
        headers: { "X-Requested-With": "XMLHttpRequest" },
        credentials: "same-origin",
      })
        .then(function (resp) {
          return resp.json();
        })
        .then(function (data) {
          var groups = data.groups || [];
          if (isStale()) {
            return;
          }
          render(groups);
          if (onCount) {
            onCount(countItems(groups));
          }
        })
        .catch(function () {
          if (!isStale()) {
            render([]);
          }
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

    // A navigation waiting out its "Taking you to X…" message. Held so closing the palette can
    // call it off: dismissing the box and then being moved to another page a moment later is not
    // something anyone asked for.
    var pendingNavigation = null;

    function cancelPendingNavigation() {
      if (pendingNavigation) {
        window.clearTimeout(pendingNavigation);
        pendingNavigation = null;
      }
    }

    // Stop a pending countdown without running it. Safe to call at any time.
    function cancelCountdown() {
      if (countdownTimer) {
        window.clearInterval(countdownTimer);
        countdownTimer = null;
      }
    }

    // The progress strip. Unlike the old version this does NOT clear the results: whatever the
    // user could already click stays on screen, dimmed, for the several seconds the model takes.
    // Losing a usable result list to a spinner is worse than waiting next to one.
    var thinkingEl = null;
    var thinkingLabel = null;
    var thinkingSteps = null;

    function showThinking(text) {
      if (!thinkingEl) {
        thinkingEl = document.createElement("div");
        thinkingEl.className = "cp-thinking px-2 py-2 mb-2";
        thinkingEl.setAttribute("aria-live", "polite");

        var current = document.createElement("div");
        current.className = "d-flex align-items-center gap-2 text-body";
        var spinner = document.createElement("span");
        spinner.className = "spinner-border spinner-border-sm flex-shrink-0";
        spinner.setAttribute("role", "status");
        current.appendChild(spinner);
        thinkingLabel = document.createElement("span");
        current.appendChild(thinkingLabel);

        thinkingSteps = document.createElement("div");
        thinkingSteps.className = "cp-thinking-steps text-muted small ps-4";

        thinkingEl.appendChild(thinkingSteps);
        thinkingEl.appendChild(current);
      }
      // Re-attach every time, not just on the first call. A debounced search that lands while we
      // are waiting calls render(), which empties the results container and takes the strip out of
      // the DOM with it — but this variable still points at the now-detached node, so every later
      // progress line would update something nobody can see and the palette would look frozen
      // until the answer arrived. The voice path hits this every single time: the interim
      // transcript arms a search, the final transcript submits the assist request.
      if (thinkingEl.parentNode !== results) {
        results.classList.add("cp-dimmed");
        results.insertBefore(thinkingEl, results.firstChild);
      }
      // The line that was current becomes a completed step, so the user can see the path it took
      // rather than just the latest frame.
      if (thinkingLabel.textContent && thinkingLabel.textContent !== text) {
        var done = document.createElement("div");
        done.className = "cp-thinking-step";
        done.textContent = thinkingLabel.textContent;
        thinkingSteps.appendChild(done);
      }
      thinkingLabel.textContent = text || "Working…";
    }

    function clearThinking() {
      if (thinkingEl && thinkingEl.parentNode) {
        thinkingEl.parentNode.removeChild(thinkingEl);
      }
      results.classList.remove("cp-dimmed");
      thinkingEl = null;
      thinkingLabel = null;
      thinkingSteps = null;
    }

    // Read a newline-delimited JSON stream, handing each complete object to `onEvent`.
    // Falls back to reading the whole body when the browser has no streaming reader, in which
    // case the progress lines simply all arrive at once at the end — same final answer.
    function readNdjson(response, onEvent) {
      function handleChunkText(text, buffer) {
        buffer += text;
        var lines = buffer.split("\n");
        buffer = lines.pop(); // last piece may be a partial line
        lines.forEach(function (line) {
          if (!line.trim()) {
            return;
          }
          try {
            onEvent(JSON.parse(line));
          } catch (err) {
            /* a truncated or malformed line is skipped rather than breaking the stream */
          }
        });
        return buffer;
      }

      if (!response.body || !response.body.getReader) {
        return response.text().then(function (text) {
          var rest = handleChunkText(text, "");
          if (rest.trim()) {
            try {
              onEvent(JSON.parse(rest));
            } catch (err) {
              /* ignore a trailing partial line */
            }
          }
        });
      }

      var reader = response.body.getReader();
      var decoder = new TextDecoder();
      var buffer = "";
      return (function pump() {
        return reader.read().then(function (chunk) {
          if (chunk.done) {
            buffer += decoder.decode();
            if (buffer.trim()) {
              try {
                onEvent(JSON.parse(buffer));
              } catch (err) {
                /* ignore a trailing partial line */
              }
            }
            return null;
          }
          buffer = handleChunkText(decoder.decode(chunk.value, { stream: true }), buffer);
          return pump();
        });
      })();
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

      // Which auction (or club) this is about to write to, worked out by the server rather than
      // taken from the model's own summary. This is the line that lets someone notice we picked
      // the wrong auction while the countdown is still running.
      if (response.context) {
        var context = document.createElement("div");
        context.className = "mb-2 small";
        var icon = document.createElement("i");
        icon.className = "bi bi-signpost-split me-1";
        context.appendChild(icon);
        var contextText = document.createElement("span");
        contextText.textContent = "In " + response.context;
        context.appendChild(contextText);
        body.appendChild(context);
      }

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
        reportCancelled(response);
        results.innerHTML = "";
        clearNav();
        results.appendChild(renderNote("Cancelled — nothing was changed.", "info", "bi-info-circle"));
      });
    }

    // Tell the server the user stopped this one. Nothing was written, so there is nothing to undo;
    // this exists purely so a command we understood as the wrong thing leaves a trace. Fire and
    // forget — a failed beacon must never be something the user sees.
    function reportCancelled(response) {
      if (!cancelUrl || !response || !response.usage_id) {
        return;
      }
      fetch(cancelUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken },
        credentials: "same-origin",
        keepalive: true,
        body: JSON.stringify({ usage_id: response.usage_id }),
      }).catch(function () {});
    }

    function runExecute(query, response) {
      showThinking((response.summary || "Doing that") + "…");
      fetch(executeUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken },
        credentials: "same-origin",
        body: JSON.stringify({
          action: response.action,
          params: response.params,
          path: window.location.pathname,
        }),
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
      clearThinking();
      var kind = response && response.kind;
      if (kind === "results") {
        render(response.groups || []);
        // The server sends a note when these results are a fallback rather than a real answer, so
        // the user is told we guessed instead of being left to assume we understood.
        if (response.note) {
          results.insertBefore(renderNote(response.note, "warning", "bi-question-circle"), results.firstChild);
        }
        return;
      }
      if (kind === "navigate" && response.url) {
        rememberExchange(query, response.message || "Opened a page", "navigate", {});
        navigatedByClick = true;
        finalized = true;
        // Say where we're going before going there. Navigating instantly gets the destination right
        // and still feels like the palette ignored you — by the time the new page paints there has
        // been nothing on screen that named it, so a wrong guess is indistinguishable from a right
        // one. The pause is short enough not to be a wait and long enough to be read.
        results.innerHTML = "";
        clearNav();
        results.appendChild(
          renderNote(response.message || "Taking you there…", "info", "bi-box-arrow-up-right")
        );
        cancelPendingNavigation();
        pendingNavigation = window.setTimeout(function () {
          pendingNavigation = null;
          window.location.href = response.url;
        }, NAVIGATE_DELAY_MS);
        return;
      }
      if (kind === "answer") {
        rememberExchange(query, response.message || "Answered", "answer", {});
        results.innerHTML = "";
        clearNav();
        results.appendChild(renderNote(response.message || "", "info", "bi-chat-left-quote"));
        // Whatever the answer was about is usually one click away; the answer alone is a dead end.
        if (response.groups && response.groups.length) {
          renderInto(response.groups);
        }
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
        } else if (response.groups && response.groups.length) {
          // The model asked without offering choices. Rather than leaving a question with nothing
          // to click — unusable by voice, annoying by keyboard — show what search found for it.
          renderInto(response.groups);
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
      renderGeneration += 1; // any search already in flight is now stale
      finalized = true; // this query ends in an assist result, not an abandoned search
      // A search armed by the last keystroke (or by the interim speech transcript) is about to be
      // answered by this request instead, so don't let it land on top of the assist result.
      clearTimeout(debounceTimer);
      showThinking("Working out what you mean…");
      var answered = false;
      fetch(assistUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken },
        credentials: "same-origin",
        // `path` lets the server work out which auction/club/lot we're looking at. It resolves the
        // path through its own URLconf and re-checks every object, so this is a hint, not a claim.
        body: JSON.stringify({ q: query, context: loadContext(), path: window.location.pathname }),
      })
        .then(function (resp) {
          return readNdjson(resp, function (event) {
            if (event && event.kind === "progress") {
              showThinking(event.message);
              return;
            }
            answered = true;
            renderAssist(query, event);
          });
        })
        .catch(function () {
          // A dropped connection mid-stream: the last thing on screen would otherwise be a
          // spinner that never resolves.
          if (!answered) {
            answered = true;
            renderAssist(query, { kind: "error", message: "I couldn't reach the assistant just now." });
          }
        })
        .then(function () {
          // The stream ended without a final object (truncated response, server restart).
          if (!answered) {
            renderAssist(query, { kind: "error", message: "I couldn't reach the assistant just now." });
          }
          assistInFlight = false;
        });
    }

    // --- Speech to text ------------------------------------------------------

    var SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    var recognition = null;
    var listening = false;
    var paletteMic = document.getElementById("command-palette-mic");
    var micHint = document.getElementById("command-palette-mic-hint");
    var micHintTimer = null;

    // Whether the mic starts on its own next time the palette opens, remembered from the last time
    // the user touched the button. localStorage rather than the account: which of your devices has
    // a microphone you're willing to talk to is a property of the device, not of you -- dictating
    // commands on a phone shouldn't switch the mic on at a desk in an office.
    var MIC_AUTO_KEY = "cp_mic_auto";
    // How many times we've explained an auto-start. Capped, because the explanation is only news
    // the first couple of times; after that the mic coming on by itself is just how it works.
    var MIC_HINT_KEY = "cp_mic_auto_hints";
    var MIC_HINT_MAX = 2;
    var MIC_HINT_MS = 6000;

    function storageGet(key) {
      try {
        return window.localStorage.getItem(key);
      } catch (err) {
        return null; // private mode, or storage disabled: fall back to "never remembered"
      }
    }

    function storageSet(key, value) {
      try {
        window.localStorage.setItem(key, value);
      } catch (err) {
        /* nothing to do; the feature just doesn't persist */
      }
    }

    function micAutoRemembered() {
      return storageGet(MIC_AUTO_KEY) === "1";
    }

    // Called only for a real click on the mic, never for an auto-start: the point is to remember
    // what the user chose. Turning it off during an auto-started session is a choice too -- it's
    // how someone who doesn't want this any more turns it back off.
    function rememberMicChoice(wanted) {
      storageSet(MIC_AUTO_KEY, wanted ? "1" : "0");
    }

    function hideMicHint() {
      window.clearTimeout(micHintTimer);
      if (micHint) {
        micHint.classList.add("d-none");
      }
    }

    function showMicHint() {
      if (!micHint) {
        return;
      }
      var shown = parseInt(storageGet(MIC_HINT_KEY), 10) || 0;
      if (shown >= MIC_HINT_MAX) {
        return;
      }
      storageSet(MIC_HINT_KEY, String(shown + 1));
      micHint.classList.remove("d-none");
      window.clearTimeout(micHintTimer);
      micHintTimer = window.setTimeout(hideMicHint, MIC_HINT_MS);
    }

    function setListening(state) {
      listening = state;
      if (paletteMic) {
        paletteMic.classList.toggle("listening", state);
        paletteMic.setAttribute("aria-pressed", state ? "true" : "false");
      }
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

    var micAvailable = false;

    if (assistEnabled && SpeechRecognition && paletteMic) {
      micAvailable = true;
      recognition = buildRecognition();
      paletteMic.classList.remove("d-none");
      paletteMic.addEventListener("click", function () {
        // A deliberate click always wins over the explanation for the automatic one.
        hideMicHint();
        if (listening) {
          stopListening();
          rememberMicChoice(false);
        } else {
          startListening();
          rememberMicChoice(true);
        }
      });
    }

    // The palette is open and the user used the mic last time, so switch it on for them. Delayed a
    // beat: the modal is still animating in, and Chrome refuses recognition.start() on an element
    // that isn't visible yet.
    function autoStartListening() {
      if (!micAvailable || listening || !micAutoRemembered()) {
        return;
      }
      window.setTimeout(function () {
        // Still worth doing? The user may have closed the palette or started typing in the 250ms
        // this waited, and neither should be interrupted by the mic coming on.
        if (!listening && modalEl.classList.contains("show") && !input.value) {
          startListening();
          // Only explain something that actually happened -- start() throws when the browser
          // won't give us the microphone, and setListening(false) has already put it back.
          if (listening) {
            showMicHint();
          }
        }
      }, 250);
    }

    // --- Events --------------------------------------------------------------

    input.addEventListener("input", function () {
      // Typing means the user has moved on: never let a countdown started a moment ago fire
      // against a query they are already editing.
      cancelCountdown();
      hideMicHint();
      // Someone who opened the palette with Ctrl+K and started typing is not talking to it, and an
      // auto-started mic that keeps listening would overwrite what they typed with a transcript
      // and submit it. Only a real keystroke gets here -- the recognizer sets input.value
      // directly, which fires no input event -- so this can't cut off dictation mid-sentence.
      stopListening();
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
      autoStartListening();
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
      // Never leave the microphone running, a countdown pending, a hint on screen, or a navigation
      // about to fire once the palette is hidden.
      stopListening();
      hideMicHint();
      cancelCountdown();
      cancelPendingNavigation();
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
