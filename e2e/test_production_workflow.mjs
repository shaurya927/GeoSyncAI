import { chromium } from 'playwright'
import { readFile } from 'node:fs/promises'

const baseUrl = process.env.BASE_URL || 'http://127.0.0.1:5173'
const browser = await chromium.launch({ headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage', '--use-gl=angle', '--use-angle=swiftshader'] })
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, acceptDownloads: true })
const errors = []
page.on('pageerror', error => errors.push(String(error)))

const geometry = { type: 'Polygon', coordinates: [[[73, 20], [73.001, 20], [73.001, 20.001], [73, 20.001], [73, 20]]] }
const body = Buffer.from(JSON.stringify({
  type: 'FeatureCollection',
  features: [{ type: 'Feature', id: '0001', properties: { parcel_id: '0001', village_code: 'TEST' }, geometry }],
}))

await page.goto(baseUrl, { waitUntil: 'networkidle' })
await page.getByLabel('Username', { exact: true }).fill('admin')
await page.getByLabel('Password', { exact: true }).fill('admin')
await page.getByRole('button', { name: 'Sign in to workspace' }).click()
await page.getByLabel('Project name').fill(`Browser acceptance ${Date.now()}`)
await page.getByRole('button', { name: 'Create project', exact: true }).click()
await page.getByRole('button', { name: 'Datasets', exact: true }).click()

for (const [name, date] of [['baseline.geojson', '2024-01-01'], ['comparison.geojson', '2025-01-01']]) {
  await page.getByLabel('Dataset file').setInputFiles({ name, mimeType: 'application/geo+json', buffer: body })
  await page.getByLabel('Capture date', { exact: true }).fill(date)
  await page.getByLabel('Source organization').fill('Synthetic browser acceptance')
  await page.getByRole('button', { name: 'Upload dataset', exact: true }).click()
  await page.getByRole('heading', { name, exact: true }).waitFor()
  const datasetSection = page.locator('section').filter({ has: page.getByRole('heading', { name, exact: true }) })
  await datasetSection.getByLabel('Source CRS', { exact: true }).fill('EPSG:4326')
  await datasetSection.getByLabel('CRS rationale').fill('Test fixture documented longitude/latitude')
  await datasetSection.getByRole('button', { name: 'Confirm CRS and reprocess', exact: true }).click()
  await page.locator('main.page-content').waitFor({ state: 'visible' })
  await datasetSection.getByLabel('Map parcel_id').selectOption('parcel_id')
  await datasetSection.getByLabel('Map village_code').selectOption('village_code')
  await datasetSection.getByRole('button', { name: 'Confirm mapping', exact: true }).click()
  await page.getByRole('heading', { name: /Schema mapping · confirmed v1/ }).waitFor()
}

await page.getByLabel('Baseline', { exact: true }).selectOption({ label: 'baseline.geojson · 2024-01-01' })
await page.getByLabel('Comparison', { exact: true }).selectOption({ label: 'comparison.geojson · 2025-01-01' })
await page.getByRole('button', { name: 'Generate matches' }).click()
await page.getByText('Processing completed', { exact: true }).waitFor({ timeout: 30000 })
await page.getByRole('button', { name: 'Review queue', exact: true }).click()
const canvas = page.locator('.live-map canvas')
await canvas.waitFor()
const bounds = await canvas.boundingBox()
if (!bounds) throw new Error('map canvas has no bounds')
await canvas.click({ position: { x: bounds.width / 2, y: bounds.height / 2 } })
await page.getByRole('heading', { name: 'Parcel evidence card', exact: true }).waitFor()
await page.getByLabel('Review rationale').fill('Verified identity only; baseline selected separately')
await page.getByRole('button', { name: 'accepted', exact: true }).click()
await page.getByText('Review decision saved', { exact: true }).waitFor()
await page.getByLabel('Geometry source', { exact: true }).selectOption({ index: 1 })
await page.getByLabel('Attribute source', { exact: true }).selectOption({ index: 1 })
await page.getByLabel('Selection rationale').fill('Explicit reviewed baseline source')
await page.getByRole('button', { name: 'Approve baseline selection' }).click()
await page.getByRole('button', { name: 'Versions & history', exact: true }).click()
await page.getByRole('button', { name: 'Run validation', exact: true }).click()
await page.getByRole('heading', { name: 'Validation: passed', exact: true }).waitFor()
await page.getByRole('button', { name: 'Publish version', exact: true }).click()
await page.getByRole('heading', { name: 'Version 1', exact: true }).waitFor()

const downloadPromise = page.waitForEvent('download')
await page.getByLabel('Export', { exact: true }).selectOption('geojson')
const download = await downloadPromise
const document = JSON.parse(await readFile(await download.path(), 'utf8'))
if (document.features?.length !== 1) throw new Error('expected one published feature')
if (document.features[0].properties?._lineage?.sources?.length !== 2) throw new Error('expected two lineage sources')

await page.getByRole('button', { name: 'Refresh', exact: true }).click()
await page.getByRole('heading', { name: 'Version 1', exact: true }).waitFor()
await page.setViewportSize({ width: 390, height: 844 })
if (await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)) throw new Error('horizontal overflow')
if (errors.length) throw new Error(JSON.stringify(errors))
console.log(JSON.stringify({ baseUrl, workflow: 'upload-publish-export-refresh', features: document.features.length, lineageSources: document.features[0].properties._lineage.sources.length, responsive: true, pageErrors: errors }))
await browser.close()
