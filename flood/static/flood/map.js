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

  map.addSource("segments", { type: "geojson", data: emptyCollection() });
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
  addRoadClicks();
  addRoadLabels(style);
  // Listen before framing: an unanimated fit fires "moveend" immediately.
  map.on("moveend", loadView);
  map.on("idle", updateApproaches);
  frame(current, false);
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
    return; // keep what's drawn; the next move retries
  }
  if (request !== latestRequest) return; // a newer move or window change already started
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

  map.on("click", "facilities", (event) => openFacility(event.features[0]));
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
    if (map.queryRenderedFeatures(box, { layers: ["facilities"] }).length) return; // its own popup
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

// --- Search box, "Recent", and Autocomplete ---

async function search(query) {
  query = query.trim();
  if (!query) return;
  let results;
  try {
    results = await getJSON(`/api/geocode?q=${encodeURIComponent(query)}`);
  } catch {
    return; // geocoder unavailable
  }
  if (results.length === 0) return;
  keepInUrl({ q: query });
  analyze(results[0]);
}

// Fit the map to a whole window, so its outline stays in view rather than zooming to the address.
// Extra room at the sides and bottom keeps the outline clear of the gear, zoom buttons and credits.
const WINDOW_PADDING = { top: 80, right: 72, bottom: 48, left: 24 };
function fitWindow(bbox) {
  map.fitBounds(bbox, { padding: WINDOW_PADDING, duration: reduceMotion ? 0 : 800 });
}

// --- Analysing a searched place: a background job on the newest satellite pass ---

const POLL_MS = 3000;
let analysisRun = 0; // a newer search abandons the older one's polling

async function analyze(place) {
  const run = ++analysisRun;
  const [lon, lat] = place.center;
  const params = new URLSearchParams({ name: place.name, lon, lat });
  let job;
  try {
    job = await getJSON(`/api/analyze?${params}`, { method: "POST" });
  } catch {
    map.fitBounds(place.bbox, { padding: PADDING, maxZoom: 16, duration: reduceMotion ? 0 : 800 });
    return;
  }
  // The window moves to the searched area straight away; the old one is no longer drawn.
  showWindow(null);
  showPending(job.bbox);
  fitWindow(job.bbox);
  while (run === analysisRun) {
    if (job.status === "done") {
      hideLoading();
      return finishAnalysis(job);
    }
    if (job.status === "failed") {
      hideLoading();
      showPending(null);
      return;
    }
    showLoading(job.bbox, job.name);
    await new Promise((resolve) => setTimeout(resolve, POLL_MS));
    try {
      job = await getJSON(`/api/analyze/${encodeURIComponent(job.slug)}`);
    } catch {
      // A failed poll is retried on the next tick.
    }
  }
}

async function finishAnalysis(job) {
  const meta = await getJSON(`/api/meta?location=${encodeURIComponent(job.slug)}`);
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
}

function hideLoading() {
  loadingMarker?.remove();
}

// Clicking a window's outline says how big it is (searches analyse a 10 km square).
let windowPopup;
function showWindowSize(event) {
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

let active = -1;
function openRecent() {
  if (!recentPanel || input.value.trim()) return;
  recentPanel.hidden = false;
  input.setAttribute("aria-expanded", "true");
}

function closeRecent() {
  if (!recentPanel) return;
  recentPanel.hidden = true;
  input.setAttribute("aria-expanded", "false");
  setActive(-1);
}

function setActive(index) {
  active = index;
  recentOptions.forEach((o, i) => o.classList.toggle("active", i === active));
  if (active >= 0) input.setAttribute("aria-activedescendant", recentOptions[active].id);
  else input.removeAttribute("aria-activedescendant");
}

input.addEventListener("focus", openRecent);
input.addEventListener("click", openRecent);

input.addEventListener("keydown", (event) => {
  const open = recentPanel && !recentPanel.hidden;
  const count = recentOptions.length;
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    if (!open) openRecent();
    if (!count || recentPanel.hidden) return;
    event.preventDefault();
    const step = event.key === "ArrowDown" ? 1 : -1;
    setActive((active + step + count) % count);
  } else if (event.key === "Enter" && open && active >= 0) {
    event.preventDefault();
    chooseRecent(active);
  } else if (event.key === "Escape" && open) {
    closeRecent();
  }
});

recentOptions.forEach((option, i) => {
  option.addEventListener("mousedown", (event) => event.preventDefault()); 
  option.addEventListener("click", () => chooseRecent(i));
});

