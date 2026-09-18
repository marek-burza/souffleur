/**
 * lib/isolate.ts
 *
 * Cross-origin isolation for a site that cannot send its own headers, which is
 * what unlocks multi-threaded WASM. See the README.
 */

const ATTEMPTED = 'souffleur.isolation-attempted'
const TIMEOUT_MS = 3000

let reloadable = true

export async function requestIsolation (): Promise<boolean> {
  if (globalThis.crossOriginIsolated) {
    return true
  }
  if (!('serviceWorker' in navigator) || attempted()) {
    return false
  }

  try {
    const registration = await withTimeout(
      navigator.serviceWorker.register(`${import.meta.env.BASE_URL}coi-serviceworker.js`),
    )
    await withTimeout(navigator.serviceWorker.ready)
    if (!registration?.active) {
      return false
    }
    remember()
    if (reloadable) {
      globalThis.location.reload()
    }
  } catch {
    remember()
  }
  return false
}

async function withTimeout<T> (work: Promise<T>): Promise<T | undefined> {
  let timer: ReturnType<typeof setTimeout> | undefined
  try {
    return await Promise.race([
      work,
      new Promise<undefined>(resolve => {
        timer = setTimeout(() => resolve(undefined), TIMEOUT_MS)
      }),
    ])
  } finally {
    clearTimeout(timer)
  }
}

function attempted (): boolean {
  try {
    return sessionStorage.getItem(ATTEMPTED) !== null
  } catch {
    return true
  }
}

function remember () {
  try {
    sessionStorage.setItem(ATTEMPTED, '1')
  } catch {
    reloadable = false
  }
}
