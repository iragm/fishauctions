/* Command palette: cross-site search + jump-to-page, opened from the navbar brand or Ctrl/Cmd+K.
 * Talks to the JSON endpoints named `command_palette` (results) and `command_palette_log` (search log).
 * Search logging keeps a single row per session: it is upserted as the query is refined, finalized as
 * "clicked" when a result is opened, or "abandoned" when the box is cleared / the palette is closed. */
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
    var csrfToken = modalEl.dataset.csrf;
    var modal = bootstrap.Modal.getOrCreateInstance(modalEl);

    var DEBOUNCE_MS = 300;
    var debounceTimer = null;
    var currentSearchId = null; // pk of the in-progress CommandPaletteSearch row
    var lastQuery = ""; // last query we ran a search for
    var lastQueryEmpty = false; // did the most recent executed query return zero results?
    var navigatedByClick = false; // suppress the abandon-on-close beacon after a click
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

    // result is "pending" while results exist, or "bounce" when the query returned nothing
    // (bounces let us mine common typos/missing phrases and add them as synonyms later).
    function logSearchState(query, result) {
      fetch(logUrl, {
        method: "POST",
        headers: { "X-CSRFToken": csrfToken },
        body: logBody({ search: query, result: result || "pending" }),
        credentials: "same-origin",
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
    }

    // Finalize the session when the box is cleared or the palette closes. A query that ended with
    // no results is recorded as a "bounce" (mined later for missing shortcuts); otherwise the user
    // looked but didn't pick anything: "abandoned". Without this, the close handler would always
    // overwrite an earlier "bounce" with "abandoned", so the analytics never counted bounces.
    function logFinal(query) {
      if (!currentSearchId || !query) {
        return;
      }
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

    function logClickAndGo(item) {
      navigatedByClick = true;
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
      fetch(logUrl, { method: "POST", headers: { "X-CSRFToken": csrfToken }, body: body, credentials: "same-origin" })
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
      fetchResults(query, function (count) {
        lastQueryEmpty = count === 0;
        logSearchState(query, count === 0 ? "bounce" : "pending");
      });
    }

    // --- Events --------------------------------------------------------------

    input.addEventListener("input", function () {
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
      currentSearchId = null;
      lastQuery = "";
      input.value = "";
      fetchResults("");
    });
    modalEl.addEventListener("hidden.bs.modal", function () {
      if (!navigatedByClick) {
        logFinal(input.value.trim());
      }
    });

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
