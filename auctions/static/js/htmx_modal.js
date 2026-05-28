(function () {
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
    if (root && root.parentNode) {
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
