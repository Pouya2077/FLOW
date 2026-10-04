// Flood map: draws the pipeline's road segments over the OpenFreeMap basemap with MapLibre.
// The map opens fitted to an observation window, the area with flood data, then zooms and pans
// freely like Google Maps. The search box moves it to any place; "Recent" searches jump
// back to an observation window.
import * as maplibregl from "https://cdn.jsdelivr.net/npm/maplibre-gl@6.12.0/dist/maplibre-gl.mjs";

const mapEl = document.getElementById("map");
const form = document.querySelector(".search");
const input = document.getElementById("q");
const recentPanel = document.getElementById("recent");
const recentOptions = [...(recentPanel?.querySelectorAll('[role="option"]') ?? [])];
const themeLinks = [...document.querySelectorAll(".theme-option")];
const settingsToggle = document.querySelector(".settings-toggle");
const settingsMenu = document.getElementById("settings-menu");
const facilitiesToggle = document.querySelector(".facilities-toggle");
const pathToggle = document.querySelector(".path-toggle");
const pathCard = document.getElementById("path-card");
const pathBody = pathCard?.querySelector(".path-body");
// One lucide glyph per kind of critical building, rendered by the template.
const facilityKinds = Object.fromEntries(
  [...(document.getElementById("facility-icons")?.content.children ?? [])].map((el) => [
    el.dataset.kind,
    { label: el.dataset.label, line: "line" in el.dataset, svg: el.querySelector("svg") },
  ]),
);

const css = getComputedStyle(document.documentElement);
const token = (name) => css.getPropertyValue(name).trim();
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

const PADDING = { top: 80, right: 24, bottom: 24, left: 24 }; // clear of the search box
const WORLD = [[-180, -85], [180, -85], [180, 85], [-180, 85], [-180, -85]];

// Roads leading into the window get a short dotted stub outside it that fades out. Its length is
// set on screen, so it looks the same at every zoom.
const APPROACH_PX = 70;
const APPROACH_STEPS = 5;
const ROAD_CLASSES = ["motorway", "trunk", "primary", "secondary", "tertiary", "minor", "service"];

// Road names appear by importance as you zoom in, like Google Maps. Most important last so it wins
// label collisions.
const LABEL_TIERS = [
  { classes: ["minor", "service"], minzoom: 14.5, color: "--text-muted" },
  { classes: ["secondary", "tertiary"], minzoom: 12.5, color: "--text-muted" },
  { classes: ["motorway", "trunk", "primary"], minzoom: 0, color: "--text" },
];

// Critical building icons sit on a badge whose ring shows the site's flood status, with the same
// three states as roads: flood-blue ring = water at the site, plain = observed clear, dashed and
// faded = not observed.
const BADGE_PX = 30;
const BADGE_RINGS = {
  flooded: { color: "--flood", width: 3 },
  clear: { color: "--road-clear", width: 1.5 },
  no_data: { color: "--road-nodata", width: 1.5, dash: [3, 2.5], alpha: 0.7 },
};

// Line widths grow with zoom so roads stay visible over the basemap's own roads at any scale.
const width = (base) => ["interpolate", ["linear"], ["zoom"], 10, base, 14, base * 2.5, 17, base * 6];

async function getJSON(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) throw new Error(`${url} returned ${response.status}`);
  return response.json();
}

const locations = await getJSON("/api/locations");
const metas = Object.fromEntries(
  await Promise.all(
    locations.map(async (l) => [l.slug, await getJSON(`/api/meta?location=${encodeURIComponent(l.slug)}`)]),
  ),
);
let current = mapEl.dataset.location; // the observation window the map opened on
const facilityImages = await makeFacilityImages();
const cautionImage = await makeCautionImage();

// Pathfinding mode (see "Pathfinding" below): two picks, then the route between them.
let pathMode = false;
let picks = []; // [{ point: [lon, lat], label, marker }]
let routePopup;
let pathRequest = 0;

const map = new maplibregl.Map({
  container: mapEl,
  style: mapEl.dataset.basemapStyle,
  bounds: metas[current]?.bbox ?? [-123.3, 48.9, -122.0, 49.4], // fall back to the Lower Mainland
  fitBoundsOptions: { padding: PADDING },
  // Zoom and pan freely; no rotating or tilting.
  dragRotate: false,
  boxZoom: false,
  touchPitch: false,
  pitchWithRotate: false,
  attributionControl: { compact: true },
});
map.touchZoomRotate.disableRotation();
map.keyboard.disableRotation();
map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "bottom-right");

let basemapSource;

map.on("load", () => {
  const style = map.getStyle();
  basemapSource = Object.entries(style.sources).find(([, s]) => s.type === "vector")?.[0];
  map.addImage("hatch", hatchPattern(token("--unobserved")), { pixelRatio: 2 });

  // Draw above the basemap's roads but under its labels, so place names stay readable.
  // Some styles (Dark) have a label layer below their roads, so start after the last non-label layer.
  const lastShape = style.layers.findLastIndex((layer) => layer.type !== "symbol");
  const beforeLabels = style.layers.slice(lastShape + 1).find((layer) => layer.type === "symbol")?.id;
  const add = (layer) => map.addLayer(layer, beforeLabels);

  map.addSource("unobserved", { type: "geojson", data: unobservedArea(shownMetas()) });
  add({ id: "unobserved", type: "fill", source: "unobserved", paint: { "fill-pattern": "hatch" } });

  map.addSource("approaches", { type: "geojson", data: emptyCollection() });
  add({
    id: "approaches",
    type: "line",
    source: "approaches",
    layout: { "line-cap": "round", "line-join": "round" },
    paint: {
      "line-color": token("--road-nodata"),
      "line-width": width(2),
      "line-dasharray": [0, 2], // zero-length dashes with round caps draw dots
      "line-opacity": ["get", "opacity"],
    },
  });

  map.addSource("segments", { type: "geojson", data: emptyCollection(), generateId: true });
  const roads = [
    // Flooded is added last so it draws on top where segments meet.
    { status: "clear", color: token("--road-clear"), width: 1.5 },
    { status: "no_data", color: token("--road-nodata"), width: 1.5, dash: [2, 1.5] },
    { status: "flooded", color: token("--flood"), width: 2.5 },
  ];
  for (const road of roads) {
    add({
      id: `road-${road.status}`,
      type: "line",
      source: "segments",
      filter: ["==", ["get", "status"], road.status],
      layout: { "line-cap": road.dash ? "butt" : "round", "line-join": "round" },
      paint: {
        "line-color": road.color,
        "line-width": width(road.width),
        ...(road.dash && { "line-dasharray": road.dash }),
      },
    });
  }

  map.addSource("windows", { type: "geojson", data: footprints(shownMetas()) });
  add({
    id: "window-edge",
    type: "line",
    source: "windows",
    paint: { "line-color": token("--accent"), "line-width": 3 },
  });

  // The area a search is analysing, dashed until its result arrives.
  map.addSource("pending", { type: "geojson", data: emptyCollection() });
  add({
    id: "window-pending",
    type: "line",
    source: "pending",
    paint: { "line-color": token("--accent"), "line-width": 3, "line-dasharray": [2, 2] },
  });

  // Invisible, wider copies of both outlines, so a click near the 3 px line shows the window's size.
  for (const [id, source] of [["window-hit", "windows"], ["window-pending-hit", "pending"]]) {
    add({ id, type: "line", source, paint: { "line-width": 16, "line-opacity": 0 } });
  }
  map.on("click", ["window-hit", "window-pending-hit"], showWindowSize);
  map.on("mouseenter", ["window-hit", "window-pending-hit"], () => (map.getCanvas().style.cursor = "pointer"));
  map.on("mouseleave", ["window-hit", "window-pending-hit"], () => (map.getCanvas().style.cursor = ""));

  addFacilityLayers();
  addPathLayers();
  addRoadClicks();
  addRoadLabels(style);
  // Listen before framing: an unanimated fit fires "moveend" immediately.
  map.on("moveend", loadView);
  map.on("idle", updateApproaches);
  frame(current, false);
  restoreRoute();
  if (input.value.trim()) search(input.value); // a reload with ?q= repeats the search
});

