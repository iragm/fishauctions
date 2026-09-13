/**
 * A filtered list, the same set drawn on a map, and a detail panel that slides in over both.
 *
 * Two pages are built this way -- the speaker directory and the club finder -- and they have to
 * behave identically: a row and its own map pin open the same panel, the panel pushes a URL worth
 * sharing, and filtering redraws the markers without a page load. That last part is why the map
 * can't just be rendered once: the htmx table response carries every matching row's coordinates
 * back out of band (see the *_table.html partials), and this redraws from that payload, so the
 * map and the list can never show different sets.
 *
 * Element ids stay per-page (`club-map`, `speaker-map`) so one page's markup can't reach into the
 * other's; everything that differs between the two lives in the config object:
 *
 *   initMapListPanel({
 *     prefix: "club",                     // ids: <prefix>-map, <prefix>-panel, <prefix>-view-map…
 *     panelUrl: function (slug) {...},    // the htmx fragment the panel is filled from
 *     pageUrl: function (slug) {...},     // the real page, pushed into the address bar
 *     origin: {lat: 42, lng: -72},        // where distances are measured from, or null
 *     originLabel: "your location",
 *     defaultView: "map",                 // which half the URL asked for
 *     callbackName: "initClubMap",        // the global Google Maps calls back
 *     menuLabelId: "interest-filter-label",   // optional: a radio menu that labels itself
 *     menuOptionClass: "interest-filter-option",
 *     menuFallbackLabel: "Interests"
 *   });
 */
