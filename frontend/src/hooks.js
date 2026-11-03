import { useEffect, useState } from 'react'

export function useLoad(fn, deps = []) {
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const load = async () => { setBusy(true); setErr(''); try { setData(await fn()) } catch (e) { setErr(e.message) } finally { setBusy(false) } }
  useEffect(() => { load() }, deps) // eslint-disable-line
  return { data, err, busy, reload: load, setData }
}