// Fit the map to a location's window.
function frame(slug, animate) {
  const bbox = metas[slug]?.bbox;
  if (!bbox) return;
  map.fitBounds(bbox, { padding: PADDING, duration: animate && !reduceMotion ? 600 : 0 });
}

// One window at a time: only the current location gets an outline, a hole in the hatch and roads.
function shownMetas() {
  return metas[current] ? [metas[current]] : [];
}

// Move the window to another location; the previous one stops being drawn.
function showWindow(slug) {
  current = slug;
  clearPath({ forget: false }); // a route belongs to one window; it comes back with its window
  restoreRoute();
  map.getSource("windows").setData(footprints(shownMetas()));
  map.getSource("unobserved").setData(unobservedArea(shownMetas()));
  map.getSource("approaches").setData(emptyCollection());
  approachKey = "";
  loadView();
}

// Fetch the current location's segments for what's on screen.
let latestRequest = 0;
async function loadView() {
  const request = ++latestRequest;
  if (!metas[current]) {
    map.getSource("segments").setData(emptyCollection());
    map.getSource("facilities").setData(emptyCollection());
    return;
  }
  const view = map.getBounds().toArray().flat(); // [west, south, east, north]
  const params = new URLSearchParams({ bbox: view.join(","), location: current });
  let segments, facilities;
  try {
    [segments, facilities] = await Promise.all([
      getJSON(`/api/flood?${params}`),
      getJSON(`/api/facilities?${params}`),
    ]);
  } catch {
    // Keep what's drawn, but say so: missing roads must never read as "no flooding".
    if (request === latestRequest) {
      showNotice("Couldn't load the roads here, so flooding may be missing from the map.", loadView, "view");
    }
    return;
  }
  if (request !== latestRequest) return; // a newer move or window change already started
  hideNotice("view");
  map.getSource("segments").setData(segments);
  map.getSource("facilities").setData(facilities);
}

// --- Critical buildings ---

async function makeFacilityImages() {
  const images = {};
  for (const [kind, { svg }] of Object.entries(facilityKinds)) {
    const glyph = await svgImage(svg, token("--text"));
    for (const [status, ring] of Object.entries(BADGE_RINGS)) {
      images[`facility-${kind}-${status}`] = badge(glyph, ring);
    }
  }
  return images;
}

async function svgImage(svg, color, size = 36) {
  const copy = svg.cloneNode(true);
  copy.setAttribute("xmlns", "http://www.w3.org/2000/svg");
  copy.setAttribute("width", size);
  copy.setAttribute("height", size);
  copy.setAttribute("color", color); // lucide strokes with currentColor
  const image = new Image();
  image.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(copy.outerHTML)}`;
  await image.decode();
  return image;
}

// Drawn at 2× for sharp icons on high-density screens.
function badge(glyph, ring) {
  const size = BADGE_PX * 2;
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext("2d");
  ctx.globalAlpha = ring.alpha ?? 1;
  const r = size / 2 - ring.width * 2;
  ctx.beginPath();
  ctx.arc(size / 2, size / 2, r, 0, 2 * Math.PI);
  ctx.fillStyle = `rgb(${token("--surface")})`;
  ctx.fill();
  ctx.lineWidth = ring.width * 2;
  ctx.strokeStyle = token(ring.color);
  ctx.setLineDash((ring.dash ?? []).map((d) => d * 2));
  ctx.stroke();
  ctx.drawImage(glyph, (size - glyph.width) / 2, (size - glyph.height) / 2);
  return ctx.getImageData(0, 0, size, size);
}

let facilityPopup;
function addFacilityLayers() {
  for (const [name, image] of Object.entries(facilityImages)) map.addImage(name, image, { pixelRatio: 2 });
  map.addSource("facilities", { type: "geojson", data: emptyCollection() });
  // A dyke's line is drawn only while its popup is open.
  map.addLayer({
    id: "facility-line",
    type: "line",
    source: "facilities",
    filter: ["==", ["get", "osm_id"], ""],
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": token("--accent"), "line-width": width(2) },
  });
  // On top of everything, so roads and labels never hide a hospital.
  map.addLayer({
    id: "facilities",
    type: "symbol",
    source: "facilities",
    filter: ["==", ["get", "shape"], "point"],
    layout: {
      "icon-image": ["concat", "facility-", ["get", "kind"], "-", ["get", "flood_status"]],
      "icon-size": ["interpolate", ["linear"], ["zoom"], 10, 0.7, 14, 1],
      "icon-allow-overlap": true, // a facility is never hidden at low zoom
      "symbol-sort-key": ["match", ["get", "flood_status"], "flooded", 2, "clear", 1, 0], // flooded on top
    },
  });

  map.on("click", "facilities", (event) => pathMode || openFacility(event.features[0])); // picks in path mode
  map.on("mouseenter", "facilities", () => (map.getCanvas().style.cursor = "pointer"));
  map.on("mouseleave", "facilities", () => (map.getCanvas().style.cursor = ""));
}

function openFacility(feature) {
  facilityPopup?.remove();
  const p = feature.properties;
  map.setFilter("facility-line", ["all", ["==", ["get", "shape"], "line"], ["==", ["get", "osm_id"], p.osm_id]]);
  facilityPopup = new maplibregl.Popup({ className: "facility-popup", maxWidth: "320px", offset: BADGE_PX / 2 })
    .setLngLat(feature.geometry.coordinates)
    .setDOMContent(facilityCard(p))
    .addTo(map);
  facilityPopup.on("close", () => map.setFilter("facility-line", ["==", ["get", "osm_id"], ""]));
}

// The popup's content. OSM text goes in with textContent, never as HTML.
function facilityCard(p) {
  const kind = facilityKinds[p.kind] ?? { label: p.kind };
  const card = el("div", "facility");
  const head = card.appendChild(el("p", "facility-kind"));
  if (kind.svg) head.appendChild(kind.svg.cloneNode(true));
  head.append(kind.label);
  card.appendChild(el("h2", "facility-name", p.name ?? `Unnamed ${kind.label.toLowerCase()}`));

  card.appendChild(el("p", `facility-flood ${p.flood_status}`, floodText(p, kind)));
  if (p.length_m) card.appendChild(el("p", "", `${(p.length_m / 1000).toFixed(1)} km in the observed area`));
  if (p.emergency !== null && p.emergency !== undefined) {
    card.appendChild(el("p", "", p.emergency ? "Emergency department" : "No emergency department"));
  }
  card.appendChild(el("p", p.address ? "" : "muted", p.address ?? "Address not in OpenStreetMap"));

  const contacts = el("ul", "facility-contact");
  for (const number of splitValues(p.phone)) contacts.appendChild(item(link(`tel:${number.replace(/[^+\d]/g, "")}`, number)));
  for (const number of splitValues(p.emergency_phone)) {
    contacts.appendChild(item(link(`tel:${number.replace(/[^+\d]/g, "")}`, `${number} (emergency)`)));
  }
  for (const email of splitValues(p.email)) contacts.appendChild(item(link(`mailto:${email}`, email)));
  const site = webAddress(p.website);
  if (site) contacts.appendChild(item(link(site.href, site.hostname.replace(/^www\./, ""))));
  if (contacts.children.length) card.appendChild(contacts);

  const source = card.appendChild(el("p", "facility-source"));
  source.append(`Satellite: ${localTime(p.observed_utc)} · Map: `);
  const osm = source.appendChild(link(`https://www.openstreetmap.org/${p.osm_id}`, `OpenStreetMap, ${p.osm_date}`));
  osm.target = "_blank";
  osm.rel = "noopener";
  return card;
}

