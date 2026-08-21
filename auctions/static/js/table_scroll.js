/**
 * Dropdown menus inside a horizontally scrolling table.
 *
 * `.table-responsive` is `overflow-x: auto`, and a box that clips one axis clips the other too --
 * so an Actions menu opened in the last row of a table used to be cut off by the edge of the
 * scroller with no way to reach the rest of it. Bootstrap positions dropdowns with Popper using
 * `strategy: "absolute"`, which is what gets clipped; `"fixed"` positions against the viewport
 * instead and escapes the scroller.
 *
 * `show.bs.dropdown` fires on the toggle *before* Bootstrap builds its Popper instance (see
 * Dropdown.prototype.show), which is the one moment the config can still be changed. Everything is
 * guarded: if a future Bootstrap renames the private config, menus go back to being clipped rather
 * than throwing.
 */
(function () {
  document.addEventListener("show.bs.dropdown", function (event) {
    var toggle = event.target;
    if (!toggle || !toggle.closest || !toggle.closest(".table-responsive")) {
      return;
    }
    try {
      var instance = window.bootstrap && bootstrap.Dropdown.getInstance(toggle);
      if (instance && instance._config) {
        instance._config.popperConfig = { strategy: "fixed" };
      }
    } catch (e) {
      /* clipped menu is better than a broken page */
    }
  });
})();
