/* Reading and writing the times on the Schedule screen (ticket 11).

 * Separate from the screen because these are the assertions worth making
 * directly: what the browser sends is a LOCAL wall time plus its zone name,
 * and what the screen prints is the wall time the Schedule was written in —
 * never one re-derived from the instant, which would silently restate the
 * promise in the viewer's own zone.
 */
import type { Schedule } from './types';

export function browserTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  } catch {
    return 'UTC';
  }
}

/* `YYYY-MM-DDTHH:MM` in the browser's own clock — the shape <input
 * type="datetime-local"> wants, and the shape the API reads as a local time. */
export function localInputValue(when: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0');
  return (
    `${when.getFullYear()}-${pad(when.getMonth() + 1)}-${pad(when.getDate())}` +
    `T${pad(when.getHours())}:${pad(when.getMinutes())}`
  );
}

export function defaultWhen(): string {
  const soon = new Date(Date.now() + 15 * 60 * 1000);
  soon.setSeconds(0, 0);
  soon.setMinutes(Math.ceil(soon.getMinutes() / 5) * 5);
  return localInputValue(soon);
}

/* The wall clock the Schedule was written in, printed as it was written. */
export function formatLocalTime(schedule: Schedule): string {
  const raw = schedule.local_time;
  if (!raw) return schedule.due_at || 'Unknown time';
  const [datePart, timePart = ''] = raw.split('T');
  const [y, m, d] = datePart.split('-').map(Number);
  if (!y || !m || !d) return raw;
  const asUtc = new Date(Date.UTC(y, m - 1, d));
  const day = asUtc.toLocaleDateString(undefined, {
    weekday: 'short',
    day: 'numeric',
    month: 'short',
    year: 'numeric',
    timeZone: 'UTC',
  });
  return `${day}, ${timePart.slice(0, 5)}`;
}

/* The same instant on the viewer's own clock, when that is a different zone.
 * Shown as well as the promised time, never instead of it. */
export function formatViewerTime(schedule: Schedule): string | null {
  if (!schedule.due_at) return null;
  if (browserTimeZone() === schedule.timezone) return null;
  const instant = new Date(schedule.due_at);
  if (Number.isNaN(instant.getTime())) return null;
  return instant.toLocaleString(undefined, {
    weekday: 'short',
    day: 'numeric',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
  });
}
