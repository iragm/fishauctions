/* The unsaved-changes bar, dirty tracking, and the abandonment beacon.
 *
 * Loaded on every page by base.html and it finds its own forms, because the include it replaced
 * had to be added by hand and was on 7 of the 99 templates that render a form. The other 92 lost
 * whatever you had typed if you clicked a link, with no warning at all.
 *
 * What a form has to be for this to attach to it:
 *
 *   - method="post" -- a GET form is a search or a filter, and nothing is lost by leaving one;
 *   - at least MIN_FIELDS editable fields, which excludes every confirm-delete page, every
 *     "mark contacted" button and every one-field table filter. A single button is not work;
 *   - not marked data-no-unsaved-warning, the opt-out for a form where a bar would be in the way.
 *
 * Dirty means a value differs from the one the field was rendered with -- not, as the old version
 * had it, that a field was ever blurred. Tabbing through a form you have not touched used to arm
 * the browser's "leave site?" dialog, which teaches people to click through those dialogs, which
 * is the opposite of what they are for.
 *
 * The bar is the part that matters. A browser's unload prompt appears once somebody has already
 * decided to leave, cannot say what would be lost, and cannot save. A bar pinned to the bottom of
 * the viewport says there are unsaved changes while there is still something to do about it, and
 * brings the Save button up from wherever the bottom of a four-dozen-field form is.
 *
 * The third job is reporting the abandonment: see auctions/friction_models.py. Field names and a
 * duration, never a value -- the whole point of the case is that the values were not saved.
 *
 * HTMx is most of the forms on this site, and it breaks every assumption a page-lifecycle version
 * of this would make. Four separate cases, each handled below:
 *
 *   1. A form that did not exist at load. Modals, inline panels and table headers arrive in a
 *      swap, so attaching once on DOMContentLoaded reaches none of them -- attach() runs again on
 *      every htmx:afterSwap, over the swapped subtree.
 *   2. A form that saves without unloading the page. There is no page-level submit and no unload,
 *      so a form saved by hx-post would stay "dirty" for ever and warn on the way out of a page
 *      whose changes were saved twenty minutes ago. A successful request over a form re-snapshots
 *      it.
 *   3. A form swapped *away* while dirty. This is an abandonment -- the values are gone and
 *      nothing was saved -- and it is one beforeunload will never see, because the page never
 *      unloads. It is reported at the moment of the swap, and confirmed first.
 *   4. Navigation that is not navigation. An hx-get link replacing the region a dirty form is in
 *      loses the work with no browser dialog anywhere, because as far as the browser is concerned
 *      nothing happened. htmx:beforeRequest is cancelable, so that is where the question is asked.
 *
 * Trackers are pruned whenever they are used: a swap detaches form elements without telling
 * anybody, and a detached form's fields still answer questions about their values.
 */
