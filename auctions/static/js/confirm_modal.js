/**
 * Styled confirmation dialogs, replacing the browser's bare confirm().
 *
 * A native confirm() is unstyled, says "127.0.0.1 says", can't be branded, is suppressed
 * outright by "prevent this page from creating additional dialogs", and reads as a phishing
 * prompt inside the mobile app's webview. See style_reference.md.
 *
 * Declarative — no per-page JavaScript:
 *
 *   <form method="post"
 *         data-confirm="Disconnect Google Calendar? Syncing will stop."
 *         data-confirm-title="Disconnect Google Calendar?"
 *         data-confirm-ok="Disconnect"
 *         data-confirm-variant="danger">
 *
 * Works on a <form> (intercepts submit) or on an <a>/<button> (intercepts click). Falls back to
 * the native confirm() only if Bootstrap's JS somehow isn't loaded, so an action can never
 * proceed unconfirmed.
 */
(function () {
  var VARIANTS = {
    // Matches the button intents in style_reference.md.
    danger: "btn-danger",
    success: "btn-success text-dark",
    primary: "btn-primary",
  };

  function buildModal(options) {
    var wrapper = document.createElement("div");
    wrapper.className = "modal fade";
    wrapper.setAttribute("tabindex", "-1");
    wrapper.setAttribute("role", "dialog");
    wrapper.setAttribute("aria-modal", "true");

    var okClass = VARIANTS[options.variant] || VARIANTS.primary;
    var dialog = document.createElement("div");
    dialog.className = "modal-dialog modal-dialog-centered";

    var content = document.createElement("div");
    content.className = "modal-content";

    var header = document.createElement("div");
    header.className = "modal-header";
    var title = document.createElement("h5");
    title.className = "modal-title";
    title.textContent = options.title;
    header.appendChild(title);

    var body = document.createElement("div");
    body.className = "modal-body";
    var message = document.createElement("p");
    message.className = "mb-0";
    // textContent, never innerHTML: these strings come from templates and must never be able to
    // inject markup into the dialog.
    message.textContent = options.message;
    body.appendChild(message);

    var footer = document.createElement("div");
    footer.className = "modal-footer";
    var cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "btn btn-secondary";
    cancel.setAttribute("data-bs-dismiss", "modal");
    cancel.textContent = options.cancelLabel;
    var ok = document.createElement("button");
    ok.type = "button";
    ok.className = "btn " + okClass;
    ok.textContent = options.okLabel;
    footer.appendChild(cancel);
    footer.appendChild(ok);

    content.appendChild(header);
    content.appendChild(body);
    content.appendChild(footer);
    dialog.appendChild(content);
    wrapper.appendChild(dialog);
    document.body.appendChild(wrapper);
    return { wrapper: wrapper, ok: ok };
  }

  /**
   * Ask the user to confirm. Calls onConfirm() only if they agree.
   */
  window.confirmAction = function (options, onConfirm) {
    options = options || {};
    var settings = {
      message: options.message || "Are you sure?",
      title: options.title || "Are you sure?",
      okLabel: options.okLabel || "Yes",
      cancelLabel: options.cancelLabel || "Cancel",
      variant: options.variant || "primary",
    };

    if (!window.bootstrap || !bootstrap.Modal) {
      // Better a bare dialog than an action that fires with no confirmation at all.
      if (window.confirm(settings.message)) {
        onConfirm();
      }
      return;
    }

    var parts = buildModal(settings);
    var modal = new bootstrap.Modal(parts.wrapper);
    var confirmed = false;

    parts.ok.addEventListener("click", function () {
      confirmed = true;
      modal.hide();
    });
    parts.wrapper.addEventListener("hidden.bs.modal", function () {
      parts.wrapper.remove();
      if (confirmed) {
        onConfirm();
      }
    });
    modal.show();
  };

  function optionsFrom(element) {
    return {
      message: element.getAttribute("data-confirm"),
      title: element.getAttribute("data-confirm-title") || "Are you sure?",
      okLabel: element.getAttribute("data-confirm-ok") || "Yes",
      cancelLabel: element.getAttribute("data-confirm-cancel") || "Cancel",
      variant: element.getAttribute("data-confirm-variant") || "primary",
    };
  }

  document.addEventListener(
    "submit",
    function (event) {
      var form = event.target;
      if (!form.matches || !form.matches("form[data-confirm]")) {
        return;
      }
      if (form.dataset.confirmed === "yes") {
        // Second pass, after the user agreed — let it through and reset for next time.
        delete form.dataset.confirmed;
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      var submitter = event.submitter;
      window.confirmAction(optionsFrom(form), function () {
        form.dataset.confirmed = "yes";
        // requestSubmit re-fires submit, so HTMX and native validation still apply. Older
        // browsers without it fall back to a plain submit.
        if (form.requestSubmit) {
          form.requestSubmit(submitter);
        } else {
          form.submit();
        }
      });
    },
    true
  );

  document.addEventListener(
    "click",
    function (event) {
      var trigger = event.target.closest ? event.target.closest("[data-confirm]") : null;
      // Forms are handled on submit, so ignore clicks originating inside one.
      if (!trigger || trigger.tagName === "FORM" || trigger.closest("form[data-confirm]")) {
        return;
      }
      if (trigger.dataset.confirmed === "yes") {
        delete trigger.dataset.confirmed;
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      window.confirmAction(optionsFrom(trigger), function () {
        trigger.dataset.confirmed = "yes";
        if (trigger.tagName === "A" && trigger.href) {
          window.location.href = trigger.href;
        } else {
          trigger.click();
        }
      });
    },
    true
  );
})();
