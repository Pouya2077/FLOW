// Flood map: draws the pipeline's road segments over the OpenFreeMap basemap with MapLibre.
// The map can't be dragged or zoomed (CLAUDE.md); the search box is the only way to move it.
import * as maplibregl from "https://cdn.jsdelivr.net/npm/maplibre-gl@6.12.0/dist/maplibre-gl.mjs";

const mapEl = document.getElementById("map");
const form = document.querySelector(".search");
const input = document.getElementById("q");
const searchMessage = document.getElementById("search-message");
const notice = document.getElementById("map-notice");

const css = getComputedStyle(document.documentElement);
const token = (name) => css.getPropertyValue(name).trim();
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

const PADDING = { top: 80, right: 24, bottom: 56, left: 24 }; // clear of the search box and notice
const WORLD = [[-180, -85], [180, -85], [180, 85], [-180, 85], [-180, -85]];

// Line widths grow with zoom so roads stay visible over the basemap's own roads at any scale.
const width = (base) => ["interpolate", ["linear"], ["zoom"], 10, base, 14, base * 2.5, 17, base * 6];

async function getJSON(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${url} returned ${response.status}`);
  return response.json();
}

const locations = await getJSON("/api/locations");
const metas = await Promise.all(
  locations.map((l) => getJSON(`/api/meta?location=${encodeURIComponent(l.slug)}`)),
);

const map = new maplibregl.Map({
  container: mapEl,
  style: mapEl.dataset.basemapStyle,
  bounds: locations[0]?.bbox ?? [-123.3, 48.9, -122.0, 49.4], // fall back to the Lower Mainland
  fitBoundsOptions: { padding: PADDING },
  interactive: false,
  attributionControl: { compact: true },
});

map.on("load", () => {
  map.addImage("hatch", hatchPattern(token("--unobserved")), { pixelRatio: 2 });
  // Draw above the basemap's roads but under its labels, so street and place names stay readable.
  // Some styles (Dark) have a label layer below their roads, so start after the last non-label layer.
  const layers = map.getStyle().layers;
  const lastShape = layers.findLastIndex((layer) => layer.type !== "symbol");
  const beforeLabels = layers.slice(lastShape + 1).find((layer) => layer.type === "symbol")?.id;

  map.addSource("unobserved", { type: "geojson", data: unobservedArea(metas) });
  map.addLayer(
    {
      id: "unobserved",
      type: "fill",
      source: "unobserved",
      paint: { "fill-pattern": "hatch" },
    },
    beforeLabels,
  );
  map.addLayer(
    {
      id: "footprint-edge",
      type: "line",
      source: "unobserved",
      paint: { "line-color": token("--text-muted"), "line-width": 1 },
    },
    beforeLabels,
  );

  map.addSource("segments", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  const roads = [
    // Flooded is added last so it draws on top where segments meet.
    { status: "clear", color: token("--road-clear"), width: 1.5 },
    { status: "no_data", color: token("--road-nodata"), width: 1.5, dash: [2, 1.5] },
    { status: "flooded", color: token("--flood"), width: 2.5 },
  ];
  for (const road of roads) {
    map.addLayer(
      {
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
      },
      beforeLabels,
    );
  }

  loadView();
  map.on("moveend", loadView);
  if (input.value.trim()) search(input.value);
});

// Fetch the segments for what's on screen and update the notice.
let latestRequest = 0;
async function loadView() {
  const view = map.getBounds().toArray().flat(); // [west, south, east, north]
  const request = ++latestRequest;
  const segments = await getJSON(`/api/flood?bbox=${view.join(",")}`);
  if (request !== latestRequest) return; // a newer move already started
  map.getSource("segments").setData(segments);

  const inView = metas.filter((m) => intersects(view, m.bbox));
  if (inView.length === 0) showNotice("No flood data for this area");
  else if (inView.some((m) => m.synthetic)) showNotice("Simulated data, not a real satellite observation");
  else showNotice("");
}

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
  history.replaceState(null, "", `?q=${encodeURIComponent(query)}`);
  map.fitBounds(results[0].bbox, { padding: PADDING, maxZoom: 16, duration: reduceMotion ? 0 : 800 });
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  search(input.value);
});

function showSearchMessage(text) {
  searchMessage.textContent = text;
  searchMessage.hidden = !text;
}

function showNotice(text) {
  notice.textContent = text;
  notice.hidden = !text;
}

function intersects(a, b) {
  return a[0] <= b[2] && b[0] <= a[2] && a[1] <= b[3] && b[1] <= a[3];
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
