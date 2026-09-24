/* Shared vocabulary for every screen: how absence is said, how times and
 * durations are printed, and the human names of Outlets. One copy, so two
 * screens cannot describe the same fact in two ways. The components that
 * render this vocabulary are in ui.tsx. */
import type { Direction } from './types';

/* The one rendering of "nothing was recorded". Every screen element that can be
 * empty routes through it, so the screen has a single vocabulary for absence and
 * can never be read as a claim. */
export const NOT_RETAINED = 'Not retained';

export const DIRECTIONS: Direction[] = ['inbound', 'outbound'];

const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;

/* A retained stamp that carries only a date (no clock time was stored). */
export function isDateOnly(isoString?: string, precision?: string | null): boolean {
  return precision === 'date' || (!precision && !!isoString && DATE_ONLY.test(isoString));
}

function dateOnlyAsUtc(isoString: string): Date {
  const [y, m, d] = isoString.slice(0, 10).split('-').map(Number);
  return new Date(Date.UTC(y, m - 1, d));
}

/* `precision` is what the store actually held. When only `metadata.date` was
 * retained there is no clock time for that call, and printing one (new Date()
 * reads a bare date as UTC midnight) would invent it. Such a stamp is rendered
 * as a date: parsed and printed in UTC so the date cannot shift a day. */
export function formatDate(isoString?: string, precision?: string | null): string {
  if (!isoString) return 'Unknown date';
  try {
    if (isDateOnly(isoString, precision)) {
      const utc = dateOnlyAsUtc(isoString);
      if (isNaN(utc.getTime())) return isoString;
      return utc.toLocaleDateString(undefined, { dateStyle: 'medium', timeZone: 'UTC' });
    }
    const d = new Date(isoString);
    if (isNaN(d.getTime())) return isoString;
    return d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
  } catch {
    return isoString;
  }
}

/* The clock time alone, for a row that already sits under its day heading.
 * A date-only stamp has no clock time, so it gets none. */
export function formatClock(isoString?: string, precision?: string | null): string | null {
  if (!isoString || isDateOnly(isoString, precision)) return null;
  const d = new Date(isoString);
  if (isNaN(d.getTime())) return null;
  return d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
}

/* The local calendar day a stamp falls on, as a sortable key, or null when the
 * stamp cannot be placed on a day at all. */
export function dayKey(isoString?: string, precision?: string | null): string | null {
  if (!isoString) return null;
  if (isDateOnly(isoString, precision)) return isoString.slice(0, 10);
  const d = new Date(isoString);
  if (isNaN(d.getTime())) return null;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

export function todayKey(offsetDays = 0): string {
  const d = new Date();
  d.setDate(d.getDate() + offsetDays);
  return dayKey(d.toISOString()) as string;
}

/* "Today", "Yesterday", "Tomorrow", then a date. */
export function dayLabel(key: string | null): string {
  if (key === null) return 'Unknown date';
  if (key === todayKey(0)) return 'Today';
  if (key === todayKey(-1)) return 'Yesterday';
  if (key === todayKey(1)) return 'Tomorrow';
  const utc = dateOnlyAsUtc(key);
  if (isNaN(utc.getTime())) return key;
  // Always with the year: the archive spans years, and a heading without one
  // would leave the reader to guess which August this was.
  return utc.toLocaleDateString(undefined, {
    weekday: 'long',
    day: 'numeric',
    month: 'long',
    year: 'numeric',
    timeZone: 'UTC',
  });
}

/* Seconds -> what a human reads. `null`/`undefined` is NOT zero: every call
 * retained before ticket 05 has no duration at all, and "0s" would say those
 * calls lasted no time. Returns null so the caller renders NOT_RETAINED. */
export function formatDuration(seconds?: number | null): string | null {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return null;
  if (seconds < 0) return null;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const mins = Math.floor(seconds / 60);
  const rest = Math.round(seconds - mins * 60);
  return `${mins}m ${String(rest).padStart(2, '0')}s`;
}

/* m:ss for a media clock. */
export function formatClockTime(seconds: number): string {
  if (!isFinite(seconds) || seconds < 0) return '0:00';
  const total = Math.floor(seconds);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
}

/* "in 2h 10m", "in 3d", "5m ago" between now and an instant. */
export function relativeTo(isoString: string, now: Date = new Date()): string | null {
  const then = new Date(isoString);
  if (isNaN(then.getTime())) return null;
  const diff = then.getTime() - now.getTime();
  const abs = Math.abs(diff);
  const minutes = Math.round(abs / 60000);
  let text: string;
  if (minutes < 1) text = 'less than a minute';
  else if (minutes < 60) text = `${minutes}m`;
  else if (minutes < 60 * 24) {
    const h = Math.floor(minutes / 60);
    const m = minutes % 60;
    text = m ? `${h}h ${m}m` : `${h}h`;
  } else text = `${Math.round(minutes / (60 * 24))}d`;
  return diff >= 0 ? `in ${text}` : `${text} ago`;
}

export function truncate(text: string, max: number): string {
  return text.length > max ? text.substring(0, max) + '...' : text;
}

/* Ticket 06: a Call with no summary must not read like a Call with an empty one.
 *
 * The producer never stores an empty or placeholder summary (see
 * services/voicecore/summary.py), so an absent summary is always one of THREE
 * distinct facts, and the screen says which:
 *
 *   nothing_to_summarise - the call held no conversation to describe. A fact about
 *                          the call, like an inbound call having no Mission.
 *   unavailable          - the Agent was asked and could not answer. A gap.
 *   (no state at all)    - nobody was asked: summarisation was off for that lane,
 *                          or the call predates this ticket. NOT_RETAINED.
 *
 * There is deliberately no fourth "still being written" state and no spinner: the
 * summary is settled before the call's document is written, so a Call the screen
 * can see is a Call whose summary question is already closed. */
const SUMMARY_ABSENCE: Record<string, string> = {
  nothing_to_summarise: 'No conversation to summarise',
  unavailable: 'Summary could not be written',
};

const SUMMARY_ABSENCE_TITLE: Record<string, string> = {
  nothing_to_summarise:
    'Nobody said enough on this call to describe. No summary was requested, and none was invented.',
  unavailable:
    'The Agent that was on this call was asked for a summary and could not answer. The call, its transcript and its recording were retained in full.',
};

export function summaryAbsence(state?: string | null): { text: string; title: string } {
  if (!state) {
    return { text: NOT_RETAINED, title: 'No summary was requested for this call.' };
  }
  return {
    text: SUMMARY_ABSENCE[state] ?? `No summary (${state})`,
    title: SUMMARY_ABSENCE_TITLE[state] ?? `Recorded summary state: ${state}`,
  };
}

/* Presentation only. WHICH Outlets exist comes from the API
 * (`active.outlet_order`, i.e. `profiles.OUTLETS`); this map just gives the two
 * that exist today a human name. An Outlet with no entry renders under its own
 * id, so a third Outlet added to the model appears without a frontend change
 * rather than silently going missing. */
const OUTLET_COPY: Record<string, { label: string; short: string; blurb: string }> = {
  phone: { label: 'Phone number', short: 'Phone', blurb: 'The Twilio line' },
  talk: { label: 'Nextcloud Talk', short: 'Talk', blurb: 'Talk rooms and calls' },
};

export function outletCopy(outlet: string) {
  return OUTLET_COPY[outlet] || { label: outlet, short: outlet, blurb: 'Outlet' };
}


export type Tone = 'neutral' | 'accent' | 'danger' | 'warning' | 'info' | 'caller';
