// Loaded inside django-summernote's editor iframe (SUMMERNOTE_CONFIG["js"] in settings.py), whose
// Bootstrap is the 5.0.2 that django-summernote pins and settings cannot change.
// Summernote 0.8.20's Bootstrap 5 build still marks its toolbar dropdowns (style, and any other
// dropdown button) with Bootstrap 4's data-toggle, which Bootstrap 5 ignores, so clicking them did
// nothing. Bootstrap 5.0 also holds one component per element, and summernote has already put a
// tooltip on each button, so that tooltip (and the click handler that hides it, which would make it
// again) gives way to a plain title before the dropdown can attach.
// The editor is an iframe as tall as the field, so a long menu (style has nine entries) scrolls
// inside it rather than running off the bottom where nobody can reach it.
$(document).on("summernote.init", function (event) {
  $(event.target)
    .next(".note-editor")
    .find('[data-toggle="dropdown"]')
    .each(function () {
      var tooltip = bootstrap.Tooltip.getInstance(this);
      if (tooltip) {
        tooltip.dispose();
      }
      $(this).off("click");
      this.setAttribute("title", this.getAttribute("aria-label") || "");
      this.removeAttribute("data-toggle");
      this.setAttribute("data-bs-toggle", "dropdown");
      $(this.parentNode).on("shown.bs.dropdown", function () {
        var menu = $(this).children(".dropdown-menu");
        menu.css({
          maxHeight: window.innerHeight - menu[0].getBoundingClientRect().top - 8,
          overflowY: "auto",
        });
      });
    });
});
