/* Client-side routing over the History API.
 *
 * The service serves index.html for any non-API path (the SPA fallback in
 * app.py), so every path here is a real, linkable, reloadable URL. Navigation
 * inside the app pushes a new entry and re-renders from `usePath`; nothing is
 * fetched from the server to change screens. */
import { useEffect, useState } from 'react';

const EVENT = 'vc:navigate';

export function navigate(to: string, options: { replace?: boolean; keepScroll?: boolean } = {}) {
  const here = window.location.pathname + window.location.search;
  if (to === here) return;
  if (options.replace) window.history.replaceState({}, '', to);
  else window.history.pushState({}, '', to);
  window.dispatchEvent(new Event(EVENT));
  if (!options.keepScroll && document.scrollingElement) document.scrollingElement.scrollTop = 0;
}

/* The current path and query, re-rendered on every navigation (ours or the
 * browser's back and forward buttons). */
export function useLocation(): { path: string; search: URLSearchParams } {
  const read = () => window.location.pathname + window.location.search;
  const [href, setHref] = useState(read);
  useEffect(() => {
    const update = () => setHref(read());
    window.addEventListener('popstate', update);
    window.addEventListener(EVENT, update);
    return () => {
      window.removeEventListener('popstate', update);
      window.removeEventListener(EVENT, update);
    };
  }, []);
  const [path, query = ''] = href.split('?');
  return { path, search: new URLSearchParams(query) };
}