(function () {
  "use strict";

  /* Brand colors, per style_reference.md: the origin pin is primary, the results are success. */
  var ORIGIN_COLOR = "#375a7f";
  var MARKER_COLOR = "#00bc8c";
  /* Roughly the northeast US, which is where this site started and where a visitor with no
     location set is least likely to be looking at an empty ocean. */
  var FALLBACK_CENTER = { lat: 42.0, lng: -72.0 };

  window.initMapListPanel = function (config) {
    var prefix = config.prefix;

    function byId(suffix) {
      return document.getElementById(prefix + "-" + suffix);
    }

    var shell = byId("panel-shell");
    var panel = byId("panel");
    var listWrapper = byId("list-wrapper");
    var mapWrapper = byId("map-wrapper");
    var listButton = byId("view-list");
    var mapButton = byId("view-map");

    if (!shell || !panel) {
      return;
    }

    /* ---- the sliding detail panel -------------------------------------- */

    // The URL the list was on, so closing the panel can restore it. htmx pushes the record's own
    // URL when the panel opens, which is what makes the link shareable.
    var listUrlBeforePanel = window.location.pathname + window.location.search;

    function openPanel() {
      shell.hidden = false;
      // Force a reflow so the CSS transition actually runs on first open.
      void shell.offsetWidth;
      shell.classList.add("is-open");
      document.body.classList.add("detail-panel-open");
    }

    function closePanel() {
      shell.classList.remove("is-open");
      document.body.classList.remove("detail-panel-open");
      shell.hidden = true;
      panel.innerHTML = "";
      if (window.location.pathname !== listUrlBeforePanel.split("?")[0]) {
        history.pushState({}, "", listUrlBeforePanel);
      }
    }

    document.body.addEventListener("htmx:beforeRequest", function (event) {
      if (event.target.closest && event.target.closest("." + prefix + "-open")) {
        listUrlBeforePanel = window.location.pathname + window.location.search;
      }
    });

    document.body.addEventListener("htmx:afterSwap", function (event) {
      if (event.target && event.target.id === prefix + "-panel") {
        openPanel();
      }
    });

    // Two spellings because the speaker panel shipped with a prefixed attribute of its own and
    // its markup is shared with that speaker's standalone page, which has no JavaScript on it at
    // all -- renaming the attribute there would be a change to a page that cannot be the one to
    // notice it broke. New markup uses the unprefixed one.
    document.addEventListener("click", function (event) {
      if (event.target.closest("[data-panel-close], [data-speaker-panel-close]")) {
        event.preventDefault();
        closePanel();
      }
    });

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !shell.hidden) {
        closePanel();
      }
    });

    // Back/forward: the pushed URL is a real page, so let the browser load it rather than trying
    // to reconstruct state.
    window.addEventListener("popstate", function () {
      if (!shell.hidden) {
        closePanel();
      }
    });

    /* ---- the radio filter menu ------------------------------------------ */

    // The menu itself remembers which radio is checked; this is so the closed button says so too.
    // Picking one only swaps the table, so the server never re-renders this label.
    if (config.menuLabelId) {
      var menuLabel = document.getElementById(config.menuLabelId);
      document.querySelectorAll("." + config.menuOptionClass).forEach(function (radio) {
        radio.addEventListener("change", function () {
          if (radio.checked && menuLabel) {
            menuLabel.textContent = radio.value
              ? radio.parentElement.textContent.trim()
              : config.menuFallbackLabel;
          }
        });
      });
    }

    /* ---- list / map toggle ---------------------------------------------- */

    var mapInitialized = false;
    var theMap = null;
    var markers = [];

    /* Selected is primary, unselected is secondary -- see style_reference.md. */
    function setViewButtons(selected, unselected) {
      selected.classList.remove("btn-secondary");
      selected.classList.add("btn-primary", "active");
      unselected.classList.remove("btn-primary", "active");
      unselected.classList.add("btn-secondary");
    }

    function setViewParam(value) {
      var url = new URL(window.location);
      if (value === "map") {
        url.searchParams.set("view", "map");
      } else {
        url.searchParams.delete("view");
      }
      history.replaceState({}, "", url);
    }

    function showList() {
      mapWrapper.classList.add("d-none");
      listWrapper.classList.remove("d-none");
      setViewButtons(listButton, mapButton);
      setViewParam("list");
    }

    function showMap() {
      listWrapper.classList.add("d-none");
      mapWrapper.classList.remove("d-none");
      setViewButtons(mapButton, listButton);
      setViewParam("map");
      initMap();
      if (theMap) {
        // The container was display:none while Google measured it, so it needs a nudge.
        window.google.maps.event.trigger(theMap, "resize");
        fitToMarkers();
      }
    }

    if (listButton && mapButton) {
      listButton.addEventListener("click", showList);
      mapButton.addEventListener("click", showMap);
    }

    /* ---- the map itself -------------------------------------------------- */

    function readMapData() {
      var node = byId("map-data");
      if (!node) {
        return [];
      }
      try {
        return JSON.parse(node.textContent) || [];
      } catch (error) {
        return [];
      }
    }

    function clearMarkers() {
      markers.forEach(function (marker) {
        marker.setMap(null);
      });
      markers = [];
    }

    function openPanelFor(slug) {
      listUrlBeforePanel = window.location.pathname + window.location.search;
      htmx
        .ajax("GET", config.panelUrl(slug), {
          target: "#" + prefix + "-panel",
          swap: "innerHTML",
        })
        .then(function () {
          history.pushState({}, "", config.pageUrl(slug));
        });
    }

    function renderMarkers(records) {
      if (!theMap) {
        return;
      }
      clearMarkers();
      records.forEach(function (record) {
        var marker = new google.maps.Marker({
          position: { lat: record.lat, lng: record.lng },
          map: theMap,
          title: record.name,
          icon: {
            path: google.maps.SymbolPath.CIRCLE,
            scale: 7,
            fillColor: MARKER_COLOR,
            fillOpacity: 1,
            strokeColor: "#fff",
            strokeWeight: 2,
          },
        });
        marker.addListener("click", function () {
          // The same panel the table rows open, so the two views never diverge.
          openPanelFor(record.slug);
        });
        markers.push(marker);
      });
      fitToMarkers();
    }

    function fitToMarkers() {
      if (!theMap || markers.length === 0) {
        return;
      }
      var bounds = new google.maps.LatLngBounds();
      markers.forEach(function (marker) {
        bounds.extend(marker.getPosition());
      });
      theMap.fitBounds(bounds);
      if (markers.length === 1) {
        theMap.setZoom(10);
      }
    }

    function initMap() {
      if (mapInitialized || !window.google || !window.google.maps) {
        return;
      }
      var origin = config.origin;
      theMap = new google.maps.Map(byId("map"), {
        zoom: 7,
        center: origin || FALLBACK_CENTER,
      });
      if (origin) {
        new google.maps.Marker({
          position: origin,
          map: theMap,
          title: config.originLabel || "",
          icon: {
            path: google.maps.SymbolPath.CIRCLE,
            scale: 9,
            fillColor: ORIGIN_COLOR,
            fillOpacity: 1,
            strokeColor: "#fff",
            strokeWeight: 2,
          },
        });
      }
      mapInitialized = true;
      renderMarkers(readMapData());
    }

    // The htmx table response replaces the map payload out of band; redraw from it so filtering
    // updates both views at once. htmx names the event differently depending on version, so listen
    // for both rather than depending on which one fires.
    function onPayloadSwap(event) {
      if (event.target && event.target.id === prefix + "-map-payload") {
        renderMarkers(readMapData());
      }
    }
    document.body.addEventListener("htmx:afterSwap", onPayloadSwap);
    document.body.addEventListener("htmx:oobAfterSwap", onPayloadSwap);

    // Google Maps calls this back by name once its script has loaded, which may be before or
    // after this runs.
    window[config.callbackName] = initMap;

    // Restore the view the URL asked for.
    if (config.defaultView === "map") {
      showMap();
    } else {
      showList();
    }
  };
})();
