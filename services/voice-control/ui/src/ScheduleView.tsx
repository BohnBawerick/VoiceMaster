/* The Schedule screen (ticket 11), list first (S2, S3).
 *
 * A Schedule is a Call that has not happened yet: a time, an Agent, a target
 * and a Mission. This screen lists what is coming and what already happened,
 * and cancels one. Writing one is the New call form with Later selected
 * (/schedule/new). When the time comes the app itself places it, through the
 * same placement the Call now button uses; this page does not have to be open.
 *
 * The time shown is the wall clock the Schedule was written in, with its zone
 * named, because that is the promise ("3pm on Tuesday"). When the browser is
 * somewhere else, the viewer's own time is shown underneath rather than
 * instead: replacing it would quietly redefine the promise.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { AlertTriangle, CalendarClock, ChevronRight, Plus, Trash2, XCircle } from 'lucide-react';
import { cancelSchedule, fetchSchedules } from './api';
import { dayLabel, relativeTo } from './format';
import { DirectionIcon, Pill } from './ui';
import type { Tone } from './format';
import { Link } from './Link';
import { formatLocalTime, formatViewerTime } from './scheduleTime';
import { PageHeader } from './Shell';
import type { Schedule } from './types';

const REFRESH_MS = 10000;

const STATUS_TEXT: Record<string, string> = {
  pending: 'Upcoming',
  placed: 'Placed',
  failed: 'Failed',
  cancelled: 'Cancelled',
};

const STATUS_TONE: Record<string, Tone> = {
  pending: 'info',
  placed: 'accent',
  failed: 'danger',
  cancelled: 'neutral',
};

/* Group by the day of the promised wall clock, in the order given. */
function groupByDay(list: Schedule[]): { key: string; items: Schedule[] }[] {
  const groups: { key: string; items: Schedule[] }[] = [];
  for (const item of list) {
    const key = (item.local_time || item.due_at || '').slice(0, 10);
    const last = groups[groups.length - 1];
    if (last && last.key === key) last.items.push(item);
    else groups.push({ key, items: [item] });
  }
  return groups;
}

function DateLeaf({ schedule }: { schedule: Schedule }) {
  const raw = (schedule.local_time || '').slice(0, 10);
  const [y, m, d] = raw.split('-').map(Number);
  if (!y || !m || !d) return <span className="date-leaf" />;
  const month = new Date(Date.UTC(y, m - 1, d)).toLocaleDateString(undefined, { month: 'short', timeZone: 'UTC' });
  return (
    <span className="date-leaf" aria-hidden="true">
      <span className="date-leaf-month">{month}</span>
      <span className="date-leaf-day mono-num">{d}</span>
    </span>
  );
}

function ScheduleRow({ schedule, onCancel }: { schedule: Schedule; onCancel?: (schedule: Schedule) => void }) {
  const viewerTime = formatViewerTime(schedule);
  const relative = relativeTo(schedule.due_at);
  return (
    <div className={`schedule-row schedule-row-${schedule.status}`} data-testid={`schedule-row-${schedule.id}`}>
      <DateLeaf schedule={schedule} />

      <div className="schedule-row-what">
        <div className="schedule-row-when">
          <span className="schedule-when-main mono-num" data-testid={`schedule-when-${schedule.id}`}>
            {formatLocalTime(schedule)}
          </span>
          <span className="schedule-when-zone mono">{schedule.timezone}</span>
          {schedule.status === 'pending' && relative && <span className="schedule-when-relative">{relative}</span>}
        </div>
        {viewerTime && <span className="schedule-when-viewer">your time: {viewerTime}</span>}
        <span className="schedule-who">
          <DirectionIcon direction="outbound" size={14} /> <strong>{schedule.agent}</strong> calls{' '}
          <span className="mono-num">{schedule.target_display || schedule.to}</span>
        </span>
        <span className="schedule-mission" title={schedule.mission}>
          {schedule.mission}
        </span>
        {schedule.reason && (
          <span className="schedule-reason" data-testid={`schedule-reason-${schedule.id}`}>
            {schedule.reason}
          </span>
        )}
        {schedule.call_id && (
          /* Whether anyone picked up is the Call's outcome, not the Schedule's
           * (ticket 11), so a placed Schedule links to the Call itself. */
          <Link
            className="inline-link"
            href={`/calls/${encodeURIComponent(schedule.call_id)}`}
            data-testid={`schedule-call-${schedule.id}`}
          >
            Open the Call <span className="mono">{schedule.call_id}</span> <ChevronRight size={13} />
          </Link>
        )}
      </div>

      <div className="schedule-row-status">
        <Pill
          tone={STATUS_TONE[schedule.status] || 'neutral'}
          icon={schedule.status === 'failed' ? <XCircle size={12} /> : undefined}
          testId={`schedule-status-${schedule.id}`}
        >
          {STATUS_TEXT[schedule.status] || schedule.status}
        </Pill>
        {onCancel && (
          <button
            type="button"
            className="btn btn-danger-ghost btn-sm schedule-cancel"
            data-testid={`schedule-cancel-${schedule.id}`}
            onClick={() => onCancel(schedule)}
          >
            <Trash2 size={13} /> Cancel
          </button>
        )}
      </div>
    </div>
  );
}

