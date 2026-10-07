export function accessToken(): string {
  try { return sessionStorage.getItem('geosyncai_token') || localStorage.getItem('geosyncai_token') || '' }
  catch { return '' }
}

export function clearAccessToken() {
  for (const storage of [sessionStorage, localStorage]) {
    try { storage.removeItem('geosyncai_token') } catch { /* inaccessible storage cannot authorize a request */ }
  }
}

export function storeAccessToken(token: string, role: string) {
  clearAccessToken()
  // Field accounts require persistent credentials for bounded offline reload.
  // Ordinary accounts keep credentials in this tab's session storage.
  const storage = role === 'field' ? localStorage : sessionStorage
  storage.setItem('geosyncai_token', token)
}