(function () {
  "use strict";

  // Below this a form is a button, not work: confirm-delete pages, single-filter table headers,
  // "mark contacted". Two is the smallest thing worth warning about losing.
  var MIN_FIELDS = 2;
  // Widgets that rewrite their own control (Summernote, select2) fire change while initialising.
  // Snapshotting before that would show the bar on a page nobody had touched.
  var SNAPSHOT_DELAY_MS = 400;

  var config = document.getElementById("unsaved-changes-config");
  var beaconUrl = config ? config.getAttribute("data-beacon-url") : "";
  var token = config ? config.getAttribute("data-token") : "";
  // The beacon posts to a DRF view using SessionAuthentication, which enforces CSRF for a signed-in
  // session -- so without this every abandonment from a logged-in organizer is a 403, which is
  // every abandonment worth having. It goes in the body rather than a header because sendBeacon
  // cannot set headers, and Django's CSRF middleware reads POST["csrfmiddlewaretoken"] first.
  var csrfToken = config ? config.getAttribute("data-csrf") : "";
  var bar = document.getElementById("unsaved-changes-bar");
  var counter = document.getElementById("unsaved-changes-count");
  var saveButton = document.getElementById("unsaved-changes-save");
  var discardButton = document.getElementById("unsaved-changes-discard");
  if (!bar || !counter || !saveButton || !discardButton) { return; }

  var openedAt = Date.now();
  var reported = [];
  var tracked = [];

  function editableFields(form) {
    return Array.prototype.filter.call(
      form.querySelectorAll("input, select, textarea"),
      function (el) {
        return el.name && el.type !== "hidden" && el.type !== "submit" && el.type !== "button" &&
          el.type !== "reset" && !el.disabled && el.name !== "csrfmiddlewaretoken";
      }
    );
  }

  function valueOf(el) {
    if (el.type === "checkbox" || el.type === "radio") { return el.checked ? "1" : ""; }
    if (el.multiple) {
      return Array.prototype.filter.call(el.options, function (o) { return o.selected; })
        .map(function (o) { return o.value; }).join(",");
    }
    if (el.type === "file") { return el.files && el.files.length ? String(el.files.length) : ""; }
    return el.value == null ? "" : String(el.value);
  }

  function qualifies(form) {
    var method = (form.getAttribute("method") || "get").toLowerCase();
    if (method !== "post") { return false; }
    if (form.hasAttribute("data-no-unsaved-warning")) { return false; }
    return editableFields(form).length >= MIN_FIELDS;
  }

  function Tracker(form) {
    this.form = form;
    this.initial = new WeakMap();
    this.submitting = false;
    this.snapshot();
  }

  Tracker.prototype.snapshot = function () {
    var initial = this.initial;
    editableFields(this.form).forEach(function (el) { initial.set(el, valueOf(el)); });
  };

  // Names only. A field added to the page after the snapshot has no initial value recorded and is
  // treated as clean rather than guessed at.
  Tracker.prototype.changed = function () {
    var initial = this.initial;
    if (this.submitting) { return []; }
    return editableFields(this.form).filter(function (el) {
      return initial.has(el) && initial.get(el) !== valueOf(el);
    }).map(function (el) { return el.name; });
  };

  // A swap detaches form elements without telling anybody, and a detached form's fields still
  // answer questions about their values -- so a stale tracker keeps the bar up for a form that is
  // no longer on the page.
  function live() {
    tracked = tracked.filter(function (tracker) { return document.contains(tracker.form); });
    return tracked;
  }

  function allChanged() {
    var names = [];
    live().forEach(function (tracker) {
      tracker.changed().forEach(function (name) { names.push(name); });
    });
    return names;
  }

  // A form that arrived in a swap can carry its own token; otherwise the page's is used. This is
  // what lets an htmx-rendered form report being abandoned at all.
  function tokenFor(form) {
    return (form && form.getAttribute("data-friction-token")) || token;
  }

  function dirtyTracker() {
    var rows = live();
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].changed().length) { return rows[i]; }
    }
    return null;
  }

  // One tracker's worth of abandonment, sent now rather than at unload -- an htmx swap destroys
  // the form without the page ever going away, so this is the only moment it can be reported.
  // keepalive because the caller may be mid-navigation; sendBeacon cannot carry the credentials
  // this needs when it is not an unload.
  function report(tracker) {
    var changed = tracker.changed();
    var formToken = tokenFor(tracker.form);
    if (!changed.length || !formToken || !beaconUrl || reported.indexOf(formToken) !== -1) { return; }
    reported.push(formToken);
    var payload = new FormData();
    payload.append("csrfmiddlewaretoken", csrfToken);
    payload.append("token", formToken);
    payload.append("fields", changed.join(","));
    payload.append("seconds", String(Math.round((Date.now() - openedAt) / 1000)));
    payload.append("url", window.location.pathname);
    if (window.fetch) {
      window.fetch(beaconUrl, { method: "POST", body: payload, keepalive: true, credentials: "same-origin" })
        .catch(function () { /* the page is going away; there is nobody to tell */ });
    } else if (navigator.sendBeacon) {
      navigator.sendBeacon(beaconUrl, payload);
    }
  }

  function refresh() {
    var changed = allChanged();
    var dirty = changed.length > 0;
    bar.classList.toggle("showing", dirty);
    document.body.classList.toggle("has-unsaved-changes-bar", dirty);
    counter.textContent = dirty
      ? " to " + changed.length + " field" + (changed.length === 1 ? "" : "s")
      : "";
  }

  function attach(form) {
    var tracker = new Tracker(form);
    tracked.push(tracker);
    form.addEventListener("input", refresh);
    form.addEventListener("change", refresh);
    form.addEventListener("submit", function (event) {
      tracker.submitting = true;
      refresh();
      // Double-submit guard, carried over from the per-template include this replaced. Two things
      // about it are load-bearing:
      //
      // On a timeout, because the browser builds the form data *after* dispatching submit, so
      // disabling a named button synchronously drops its name and value from the payload -- which
      // is how a multi-button form quietly loses the button that was pressed.
      //
      // And only when the submission is actually going somewhere. Ten templates here call
      // preventDefault() in their own submit handler and post with fetch (the treasurer ledger,
      // the custom-fields form, the image form): the page never navigates, so a button disabled
      // on submit is a button that works once. defaultPrevented is checked inside the timeout,
      // by which point every other handler has run.
      window.setTimeout(function () {
        if (event.defaultPrevented) { return; }
        Array.prototype.forEach.call(form.querySelectorAll("[type=submit]"), function (button) {
          button.disabled = true;
        });
      }, 0);
      // A cancelled submit is not a save. Anything that posts with fetch re-snapshots through
      // htmx:afterRequest or leaves the form dirty, which is the honest answer either way.
      window.setTimeout(function () {
        if (event.defaultPrevented) {
          tracker.submitting = false;
          refresh();
        }
      }, 0);
    });
    // Case 2: an HTMx form never fires a page-level submit and never unloads the page, so without
    // this it stays dirty for ever and warns on the way out of a page whose changes were saved.
    form.addEventListener("htmx:afterRequest", function (event) {
      if (event.detail && event.detail.successful) {
        tracker.submitting = false;
        tracker.snapshot();
        refresh();
      }
    });
    if (window.jQuery) {
      window.jQuery(form).on("summernote.change select2:select select2:unselect select2:clear", refresh);
    }
  }

  function attached(form) {
    for (var i = 0; i < tracked.length; i++) {
      if (tracked[i].form === form) { return true; }
    }
    return false;
  }

  function scan(root) {
    if (!root || !root.querySelectorAll) { return; }
    if (root.tagName === "FORM" && qualifies(root) && !attached(root)) { attach(root); }
    Array.prototype.forEach.call(root.querySelectorAll("form"), function (form) {
      if (qualifies(form) && !attached(form)) { attach(form); }
    });
    refresh();
  }

  function start() {
    scan(document);
  }

  saveButton.addEventListener("click", function () {
    var tracker = dirtyTracker();
    if (!tracker) { return; }
    var button = tracker.form.querySelector("[type=submit]");
    if (button) { button.click(); } else { tracker.form.submit(); }
  });

  discardButton.addEventListener("click", function () {
    tracked.forEach(function (tracker) {
      editableFields(tracker.form).forEach(function (el) {
        if (!tracker.initial.has(el)) { return; }
        var was = tracker.initial.get(el);
        if (el.type === "checkbox" || el.type === "radio") { el.checked = was === "1"; }
        else if (el.type !== "file") { el.value = was; }
      });
      if (window.jQuery) { window.jQuery(tracker.form).find("select").trigger("change"); }
    });
    refresh();
  });

  // Case 4: an hx-get link replacing the region a dirty form sits in loses the work with no
  // browser dialog anywhere, because as far as the browser is concerned nothing happened.
  // htmx:beforeRequest is cancelable, which is the only place this question can be asked.
  document.body.addEventListener("htmx:beforeRequest", function (event) {
    var detail = event.detail || {};
    var target = detail.target;
    var source = detail.elt;
    if (!target || !target.contains) { return; }
    var doomed = live().filter(function (tracker) {
      // A request the form itself made is the form being saved, not the form being destroyed.
      if (source && tracker.form.contains(source)) { return false; }
      return (target === tracker.form || target.contains(tracker.form)) && tracker.changed().length;
    });
    if (!doomed.length) { return; }
    if (!window.confirm("You have unsaved changes here. Leave without saving them?")) {
      event.preventDefault();
      return;
    }
    doomed.forEach(report);
  });

  // Case 3: a dirty form swapped away is an abandonment, and it is the one abandonment beforeunload
  // will never see. Reported here because after the swap the form -- and everything that was typed
  // into it -- is gone.
  document.body.addEventListener("htmx:beforeSwap", function (event) {
    var target = event.detail && event.detail.target;
    if (!target || !target.contains) { return; }
    live().forEach(function (tracker) {
      if (target === tracker.form || target.contains(tracker.form)) { report(tracker); }
    });
  });

  // Case 1: forms that did not exist at page load -- modals, inline panels, table headers.
  document.body.addEventListener("htmx:afterSwap", function (event) {
    scan(event.detail && event.detail.target);
  });

  window.addEventListener("beforeunload", function (event) {
    if (allChanged().length) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  // pagehide rather than beforeunload: beforeunload never fires on iOS, and is not a place from
  // which anything can reliably be sent. report() prefers fetch(keepalive) and falls back to
  // sendBeacon, both of which survive the page going away.
  window.addEventListener("pagehide", function () {
    live().forEach(report);
  });

  window.setTimeout(start, SNAPSHOT_DELAY_MS);
})();
