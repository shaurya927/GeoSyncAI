const cartoKey = import.meta.env.VITE_CARTO_BASEMAP_KEY?.trim()
const osmAttribution = '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'

// CARTO now returns an API-key watermark for anonymous tile requests. Use the
// standard OSM service for interactive viewing unless a browser key is supplied.
export const BASEMAP = cartoKey ? {
  name: 'CARTO',
  tileUrl: `https://basemaps.cartocdn.com/rastertiles/light_all/{z}/{x}/{y}.png?key=${encodeURIComponent(cartoKey)}`,
  host: 'basemaps.cartocdn.com',
  maxzoom: 20,
  attribution: `${osmAttribution} · © <a href="https://carto.com/attribution/">CARTO</a>`,
} : {
  name: 'OpenStreetMap',
  tileUrl: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
  host: 'tile.openstreetmap.org',
  maxzoom: 19,
  attribution: osmAttribution,
}