export function ScheduleView() {
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<'upcoming' | 'past'>('upcoming');

  const reload = useCallback(async () => {
    const payload = await fetchSchedules();
    setSchedules(payload.schedules);
  }, []);

  useEffect(() => {
    let alive = true;
    fetchSchedules()
      .then((payload) => {
        if (alive) setSchedules(payload.schedules);
      })
      .catch((err: Error) => {
        if (alive) setLoadError(err.message);
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, []);

  /* A Schedule fires whether or not anyone is looking, so an open screen has to
   * keep asking — otherwise it shows a Call as upcoming minutes after it rang. */
  useEffect(() => {
    const timer = setInterval(() => {
      reload().catch(() => undefined);
    }, REFRESH_MS);
    return () => clearInterval(timer);
  }, [reload]);

  const upcoming = useMemo(
    () => schedules.filter((s) => s.status === 'pending').sort((a, b) => a.due_at.localeCompare(b.due_at)),
    [schedules]
  );
  const settled = useMemo(
    () =>
      schedules
        .filter((s) => s.status !== 'pending')
        .sort((a, b) => (b.settled_at || b.due_at).localeCompare(a.settled_at || a.due_at)),
    [schedules]
  );

  /* Cancelling races the scheduler for the same claim, so a refusal is a real
   * answer: the Call is already being placed, and the row says what it became. */
  const onCancel = async (schedule: Schedule) => {
    setError(null);
    try {
      await cancelSchedule(schedule.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not cancel it.');
    }
    await reload().catch(() => undefined);
  };

  if (loading) return <div className="spinner" />;

  if (loadError) {
    return (
      <div className="page">
        <div className="alert-banner alert-unreachable">
          <AlertTriangle className="alert-icon" />
          <div>
            <strong>Could not load the Schedule</strong>
            <div>{loadError}</div>
          </div>
        </div>
      </div>
    );
  }

  const next = upcoming[0];
  const list = tab === 'upcoming' ? upcoming : settled;

  return (
    <div className="page schedule-screen" data-testid="schedule">
      <PageHeader
        title="Schedule"
        icon={<CalendarClock size={20} />}
        lede="Calls that have not happened yet. At its time each one is placed exactly as Call now places it: one attempt, no retries."
        actions={
          <Link className="btn btn-primary" href="/schedule/new" data-testid="schedule-new">
            <Plus size={16} /> Schedule a call
          </Link>
        }
      />

      {next && (
        <div className="next-banner" data-testid="schedule-next">
          <span className="next-banner-icon">
            <CalendarClock size={18} />
          </span>
          <div className="next-banner-text">
            <span className="next-banner-when">
              Next call {relativeTo(next.due_at) ?? ''}
              <span className="muted mono-num"> · {formatLocalTime(next)}</span>
            </span>
            <span>
              <strong>{next.agent}</strong> calls <span className="mono-num">{next.target_display || next.to}</span>:{' '}
              <span className="muted">{next.mission}</span>
            </span>
          </div>
        </div>
      )}

      {error && (
        <div className="alert-banner alert-unreachable" data-testid="schedule-error">
          <AlertTriangle className="alert-icon" />
          <div>
            <strong>Not cancelled</strong>
            <div>{error}</div>
          </div>
        </div>
      )}

      <div className="tabs" role="tablist" aria-label="Schedules">
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'upcoming'}
          className={'tab' + (tab === 'upcoming' ? ' active' : '')}
          data-testid="schedule-tab-upcoming"
          onClick={() => setTab('upcoming')}
        >
          Upcoming <span className="seg-count mono-num">{upcoming.length}</span>
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'past'}
          className={'tab' + (tab === 'past' ? ' active' : '')}
          data-testid="schedule-tab-past"
          onClick={() => setTab('past')}
        >
          Past <span className="seg-count mono-num">{settled.length}</span>
        </button>
      </div>

      <section className="schedule-list" data-testid={tab === 'upcoming' ? 'schedule-upcoming' : 'schedule-past'}>
        {list.length === 0 ? (
          <div className="empty-state empty-state-inline">
            <p className="empty-state-desc" data-testid={`schedule-${tab}-empty`}>
              {tab === 'upcoming' ? 'No Calls are scheduled.' : 'No Schedule has come due yet.'}
            </p>
            {tab === 'upcoming' && (
              <Link className="btn btn-secondary" href="/schedule/new">
                <Plus size={15} /> Schedule a call
              </Link>
            )}
          </div>
        ) : (
          groupByDay(list).map((group) => (
            <div className="day-group" key={group.key}>
              <h2 className="day-heading">{dayLabel(group.key || null)}</h2>
              <div className="schedule-rows">
                {group.items.map((schedule) => (
                  <ScheduleRow
                    key={schedule.id}
                    schedule={schedule}
                    onCancel={schedule.status === 'pending' ? onCancel : undefined}
                  />
                ))}
              </div>
            </div>
          ))
        )}
      </section>
    </div>
  );
}