// --- Road popup: what the satellite saw on a stretch, and the street's totals ---

const ROAD_LAYERS = ["road-flooded", "road-no_data", "road-clear"]; // preferred in this order
const CLICK_PX = 6; // roads are thin; accept clicks this close
let roadPopup;

function addRoadClicks() {
  map.on("click", (event) => {
    const { x, y } = event.point;
    const box = [[x - CLICK_PX, y - CLICK_PX], [x + CLICK_PX, y + CLICK_PX]];
    if (pathMode) return; // streets are picked instead
    if (map.queryRenderedFeatures(box, { layers: ["facilities", "route-line"] }).length) return; // own popups
    const hits = map.queryRenderedFeatures(box, { layers: ROAD_LAYERS });
    if (!hits.length) return;
    hits.sort((a, b) => ROAD_LAYERS.indexOf(a.layer.id) - ROAD_LAYERS.indexOf(b.layer.id));
    openRoad(hits[0], event.lngLat);
  });
  map.on("mouseenter", ROAD_LAYERS, () => (map.getCanvas().style.cursor = "pointer"));
  map.on("mouseleave", ROAD_LAYERS, () => (map.getCanvas().style.cursor = ""));
}

async function openRoad(feature, lngLat) {
  roadPopup?.remove();
  const p = feature.properties;
  const popup = new maplibregl.Popup({ className: "facility-popup", maxWidth: "320px" })
    .setLngLat(lngLat)
    .setDOMContent(roadCard(p, null))
    .addTo(map);
  roadPopup = popup;
  const street = await streetSummary(p.name);
  if (street && roadPopup === popup) popup.setDOMContent(roadCard(p, street)); // add the totals
}

// Per-street totals of the current window, fetched once per window
const streetTotals = {};
async function streetSummary(name) {
  const slug = current;
  if (!name || !metas[slug]) return null;
  if (!streetTotals[slug]) {
    const params = new URLSearchParams({ bbox: metas[slug].bbox.join(","), location: slug });
    try {
      const rows = await getJSON(`/api/streets?${params}`);
      streetTotals[slug] = new Map(rows.map((s) => [s.name, s]));
    } catch {
      return null; // the popup still shows the stretch
    }
  }
  return streetTotals[slug].get(name) ?? null;
}

// Same card style as a critical building. Street names are OSM text: textContent only.
function roadCard(p, street) {
  const card = el("div", "facility");
  card.appendChild(el("p", "facility-kind", roadKind(p.highway)));
  card.appendChild(el("h2", "facility-name", p.name ?? "Unnamed road"));
  card.appendChild(el("p", `facility-flood ${p.status}`, stretchText(p)));
  if (street) card.appendChild(el("p", "", streetText(street)));
  card.appendChild(el("p", "facility-source", `Satellite: ${localTime(p.observed_utc)}`));
  return card;
}

function roadKind(highway) {
  const kinds = { motorway: "Highway", trunk: "Highway", primary: "Major road", residential: "Residential street", service: "Service road" };
  return kinds[String(highway).replace("_link", "")] ?? "Road";
}

// Radar sees water extent, not depth: "water detected", never "impassable".
function stretchText(p) {
  const pct = (v) => `${Math.round((v ?? 0) * 100)}%`;
  if (p.status === "flooded") {
    return `Water detected on this stretch (${pct(p.flooded_fraction)} of the road corridor, confidence ${pct(p.confidence)})`;
  }
  if (p.status === "clear") return `No water detected on this stretch (confidence ${pct(p.confidence)})`;
  return "This stretch was not observed by the satellite";
}

function streetText(s) {
  const km = (m) => `${(m / 1000).toFixed(1)} km`;
  const unseen = s.no_data_m ? `, ${km(s.no_data_m)} not observed` : "";
  return `Whole street: water on ${km(s.flooded_m)} of ${km(s.total_m)} (${s.pct}%)${unseen}`;
}

// Radar sees water extent, not depth: say how much of the site is wet, never "inaccessible".
// Confidence isn't shown: for a site it's the same number as the water share (or 100% minus it).
function floodText(p, kind) {
  const where = kind.line ? "the area along it" : "the site";
  const pct = Math.round((p.flooded_fraction ?? 0) * 100);
  if (p.flood_status === "flooded") return `Water detected on ${pct}% of ${where}`;
  if (p.flood_status === "clear") return pct ? `No flooding detected (water on ${pct}% of ${where})` : "No water detected";
  return "Not observed by the satellite";
}

function localTime(utc) {
  if (!utc) return "unknown";
  return new Intl.DateTimeFormat(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZone: metas[current]?.timezone,
    timeZoneName: "short",
  }).format(new Date(utc));
}

// OSM separates several values with ";".
function splitValues(value) {
  return value ? value.split(";").map((v) => v.trim()).filter(Boolean) : [];
}

// Only http(s) links; OSM often leaves the scheme off.
function webAddress(value) {
  if (!value) return null;
  try {
    const url = new URL(/^https?:\/\//i.test(value) ? value : `https://${value}`);
    return url.protocol === "https:" || url.protocol === "http:" ? url : null;
  } catch {
    return null;
  }
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function link(href, text) {
  const a = el("a", "", text);
  a.href = href;
  return a;
}

function item(child) {
  const li = el("li");
  li.appendChild(child);
  return li;
}

facilitiesToggle?.addEventListener("click", () => {
  const show = facilitiesToggle.getAttribute("aria-pressed") !== "true";
  facilitiesToggle.setAttribute("aria-pressed", String(show));
  for (const id of ["facilities", "facility-line"]) map.setLayoutProperty(id, "visibility", show ? "visible" : "none");
  if (!show) facilityPopup?.remove();
});

// The gear opens and closes the settings; Escape closes them too.
function setSettingsOpen(open) {
  settingsToggle.setAttribute("aria-expanded", String(open));
  settingsMenu.hidden = !open;
}
settingsToggle?.addEventListener("click", () => {
  setSettingsOpen(settingsToggle.getAttribute("aria-expanded") !== "true");
});

document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || event.target.closest?.(".search")) return; // the search box has its own
  if (facilityPopup?.isOpen()) facilityPopup.remove();
  else if (routePopup?.isOpen()) routePopup.remove();
  else if (pathMode) {
    setPathMode(false);
    pathToggle.focus();
  }
  else if (settingsMenu && !settingsMenu.hidden) {
    setSettingsOpen(false);
    settingsToggle.focus();
  }
});

