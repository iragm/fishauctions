(function () {
  // The one container every modal on this site is swapped into. base.html renders it, once, and
  // nothing else may: a second element with this id anywhere on a page makes htmx and
  // document.querySelector pick whichever comes first, so the two take turns being destroyed --
  // which is what made this bug survive three attempts to fix it as "the modal won't reopen".
  // auctions/template_lint.py fails the build if another template declares it.
  var MODAL_CONTAINER_ID = "modals-here";

  // Force innerHTML for anything aimed at the modal container, whatever the trigger inherited.
  //
  // hx-swap is an inherited attribute: htmx walks up from the element that was clicked and uses the
  // first hx-swap it finds. Every one of these modals is opened by a link *inside* a table, and a
  // table whose wrapper refreshes itself carries hx-swap="outerHTML" for its own request. The link
  // inherits it, so opening a modal REPLACED #modals-here with the modal markup instead of filling
  // it. The modal still appeared, so it looked like it worked -- but the container was gone, and
  // the next click had nothing to swap into and failed silently with htmx:targetError.
  //
  // The wrappers now carry hx-disinherit="hx-swap" so the inheritance stops at the source. This is
  // the belt to that pair of braces, because the next table written without it would bring the bug
  // back. htmx 1.x reads the override off detail.etc (the same object it later consults); htmx 2.x
  // reads detail.swapOverride. Setting both means an htmx upgrade cannot quietly undo this.
  document.addEventListener("htmx:beforeSwap", function (event) {
    var detail = event.detail || {};
    var target = detail.target;
    if (!target || target.id !== MODAL_CONTAINER_ID) {
      return;
    }
    detail.swapOverride = "innerHTML";
    if (detail.etc) {
      detail.etc.swapOverride = "innerHTML";
    }
  });

  function parseEventNames(eventName) {
    if (!eventName) {
      return [];
    }
    return String(eventName)
      .split(",")
      .map(function (name) {
        return name.trim();
      })
      .filter(Boolean);
  }

  function triggerEvents(eventName) {
    if (typeof htmx === "undefined") {
      return;
    }
    parseEventNames(eventName).forEach(function (name) {
      htmx.trigger(document.body, name);
    });
  }

  function removeModalRoot(root) {
    // Never the container itself. Closing a modal must not take the thing every later modal is
    // swapped into with it -- if some future response ever hands us the container as the root, the
    // modal failing to close is a bug you can see, and a page that can no longer open one is not.
    if (root && root.id !== MODAL_CONTAINER_ID && root.parentNode) {
      root.parentNode.removeChild(root);
    }
  }

  var FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), ' +
    'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

  function HtmxModalController(modalRoot) {
    var self = this;
    this.modalRoot = modalRoot;
    this.previouslyFocused = document.activeElement;
    if (this.modalRoot && this.modalRoot.parentNode !== document.body) {
      document.body.appendChild(this.modalRoot);
    }
    this.bindCloseButtons();
    this.bindAccessibility();
    this.onKeydown = function (event) {
      if (!self.modalRoot || !self.modalRoot.isConnected) {
        return;
      }
      if (event.key === "Escape") {
        // A widget open inside this modal, or a dialog stacked on top of it, takes the Escape itself.
        var stackedDialog = Array.prototype.some.call(document.querySelectorAll(".modal.show"), function (m) {
          return !self.modalRoot.contains(m);
        });
        var openWidget = Array.prototype.some.call(
          document.querySelectorAll(".select2-container--open, .bootstrap-datetimepicker-widget"),
          function (el) { return el.offsetParent !== null; }
        );
        if (stackedDialog || openWidget) {
          return;
        }
        event.preventDefault();
        self.close({ action: "none" });
      } else if (event.key === "Tab") {
        // Keep focus inside the dialog: wrap from the last focusable element to the first.
        var items = Array.prototype.filter.call(self.modalRoot.querySelectorAll(FOCUSABLE), function (el) {
          return el.offsetParent !== null;
        });
        if (!items.length) {
          return;
        }
        var first = items[0];
        var last = items[items.length - 1];
        var inside = self.modalRoot.contains(document.activeElement);
        if (event.shiftKey && (!inside || document.activeElement === first)) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && (!inside || document.activeElement === last)) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", this.onKeydown, true);
    this.focusFirst();
  }

  HtmxModalController.prototype.dialogElement = function () {
    if (!this.modalRoot) {
      return null;
    }
    return this.modalRoot.classList.contains("modal") ? this.modalRoot : this.modalRoot.querySelector(".modal");
  };

  HtmxModalController.prototype.bindAccessibility = function () {
    var dialog = this.dialogElement();
    if (!dialog) {
      return;
    }
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    var title = dialog.querySelector(".modal-title[id]");
    if (title) {
      dialog.setAttribute("aria-labelledby", title.id);
    }
  };

  HtmxModalController.prototype.focusFirst = function () {
    var dialog = this.dialogElement();
    if (!dialog) {
      return;
    }
    var first = dialog.querySelector(FOCUSABLE);
    if (first) {
      first.focus();
    }
  };

  HtmxModalController.prototype.bindCloseButtons = function () {
    var self = this;
    if (!this.modalRoot) {
      return;
    }
    this.modalRoot.querySelectorAll("[data-modal-close-action]").forEach(function (button) {
      button.addEventListener("click", function () {
        self.close({ action: button.dataset.modalCloseAction || "none" });
      });
    });
  };

  HtmxModalController.prototype.close = function (detail) {
    detail = detail || {};
    document.removeEventListener("keydown", this.onKeydown, true);
    removeModalRoot(this.modalRoot);
    if (this.previouslyFocused && this.previouslyFocused.isConnected && this.previouslyFocused.focus) {
      this.previouslyFocused.focus();
    }

    triggerEvents(detail.eventName);

    if (detail.action === "reload-page") {
      window.location.reload();
    } else if (detail.action === "redirect" && detail.redirectUrl) {
      window.location.href = detail.redirectUrl;
    } else if (detail.action === "reload-table") {
      var selector = detail.tableSelector || ".table-container";
      var tableElement = document.querySelector(selector);
      if (tableElement && typeof htmx !== "undefined") {
        htmx.trigger(tableElement, "refresh");
      }
    }
  };

  var activeModal = null;

  window.mountHtmxModal = function (modalRoot) {
    if (activeModal) {
      activeModal.close({ action: "none" });
    }
    activeModal = new HtmxModalController(modalRoot);
    return activeModal;
  };

  window.closeModal = function (detail) {
    if (activeModal) {
      activeModal.close(detail);
      activeModal = null;
      return;
    }

    removeModalRoot(document.querySelector("[data-htmx-modal-root]"));
    detail = detail || {};
    triggerEvents(detail.eventName);
    if (detail.action === "reload-page") {
      window.location.reload();
    } else if (detail.action === "redirect" && detail.redirectUrl) {
      window.location.href = detail.redirectUrl;
    }
  };
})();
