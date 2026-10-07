import { chromium } from 'playwright'

const baseUrl = process.env.BASE_URL || 'http://127.0.0.1:5173'
const browser = await chromium.launch({ headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage', '--use-gl=angle', '--use-angle=swiftshader'] })
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
const errors = []
page.on('pageerror', error => errors.push(String(error)))
await page.goto(baseUrl, { waitUntil: 'networkidle' })
await page.getByLabel('Username', { exact: true }).fill('admin')
await page.getByLabel('Password', { exact: true }).fill('admin')
await page.getByRole('button', { name: 'Sign in to workspace' }).click()
await page.getByRole('button', { name: 'Datasets', exact: true }).waitFor()
for (const view of ['Boundary editor', 'Read-only queries', 'Compliance screening']) {
  await page.getByRole('button', { name: view, exact: true }).click()
  await page.locator('h1', { hasText: view === 'Read-only queries' ? 'Read-only query workspace' : view }).waitFor()
}
await page.setViewportSize({ width: 390, height: 844 })
if (await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)) throw new Error('horizontal overflow')
if (errors.length) throw new Error(JSON.stringify(errors))
console.log(JSON.stringify({ baseUrl, responsive: true, pageErrors: errors }))
await browser.close()