// Replace the basemap's road-name layers with tiers that appear by importance. Route shields stay.
function addRoadLabels(style) {
  let font = ["Noto Sans Regular"];
  for (const layer of style.layers) {
    if (layer.type !== "symbol" || layer["source-layer"] !== "transportation_name") continue;
    if (JSON.stringify(layer.layout?.["text-field"] ?? "").includes('"ref"')) continue;
    font = layer.layout?.["text-font"] ?? font;
    map.setLayoutProperty(layer.id, "visibility", "none");
  }
  if (!basemapSource) return;
  LABEL_TIERS.forEach((tier, i) =>
    map.addLayer({
      id: `road-label-${i}`,
      type: "symbol",
      source: basemapSource,
      "source-layer": "transportation_name",
      minzoom: tier.minzoom,
      filter: ["in", ["get", "class"], ["literal", tier.classes]],
      layout: {
        "symbol-placement": "line",
        "text-field": ["coalesce", ["get", "name_en"], ["get", "name"]],
        "text-font": font,
        "text-size": ["interpolate", ["linear"], ["zoom"], 11, 11, 16, 13],
        "symbol-spacing": 350,
        "text-max-angle": 30,
        "text-padding": 4,
      },
      paint: {
        "text-color": token(tier.color),
        "text-halo-color": token("--map-bg"),
        "text-halo-width": 1.5,
      },
    }),
  );
}

// Dotted stubs on basemap roads just outside the window, fading with distance from its edge.
let approachKey = "";
function updateApproaches() {
  const ring = outerRing(metas[current]?.footprint);
  if (!ring || !basemapSource) return;
  const roads = map.querySourceFeatures(basemapSource, {
    sourceLayer: "transportation",
    filter: ["in", ["get", "class"], ["literal", ROAD_CLASSES]],
  });
  const key = `${current}:${map.getZoom().toFixed(1)}:${roads.length}`;
  if (key === approachKey) return;
  approachKey = key;
  // Metres per screen pixel at this zoom (512 px tiles) and latitude.
  const lat = map.getCenter().lat;
  const stubMetres = (APPROACH_PX * 78271.517 * Math.cos((lat * Math.PI) / 180)) / 2 ** map.getZoom();

  // Tiles overlap at their edges, so the same crossing can appear twice; keep the longest stub.
  const stubs = new Map();
  for (const road of roads) {
    const { type, coordinates } = road.geometry;
    const lines = type === "LineString" ? [coordinates] : type === "MultiLineString" ? coordinates : [];
    for (const line of lines) {
      for (const stub of stubsLeaving(line, ring, stubMetres)) {
        const at = stub[0].map((v) => v.toFixed(4)).join();
        if (!stubs.has(at) || lineLength(stubs.get(at)) < lineLength(stub)) stubs.set(at, stub);
      }
    }
  }
  const features = [...stubs.values()].flatMap(fade);
  map.getSource("approaches").setData({ type: "FeatureCollection", features });
}

// The parts of a road outside the ring, starting where it crosses the ring, up to maxMetres long.
function stubsLeaving(line, ring, maxMetres) {
  const stubs = [];
  for (let i = 0; i < line.length - 1; i++) {
    const aIn = inside(line[i], ring);
    if (aIn === inside(line[i + 1], ring)) continue;
    const crossing = crossingPoint(line[i], line[i + 1], ring, aIn);
    if (!crossing) continue;
    const onward = aIn ? line.slice(i + 1) : line.slice(0, i + 1).reverse();
    const reentry = onward.findIndex((point) => inside(point, ring));
    const outside = reentry === -1 ? onward : onward.slice(0, reentry);
    stubs.push(cut([crossing, ...outside], 0, maxMetres));
  }
  return stubs;
}

// Split a stub into steps whose opacity drops away from the window.
function fade(stub) {
  const length = lineLength(stub);
  const step = length / APPROACH_STEPS;
  return Array.from({ length: APPROACH_STEPS }, (_, i) => ({
    type: "Feature",
    properties: { opacity: 1 - i / APPROACH_STEPS },
    geometry: { type: "LineString", coordinates: cut(stub, i * step, (i + 1) * step) },
  })).filter((f) => f.geometry.coordinates.length > 1);
}

// --- Geometry helpers (lon/lat; distances are short, so a flat approximation is fine) ---

function metres(a, b) {
  const kx = 111320 * Math.cos((((a[1] + b[1]) / 2) * Math.PI) / 180);
  return Math.hypot((b[0] - a[0]) * kx, (b[1] - a[1]) * 110540);
}

function lineLength(line) {
  let total = 0;
  for (let i = 0; i < line.length - 1; i++) total += metres(line[i], line[i + 1]);
  return total;
}

// The piece of a line between two distances along it.
function cut(line, from, to) {
  const out = [];
  let travelled = 0;
  for (let i = 0; i < line.length - 1; i++) {
    const a = line[i];
    const b = line[i + 1];
    const d = metres(a, b);
    const lerp = (m) => {
      const t = d ? (m - travelled) / d : 0;
      return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];
    };
    if (travelled + d >= from && travelled <= to) {
      if (!out.length) out.push(travelled >= from ? a : lerp(from));
      if (travelled + d > to) {
        out.push(lerp(to));
        break;
      }
      out.push(b);
    }
    travelled += d;
  }
  return out;
}

function inside(point, ring) {
  let result = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i];
    const [xj, yj] = ring[j];
    if (yi > point[1] !== yj > point[1] && point[0] < ((xj - xi) * (point[1] - yi)) / (yj - yi) + xi) {
      result = !result;
    }
  }
  return result;
}

// Where segment a→b crosses the ring; nearest the inside end.
function crossingPoint(a, b, ring, aInside) {
  let best = null;
  for (let i = 0; i < ring.length - 1; i++) {
    const t = intersect(a, b, ring[i], ring[i + 1]);
    if (t !== null && (best === null || (aInside ? t < best : t > best))) best = t;
  }
  return best === null ? null : [a[0] + (b[0] - a[0]) * best, a[1] + (b[1] - a[1]) * best];
}

// Fraction along p→q where it meets segment r→s, or null if they don't meet.
function intersect(p, q, r, s) {
  const d = (q[0] - p[0]) * (s[1] - r[1]) - (q[1] - p[1]) * (s[0] - r[0]);
  if (d === 0) return null;
  const t = ((r[0] - p[0]) * (s[1] - r[1]) - (r[1] - p[1]) * (s[0] - r[0])) / d;
  const u = ((r[0] - p[0]) * (q[1] - p[1]) - (r[1] - p[1]) * (q[0] - p[0])) / d;
  return t >= 0 && t <= 1 && u >= 0 && u <= 1 ? t : null;
}

