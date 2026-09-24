/* One Agent's page (A2): /agents/<id>, /agents/<id>/voice, /agents/<id>/tools.
 *
 * Everything about one Agent in one place, the way Vapi lays out an assistant:
 * the roster down the left, the Agent on the right with its tabs. Overview says
 * where it sits, what it did last and what it is about to do; Voice and Tools
 * are the per-Agent editor that used to sit on Settings behind a dropdown.
 * Assignment stays on the Agents screen (ticket 02), which this page links to. */
import { useEffect, useState } from 'react';
import { AlertTriangle, ArrowLeft, Bot, CalendarClock, ChevronRight, Phone, Plus } from 'lucide-react';
import { AgentEditor } from './AgentEditor';
import { AGENT_TYPE_LABEL, agentIsBroken, agentStack, heldSlots } from './agents';
import { AgentStatus } from './AgentsView';
import { fetchAgentsScreen, fetchCalls, fetchHermesState, fetchSchedules, fetchSettings, fetchVoices } from './api';
import { formatDate, formatDuration, outletCopy, relativeTo, summaryAbsence } from './format';
import { DirectionIcon, OutcomePill, OutletIcon, Pill } from './ui';
import { formatLocalTime } from './scheduleTime';
import { Link } from './Link';
import type { ActiveResponse, Agent, Call, Schedule, SelectableProfile, SettingsData, VoiceCatalog } from './types';

const TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'voice', label: 'Voice' },
  { id: 'tools', label: 'Tools' },
] as const;

