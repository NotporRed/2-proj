(function () {
  var lineInput = document.getElementById("lineInput");
  var btnPreview = document.getElementById("btnPreview");
  var btnFlip = document.getElementById("btnFlip");
  var statusEl = document.getElementById("status");
  var stopsPanel = document.getElementById("stopsPanel");
  var timetableModal = document.getElementById("timetableModal");
  var btnCloseModal = document.getElementById("btnCloseModal");
  var modalTitle = document.getElementById("modalTitle");
  var modalBody = document.getElementById("modalBody");

  var map = L.map("map").setView([55.17, 23.93], 8);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap contributors",
  }).addTo(map);

  var routeLayer = null;
  var markersLayer = null;

  var FETCH_SHORT = 120000;
  var FETCH_LONG = 720000;

  /** @type {string|null} */
  var previewKey = null;
  /** @type {object|null} */
  var lastPreview = null;
  /** @type {string|null} */
  var currentDirection = null;
  /** @type {string[]} */
  var availableDirections = [];

  function setStatus(msg, kind) {
    statusEl.textContent = msg;
    statusEl.className = "status" + (kind ? " " + kind : "");
  }

  function fetchWithTimeout(url, ms) {
    var ctrl = new AbortController();
    var t = setTimeout(function () {
      ctrl.abort();
    }, ms);
    return fetch(url, { signal: ctrl.signal }).finally(function () {
      clearTimeout(t);
    });
  }

  function parseJsonSafe(r) {
    return r.text().then(function (raw) {
      if (!raw.trim()) return {};
      try {
        return JSON.parse(raw);
      } catch (e) {
        return { parse_error: true, snippet: raw.slice(0, 200) };
      }
    });
  }

  function setBusy(disabled) {
    btnPreview.disabled = disabled;
    btnFlip.disabled = disabled || availableDirections.length < 2;
  }

  function clearMapExtras() {
    if (routeLayer) {
      map.removeLayer(routeLayer);
      routeLayer = null;
    }
    if (markersLayer) {
      map.removeLayer(markersLayer);
      markersLayer = null;
    }
    stopsPanel.hidden = true;
    stopsPanel.innerHTML = "";
  }

  function invalidatePreview() {
    previewKey = null;
    lastPreview = null;
    currentDirection = null;
    availableDirections = [];
    btnFlip.disabled = true;
  }

  lineInput.addEventListener("input", function () {
    invalidatePreview();
  });

  function pathFromPreview(p) {
    if (p.shape_path && p.shape_path.length >= 2) {
      var poly = [];
      for (var i = 0; i < p.shape_path.length; i++) {
        var pt = p.shape_path[i];
        if (pt && pt.length >= 2) poly.push([+pt[0], +pt[1]]);
      }
      if (poly.length >= 2) return poly;
    }
    var out = [];
    (p.stops || []).forEach(function (s) {
      if (s.lat != null && s.lon != null) out.push([+s.lat, +s.lon]);
    });
    return out.length >= 2 ? out : null;
  }

  function stopsMergedFromPreview(p) {
    return (p.stops || []).map(function (s) {
      var c =
        s.lat != null && s.lon != null
          ? { lat: +s.lat, lon: +s.lon, display_name: s.name }
          : null;
      return { order: s.order, name: s.name, coord: c, stop_id: s.stop_id };
    });
  }

  function canTryClientMap(p) {
    if (!p || !p.stops || !p.stops.length) return false;
    return p.stops.every(function (s) {
      return s.lat != null && s.lon != null;
    });
  }

  function renderStopsList(stops) {
    var h = document.createElement("h2");
    h.textContent = "Stotelės";
    var ol = document.createElement("ol");
    ol.className = "stops-list";
    stops.forEach(function (s) {
      var li = document.createElement("li");
      var label = document.createElement("span");
      label.className = "stop-name";
      label.textContent = (s.name && String(s.name).trim()) || "#" + s.order;
      label.tabIndex = s.stop_id ? 0 : -1;
      li.appendChild(label);

      if (s.stop_id) {
        label.addEventListener("click", function () {
          loadStopTimetable(s.stop_id, label.textContent);
        });
        label.addEventListener("keydown", function (ev) {
          if (ev.key === "Enter" || ev.key === " ") {
            ev.preventDefault();
            loadStopTimetable(s.stop_id, label.textContent);
          }
        });
        li.addEventListener("click", function (ev) {
          if (ev.target === label) return;
          loadStopTimetable(s.stop_id, label.textContent);
        });
      }

      if (!s.coord && (s.lat == null || s.lon == null)) {
        li.classList.add("miss");
      }
      ol.appendChild(li);
    });
    stopsPanel.appendChild(h);
    stopsPanel.appendChild(ol);
    stopsPanel.hidden = false;
  }

  function hhmm(raw) {
    var t = (raw || "").trim();
    var parts = t.split(":");
    if (parts.length < 2) return t || "--:--";
    return parts[0] + ":" + parts[1];
  }

  function hourKey(raw) {
    var t = hhmm(raw);
    return t.length >= 2 ? t.slice(0, 2) : "--";
  }

  function directionLabel(dir) {
    if (dir == null || dir === "") return "nežinoma";
    return String(dir);
  }

  function openModal(title) {
    modalTitle.textContent = title || "Tvarkaraštis";
    modalBody.innerHTML = "<p>Kraunama…</p>";
    timetableModal.hidden = false;
  }

  function closeModal() {
    timetableModal.hidden = true;
  }

  function renderTimetable(stopName, rows, dir) {
    modalTitle.textContent =
      "Tvarkaraštis: " + stopName + " (kryptis " + directionLabel(dir) + ")";
    modalBody.innerHTML = "";
    if (!rows || !rows.length) {
      var p = document.createElement("p");
      p.textContent = "Šiai stotelei išvykimų nerasta.";
      modalBody.appendChild(p);
      return;
    }
    var groups = {};
    rows.forEach(function (r) {
      var key = hourKey(r.departure_time || r.arrival_time || "");
      if (!groups[key]) groups[key] = [];
      groups[key].push(r);
    });

    Object.keys(groups)
      .sort()
      .forEach(function (hr) {
        var subTitle = document.createElement("h4");
        subTitle.textContent = hr + ":00";
        modalBody.appendChild(subTitle);
        var list = document.createElement("ul");
        groups[hr].forEach(function (r) {
          var li = document.createElement("li");
          var txt = hhmm(r.departure_time || r.arrival_time || "--:--");
          if (r.headsign) txt += "  →  " + r.headsign;
          li.textContent = txt;
          list.appendChild(li);
        });
        modalBody.appendChild(list);
      });
  }

  async function loadStopTimetable(stopId, stopName) {
    var line = lineInput.value.trim();
    if (!line) {
      setStatus("Pirma įveskite maršruto numerį.", "error");
      return;
    }
    openModal(
      "Tvarkaraštis: " +
        stopName +
        " (kryptis " +
        directionLabel(currentDirection) +
        ")"
    );
    try {
      var r = await fetchWithTimeout(
        "/api/bus/" +
          encodeURIComponent(line) +
          "/stops/" +
          encodeURIComponent(stopId) +
          "/timetable" +
          (currentDirection != null ? "?direction=" + encodeURIComponent(currentDirection) : ""),
        FETCH_SHORT
      );
      var data = await parseJsonSafe(r);
      if (data.parse_error) {
        modalBody.innerHTML = "<p>Nepavyko nuskaityti JSON.</p>";
        return;
      }
      if (!r.ok) {
        modalBody.innerHTML = "<p>" + (data.error || "Klaida") + "</p>";
        return;
      }
      renderTimetable(
        data.stop_name || stopName,
        data.departures || [],
        data.direction_id != null ? data.direction_id : currentDirection
      );
    } catch (e) {
      modalBody.innerHTML = "<p>Nepavyko užkrauti.</p>";
    }
  }

  async function loadPreview() {
    var id = lineInput.value.trim();
    if (!id) {
      setStatus("Įveskite maršruto numerį.", "error");
      return;
    }
    btnPreview.disabled = true;
    setStatus("Tikrinamas maršrutas ir gaunamas stotelių sąrašas…");
    clearMapExtras();
    try {
      var q = currentDirection != null ? "?direction=" + encodeURIComponent(currentDirection) : "";
      var r = await fetchWithTimeout("/api/bus/" + encodeURIComponent(id) + "/preview" + q, FETCH_LONG);
      var data = await parseJsonSafe(r);
      if (data.parse_error) {
        setStatus("Nepavyko nuskaityti atsakymo JSON.", "error");
        return;
      }
      if (!r.ok) {
        var detail = data && data.detail ? " (" + data.detail + ")" : "";
        setStatus((data && data.error) || ("Maršrutas nerastas." + detail), "error");
        return;
      }
      previewKey = id;
      lastPreview = data;
      currentDirection = data.direction_id != null ? String(data.direction_id) : null;
      availableDirections = Array.isArray(data.available_directions)
        ? data.available_directions.map(function (x) {
            return String(x);
          })
        : [];
      btnFlip.disabled = availableDirections.length < 2;
      var src = data.data_source === "gtfs" ? "GTFS" : "NeTEx";
      var dirMsg = currentDirection != null ? ", kryptis " + currentDirection : "";
      setStatus(
        "Rasta „" +
          data.line +
          "“ (" +
          src +
          "), " +
          (data.stops || []).length +
          " stotelės(-ių)" +
          dirMsg +
          ".",
        "ok"
      );
      var forList = (data.stops || []).map(function (s) {
        return {
          order: s.order,
          name: s.name,
          stop_id: s.stop_id,
          lat: s.lat,
          lon: s.lon,
          coord: s.lat != null && s.lon != null ? { lat: s.lat, lon: s.lon } : null,
        };
      });
      renderStopsList(forList);
      await showOnMap(true);
    } catch (e) {
      if (e.name === "AbortError") {
        setStatus("Užklausa per ilga (timeout).", "error");
      } else {
        setStatus("Klaida tinklo ar serverio lygmenyje.", "error");
      }
    } finally {
      btnPreview.disabled = false;
    }
  }

  function drawRoute(path, mergedStops) {
    var lineCode = "";
    if (lastPreview && lastPreview.line) {
      lineCode = String(lastPreview.line).toUpperCase();
    } else {
      lineCode = String(lineInput.value || "").toUpperCase();
    }
    var routeId = lastPreview && lastPreview.route_id ? String(lastPreview.route_id).toLowerCase() : "";
    var isTrolley =
      routeId.indexOf("_trol_") !== -1 ||
      routeId.indexOf("trolley") !== -1 ||
      String(lineInput.value || "").trim().toUpperCase().indexOf("T") === 0;
    var routeColor = "#2563eb";
    if (isTrolley) {
      routeColor = "#dc2626";
    } else if (lineCode.indexOf("N") !== -1) {
      routeColor = "#111111";
    } else if (lineCode.indexOf("G") !== -1) {
      routeColor = "#15803d";
    }

    routeLayer = L.polyline(path, { color: routeColor, weight: 5, opacity: 0.85 }).addTo(map);
    markersLayer = L.layerGroup();
    mergedStops.forEach(function (s) {
      if (s.coord) {
        L.circleMarker([s.coord.lat, s.coord.lon], {
          radius: 6,
          color: routeColor,
          fillColor: routeColor,
          fillOpacity: 0.9,
          weight: 2,
        })
          .bindPopup("<strong>#" + s.order + "</strong><br/>" + (s.name || ""))
          .addTo(markersLayer);
      }
    });
    markersLayer.addTo(map);
    map.fitBounds(routeLayer.getBounds(), { padding: [40, 40] });
  }

  async function showOnMap(fromPreview) {
    var id = lineInput.value.trim();
    if (!id) {
      setStatus("Įveskite maršruto numerį.", "error");
      return;
    }
    if (!lastPreview || previewKey !== id) {
      setStatus('Pirmiausia spauskite „Patikrinti, rodyti stoteles ir žemėlapį“.', "error");
      return;
    }
    setStatus(fromPreview ? "Piešiamas žemėlapis…" : "Atnaujinamas žemėlapis…");

    try {
      if (canTryClientMap(lastPreview)) {
        var path = pathFromPreview(lastPreview);
        var merged = stopsMergedFromPreview(lastPreview);
        if (!path || path.length < 2) {
          setStatus("Nepakanka koordinačių linijai (shapes/stops).", "error");
          return;
        }
        clearMapExtras();
        drawRoute(path, merged);
        var label = lastPreview.line_name
          ? "„" + lastPreview.line + "“ (" + lastPreview.line_name + ")"
          : "„" + lastPreview.line + "“";
        setStatus("Maršrutas " + label + " parodytas žemėlapyje (GTFS stops/shapes).", "ok");
        renderStopsList(merged);
        return;
      }

      clearMapExtras();
      var r = await fetchWithTimeout("/api/bus/" + encodeURIComponent(id) + "/route", FETCH_LONG);
      if (currentDirection != null) {
        r = await fetchWithTimeout(
          "/api/bus/" + encodeURIComponent(id) + "/route?direction=" + encodeURIComponent(currentDirection),
          FETCH_LONG
        );
      }
      var data = await parseJsonSafe(r);
      if (data.parse_error || !r.ok) {
        setStatus((data && data.error) || "Nepavyko gauti maršruto geokodavimui.", "error");
        return;
      }
      if (data.geo_error) {
        setStatus("Geokodavimo klaida.", "error");
        return;
      }
      var rpath = data.path || [];
      if (rpath.length < 2) {
        setStatus("Per mažai taškų linijai po geokodavimo.", "error");
        renderStopsList(data.stops || []);
        return;
      }
      drawRoute(rpath, data.stops || []);
      var lbl = data.line_name
        ? "„" + data.line + "“ (" + data.line_name + ")"
        : "„" + data.line + "“";
      setStatus("Maršrutas " + lbl + " parodytas (su geokodavimu kur trūko koord.).", "ok");
      renderStopsList(data.stops || []);
    } catch (e) {
      setStatus(e.name === "AbortError" ? "Timeout žemėlapiui." : "Klaida piešiant žemėlapį.", "error");
    }
  }

  btnPreview.addEventListener("click", loadPreview);
  btnFlip.addEventListener("click", function () {
    if (!availableDirections || availableDirections.length < 2) {
      setStatus("Šiam maršrutui nėra alternatyvios krypties.", "error");
      return;
    }
    var idx = availableDirections.indexOf(String(currentDirection));
    var next = availableDirections[(idx + 1) % availableDirections.length];
    currentDirection = next;
    setStatus("Perjungiama į kryptį " + next + "…");
    loadPreview();
  });
  btnCloseModal.addEventListener("click", closeModal);
  timetableModal.addEventListener("click", function (e) {
    if (e.target && e.target.getAttribute("data-close-modal") === "true") {
      closeModal();
    }
  });
  lineInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter") {
      e.preventDefault();
      loadPreview();
    }
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && !timetableModal.hidden) {
      closeModal();
    }
  });
})();