// --- NEW Autocomplete Logic ---

const autocompleteContainer = document.createElement('div');
autocompleteContainer.className = 'recent';
autocompleteContainer.hidden = true;
// We removed the ID here so it doesn't conflict with your HTML file
autocompleteContainer.innerHTML = '<ul role="listbox"></ul>';
form.appendChild(autocompleteContainer);

// Grab the exact UL we just created, ignoring the rest of the page
const autocompleteList = autocompleteContainer.querySelector('ul');
let currentSuggestions = []; 
let debounceTimer;

// The magnifying glass turns into a green Enter button only while someone is typing: the box has
// focus and text in it.
function updateSearchButton() {
  form.classList.toggle("has-text", document.activeElement === input && Boolean(input.value.trim()));
}
input.addEventListener("focus", updateSearchButton);
input.addEventListener("blur", updateSearchButton);
// Pressing the button would blur the box first and turn it back into a magnifying glass mid-click.
form.querySelector(".search-button").addEventListener("mousedown", (event) => event.preventDefault());

input.addEventListener("input", (e) => {
  updateSearchButton();
  const query = e.target.value.trim();
  clearTimeout(debounceTimer);

  if (!query) {
    openRecent();
    autocompleteContainer.hidden = true;
    currentSuggestions = [];
    return;
  }

  closeRecent();

  if (query.length < 3) {
    autocompleteContainer.hidden = true;
    currentSuggestions = [];
    return;
  }

  debounceTimer = setTimeout(async () => {
    try {
      const response = await fetch(`https://photon.komoot.io/api/?q=${encodeURIComponent(query)}&limit=5&lat=49.28&lon=-123.12`);
      const data = await response.json();
      currentSuggestions = data.features;
      renderSuggestions(currentSuggestions);
    } catch (err) {
      console.error("Autocomplete fetch failed:", err);
    }
  }, 300);
});

function renderSuggestions(features) {
  autocompleteList.innerHTML = '';
  
  if (features.length === 0) {
    autocompleteContainer.hidden = true;
    return;
  }

  // An SVG map pin icon that perfectly matches your "Recent" clock icon
  const pinIcon = `<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z"/><circle cx="12" cy="10" r="3"/></svg>`;

  features.forEach((feature) => {
    const li = document.createElement('li');
    li.role = 'option';
    
    const props = feature.properties;
    
    // Format the address cleanly
    const streetInfo = [props.housenumber, props.street].filter(Boolean).join(' ');
    const placeName = props.name || streetInfo || props.city || "Unknown Location";
    const regionDetails = [props.city, props.state].filter((item) => item && item !== placeName).join(', ');

    // Use your native HTML structure so app.css styles it perfectly
    li.innerHTML = `
      ${pinIcon}
      <span class="recent-text">
        <span class="recent-place">${placeName}</span>
        ${regionDetails ? `<span class="recent-when">${regionDetails}</span>` : ''}
      </span>
    `;

li.addEventListener('mousedown', (event) => event.preventDefault()); 
    li.addEventListener('click', () => {
      const exactAddress = [placeName, regionDetails].filter(Boolean).join(', ');
      input.value = exactAddress;
      autocompleteContainer.hidden = true;
      
      // SEND TO DJANGO: Force a page reload so Python's index view catches the ?q= parameter
      window.location.href = `/?q=${encodeURIComponent(exactAddress)}`;
    });

    autocompleteList.appendChild(li);
  });

  autocompleteContainer.hidden = false;
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  closeRecent();
  autocompleteContainer.hidden = true;

  if (currentSuggestions.length > 0) {
    const props = currentSuggestions[0].properties;
    const streetInfo = [props.housenumber, props.street].filter(Boolean).join(' ');
    const placeName = props.name || streetInfo || props.city || "Unknown Location";
    const regionDetails = [props.city, props.state].filter(item => item && item !== placeName).join(', ');
    
    const exactAddress = [placeName, regionDetails].filter(Boolean).join(', ');
    input.value = exactAddress;
    
    // SEND TO DJANGO
    window.location.href = `/?q=${encodeURIComponent(exactAddress)}`;
  } else {
    // SEND TO DJANGO (Fallback for exactly what they typed)
    window.location.href = `/?q=${encodeURIComponent(input.value)}`;
  }
});

document.addEventListener("click", (event) => {
  if (!event.target.closest(".search")) {
    closeRecent();
    autocompleteContainer.hidden = true;
  }
});