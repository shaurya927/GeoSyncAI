import { useCallback, useEffect, useState } from 'react'
import * as w from './workflowApi'

type Account = { id: string; username: string; role: string; is_active: boolean; auth_version: number }
type Traffic = { backend: string; response_status_counts: Record<string, number>; active_requests_this_process: number; active_heavy_requests_this_process: number; limits: Record<string, number | boolean> }
const roles = ['viewer', 'processor', 'reviewer', 'steward', 'field', 'citizen', 'admin']

export default function SecurityPanel({ user, offline, run, onPasswordChanged }: {
  user: w.User; offline: boolean; run: (action: () => Promise<void>) => void; onPasswordChanged: () => Promise<void>
}) {
  const [accounts, setAccounts] = useState<Account[]>([])
  const [traffic, setTraffic] = useState<Traffic>()
  const [username, setUsername] = useState(''), [password, setPassword] = useState(''), [role, setRole] = useState('viewer')
  const [currentPassword, setCurrentPassword] = useState(''), [newPassword, setNewPassword] = useState('')
  const [rationale, setRationale] = useState(''), [message, setMessage] = useState('')
  const [edits, setEdits] = useState<Record<string, { role: string; is_active: boolean }>>({})
  const load = useCallback(async () => {
    if (user.role !== 'admin' || offline) return
    const [users, statistics] = await Promise.all([w.get<Account[]>('/admin/users'), w.get<Traffic>('/admin/security/traffic')])
    setAccounts(users); setTraffic(statistics); setEdits({})
  }, [user.role, offline])
  useEffect(() => { run(load) }, [load, run])
  const count = Object.values(traffic?.response_status_counts || {}).reduce((sum, value) => sum + value, 0)
  return <><h1>Account &amp; security</h1>{message && <p role="status">{message}</p>}
    <form className="panel inspector" onSubmit={e => { e.preventDefault(); run(async () => {
      await w.post('/auth/password', { current_password: currentPassword, new_password: newPassword })
      setCurrentPassword(''); setNewPassword(''); await onPasswordChanged()
    }) }}><h2>Change your passphrase</h2><p>Use at least 15 characters. Changing it revokes every active session. Device field drafts stay retained.</p>
      <label>Current passphrase<input required type="password" autoComplete="current-password" value={currentPassword} onChange={e => setCurrentPassword(e.target.value)} /></label>
      <label>New passphrase<input required type="password" autoComplete="new-password" minLength={15} maxLength={128} value={newPassword} onChange={e => setNewPassword(e.target.value)} /></label>
      <button className="button button-primary" disabled={offline}>Change passphrase and revoke sessions</button>
      {offline && <p>Reconnect before changing account security.</p>}</form>
    {user.role === 'admin' && !offline && <><section className="panel inspector"><h2>Traffic protection</h2>
      <button className="button button-secondary" onClick={() => run(load)}>Refresh security</button>
      <p>Counter backend: {traffic?.backend || 'Loading'} · Active requests on this process: {traffic?.active_requests_this_process ?? '—'} · Heavy requests: {traffic?.active_heavy_requests_this_process ?? '—'}</p>
      <p>Response counters are retained by this process or shared Redis store; they are not a capacity benchmark.</p>
      {Object.entries(traffic?.response_status_counts || {}).sort().map(([status, value]) => <div className="security-traffic-row" key={status}><span>HTTP {status}</span><progress max={Math.max(1, count)} value={value} /><strong>{value}</strong></div>)}
      <details><summary>Configured request limits</summary><pre className="json-preview">{JSON.stringify(traffic?.limits || {}, null, 2)}</pre></details></section>
      <form className="panel inspector" onSubmit={e => { e.preventDefault(); run(async () => {
        await w.post('/admin/users', { username, password, role }); setPassword(''); setUsername(''); await load(); setMessage('Account created. Assign project permissions separately in the project workspace.')
      }) }}><h2>Create an account</h2><label>New username<input required pattern="[a-zA-Z0-9_.-]+" minLength={3} maxLength={80} value={username} onChange={e => setUsername(e.target.value)} /></label>
        <label>Initial passphrase<input required type="password" autoComplete="new-password" minLength={15} maxLength={128} value={password} onChange={e => setPassword(e.target.value)} /></label>
        <label>New account role<select value={role} onChange={e => setRole(e.target.value)}>{roles.map(value => <option key={value}>{value}</option>)}</select></label>
        <button className="button button-primary">Create secure account</button></form>
      <section className="panel inspector"><h2>Account access and sessions</h2><label>Account change rationale<input value={rationale} onChange={e => setRationale(e.target.value)} maxLength={500} /></label>
        {accounts.map(account => { const edit = edits[account.id] || account; const own = account.id === user.id
          return <div className="security-account-row" key={account.id}><strong>{account.username}{own ? ' (you)' : ''}</strong>
            <label>Role<select aria-label={`Role for ${account.username}`} disabled={own} value={edit.role} onChange={e => setEdits({ ...edits, [account.id]: { ...edit, role: e.target.value } })}>{roles.map(value => <option key={value}>{value}</option>)}</select></label>
            <label><input type="checkbox" aria-label={`Active account ${account.username}`} disabled={own} checked={edit.is_active} onChange={e => setEdits({ ...edits, [account.id]: { ...edit, is_active: e.target.checked } })} /> Active</label>
            <button className="button button-secondary" aria-label={`Save access for ${account.username}`} disabled={own || !rationale.trim() || !edits[account.id]} onClick={() => run(async () => { await w.patch(`/admin/users/${account.id}`, { ...edit, expected_auth_version: account.auth_version, rationale }); await load(); setMessage('Access updated and previous sessions revoked.') })}>Save access</button>
            <button className="button button-secondary" aria-label={`Revoke sessions for ${account.username}`} disabled={own} onClick={() => run(async () => { await w.post(`/admin/users/${account.id}/revoke-sessions`); await load(); setMessage('All sessions for the selected account revoked.') })}>Revoke sessions</button></div>
        })}</section></>}
  </>
}