function outerRing(footprint) {
  if (!footprint) return null;
  return footprint.type === "MultiPolygon" ? footprint.coordinates[0][0] : footprint.coordinates[0];
}

function emptyCollection() {
  return { type: "FeatureCollection", features: [] };
}

function footprints(metaList) {
  return {
    type: "FeatureCollection",
    features: metaList
      .filter((m) => m.footprint)
      .map((m) => ({ type: "Feature", properties: {}, geometry: m.footprint })),
  };
}

// Everything outside the satellite footprints: a world-sized polygon with each footprint as a hole,
// so unobserved roads sit under a hatch and never look safe.
function unobservedArea(metaList) {
  const holes = metaList.flatMap((m) => {
    const fp = m.footprint;
    if (!fp) return [];
    const polygons = fp.type === "MultiPolygon" ? fp.coordinates : [fp.coordinates];
    return polygons.map((polygon) => orient(polygon[0], false));
  });
  return { type: "Polygon", coordinates: [orient(WORLD, true), ...holes] };
}

// GeoJSON wants outer rings counter-clockwise and holes clockwise.
function orient(ring, counterClockwise) {
  let area = 0;
  for (let i = 0; i < ring.length - 1; i++) {
    area += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1];
  }
  return area > 0 === counterClockwise ? ring : [...ring].reverse();
}

// Diagonal stripes for the unobserved area, drawn in the theme's colour.
function hatchPattern(color) {
  const size = 16;
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext("2d");
  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  ctx.beginPath();
  for (const offset of [-size, 0, size]) {
    ctx.moveTo(offset, size);
    ctx.lineTo(offset + size, 0);
  }
  ctx.stroke();
  return ctx.getImageData(0, 0, size, size);
}

// --- Errors: a bar at the bottom, so no failure goes unnoticed ---

const notice = document.querySelector(".notice");
const noticeText = notice.querySelector(".notice-text");
const noticeRetry = notice.querySelector(".notice-retry");
let noticeKind = null;
let noticeRetryFn = null;

// Show an error; `retry` adds a "Try again" button. `kind` lets hideNotice(kind) clear only its own.
function showNotice(text, retry = null, kind = "error") {
  notice.hidden = false;
  noticeText.textContent = text;
  noticeRetry.hidden = !retry;
  noticeRetryFn = retry;
  noticeKind = kind;
}

function hideNotice(kind) {
  if (kind && kind !== noticeKind) return;
  notice.hidden = true;
  noticeRetryFn = null;
  noticeKind = null;
}

noticeRetry.addEventListener("click", () => {
  const retry = noticeRetryFn;
  hideNotice();
  retry?.();
});
notice.querySelector(".notice-close").addEventListener("click", () => hideNotice());

// --- Search box, "Recent", and Autocomplete ---

async function search(query) {
  query = query.trim();
  if (!query) return;
  hideNotice();
  let results;
  try {
    results = await geocode(query);
  } catch {
    showNotice("Search isn't available right now.", () => search(query));
    return;
  }
  if (results.length === 0) {
    showNotice(`No places found for “${query}”.`);
    return;
  }
  keepInUrl({ q: query });
  analyze(results[0]);
}

// Places matching the text, nearest the map's centre first (geocoded and cached by the backend).
function geocode(query) {
  const { lng, lat } = map.getCenter();
  return getJSON(`/api/geocode?${new URLSearchParams({ q: query, lon: lng.toFixed(1), lat: lat.toFixed(1) })}`);
}

// Fit the map to a whole window, so its outline stays in view rather than zooming to the address.
// Extra room at the sides and bottom keeps the outline clear of the gear, zoom buttons and credits.
const WINDOW_PADDING = { top: 80, right: 72, bottom: 48, left: 24 };
function fitWindow(bbox) {
  map.fitBounds(bbox, { padding: WINDOW_PADDING, duration: reduceMotion ? 0 : 800 });
}

// --- Analysing a searched place: a background job on the newest satellite pass ---

const POLL_MS = 3000;
const MAX_FAILED_POLLS = 5; // ~15 s without an answer: tell the user rather than spin forever
let analysisRun = 0; // a newer search abandons the older one's polling

// "Try again" re-posts the place: the server reruns a failed job, or rejoins one still running.
async function analyze(place) {
  const run = ++analysisRun;
  const retry = () => analyze(place);
  hideNotice();
  const [lon, lat] = place.center;
  const params = new URLSearchParams({ name: place.name, lon, lat });
  let job;
  try {
    job = await getJSON(`/api/analyze?${params}`, { method: "POST" });
  } catch {
    map.fitBounds(place.bbox, { padding: PADDING, maxZoom: 16, duration: reduceMotion ? 0 : 800 });
    showNotice(`Couldn't start the analysis of ${place.name}.`, retry);
    return;
  }
  // The window moves to the searched area straight away; the old one is no longer drawn.
  showWindow(null);
  showPending(job.bbox);
  fitWindow(job.bbox);
  let failedPolls = 0;
  while (run === analysisRun) {
    if (job.status === "done") {
      hideLoading();
      return finishAnalysis(job, retry);
    }
    if (job.status === "failed") {
      hideLoading();
      showPending(null);
      showNotice(job.message || `The analysis of ${job.name} failed.`, retry);
      return;
    }
    showLoading(job.bbox, job.name);
    await new Promise((resolve) => setTimeout(resolve, POLL_MS));
    try {
      job = await getJSON(`/api/analyze/${encodeURIComponent(job.slug)}`);
      failedPolls = 0;
    } catch {
      if (++failedPolls < MAX_FAILED_POLLS || run !== analysisRun) continue; // retried next tick
      hideLoading();
      showPending(null);
      showNotice(`Lost contact with the server while analysing ${job.name}.`, retry);
      return;
    }
  }
}

async function finishAnalysis(job, retry) {
  let meta;
  try {
    meta = await getJSON(`/api/meta?location=${encodeURIComponent(job.slug)}`);
  } catch {
    showPending(null);
    showNotice(`Couldn't load the results for ${job.name}.`, retry);
    return;
  }
  metas[job.slug] = meta;
  showPending(null);
  showWindow(job.slug);
  keepInUrl({ q: input.value.trim(), location: job.slug });
  frame(job.slug, true);
}

// A large spinner in the middle of the window being analysed; the detail goes under it,
// so the search box stays clear.
let loadingMarker;
function showLoading(bbox, place) {
  if (!loadingMarker) {
    const card = el("div", "window-loading");
    card.setAttribute("role", "status");
    card.appendChild(el("div", "spinner"));
    card.appendChild(el("span", "visually-hidden", "Loading")); // the spinner, for screen readers
    card.appendChild(el("p", "window-loading-detail"));
    loadingMarker = new maplibregl.Marker({ element: card });
  }
  const [w, s, e, n] = bbox;
  // The place stands out from the rest of the line; text nodes, never HTML.
  loadingMarker
    .getElement()
    .querySelector(".window-loading-detail")
    .replaceChildren(
      "Analysing the latest satellite pass for ",
      el("span", "window-loading-place", place),
      ". This may take a minute.",
    );
  loadingMarker.setLngLat([(w + e) / 2, (s + n) / 2]).addTo(map);
  loadingBbox = bbox;
  sizeLoading();
  map.off("zoom", sizeLoading); // called on every poll: keep a single listener
  map.on("zoom", sizeLoading);
}

