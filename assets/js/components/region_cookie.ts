// The cookie holding the navbar region choice, which
// regions.context_processors.current_region reads back on every render.
// Its own module because two unrelated surfaces write it — the navbar
// selector and the global homepage's region picker (cards and map pins)
// — and a mismatch in path or max-age between them would strand a
// selection.

const COOKIE_MAX_AGE_SECONDS = 60 * 60 * 24 * 365;

/**
 * Persist a region's Wikidata QID, or clear the cookie when given null
 * (= global).
 * The name comes from the server (regions.context_processors), never
 * hardcoded here.
 */
export function writeRegionCookie(name: string, qid: string | null): void {
  if (qid) {
    document.cookie = `${name}=${encodeURIComponent(qid)}; path=/; max-age=${COOKIE_MAX_AGE_SECONDS}; SameSite=Lax`;
  } else {
    document.cookie = `${name}=; path=/; max-age=0; SameSite=Lax`;
  }
}
