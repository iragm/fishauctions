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

  function HtmxModalController(modalRoot) {
    this.modalRoot = modalRoot;
    if (this.modalRoot && this.modalRoot.parentNode !== document.body) {
      document.body.appendChild(this.modalRoot);
    }
    this.bindCloseButtons();
  }

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
    removeModalRoot(this.modalRoot);

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
