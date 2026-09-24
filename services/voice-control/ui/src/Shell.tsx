/* The app frame: a grouped left sidebar on desktop, a top bar plus bottom tabs
 * on a phone. Monitor holds what happened and what is about to (Calls,
 * Schedule); Configure holds who answers and how (Agents, Settings). "New
 * call" is one action, not a screen in the list: it opens the one placement
 * form, for now or for later.
 *
 * The footer states each Outlet's assignment, read from GET /api/active - the
 * same endpoint the Agents screen and the bridges' own resolution use - with a
 * red dot on a slot the API reports broken. */
import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { Bot, CalendarClock, Phone, Plus, Settings2 } from 'lucide-react';
import { fetchAgentsScreen } from './api';
import { DIRECTIONS, outletCopy } from './format';
import { Link } from './Link';
import type { ActiveResponse } from './types';

export type Section = 'calls' | 'schedule' | 'agents' | 'settings' | 'new-call';

const NAV: { group: string; items: { id: Section; label: string; href: string; icon: ReactNode }[] }[] = [
  {
    group: 'Monitor',
    items: [
      { id: 'calls', label: 'Calls', href: '/', icon: <Phone size={16} /> },
      { id: 'schedule', label: 'Schedule', href: '/schedule', icon: <CalendarClock size={16} /> },
    ],
  },
  {
    group: 'Configure',
    items: [
      { id: 'agents', label: 'Agents', href: '/agents', icon: <Bot size={16} /> },
      { id: 'settings', label: 'Settings', href: '/settings', icon: <Settings2 size={16} /> },
    ],
  },
];

function BrandMark() {
  return (
    <svg className="brand-mark" viewBox="0 0 32 32" aria-hidden="true">
      <rect width="32" height="32" rx="8" className="brand-mark-bg" />
      <g className="brand-mark-bars">
        <rect x="7" y="12" width="3" height="8" rx="1.5" />
        <rect x="12.5" y="7" width="3" height="18" rx="1.5" />
        <rect x="18" y="10" width="3" height="12" rx="1.5" />
        <rect x="23.5" y="13" width="3" height="6" rx="1.5" />
      </g>
    </svg>
  );
}

/* A shape check, not a trust exercise: a response that is not an
 * ActiveResponse renders no status rather than a wrong one. */
function isActive(value: unknown): value is ActiveResponse {
  const v = value as ActiveResponse | null;
  return !!v && typeof v.outlets === 'object' && v.outlets !== null && Array.isArray(v.outlet_order);
}

function useOutletStatus(section: Section): ActiveResponse | null {
  const [active, setActive] = useState<ActiveResponse | null>(null);
  useEffect(() => {
    let alive = true;
    fetchAgentsScreen()
      .then(({ active: next }) => {
        if (alive) setActive(isActive(next) ? next : null);
      })
      .catch(() => {
        if (alive) setActive(null);
      });
    return () => {
      alive = false;
    };
  }, [section]);
  return active;
}

export function OutletStatus({ active, compact = false }: { active: ActiveResponse; compact?: boolean }) {
  return (
    <ul className={'outlet-status' + (compact ? ' outlet-status-compact' : '')} data-testid="outlet-status">
      {active.outlet_order.map((outlet) => {
        const slots = active.outlets[outlet] || { inbound: null, outbound: null };
        const warnings = (active.slot_warnings || {})[outlet] || { inbound: null, outbound: null };
        return (
          <li key={outlet} className="outlet-status-row">
            <span className="outlet-status-name">{outletCopy(outlet).short}</span>
            {DIRECTIONS.map((direction) => {
              const broken = !!warnings[direction];
              const agent = slots[direction] ?? null;
              return (
                <span
                  key={direction}
                  className={'outlet-status-slot' + (broken ? ' is-broken' : '')}
                  title={broken ? (warnings[direction] as string) : undefined}
                  data-testid={`status-slot-${outlet}-${direction}`}
                >
                  <span className={'dot ' + (broken ? 'dot-danger' : agent ? 'dot-accent' : 'dot-muted')} />
                  <span className="outlet-status-dir">{direction === 'inbound' ? 'in' : 'out'}</span>
                  <span className="outlet-status-agent">{agent ?? 'default'}</span>
                </span>
              );
            })}
          </li>
        );
      })}
    </ul>
  );
}

export function Shell({ section, children }: { section: Section; children: ReactNode }) {
  const active = useOutletStatus(section);
  const broken =
    active !== null &&
    Object.values(active.slot_warnings || {}).some((byDir) =>
      Object.values(byDir || {}).some((message) => !!message)
    );

  return (
    <div className="app-shell">
      <aside className="sidebar" aria-label="Main">
        <Link className="brand" href="/">
          <BrandMark />
          <span className="brand-name">Voice Control</span>
          <span className="brand-env mono">hermes · nas</span>
        </Link>

        <Link
          className={'btn btn-primary sidebar-new-call' + (section === 'new-call' ? ' is-current' : '')}
          href="/place"
          data-testid="nav-place"
        >
          <Plus size={16} /> New call
        </Link>

        <nav className="sidebar-nav">
          {NAV.map((group) => (
            <div className="sidebar-group" key={group.group}>
              <div className="sidebar-group-label">{group.group}</div>
              {group.items.map((item) => (
                <Link
                  key={item.id}
                  href={item.href}
                  className={'nav-link' + (section === item.id ? ' active' : '')}
                  aria-current={section === item.id ? 'page' : undefined}
                  data-testid={`nav-${item.id}`}
                >
                  {item.icon}
                  {item.label}
                </Link>
              ))}
            </div>
          ))}
        </nav>

        {active && (
          <Link className="sidebar-footer" href="/agents" title="Who answers each Outlet">
            <div className="sidebar-footer-head">
              <span>Outlets</span>
              <span className={'dot ' + (broken ? 'dot-danger' : 'dot-accent')} />
            </div>
            <OutletStatus active={active} />
          </Link>
        )}
      </aside>

      <header className="mobile-bar">
        <Link className="brand" href="/">
          <BrandMark />
          <span className="brand-name">Voice Control</span>
        </Link>
        <Link className="btn btn-primary btn-sm" href="/place" data-testid="nav-place-mobile">
          <Plus size={15} /> New call
        </Link>
      </header>

      <main className="main" id="main">
        {children}
      </main>

      <nav className="tabbar" aria-label="Main">
        {NAV.flatMap((group) => group.items).map((item) => (
          <Link
            key={item.id}
            href={item.href}
            className={'tabbar-link' + (section === item.id ? ' active' : '')}
            aria-current={section === item.id ? 'page' : undefined}
          >
            {item.icon}
            <span>{item.label}</span>
          </Link>
        ))}
      </nav>
    </div>
  );
}

/* The page header every screen opens with: title, one line of what the screen
 * is for, and the screen's own actions on the right. */
export function PageHeader({
  title,
  lede,
  icon,
  actions,
  count,
}: {
  title: ReactNode;
  lede?: ReactNode;
  icon?: ReactNode;
  actions?: ReactNode;
  count?: ReactNode;
}) {
  return (
    <div className="page-header">
      <div className="page-header-text">
        <h1 className="page-title">
          {icon && <span className="page-title-icon">{icon}</span>}
          {title}
          {count}
        </h1>
        {lede && <p className="page-lede">{lede}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </div>
  );
}
