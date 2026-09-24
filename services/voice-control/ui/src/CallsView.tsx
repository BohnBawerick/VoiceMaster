/* The Calls screen: the archive as a day-grouped list, with the Call itself
 * opening at its own URL (/calls/<call_id>) in a drawer over the list.
 *
 * Filters live in the URL query (?agent=, ?outlet=, ?q=, ?page=, ?show=), so a
 * filtered view is linkable, survives a reload, and the drawer's prev/next
 * walk exactly the list the owner is looking at. Agent and Outlet are sent to
 * the API; the quick tabs (direction, outcome) filter only the page that was
 * loaded, and say so, because the API does not filter on them. */
import { useEffect, useMemo, useRef, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import {
  AlertTriangle,
  Bot,
  CalendarClock,
  ChevronLeft,
  ChevronRight,
  Database,
  Phone,
  Plus,
  Search,
  X,
} from 'lucide-react';
import { fetchCalls, fetchSchedules } from './api';
import { CallDetail } from './CallDetail';
import { dayKey, dayLabel, formatClock, formatDate, formatDuration, NOT_RETAINED, outletCopy, relativeTo, summaryAbsence, todayKey } from './format';
import { DirectionIcon, OutcomePill, OutletIcon } from './ui';
import { PageHeader } from './Shell';
import { Link } from './Link';
import { navigate, useLocation } from './router';
import type { Call, Schedule } from './types';
import { UNKNOWN_FILTER } from './types';

const PAGE_SIZE = 20;

type Show = 'all' | 'inbound' | 'outbound' | 'error';

const SHOW_TABS: { id: Show; label: string; test: (call: Call) => boolean }[] = [
  { id: 'all', label: 'All', test: () => true },
  { id: 'inbound', label: 'Inbound', test: (c) => c.direction === 'inbound' },
  { id: 'outbound', label: 'Outbound', test: (c) => c.direction === 'outbound' },
  { id: 'error', label: 'Ended in error', test: (c) => c.outcome === 'error' },
];

interface Query {
  page: number;
  q: string;
  agent: string;
  outlet: string;
  show: Show;
}

function readQuery(search: URLSearchParams): Query {
  const page = Number(search.get('page') || '1');
  const show = (search.get('show') || 'all') as Show;
  return {
    page: Number.isInteger(page) && page > 0 ? page : 1,
    q: search.get('q') || '',
    agent: search.get('agent') || '',
    outlet: search.get('outlet') || '',
    show: SHOW_TABS.some((t) => t.id === show) ? show : 'all',
  };
}

function queryString(query: Query): string {
  const params = new URLSearchParams();
  if (query.page > 1) params.set('page', String(query.page));
  if (query.q) params.set('q', query.q);
  if (query.agent) params.set('agent', query.agent);
  if (query.outlet) params.set('outlet', query.outlet);
  if (query.show !== 'all') params.set('show', query.show);
  const text = params.toString();
  return text ? `?${text}` : '';
}

function callHref(callId: string, query = ''): string {
  return `/calls/${encodeURIComponent(callId)}${query}`;
}

function filterLabel(value: string): string {
  return value === UNKNOWN_FILTER ? NOT_RETAINED : value;
}

/* "+ Agent" until one is chosen, then "Agent: ada ×". The menu offers only
 * what the API found in the calls it read, so a chosen filter always has calls
 * behind it; "(not retained)" reaches the calls where the field was never
 * written, which is the whole archive from before ticket 05. */
function FilterChip({
  name,
  value,
  options,
  onChange,
}: {
  name: 'agent' | 'outlet';
  value: string;
  options: string[];
  onChange: (next: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const label = name === 'agent' ? 'Agent' : 'Outlet';

  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', close);
    document.addEventListener('keydown', escape);
    return () => {
      document.removeEventListener('mousedown', close);
      document.removeEventListener('keydown', escape);
    };
  }, [open]);

  if (value) {
    return (
      <span className="chip chip-set" data-testid={`filter-${name}-set`}>
        {label}: <strong>{name === 'outlet' && value !== UNKNOWN_FILTER ? outletCopy(value).short : filterLabel(value)}</strong>
        <button
          type="button"
          className="chip-clear"
          aria-label={`Remove the ${label} filter`}
          onClick={() => onChange('')}
        >
          <X size={13} />
        </button>
      </span>
    );
  }

  return (
    <div className="chip-menu" ref={ref}>
      <button
        type="button"
        className="chip chip-add"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={`Filter by ${label}`}
        data-testid={`filter-${name}`}
        disabled={options.length === 0}
        onClick={() => setOpen((v) => !v)}
      >
        <Plus size={13} /> {label}
      </button>
      {open && (
        <ul className="menu" role="listbox" aria-label={`Filter by ${label}`}>
          {options.map((option) => (
            <li key={option}>
              <button
                type="button"
                role="option"
                aria-selected={false}
                className="menu-item"
                data-testid={`filter-${name}-option-${option}`}
                onClick={() => {
                  setOpen(false);
                  onChange(option);
                }}
              >
                {name === 'outlet' && option !== UNKNOWN_FILTER && <OutletIcon outlet={option} size={14} />}
                {name === 'agent' && option !== UNKNOWN_FILTER && <Bot size={14} />}
                <span className={option === UNKNOWN_FILTER ? 'meta-item-absent' : undefined}>
                  {name === 'outlet' && option !== UNKNOWN_FILTER ? outletCopy(option).label : filterLabel(option)}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function CallRow({ call, href }: { call: Call; href: string }) {
  const duration = formatDuration(call.duration_s);
  const clock = formatClock(call.when, call.when_precision);
  const absence = summaryAbsence(call.summary_state);
  return (
    <Link
      href={href}
      className="call-row clickable-row"
      data-testid="call-row"
      data-call-id={call.call_id}
    >
      <span className={`call-dir call-dir-${call.direction}`} data-field="direction" title={call.direction}>
        <DirectionIcon direction={call.direction} />
        <span className="sr-only">{call.direction}</span>
      </span>

      <span className="call-main">
        <span className="call-line1">
          <strong className="call-who" data-field="who">
            {call.who}
          </strong>
          <span className="call-chips">
            <span className="chip chip-static" data-field="agent" aria-label="Agent">
              <Bot size={12} />
              {call.agent ? call.agent : <span className="meta-item-absent">{NOT_RETAINED}</span>}
            </span>
            <span className="chip chip-static" data-field="outlet" aria-label="Outlet">
              {call.outlet ? (
                <>
                  <OutletIcon outlet={call.outlet} size={12} />
                  {outletCopy(call.outlet).short}
                </>
              ) : (
                <span className="meta-item-absent">{NOT_RETAINED}</span>
              )}
            </span>
          </span>
        </span>
        <span className="call-line2">
          {call.summary ? (
            <span className="call-summary" data-field="summary" data-testid="summary-cell" title={call.summary}>
              {call.summary}
            </span>
          ) : (
            <span className="call-summary" data-field="summary" data-testid="summary-cell">
              <span className="meta-item-absent" title={absence.title}>
                {absence.text}
              </span>
            </span>
          )}
          {!call.summary && call.direction === 'outbound' && call.mission && (
            <span className="call-mission" data-field="mission" title={call.mission}>
              Mission: {call.mission}
            </span>
          )}
        </span>
      </span>

      <span className="call-side">
        <span data-field="outcome">
          <OutcomePill outcome={call.outcome} />
        </span>
        <span className="call-duration mono-num" data-field="duration">
          {duration ?? <span className="meta-item-absent">{NOT_RETAINED}</span>}
        </span>
        <time className="call-when mono-num" data-field="when" dateTime={call.when || undefined}>
          {clock ?? formatDate(call.when, call.when_precision)}
        </time>
      </span>
    </Link>
  );
}

/* One line above the list: the next Schedule and today's count. No charts and
 * no cost: the archive holds neither, and an empty panel would look like a
 * claim. */
function StatusStrip({ calls, partial, query }: { calls: Call[]; partial: boolean; query: Query }) {
  const [next, setNext] = useState<Schedule | null>(null);
  useEffect(() => {
    let alive = true;
    fetchSchedules()
      .then((payload) => {
        if (!alive || !Array.isArray(payload?.schedules)) return;
        const pending = payload.schedules
          .filter((s) => s.status === 'pending')
          .sort((a, b) => a.due_at.localeCompare(b.due_at));
        setNext(pending[0] ?? null);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, []);

  /* Today's count is exact only when the newest page reached past today; when
   * every call on it is from today there may be more, so it says "at least". */
  const unfiltered = query.page === 1 && !query.q && !query.agent && !query.outlet;
  const today = todayKey(0);
  const todays = calls.filter((c) => dayKey(c.when, c.when_precision) === today).length;
  const reachedPast = calls.some((c) => {
    const key = dayKey(c.when, c.when_precision);
    return key !== null && key < today;
  });
  const exact = reachedPast && !partial;

  if (!next && !unfiltered) return null;
  return (
    <div className="status-strip" data-testid="status-strip">
      {unfiltered && (
        <span className="status-item">
          <Phone size={14} />
          <span>
            <strong className="mono-num">{exact ? todays : `at least ${todays}`}</strong>{' '}
            {todays === 1 ? 'call' : 'calls'} today
          </span>
        </span>
      )}
      {next && (
        <Link className="status-item status-link" href="/schedule" data-testid="status-next-schedule">
          <CalendarClock size={14} />
          <span>
            Next: <strong>{next.agent}</strong> calls{' '}
            <span className="mono-num">{next.target_display || next.to}</span>{' '}
            <span className="status-when">{relativeTo(next.due_at) ?? next.local_time}</span>
          </span>
        </Link>
      )}
    </div>
  );
}

function groupByDay(calls: Call[]): { key: string | null; calls: Call[] }[] {
  const groups: { key: string | null; calls: Call[] }[] = [];
  for (const call of calls) {
    const key = dayKey(call.when, call.when_precision);
    const last = groups[groups.length - 1];
    if (last && last.key === key) last.calls.push(call);
    else groups.push({ key, calls: [call] });
  }
  return groups;
}

export function CallsView({ callId }: { callId: string | null }) {
  const { search } = useLocation();
  const query = readQuery(search);
  const qs = queryString(query);

  const [calls, setCalls] = useState<Call[]>([]);
  const [total, setTotal] = useState<number>(0);
  const [hasMore, setHasMore] = useState<boolean>(false);
  const [unreachable, setUnreachable] = useState<boolean>(false);
  const [partial, setPartial] = useState<boolean>(false);
  const [warning, setWarning] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [agentOptions, setAgentOptions] = useState<string[]>([]);
  const [outletOptions, setOutletOptions] = useState<string[]>([]);
  const [searchText, setSearchText] = useState<string>(query.q);

  useEffect(() => setSearchText(query.q), [query.q]);

  useEffect(() => {
    let isMounted = true;
    setLoading(true);
    fetchCalls(query.page, PAGE_SIZE, query.q, query.agent, query.outlet)
      .then((res) => {
        if (!isMounted) return;
        setCalls(res.calls || []);
        setTotal(res.total || 0);
        setAgentOptions(res.agents || []);
        setOutletOptions(res.outlets || []);
        // `has_more` is the API's own answer and the only thing the Next button
        // may consult. If a response ever arrives without it, derive it from the
        // total -- never from "this page came back full", which is true of the
        // LAST page of a corpus that is an exact multiple of the page size and is
        // what used to walk the pager onto an empty page reading "No calls
        // recorded yet" against a store holding 100 calls.
        setHasMore(
          typeof res.has_more === 'boolean' ? res.has_more : query.page * PAGE_SIZE < (res.total || 0)
        );
        setUnreachable(res.unreachable || false);
        setPartial(res.partial || false);
        setWarning(res.warning || null);
      })
      .catch(() => {
        if (!isMounted) return;
        setCalls([]);
        setUnreachable(true);
        setPartial(false);
        setWarning(null);
        setAgentOptions([]);
        setOutletOptions([]);
      })
      .finally(() => {
        if (isMounted) setLoading(false);
      });
    return () => {
      isMounted = false;
    };
  }, [query.page, query.q, query.agent, query.outlet]);

  const setQuery = (patch: Partial<Query>) => {
    const next = { ...query, ...patch };
    navigate('/' + queryString(next), { keepScroll: !('page' in patch) });
  };

  const onSearch = (event: FormEvent) => {
    event.preventDefault();
    setQuery({ q: searchText.trim(), page: 1 });
  };

  const tab = SHOW_TABS.find((t) => t.id === query.show) || SHOW_TABS[0];
  const shown = useMemo(() => calls.filter(tab.test), [calls, tab]);
  const groups = useMemo(() => groupByDay(shown), [shown]);

  /* `total` is a count only when the read finished. When a bound stopped the
   * fetch, or a bank could not be read, it is a FLOOR -- so the heading badge
   * and the pager have to say so. They used to render byte-identical text for a
   * complete history of ten calls and for a 210-document bank the fetch could not
   * get to the end of, which is the one number an operator is told
   * to trust. */
  const countPhrase = (partial ? 'at least ' : '') + total + (total === 1 ? ' call' : ' calls');
  const filtering = query.agent !== '' || query.outlet !== '';

  const emptyTitle = () => {
    if (unreachable) return 'Store Unreachable';
    // "No calls recorded yet" would be a claim about history that a bank we
    // could not read is not entitled to make.
    if (partial) return 'Some call history could not be read';
    if (query.q) return 'No matching calls found';
    // ... and so would it here: the history is not empty, this filter is.
    if (filtering) return 'No calls match these filters';
    return 'No calls recorded yet';
  };

  const emptyDesc = () => {
    if (unreachable) return 'Voice Control could not read the call archive.';
    if (partial) return warning || 'Part of the call history could not be read.';
    if (query.q) return 'Nothing said on any retained call matched that search. Try other words.';
    if (filtering) return 'Other calls exist; none of them match the Agent and Outlet filters above.';
    return 'When calls take place and are archived, they will appear here.';
  };

  const index = callId ? shown.findIndex((c) => c.call_id === callId) : -1;
  const neighbour = (offset: number) => {
    const target = shown[index + offset];
    return target ? callHref(target.call_id, qs) : null;
  };

  let banner: ReactNode = null;
  if (unreachable) {
    banner = (
      <div className="alert-banner alert-unreachable">
        <AlertTriangle className="alert-icon" />
        <div>
          <strong>Call Archive Unreachable</strong>
          <div>
            Voice Control cannot read the call archive (the SQLite file or the Hindsight store it is
            configured with). Showing empty state.
          </div>
        </div>
      </div>
    );
  } else if (partial && warning) {
    banner = (
      <div className="alert-banner alert-partial">
        <AlertTriangle className="alert-icon" />
        <div>
          <strong>Partial call history</strong>
          <div>{warning}</div>
        </div>
      </div>
    );
  }

  return (
    <div className="page page-wide calls-page">
      <PageHeader
        title="Calls"
        icon={<Phone size={20} />}
        count={!loading && !unreachable ? <span className="count-badge">{countPhrase}</span> : null}
        lede="Every Call the archive holds, newest first. Open one for its recording, summary and transcript."
        actions={
          <Link className="btn btn-primary" href="/place" data-testid="place-from-calls">
            <Plus size={16} /> New call
          </Link>
        }
      />

      {banner}

      {!loading && !unreachable && <StatusStrip calls={calls} partial={partial} query={query} />}

      <div className="calls-toolbar">
        <div className="seg-tabs" role="tablist" aria-label="Show">
          {SHOW_TABS.map((t) => (
            <button
              key={t.id}
              type="button"
              role="tab"
              aria-selected={t.id === tab.id}
              className={'seg-tab' + (t.id === tab.id ? ' active' : '')}
              data-testid={`show-${t.id}`}
              onClick={() => setQuery({ show: t.id })}
            >
              {t.label}
              <span className="seg-count mono-num">{loading ? '–' : calls.filter(t.test).length}</span>
            </button>
          ))}
        </div>

        <form onSubmit={onSearch} className="search-box" role="search">
          <Search className="search-icon-inside" aria-hidden="true" />
          <input
            type="search"
            placeholder="Search what was said"
            aria-label="Search what was said on retained calls"
            className="search-input"
            value={searchText}
            onChange={(e) => setSearchText(e.target.value)}
          />
          {searchText !== '' && (
            <button
              type="button"
              className="search-clear-btn"
              onClick={() => {
                setSearchText('');
                setQuery({ q: '', page: 1 });
              }}
              title="Clear search"
              aria-label="Clear search"
            >
              <X size={15} />
            </button>
          )}
        </form>
      </div>

      <div className="filter-row">
        <FilterChip
          name="agent"
          value={query.agent}
          options={agentOptions}
          onChange={(agent) => setQuery({ agent, page: 1 })}
        />
        <FilterChip
          name="outlet"
          value={query.outlet}
          options={outletOptions}
          onChange={(outlet) => setQuery({ outlet, page: 1 })}
        />
        {query.q && (
          <span className="chip chip-set" data-testid="search-chip">
            Said: <strong>{query.q}</strong>
            <button
              type="button"
              className="chip-clear"
              aria-label="Clear search"
              onClick={() => setQuery({ q: '', page: 1 })}
            >
              <X size={13} />
            </button>
          </span>
        )}
        {(filtering || query.q) && (
          <button
            type="button"
            className="link-btn filter-clear-btn"
            onClick={() => setQuery({ agent: '', outlet: '', q: '', page: 1 })}
          >
            Clear filters
          </button>
        )}
        {query.show !== 'all' && (
          <span className="filter-note">Tab counts and filtering cover the calls on this page only.</span>
        )}
      </div>

      <section className="calls-list" aria-label="Calls" data-testid="calls-list">
        {loading && <div className="spinner" />}

        {!loading && calls.length === 0 && (
          <div className="empty-state">
            <Database className="empty-state-icon" />
            <div className="empty-state-title">{emptyTitle()}</div>
            <div className="empty-state-desc">{emptyDesc()}</div>
            {!unreachable && !partial && !query.q && !filtering && (
              <Link className="btn btn-secondary" href="/place" data-testid="place-from-empty">
                <Plus size={15} /> New call
              </Link>
            )}
          </div>
        )}

        {!loading && calls.length > 0 && shown.length === 0 && (
          <div className="empty-state empty-state-inline" data-testid="tab-empty">
            <div className="empty-state-desc">
              None of the {calls.length} calls on this page {tab.id === 'error' ? 'ended in error' : `is ${tab.id}`}.
            </div>
          </div>
        )}

        {!loading &&
          groups.map((group) => (
            <div className="day-group" key={group.key ?? 'unknown'}>
              <h2 className="day-heading">
                {dayLabel(group.key)}
                <span className="day-count mono-num">{group.calls.length}</span>
              </h2>
              <div className="call-rows">
                {group.calls.map((call) => (
                  <CallRow
                    key={call.call_id}
                    call={call}
                    href={callHref(call.call_id, qs)}
                  />
                ))}
              </div>
            </div>
          ))}

        {!loading && calls.length > 0 && (
          <div className="pagination-bar">
            <div>
              Page {query.page} {total > 0 ? '(Showing ' + calls.length + ' of ' + countPhrase + ')' : ''}
            </div>
            <div className="pagination-buttons">
              <button
                className="pagination-btn"
                disabled={query.page <= 1}
                onClick={() => setQuery({ page: Math.max(1, query.page - 1) })}
              >
                <ChevronLeft size={16} /> Previous
              </button>
              <button className="pagination-btn" disabled={!hasMore} onClick={() => setQuery({ page: query.page + 1 })}>
                Next <ChevronRight size={16} />
              </button>
            </div>
          </div>
        )}
      </section>

      {callId && (
        <CallDetail
          callId={callId}
          position={index >= 0 ? { index, total: shown.length } : null}
          prevHref={index >= 0 ? neighbour(-1) : null}
          nextHref={index >= 0 ? neighbour(1) : null}
          closeHref={'/' + qs}
        />
      )}
    </div>
  );
}
