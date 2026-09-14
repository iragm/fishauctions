/**
 * A filtered list and the same set drawn on a map, with the two kept in step.
 *
 * Two pages are built this way -- the speaker directory and the club finder -- and the part they
 * share is that filtering must move both halves at once. The htmx table response carries every
 * matching row's coordinates back out of band (see the *_table.html partials) and this redraws the
 * markers from that payload, so the map and the list can never show different sets.
 *
 * Where they differ is what a result opens, and that is what `panelUrl` selects:
 *
 *   * With `panelUrl` (the speaker directory) a row and its own pin both open a detail panel that
 *     slides in over the list, pushing a URL worth sharing. Speakers are a page you compare on --
 *     filter by topic, check who is nearest, look at three of them -- so keeping the list, the map
 *     and the filters underneath is worth a panel.
 *   * Without it (the club finder) a pin opens a small info window naming the club, whose name is
 *     a link to the club's own page; rows are already plain links to the same place. Finding a
 *     club is a find-one task, and a summary beside the list would be a second public surface with
 *     the same privacy rules to keep in step with the club page.
 *
 * The map follows Google's current Maps JavaScript API guidance:
 *
 *   * The page includes Google's dynamic library import bootstrap loader
 *     (partials/google_maps_loader.html) rather than a script tag, and this imports the `maps`,
 *     `marker` and `core` libraries through `google.maps.importLibrary` the first time the map is
 *     shown -- so a visitor who stays on the list never downloads the Maps API at all.
 *   * Pins are `AdvancedMarkerElement`s (`google.maps.Marker` is deprecated). Advanced markers
 *     require a Map ID -- "If the map ID is missing, advanced markers cannot load" -- which is the
 *     `mapId` below; see GOOGLE_MAPS_MAP_ID in .env.example.
 *   * Clicks follow the accessible-marker pattern: `gmpClickable: true`, a `title` screen readers
 *     announce, and `addEventListener('gmp-click')`, which Google only supports through
 *     addEventListener, never google.maps.event's addListener.
 *
 * Element ids stay per-page (`club-map`, `speaker-map`) so one page's markup can't reach into the
 * other's; everything that differs lives in the config object:
 *
 *   initMapListPanel({
 *     prefix: "club",                     // ids: <prefix>-map, <prefix>-view-map, <prefix>-panel…
 *     pageUrl: function (slug) {...},     // the real page a result opens
 *     panelUrl: function (slug) {...},    // optional: the htmx fragment the panel is filled from
 *     mapId: "DEMO_MAP_ID",               // the Google Map ID advanced markers need
 *     origin: {lat: 42, lng: -72},        // where distances are measured from, or null
 *     originLabel: "your location",
 *     defaultView: "map",                 // which half the URL asked for
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
  /* Google's Map ID for testing. Production sets a real one; see GOOGLE_MAPS_MAP_ID. */
  var FALLBACK_MAP_ID = "DEMO_MAP_ID";

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
    var hasPanel = !!(shell && panel && config.panelUrl);

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

    if (hasPanel) {
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
    }

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

    // Set once the libraries have been imported and the map exists. `mapReady` is the promise of
    // that, taken the first time the map is shown, so a second click can't build a second map.
    var mapReady = null;
    var theMap = null;
    var Marker = null;
    var LatLngBounds = null;
    var infoWindow = null;
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
      // The map notices its container being shown by itself; it only needs re-fitting, because
      // bounds fitted while the container was hidden were measured against a box with no size.
      var ready = initMap();
      if (ready) {
        ready.then(fitToMarkers);
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

    /**
     * A round pin: the marker's custom content, which Google takes as any DOM element.
     *
     * Where it sits is not styled here. An advanced marker hangs its content from the coordinate
     * by the bottom center -- right for a teardrop, wrong for a dot -- and the documented way to
     * move that is the marker's own anchorLeft/anchorTop options (DOT_ANCHOR), not a transform.
     */
    function dot(color, size) {
      var element = document.createElement("div");
      element.style.width = size + "px";
      element.style.height = size + "px";
      element.style.borderRadius = "50%";
      element.style.background = color;
      element.style.border = "2px solid #fff";
      element.style.boxShadow = "0 1px 3px rgba(0, 0, 0, 0.4)";
      element.style.boxSizing = "border-box";
      return element;
    }

    /* The coordinate at the center of a dot, rather than the default bottom middle
       (anchorLeft "-50%", anchorTop "-100%"). */
    var DOT_ANCHOR = { anchorLeft: "-50%", anchorTop: "-50%" };

    function clearMarkers() {
      markers.forEach(function (marker) {
        marker.map = null;
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

    /**
     * The info window a pin opens when there is no panel: the name, as a link to the page.
     *
     * Built as a DOM node rather than an HTML string because the name is somebody's typed-in club
     * name. Google's info window is white whatever the page's theme, so the link carries its own
     * color -- the site's primary blue, which is the one brand color that stays legible on it.
     */
    function nameLink(record) {
      var link = document.createElement("a");
      link.href = config.pageUrl(record.slug);
      link.textContent = record.name;
      link.style.color = ORIGIN_COLOR;
      link.style.fontWeight = "bold";
      return link;
    }

    function renderMarkers(records) {
      if (!theMap) {
        return;
      }
      clearMarkers();
      records.forEach(function (record) {
        var marker = new Marker({
          position: { lat: record.lat, lng: record.lng },
          map: theMap,
          title: record.name,
          content: dot(MARKER_COLOR, 14),
          gmpClickable: true,
          anchorLeft: DOT_ANCHOR.anchorLeft,
          anchorTop: DOT_ANCHOR.anchorTop,
        });
        marker.addEventListener("gmp-click", function () {
          if (hasPanel) {
            // The same panel the table rows open, so the two views never diverge.
            openPanelFor(record.slug);
            return;
          }
          // Name first, page second. A pin that navigated on the first tap would be a blind jump
          // on a touch screen, where there is no hover to tell you which one you are about to open.
          infoWindow.close();
          infoWindow.setContent(nameLink(record));
          infoWindow.open({ anchor: marker, map: theMap });
        });
        markers.push(marker);
      });
      fitToMarkers();
    }

    function fitToMarkers() {
      if (!theMap || markers.length === 0) {
        return;
      }
      var bounds = new LatLngBounds();
      markers.forEach(function (marker) {
        bounds.extend(marker.position);
      });
      theMap.fitBounds(bounds);
      if (markers.length === 1) {
        theMap.setZoom(10);
      }
    }

    function initMap() {
      if (mapReady) {
        return mapReady;
      }
      var mapElement = byId("map");
      if (!mapElement || !window.google || !window.google.maps || !window.google.maps.importLibrary) {
        // No map on this page: the loader is only included when a Maps API key is configured.
        return null;
      }
      mapReady = Promise.all([
        google.maps.importLibrary("maps"),
        google.maps.importLibrary("marker"),
        google.maps.importLibrary("core"),
      ]).then(function (libraries) {
        var maps = libraries[0];
        Marker = libraries[1].AdvancedMarkerElement;
        LatLngBounds = libraries[2].LatLngBounds;
        theMap = new maps.Map(mapElement, {
          zoom: 7,
          center: config.origin || FALLBACK_CENTER,
          mapId: config.mapId || FALLBACK_MAP_ID,
        });
        infoWindow = new maps.InfoWindow();
        if (config.origin) {
          new Marker({
            position: config.origin,
            map: theMap,
            title: config.originLabel || "",
            content: dot(ORIGIN_COLOR, 18),
            anchorLeft: DOT_ANCHOR.anchorLeft,
            anchorTop: DOT_ANCHOR.anchorTop,
          });
        }
        renderMarkers(readMapData());
        return theMap;
      });
      return mapReady;
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

    // Restore the view the URL asked for.
    if (config.defaultView === "map") {
      showMap();
    } else {
      showList();
    }
  };
})();