function hideLoading() {
  loadingMarker?.remove();
  map.off("zoom", sizeLoading);
}

// Zoomed out, the window gets smaller than the card: shrink the card to a spinner badge so it doesn't
// cover the region around the window, but stays visible at country or continent zoom.
const LOADING_FULL_PX = 280; // the full card's width, plus a little room
let loadingBbox = null;
function sizeLoading() {
  if (!loadingBbox || !loadingMarker) return;
  const [w, s, e, n] = loadingBbox;
  const mid = (s + n) / 2;
  const shownWidth = map.project([e, mid]).x - map.project([w, mid]).x;
  loadingMarker.getElement().classList.toggle("compact", shownWidth < LOADING_FULL_PX);
}

// Clicking a window's outline says how big it is (searches analyse a 10 km square).
let windowPopup;
function showWindowSize(event) {
  if (pathMode) return;
  if (map.queryRenderedFeatures(event.point, { layers: ["facilities"] }).length) return; // icon wins
  // Not the clicked feature's geometry: that's cut to the map tile it was drawn in.
  const pending = event.features[0].layer.id === "window-pending-hit";
  const bbox = pending ? pendingBbox : metas[current]?.bbox;
  if (!bbox) return;
  const [w, s, e, n] = bbox;
  const lat = (s + n) / 2;
  const wide = metres([w, lat], [e, lat]) / 1000;
  const tall = metres([w, s], [w, n]) / 1000;
  const km = (v) => (v >= 5 ? Math.round(v) : v.toFixed(1));
  windowPopup?.remove();
  windowPopup = new maplibregl.Popup({ className: "window-popup", closeButton: false })
    .setLngLat(event.lngLat)
    .setText(`${pending ? "Analysing" : "Satellite window"}: ${km(wide)} km × ${km(tall)} km`)
    .addTo(map);
}

let pendingBbox = null;
function showPending(bbox) {
  pendingBbox = bbox;
  map.getSource("pending").setData(bbox ? bboxOutline(bbox) : emptyCollection());
}

function bboxOutline([w, s, e, n]) {
  const ring = [[w, s], [e, s], [e, n], [w, n], [w, s]];
  return { type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: ring } };
}

function chooseRecent(index) {
  const option = recentOptions[index];
  recentOptions.forEach((o) => o.setAttribute("aria-selected", String(o === option)));
  input.value = option.querySelector(".recent-place").textContent;
  closeRecent();
  analysisRun++; // stop following a search that's still being analysed
  hideNotice();
  hideLoading();
  showPending(null);
  showWindow(option.dataset.slug);
  keepInUrl({ location: current });
  frame(current, true);
}

function keepInUrl(params) {
  history.replaceState(null, "", `?${new URLSearchParams(params)}`);
  for (const link of themeLinks) {
    link.href = `?${new URLSearchParams({ theme: link.dataset.themeOption, ...params })}`;
  }
}

// The search box's list (WAI-ARIA combobox): "Recent" while the box is empty, place suggestions
// while typing. Both share the keys: ↑/↓ move, Enter picks, Escape closes.
const suggestPanel = document.getElementById("suggestions");
const suggestList = document.getElementById("suggestion-list");
const pinIcon = document.getElementById("suggestion-icon").content.firstElementChild;
let suggestions = []; // places shown in suggestPanel, from /api/geocode
let active = -1;

// Options of whichever list is open
function shownOptions() {
  if (recentPanel && !recentPanel.hidden) return recentOptions;
  if (!suggestPanel.hidden) return [...suggestList.children];
  return [];
}

function openRecent() {
  if (!recentPanel || input.value.trim()) return;
  closeSuggestions();
  recentPanel.hidden = false;
  input.setAttribute("aria-controls", "recent-list");
  input.setAttribute("aria-expanded", "true");
}

function closeRecent() {
  if (!recentPanel) return;
  recentPanel.hidden = true;
  input.setAttribute("aria-expanded", "false");
  setActive(-1);
}

// Place names come from OpenStreetMap: textContent only, never HTML.
function showSuggestions(places) {
  suggestions = places;
  suggestList.replaceChildren(
    ...places.map((place, i) => {
      const option = el("li");
      option.id = `suggestion-${i}`;
      option.setAttribute("role", "option");
      option.setAttribute("aria-selected", "false");
      option.appendChild(pinIcon.cloneNode(true));
      const text = option.appendChild(el("span", "recent-text"));
      text.appendChild(el("span", "recent-place", place.place));
      if (place.detail) text.appendChild(el("span", "recent-when", place.detail));
      option.addEventListener("mousedown", (event) => event.preventDefault()); // keep focus in the box
      option.addEventListener("click", () => chooseSuggestion(i));
      return option;
    }),
  );
  setActive(-1);
  suggestPanel.hidden = !places.length;
  input.setAttribute("aria-controls", "suggestion-list");
  input.setAttribute("aria-expanded", String(places.length > 0));
}

function closeSuggestions() {
  suggestPanel.hidden = true;
  input.setAttribute("aria-expanded", "false");
  setActive(-1);
}

function chooseSuggestion(index) {
  const place = suggestions[index];
  input.value = place.name;
  closeSuggestions();
  updateSearchButton();
  keepInUrl({ q: place.name });
  analyze(place); // already geocoded: no second lookup
}

function setActive(index) {
  const options = shownOptions();
  active = index;
  for (const o of [...recentOptions, ...suggestList.children]) o.classList.toggle("active", o === options[index]);
  if (active >= 0) input.setAttribute("aria-activedescendant", options[active].id);
  else input.removeAttribute("aria-activedescendant");
}

input.addEventListener("focus", openRecent);
input.addEventListener("click", openRecent);

input.addEventListener("keydown", (event) => {
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    if (!shownOptions().length) openRecent();
    const options = shownOptions();
    if (!options.length) return;
    event.preventDefault();
    const step = event.key === "ArrowDown" ? 1 : -1;
    setActive((active + step + options.length) % options.length);
  } else if (event.key === "Enter" && active >= 0) {
    event.preventDefault();
    if (recentPanel && !recentPanel.hidden) chooseRecent(active);
    else chooseSuggestion(active);
  } else if (event.key === "Escape" && shownOptions().length) {
    closeRecent();
    closeSuggestions();
  }
});

recentOptions.forEach((option, i) => {
  option.addEventListener("mousedown", (event) => event.preventDefault());
  option.addEventListener("click", () => chooseRecent(i));
});

// Suggestions follow the text after a short pause; an answer for older text is dropped.
const SUGGEST_DELAY_MS = 300;
let suggestTimer;
let suggestRequest = 0;
input.addEventListener("input", () => {
  updateSearchButton();
  const query = input.value.trim();
  clearTimeout(suggestTimer);
  const request = ++suggestRequest;
  if (!query) {
    openRecent();
    return;
  }
  closeRecent();
  if (query.length < 3) {
    closeSuggestions();
    return;
  }
  suggestTimer = setTimeout(async () => {
    let places;
    try {
      places = await geocode(query);
    } catch {
      return; // geocoder unavailable: keep typing, Enter still searches
    }
    if (request === suggestRequest && document.activeElement === input) showSuggestions(places);
  }, SUGGEST_DELAY_MS);
});