function Overview({ agent, active }: { agent: Agent; active: ActiveResponse }) {
  const [calls, setCalls] = useState<Call[] | null>(null);
  const [callsError, setCallsError] = useState(false);
  const [schedules, setSchedules] = useState<Schedule[] | null>(null);

  useEffect(() => {
    let alive = true;
    setCalls(null);
    setSchedules(null);
    fetchCalls(1, 5, '', agent.id, '')
      .then((res) => {
        if (!alive) return;
        setCallsError(res.unreachable);
        setCalls(res.calls || []);
      })
      .catch(() => alive && setCallsError(true));
    fetchSchedules()
      .then((payload) => {
        if (!alive) return;
        setSchedules(
          (payload.schedules || [])
            .filter((s) => s.status === 'pending' && s.agent === agent.id)
            .sort((a, b) => a.due_at.localeCompare(b.due_at))
        );
      })
      .catch(() => alive && setSchedules([]));
    return () => {
      alive = false;
    };
  }, [agent.id]);

  const held = heldSlots(agent, active.outlet_order);

  return (
    <div className="agent-overview">
      <section className="panel">
        <div className="panel-head">
          <h2 className="panel-title">About</h2>
        </div>
        <dl className="meta-rows">
          <div className="meta-row">
            <dt className="meta-item-label">Description</dt>
            <dd className={'meta-item-value' + (agent.description ? '' : ' agent-description-absent')}>
              {agent.description || 'No description in the profile.'}
            </dd>
          </div>
          <div className="meta-row">
            <dt className="meta-item-label">Hermes profile</dt>
            <dd className="meta-item-value">
              <span className="mono">{agent.hermes_profile || 'not set'}</span>{' '}
              {agent.hermes_profile &&
                (agent.hermes_routable ? (
                  <Pill tone="accent">gateway running</Pill>
                ) : (
                  <Pill tone="warning">no gateway running</Pill>
                ))}
            </dd>
          </div>
          <div className="meta-row">
            <dt className="meta-item-label">What you are talking to</dt>
            <dd className="meta-item-value">
              {AGENT_TYPE_LABEL[agent.agent_type] || agent.agent_type}{' '}
              <span className="mono muted">{agentStack(agent)}</span>
            </dd>
          </div>
          <div className="meta-row">
            <dt className="meta-item-label">Where it sits</dt>
            <dd className="meta-item-value">
              {held.length ? (
                <span className="agent-outlets" data-testid={`agent-outlets-${agent.id}`}>
                  {held.map(({ outlet, direction }) => (
                    <span className="chip chip-static" key={`${outlet}-${direction}`}>
                      <OutletIcon outlet={outlet} size={12} />
                      {outletCopy(outlet).label} · {direction}
                    </span>
                  ))}
                </span>
              ) : (
                <span className="muted">On no Outlet.</span>
              )}{' '}
              <Link href="/agents" className="inline-link">
                Change on Agents
              </Link>
            </dd>
          </div>
        </dl>
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2 className="panel-title">
            <Phone size={16} /> Last calls
          </h2>
          <Link href={`/?agent=${encodeURIComponent(agent.id)}`} className="inline-link">
            All of {agent.id}&apos;s calls <ChevronRight size={14} />
          </Link>
        </div>
        {calls === null && !callsError && <div className="spinner spinner-sm" />}
        {callsError && <p className="muted">The call archive could not be read.</p>}
        {calls && calls.length === 0 && !callsError && <p className="muted">No retained calls name this Agent.</p>}
        {calls && calls.length > 0 && (
          <ul className="mini-list">
            {calls.map((call) => (
              <li key={call.call_id}>
                <Link className="mini-row" href={`/calls/${encodeURIComponent(call.call_id)}?agent=${encodeURIComponent(agent.id)}`}>
                  <span className={`call-dir call-dir-${call.direction}`}>
                    <DirectionIcon direction={call.direction} size={13} />
                  </span>
                  <span className="mini-main">
                    <strong>{call.who}</strong>
                    <span className="mini-sub">
                      {call.summary || (
                        <span className="meta-item-absent">{summaryAbsence(call.summary_state).text}</span>
                      )}
                    </span>
                  </span>
                  <span className="mini-side">
                    <OutcomePill outcome={call.outcome} />
                    <span className="mono-num muted">{formatDuration(call.duration_s) ?? ''}</span>
                    <span className="mono-num muted">{formatDate(call.when, call.when_precision)}</span>
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="panel">
        <div className="panel-head">
          <h2 className="panel-title">
            <CalendarClock size={16} /> Coming up
          </h2>
          <Link href="/schedule" className="inline-link">
            Schedule <ChevronRight size={14} />
          </Link>
        </div>
        {schedules === null && <div className="spinner spinner-sm" />}
        {schedules && schedules.length === 0 && <p className="muted">Nothing scheduled for this Agent.</p>}
        {schedules && schedules.length > 0 && (
          <ul className="mini-list">
            {schedules.map((s) => (
              <li key={s.id} className="mini-row">
                <span className="call-dir call-dir-outbound">
                  <DirectionIcon direction="outbound" size={13} />
                </span>
                <span className="mini-main">
                  <strong className="mono-num">{s.target_display || s.to}</strong>
                  <span className="mini-sub">{s.mission}</span>
                </span>
                <span className="mini-side">
                  <span className="mono-num">{formatLocalTime(s)}</span>
                  <span className="muted">{relativeTo(s.due_at)}</span>
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

export function AgentDetail({ agentId, tab }: { agentId: string; tab: string }) {
  const [agents, setAgents] = useState<Agent[] | null>(null);
  const [active, setActive] = useState<ActiveResponse | null>(null);
  const [settings, setSettings] = useState<SettingsData | null>(null);
  const [selectable, setSelectable] = useState<SelectableProfile[]>([]);
  const [elevenVoices, setElevenVoices] = useState<VoiceCatalog | null>(null);
  const [auraVoices, setAuraVoices] = useState<VoiceCatalog | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    fetchAgentsScreen()
      .then((data) => {
        if (!alive) return;
        setAgents(data.agents);
        setActive(data.active);
      })
      .catch((err: Error) => alive && setError(err.message));
    fetchSettings()
      .then((data) => alive && setSettings(data))
      .catch((err: Error) => alive && setError(err.message));
    /* The profile picker and the ElevenLabs catalog are conveniences over a
       free-text answer, so a failed fetch degrades the control and never the page:
       the picker falls back to the Agent's current profile plus `default`, and the
       voice field stays a text input that says why there is no list. */
    fetchHermesState()
      .then((hermes) => alive && setSelectable(hermes.selectable || []))
      .catch(() => undefined);
    fetchVoices('elevenlabs')
      .then((catalog) => alive && setElevenVoices(catalog))
      .catch((err) => {
        if (!alive) return;
        setElevenVoices({
          source: 'unavailable',
          default: null,
          voices: null,
          detail: String(err instanceof Error ? err.message : err),
        });
      });
    fetchVoices('deepgram-aura')
      .then((catalog) => alive && setAuraVoices(catalog))
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, []);

  if (error) {
    return (
      <div className="page">
        <div className="alert-banner alert-unreachable" data-testid="agent-error">
          <AlertTriangle className="alert-icon" />
          <div>
            <strong>This Agent could not be loaded</strong>
            <div>{error}</div>
          </div>
        </div>
      </div>
    );
  }

  if (!agents || !active) return <div className="spinner" />;

  const agent = agents.find((a) => a.id === agentId);
  const current = TABS.find((t) => t.id === tab)?.id ?? 'overview';
  const base = `/agents/${encodeURIComponent(agentId)}`;

  return (
    <div className="page page-wide master-detail" data-testid="agent-detail">
      <aside className="master" aria-label="Agents">
        <div className="master-head">
          <Link href="/agents" className="master-title">
            <Bot size={16} /> Agents
          </Link>
          <Link href="/agents/new" className="icon-btn" aria-label="New Agent" title="New Agent">
            <Plus size={16} />
          </Link>
        </div>
        <ul className="master-list">
          {agents.map((row) => (
            <li key={row.id}>
              <Link
                href={`/agents/${encodeURIComponent(row.id)}${current === 'overview' ? '' : '/' + current}`}
                className={'master-item' + (row.id === agentId ? ' active' : '')}
                aria-current={row.id === agentId ? 'page' : undefined}
              >
                <span className="master-item-name">
                  {row.id}
                  {agentIsBroken(row) && <span className="dot dot-danger" title="Refuses calls" />}
                </span>
                <span className="master-item-sub mono">{agentStack(row)}</span>
              </Link>
            </li>
          ))}
        </ul>
      </aside>

      <div className="detail">
        <Link href="/agents" className="back-link">
          <ArrowLeft size={15} /> Agents
        </Link>

        {!agent ? (
          <div className="empty-state" data-testid="agent-missing">
            <div className="empty-state-title">No Agent named {agentId}</div>
            <div className="empty-state-desc">
              The roster has no Agent whose document id is <span className="mono">{agentId}</span>.
            </div>
          </div>
        ) : (
          <>
            <header className="detail-head">
              <span className="agent-avatar agent-avatar-lg">
                <Bot size={22} />
              </span>
              <div className="detail-head-text">
                <h1 className="page-title">{agent.id}</h1>
                <div className="detail-head-meta">
                  <span className="mono muted">hermes profile: {agent.hermes_profile || 'not set'}</span>
                  <AgentStatus agent={agent} compact />
                </div>
              </div>
              <div className="page-actions">
                <Link
                  className={'btn btn-secondary' + (agentIsBroken(agent) ? ' is-disabled' : '')}
                  href={`/place?agent=${encodeURIComponent(agent.id)}`}
                  aria-disabled={agentIsBroken(agent)}
                  onClick={(e) => {
                    if (agentIsBroken(agent)) e.preventDefault();
                  }}
                  data-testid="agent-new-call"
                >
                  <Plus size={15} /> New call as {agent.id}
                </Link>
              </div>
            </header>

            {(!agent.valid || !agent.enabled) && (
              <div className="alert-banner alert-unreachable">
                <AlertTriangle className="alert-icon" />
                <div>
                  <AgentStatus agent={agent} />
                </div>
              </div>
            )}

            <nav className="tabs" aria-label="Agent">
              {TABS.map((t) => (
                <Link
                  key={t.id}
                  href={t.id === 'overview' ? base : `${base}/${t.id}`}
                  className={'tab' + (current === t.id ? ' active' : '')}
                  aria-current={current === t.id ? 'page' : undefined}
                  data-testid={`agent-tab-${t.id}`}
                >
                  {t.label}
                </Link>
              ))}
            </nav>

            {current === 'overview' && <Overview agent={agent} active={active} />}
            {current !== 'overview' &&
              (settings ? (
                /* Keyed by the Agent: opening another Agent must start from THAT
                   Agent's document, never show the last one's draft for a frame. */
                <AgentEditor
                  key={agent.id}
                  agent={agent}
                  settings={settings}
                  selectable={selectable}
                  elevenVoices={elevenVoices}
                  auraVoices={auraVoices}
                  tab={current}
                  onSaved={setAgents}
                />
              ) : (
                <div className="spinner" />
              ))}
          </>
        )}
      </div>
    </div>
  );
}
