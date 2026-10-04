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
const searchMessage = document.getElementById("search-message");
const themeLink = document.querySelector(".theme-toggle");

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
    return;
  }
  const view = map.getBounds().toArray().flat(); // [west, south, east, north]
  const params = new URLSearchParams({ bbox: view.join(","), location: current });
  const segments = await getJSON(`/api/flood?${params}`);
  if (request !== latestRequest) return; // a newer move or window change already started
  map.getSource("segments").setData(segments);
}

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
  showSearchMessage("");
  let results;
  try {
    results = await getJSON(`/api/geocode?q=${encodeURIComponent(query)}`);
  } catch {
    showSearchMessage("Search isn't available right now. Try again in a moment.");
    return;
  }
  if (results.length === 0) {
    showSearchMessage(`No places found for “${query}”.`);
    return;
  }
  keepInUrl({ q: query });
  map.fitBounds(results[0].bbox, { padding: PADDING, maxZoom: 16, duration: reduceMotion ? 0 : 800 });
  analyze(results[0]);
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
    showSearchMessage("Satellite analysis isn't available right now. Try again in a moment.");
    return;
  }
  // The window moves to the searched area straight away; the old one is no longer drawn.
  showWindow(null);
  showPending(job.bbox);
  while (run === analysisRun) {
    if (job.status === "done") return finishAnalysis(job);
    if (job.status === "failed") {
      showPending(null);
      showSearchMessage(`No satellite result for ${job.name}: ${job.message}`);
      return;
    }
    showSearchMessage(`Analysing the latest satellite pass for ${job.name}. This may take a minute.`);
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
  const when = observedLocal(meta);
  showSearchMessage(when ? `Satellite pass of ${when}. Blue = water detected.` : "");
}

function showPending(bbox) {
  map.getSource("pending").setData(bbox ? bboxOutline(bbox) : emptyCollection());
}

function bboxOutline([w, s, e, n]) {
  const ring = [[w, s], [e, s], [e, n], [w, n], [w, s]];
  return { type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: ring } };
}

// Observation time in the location's own time zone, e.g. "Sep 28, 2026, 7:20 AM PDT"
function observedLocal(meta) {
  if (!meta.observed_utc) return "";
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZone: meta.timezone || "UTC",
    timeZoneName: "short", // can't be combined with dateStyle/timeStyle
  }).format(new Date(meta.observed_utc));
}

function chooseRecent(index) {
  const option = recentOptions[index];
  recentOptions.forEach((o) => o.setAttribute("aria-selected", String(o === option)));
  input.value = option.querySelector(".recent-place").textContent;
  closeRecent();
  showSearchMessage("");
  analysisRun++; // stop following a search that's still being analysed
  showPending(null);
  showWindow(option.dataset.slug);
  keepInUrl({ location: current });
  frame(current, true);
}

function keepInUrl(params) {
  history.replaceState(null, "", `?${new URLSearchParams(params)}`);
  const themeUrl = new URL(themeLink.href);
  const theme = themeUrl.searchParams.get("theme");
  themeLink.href = `?${new URLSearchParams({ theme, ...params })}`;
}

function showSearchMessage(text) {
  searchMessage.textContent = text;
  searchMessage.hidden = !text;
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

input.addEventListener("input", (e) => {
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