// The magnifying glass turns into a green Enter button only while someone is typing: the box has
// focus and text in it.
function updateSearchButton() {
  form.classList.toggle("has-text", document.activeElement === input && Boolean(input.value.trim()));
}
input.addEventListener("focus", updateSearchButton);
input.addEventListener("blur", updateSearchButton);
// Pressing the button would blur the box first and turn it back into a magnifying glass mid-click.
form.querySelector(".search-button").addEventListener("mousedown", (event) => event.preventDefault());

form.addEventListener("submit", (event) => {
  event.preventDefault();
  suggestRequest++; // a suggestion answer arriving now is stale
  closeRecent();
  closeSuggestions();
  search(input.value);
});

document.addEventListener("click", (event) => {
  if (event.target.closest(".search")) return;
  closeRecent();
  closeSuggestions();
});

// --- Pathfinding: pick two points (building icons or streets), route between them ---
// The server finds the fastest route that avoids roads where water was detected (green). A route
// through flooding, or over roads the satellite didn't see, is "cautionary" (red, caution icons).

const ROUTE_TIMEOUT_MS = 15000;
const SAME_POINT_M = 5;

// The caution icon repeated along a red route: a white triangle on a red badge.
async function makeCautionImage() {
  const svg = document.getElementById("route-icons")?.content.querySelector("svg");
  if (!svg) return null;
  const glyph = await svgImage(svg, "#ffffff", 28);
  const size = 48;
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext("2d");
  ctx.beginPath();
  ctx.arc(size / 2, size / 2, size / 2 - 3, 0, 2 * Math.PI);
  ctx.fillStyle = token("--danger");
  ctx.fill();
  ctx.lineWidth = 3;
  ctx.strokeStyle = "#ffffff";
  ctx.stroke();
  ctx.drawImage(glyph, (size - glyph.width) / 2, (size - glyph.height) / 2 - 1);
  return ctx.getImageData(0, 0, size, size);
}

function addPathLayers() {
  // Highlights of what can be picked, shown only in pathfinding mode; stronger under the pointer.
  const hovered = ["boolean", ["feature-state", "hover"], false];
  map.addLayer(
    {
      id: "pick-streets",
      type: "line",
      source: "segments",
      layout: { visibility: "none", "line-cap": "round", "line-join": "round" },
      paint: {
        "line-color": token("--accent"),
        "line-width": width(5),
        "line-opacity": ["case", hovered, 0.8, 0.3],
      },
    },
    "road-clear", // under the roads, so it reads as a casing
  );
  map.addLayer(
    {
      id: "pick-icons",
      type: "circle",
      source: "facilities",
      filter: ["==", ["get", "shape"], "point"],
      layout: { visibility: "none" },
      paint: {
        "circle-radius": ["interpolate", ["linear"], ["zoom"], 10, 14, 14, 19],
        "circle-color": "rgba(0, 0, 0, 0)",
        "circle-stroke-color": token("--accent"),
        "circle-stroke-width": 3,
      },
    },
    "facilities",
  );

  // The route: above the flood traces, below the building icons.
  if (cautionImage) map.addImage("route-caution", cautionImage, { pixelRatio: 2 });
  map.addSource("route", { type: "geojson", data: emptyCollection() });
  map.addLayer(
    {
      id: "route-line",
      type: "line",
      source: "route",
      layout: { "line-cap": "round", "line-join": "round" },
      paint: {
        "line-color": ["match", ["get", "kind"], "clear", token("--go"), token("--danger")],
        "line-width": width(3.5),
      },
    },
    "facility-line",
  );
  map.addLayer(
    {
      id: "route-caution",
      type: "symbol",
      source: "route",
      filter: ["==", ["get", "kind"], "cautionary"],
      layout: {
        "symbol-placement": "line",
        "symbol-spacing": 160,
        "icon-image": "route-caution",
        "icon-size": ["interpolate", ["linear"], ["zoom"], 10, 0.7, 15, 1],
        "icon-allow-overlap": true,
        "icon-rotation-alignment": "viewport", // the triangle stays upright along the line
      },
    },
    "facility-line",
  );

  map.on("click", onPathClick);
  map.on("click", "route-line", (event) => pathMode || openRoute(event.features[0], event.lngLat));
  map.on("mouseenter", "route-line", () => (map.getCanvas().style.cursor = "pointer"));
  map.on("mouseleave", "route-line", () => (map.getCanvas().style.cursor = ""));
  trackHover("segments", ROAD_LAYERS);
  trackHover("facilities", ["facilities"]);
}

// Strengthen the highlight of the street or icon under the pointer, in pathfinding mode only.
function trackHover(source, layers) {
  let hoverId = null;
  const clear = () => {
    if (hoverId !== null) map.setFeatureState({ source, id: hoverId }, { hover: false });
    hoverId = null;
  };
  map.on("mousemove", layers, (event) => {
    if (!pathMode) return;
    const id = event.features[0]?.id;
    if (id === hoverId) return;
    clear();
    if (id !== undefined) {
      hoverId = id;
      map.setFeatureState({ source, id }, { hover: true });
    }
  });
  map.on("mouseleave", layers, clear);
}

function setPathMode(on) {
  pathMode = on;
  if (!on && picks.length === 1) clearPath(); // a lone start is dropped; a drawn route stays
  pathToggle.setAttribute("aria-pressed", String(on));
  const iconsShown = facilitiesToggle?.getAttribute("aria-pressed") !== "false"; // unless hidden in settings
  map.setLayoutProperty("pick-streets", "visibility", on ? "visible" : "none");
  map.setLayoutProperty("pick-icons", "visibility", on && iconsShown ? "visible" : "none");
  pathCard.hidden = !on;
  facilityPopup?.remove();
  roadPopup?.remove();
  if (on) {
    setSettingsOpen(false);
    showPathStep(picks.length === 1 ? "Choose a destination: a building or a street." : "Choose a start: a building or a street.");
  }
}

pathToggle?.addEventListener("click", () => setPathMode(!pathMode));

// In pathfinding mode a click picks a building icon or a street; the route keeps its popup.
function onPathClick(event) {
  if (!pathMode || event.originalEvent?.target?.closest?.(".path-marker")) return; // markers handle their own
  const { x, y } = event.point;
  const box = [[x - CLICK_PX, y - CLICK_PX], [x + CLICK_PX, y + CLICK_PX]];
  const route = map.queryRenderedFeatures(box, { layers: ["route-line"] });
  if (route.length) return openRoute(route[0], event.lngLat);
  const icon = map.queryRenderedFeatures(box, { layers: ["facilities"] })[0];
  if (icon) {
    const kind = facilityKinds[icon.properties.kind]?.label ?? "Building";
    return pick(icon.geometry.coordinates, icon.properties.name ?? `Unnamed ${kind.toLowerCase()}`);
  }
  const road = map.queryRenderedFeatures(box, { layers: ROAD_LAYERS })[0];
  if (road) pick([event.lngLat.lng, event.lngLat.lat], road.properties.name ?? "Unnamed road");
}

function pick(point, label) {
  if (picks.length === 2) clearPath(); // a third pick starts a new route
  if (picks.length === 1 && metres(picks[0].point, point) < SAME_POINT_M) return clearPath(); // picked again: de-select
  addPick(point, label);
  if (picks.length === 1) showPathStep(`From ${label}. Choose a destination: a building or a street.`);
  else findRoute();
}

function addPick(point, label) {
  const element = el("div", "path-marker", picks.length ? "B" : "A");
  // Clicking the start again de-selects it; once a route is drawn, "Remove route" in its popup clears it.
  element.addEventListener("click", (event) => {
    if (!pathMode) return;
    event.stopPropagation();
    if (picks.length === 1) clearPath();
  });
  const marker = new maplibregl.Marker({ element }).setLngLat(point).addTo(map);
  picks.push({ point, label, marker });
}

// `restoring`: redrawing a route saved before a reload, so no camera move, and a failure just forgets it.
async function findRoute({ restoring = false } = {}) {
  const [a, b] = picks;
  const request = ++pathRequest;
  showPathStep(`Finding a route from ${a.label} to ${b.label}…`);
  const params = new URLSearchParams({ location: current, start: a.point.join(","), end: b.point.join(",") });
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), ROUTE_TIMEOUT_MS);
  let response;
  try {
    response = await fetch(`/api/route?${params}`, { signal: controller.signal });
  } catch {
    if (request !== pathRequest) return;
    if (restoring) clearPath();
    else showPathStep("Couldn't reach the routing service. Try again.", true, findRoute);
    return;
  } finally {
    clearTimeout(timer);
  }
  if (request !== pathRequest) return; // cleared or replaced meanwhile
  const body = await response.json().catch(() => ({}));
  if (!response.ok && restoring) return clearPath();
  if (!response.ok) {
    const message = typeof body.detail === "string" ? body.detail : "Couldn't find a route. Try again.";
    showPathStep(`${message} Choose a new start to try again.`, true);
    return;
  }
  map.getSource("route").setData(body);
  saveRoute();
  if (!restoring) {
    const coords = body.geometry.coordinates;
    const bounds = coords.reduce((b, c) => b.extend(c), new maplibregl.LngLatBounds(coords[0], coords[0]));
    map.fitBounds(bounds, { padding: WINDOW_PADDING, maxZoom: 16, duration: reduceMotion ? 0 : 600 });
  }
  const p = body.properties;
  const caution = p.kind === "clear" ? "" : " Cautionary route: click it to see why.";
  showPathStep(`Route found: about ${duration(p.duration_s)} (estimated).${caution} Click a building or street to start a new route.`);
}

// The instruction card: one step at a time. Errors are red; `retry` adds a "Try again" button.
// Its X hides it until the next step; it only shows in pathfinding mode.
function showPathStep(text, isError = false, retry = null) {
  const line = el("p", isError ? "path-error" : "", text);
  pathBody.replaceChildren(line);
  if (retry) {
    const button = pathBody.appendChild(el("button", "path-retry", "Try again"));
    button.type = "button";
    button.addEventListener("click", retry);
  }
  pathCard.hidden = !pathMode;
}

pathCard?.querySelector(".path-close").addEventListener("click", () => (pathCard.hidden = true));

// Remove the route and both markers. `forget: false` keeps the saved copy (the window changed).
function clearPath({ forget = true } = {}) {
  pathRequest++;
  for (const p of picks) p.marker.remove();
  picks = [];
  routePopup?.remove();
  map.getSource("route")?.setData(emptyCollection());
  if (forget) storage("removeItem", ROUTE_KEY);
  if (pathMode) showPathStep("Choose a start: a building or a street.");
}

// The last route survives a reload: its two picks are kept in this browser and the route is asked
// for again, so it reflects the window's current data. Storage can throw (private windows).
const ROUTE_KEY = "flow.route";

function storage(method, ...args) {
  try {
    return localStorage[method](...args);
  } catch {
    return null;
  }
}

function saveRoute() {
  const saved = { location: current, picks: picks.map(({ point, label }) => ({ point, label })) };
  storage("setItem", ROUTE_KEY, JSON.stringify(saved));
}

function restoreRoute() {
  let saved;
  try {
    saved = JSON.parse(storage("getItem", ROUTE_KEY));
  } catch {
    saved = null;
  }
  if (!saved || !current || saved.location !== current) return;
  const valid = (p) => Array.isArray(p?.point) && p.point.length === 2 && p.point.every(Number.isFinite);
  if (saved.picks?.length !== 2 || !saved.picks.every(valid)) return storage("removeItem", ROUTE_KEY);
  for (const { point, label } of saved.picks) addPick(point, String(label ?? "Unnamed road"));
  findRoute({ restoring: true });
}

// Travel time is an estimate from speed limits, never a promise: said in the popup.
function openRoute(feature, lngLat) {
  routePopup?.remove();
  routePopup = new maplibregl.Popup({ className: "facility-popup", maxWidth: "320px" })
    .setLngLat(lngLat)
    .setDOMContent(routeCard(feature.properties))
    .addTo(map);
}

function routeCard(p) {
  // Properties of rendered features come back as strings for arrays.
  const reasons = typeof p.reasons === "string" ? JSON.parse(p.reasons) : (p.reasons ?? []);
  const card = el("div", "facility");
  if (p.kind === "clear") {
    card.appendChild(el("p", "facility-kind", "Clear route: no water detected on it"));
  } else {
    const head = card.appendChild(el("p", "route-caution-head"));
    const svg = document.getElementById("route-icons")?.content.querySelector("svg");
    if (svg) head.appendChild(svg.cloneNode(true));
    head.append("Cautionary route — potentially dangerous");
  }
  card.appendChild(el("h2", "facility-name", `Estimated travel time: ${duration(p.duration_s)}`));
  card.appendChild(el("p", "", `${km(p.distance_m)} by road`));
  if (reasons.length) {
    const list = card.appendChild(el("ul", "route-reasons"));
    const pct = Math.round((p.avg_flooded_fraction ?? 0) * 100);
    const lines = {
      flooded: `Crosses ${km(p.flooded_m)} of flooded road. On average, water covers ${pct}% of the observed road on this route.`,
      unobserved: `No satellite data for part of this route. ${km(p.unobserved_m)} of it wasn't observed, so we can't tell whether it's flooded. Check before relying on it.`,
      flooded_endpoint: "Starts or ends where water was detected.",
    };
    for (const reason of reasons) if (lines[reason]) list.appendChild(el("li", "", lines[reason]));
  }
  card.appendChild(el("p", "route-note", "Time estimated from speed limits only: no traffic or road closures. Radar sees water, not depth."));
  card.appendChild(el("p", "facility-source", `Satellite: ${localTime(p.observed_utc)}`));
  const remove = card.appendChild(el("button", "route-remove"));
  remove.type = "button";
  const trash = document.getElementById("remove-icon")?.content.querySelector("svg");
  if (trash) remove.appendChild(trash.cloneNode(true));
  remove.append("Remove route");
  remove.addEventListener("click", () => clearPath());
  return card;
}

function duration(seconds) {
  const minutes = Math.max(1, Math.round(seconds / 60));
  return minutes < 60 ? `${minutes} min` : `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

function km(m) {
  return m >= 1000 ? `${(m / 1000).toFixed(1)} km` : `${Math.round(m)} m`;
}
