/* The Agents screen (ticket 02), built on the Outlet axis ticket 16 landed.
 *
 * The screen answers one question: which Agent answers the phone number, and
 * which one answers Nextcloud Talk. The first attempt (PR #5) claimed to answer
 * it while reading `active.yaml`'s single `inbound` / `outbound` keys, which are
 * a call DIRECTION switch shared by both Outlets -- so the two cards could never
 * show two different Agents, and the claim was false by construction. Ticket 16
 * made the assignment per Outlet. This screen reads that, and nothing else:
 *
 *   - `active.outlets[outlet][direction]`   who is assigned, per Outlet
 *   - `active.slot_warnings[outlet][dir]`   why that slot is dead, if it is
 *   - `active.outlet_order`                 which Outlets exist (profiles.OUTLETS)
 *
 * It is the ONLY surface that writes an assignment. Every write names exactly
 * the one Outlet and the one direction the operator touched. The flat
 * `inbound` / `outbound` keys that used to mean "both Outlets" were deleted with
 * ticket 17, so there is no longer a shape this screen (or any other client)
 * could send that would reassign an Outlet nobody touched.
 *
 * Layout (A1, A3): the Outlets as a compact switchboard, one row per Outlet
 * with its two slots, then the roster as rows that open each Agent's page.
 */
import { useCallback, useEffect, useState } from 'react';
import { assignOutletSlot, fetchAgentsScreen, fetchHermesState } from './api';
import { CreateAgentWizard } from './CreateAgentWizard';
import { DIRECTIONS, outletCopy } from './format';
import { OutletIcon, Pill } from './ui';
import { agentIsBroken, agentStack, heldSlots } from './agents';
import { Link } from './Link';
import { navigate } from './router';
import { PageHeader } from './Shell';
import type { ActiveResponse, Agent, Direction, HermesState } from './types';
import { AlertTriangle, Bot, CheckCircle2, ChevronRight, PauseCircle, Plus, XCircle } from 'lucide-react';

const UNASSIGNED = 'No Agent assigned';

export function AgentStatus({ agent, compact = false }: { agent: Agent; compact?: boolean }) {
  if (!agent.valid) {
    return (
      <div className="agent-status agent-status-broken" data-testid={`agent-status-${agent.id}`}>
        <XCircle size={15} />
        <div>
          <strong>Profile is broken</strong>
          {!compact &&
            agent.errors.map((reason) => (
              <div className="agent-error-line" key={reason}>
                {reason}
              </div>
            ))}
        </div>
      </div>
    );
  }
  if (!agent.enabled) {
    return (
      <div className="agent-status agent-status-broken" data-testid={`agent-status-${agent.id}`}>
        <PauseCircle size={15} />
        <div>
          <strong>Disabled</strong>
          {!compact && (
            <div className="agent-error-line">
              enabled: false — every call on an Outlet slot naming it refuses to start.
            </div>
          )}
        </div>
      </div>
    );
  }
  return (
    <div className="agent-status agent-status-ok" data-testid={`agent-status-${agent.id}`}>
      <CheckCircle2 size={15} /> Available
    </div>
  );
}

interface SlotProps {
  outlet: string;
  direction: Direction;
  assigned: string | null;
  warning: string | null;
  agents: Agent[];
  pending: boolean;
  error: string | null;
  onAssign: (outlet: string, direction: Direction, agentId: string | null) => void;
}

/* One slot: the Agent it names, as a select that changes it in place. */
function OutletSlot({ outlet, direction, assigned, warning, agents, pending, error, onAssign }: SlotProps) {
  const broken = warning !== null;
  /* Only agents the call path would accept are offered. A slot already pointing
   * at a broken one still shows it -- silently rendering something else as the
   * current value would be the same lie in a new place. */
  const selectable = agents.filter((agent) => !agentIsBroken(agent));
  const strandedCurrent = assigned !== null && !selectable.some((agent) => agent.id === assigned);

  return (
    <div
      className={'outlet-slot' + (broken ? ' outlet-slot-broken' : '')}
      data-testid={`slot-${outlet}-${direction}`}
    >
      <label className="outlet-slot-line">
        <span className="outlet-slot-label">{direction === 'inbound' ? 'Inbound' : 'Outbound'}</span>
        <span
          className={
            'outlet-slot-agent' +
            (broken ? ' outlet-slot-agent-broken' : '') +
            (assigned === null ? ' outlet-slot-agent-default' : '')
          }
          data-testid={`slot-agent-${outlet}-${direction}`}
        >
          {broken && <AlertTriangle size={14} className="outlet-slot-agent-icon" />}
          {assigned === null ? UNASSIGNED : assigned}
        </span>
        <span className="outlet-slot-control">
          <span className="sr-only">
            Change the {direction} Agent on {outletCopy(outlet).label}
          </span>
          <select
            className="outlet-slot-select"
            data-testid={`slot-select-${outlet}-${direction}`}
            value={assigned === null ? '' : assigned}
            disabled={pending}
            onChange={(event) => onAssign(outlet, direction, event.target.value === '' ? null : event.target.value)}
          >
            <option value="">{UNASSIGNED}</option>
            {strandedCurrent && <option value={assigned as string}>{assigned} — broken, stored anyway</option>}
            {selectable.map((agent) => (
              <option key={agent.id} value={agent.id}>
                {agent.id}
              </option>
            ))}
          </select>
          <span className="outlet-slot-change" aria-hidden="true">
            Change
          </span>
        </span>
      </label>
      {broken && (
        <p className="outlet-slot-warning" data-testid={`slot-warning-${outlet}-${direction}`}>
          <span className="outlet-slot-flag" data-testid={`slot-flag-${outlet}-${direction}`}>
            <XCircle size={13} /> Calls refuse
          </span>{' '}
          {warning}
        </p>
      )}
      {error && (
        <p className="outlet-slot-error" data-testid={`slot-error-${outlet}-${direction}`}>
          <AlertTriangle size={14} /> {error}
        </p>
      )}
    </div>
  );
}

function OutletRow({
  outlet,
  active,
  agents,
  pending,
  errors,
  onAssign,
}: {
  outlet: string;
  active: ActiveResponse;
  agents: Agent[];
  pending: string | null;
  errors: Record<string, string>;
  onAssign: (outlet: string, direction: Direction, agentId: string | null) => void;
}) {
  const slots = active.outlets[outlet] || { inbound: null, outbound: null };
  const warnings = active.slot_warnings[outlet] || { inbound: null, outbound: null };
  const dead = DIRECTIONS.some((direction) => warnings[direction] !== null);
  const copy = outletCopy(outlet);

  return (
    <article className={'outlet-card' + (dead ? ' outlet-card-broken' : '')} data-testid={`outlet-card-${outlet}`}>
      <header className="outlet-card-head">
        <span className="outlet-card-icon">
          <OutletIcon outlet={outlet} size={17} />
        </span>
        <div className="outlet-card-title">
          <h3 className="outlet-card-name">{copy.label}</h3>
          <div className="outlet-card-blurb">{copy.blurb}</div>
        </div>
        {dead && (
          <span className="outlet-card-flag" data-testid={`outlet-dead-${outlet}`}>
            <AlertTriangle size={13} /> Outlet broken
          </span>
        )}
      </header>
      <div className="outlet-card-slots">
        {DIRECTIONS.map((direction) => (
          <OutletSlot
            key={direction}
            outlet={outlet}
            direction={direction}
            assigned={slots[direction] ?? null}
            warning={warnings[direction] ?? null}
            agents={agents}
            pending={pending === `${outlet}.${direction}`}
            error={errors[`${outlet}.${direction}`] || null}
            onAssign={onAssign}
          />
        ))}
      </div>
    </article>
  );
}

function AgentRow({ agent, outletOrder }: { agent: Agent; outletOrder: string[] }) {
  const broken = agentIsBroken(agent);
  const held = heldSlots(agent, outletOrder);
  return (
    <Link
      href={`/agents/${encodeURIComponent(agent.id)}`}
      className={'agent-row agent-card' + (broken ? ' agent-card-broken' : '')}
      data-testid={`agent-card-${agent.id}`}
    >
      <span className="agent-avatar">
        <Bot size={17} />
      </span>
      <span className="agent-row-main">
        <span className="agent-row-title">
          <h3 className="agent-name">{agent.id}</h3>
          <span className="agent-stack mono">{agentStack(agent)}</span>
        </span>
        <span className={'agent-description' + (agent.description ? '' : ' agent-description-absent')}>
          {agent.description || 'No description in the profile.'}
        </span>
        <span className="agent-profile mono">
          {agent.hermes_profile ? `hermes profile: ${agent.hermes_profile}` : 'hermes profile: not set'}
        </span>
      </span>
      <span className="agent-row-outlets">
        {held.length > 0 && (
          <span className="agent-outlets" data-testid={`agent-outlets-${agent.id}`}>
            {held.map(({ outlet, direction }) => (
              <span className="chip chip-static agent-outlet-chip" key={`${outlet}-${direction}`}>
                <OutletIcon outlet={outlet} size={12} />
                {outletCopy(outlet).label} · {direction}
              </span>
            ))}
          </span>
        )}
      </span>
      <span className="agent-row-status">
        <AgentStatus agent={agent} compact />
      </span>
      <ChevronRight size={16} className="agent-row-chevron" aria-hidden="true" />
    </Link>
  );
}

export function AgentsView({ wizard }: { wizard: boolean }) {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [active, setActive] = useState<ActiveResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const [slotErrors, setSlotErrors] = useState<Record<string, string>>({});
  const [hermes, setHermes] = useState<HermesState | null>(null);

  const load = useCallback(() => {
    return fetchAgentsScreen()
      .then((data) => {
        setAgents(data.agents);
        setActive(data.active);
        setError(null);
      })
      .catch((reason: Error) => setError(reason.message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load();
    /* Whether a real profile can be created is a fact about the DEPLOY, not
     * about this screen, so the roster reads it too: the New Agent control has
     * to say why it cannot be used rather than fail after being pressed. */
    fetchHermesState()
      .then(setHermes)
      .catch(() => setHermes(null));
  }, [load]);

  const onAssign = useCallback((outlet: string, direction: Direction, agentId: string | null) => {
    const key = `${outlet}.${direction}`;
    setPending(key);
    setSlotErrors((previous) => {
      const next = { ...previous };
      delete next[key];
      return next;
    });
    assignOutletSlot(outlet, direction, agentId)
      .then((response) => {
        setActive(response);
        /* The roster's per-Outlet chips are derived server-side, so a write
         * that moved a slot has to re-read the roster too. */
        return fetchAgentsScreen().then((data) => setAgents(data.agents));
      })
      .catch((reason: Error) => setSlotErrors((previous) => ({ ...previous, [key]: reason.message })))
      .finally(() => setPending(null));
  }, []);

  if (wizard) {
    return (
      <CreateAgentWizard
        onClose={() => navigate('/agents')}
        onCreated={() => {
          /* Straight back to the roster, reloaded: the new Agent is on it and
           * selectable in every Outlet picker immediately -- no restart, no
           * redeploy, and no Outlet touched on the way. */
          navigate('/agents');
          setLoading(true);
          load();
        }}
      />
    );
  }

  if (loading) return <div className="spinner" />;

  if (error || active === null) {
    return (
      <div className="page">
        <div className="alert-banner alert-unreachable" data-testid="agents-error">
          <AlertTriangle className="alert-icon" />
          <div>
            <strong>The Agents screen could not be loaded</strong>
            <div>{error || 'No configuration was returned.'}</div>
          </div>
        </div>
      </div>
    );
  }

  /* A warning that names a slot is painted on that slot. Anything left over --
   * an `active.yaml` that could not be read at all, say -- belongs to no slot and
   * stays a page-level banner rather than disappearing. */
  const attributed = new Set(
    Object.values(active.slot_warnings)
      .flatMap((byDirection) => Object.values(byDirection))
      .filter((message): message is string => typeof message === 'string')
  );
  const pageWarnings = active.warnings.filter((warning) => !attributed.has(warning));
  const outletOrder = active.outlet_order;
  const canCreate = !(hermes !== null && !hermes.available);

  return (
    <div className="page page-wide agents-page">
      <PageHeader
        title="Agents"
        icon={<Bot size={20} />}
        lede="Who exists, and who answers on each Outlet. An Agent is a Hermes profile (ADR 0001)."
        actions={
          <button
            className="btn btn-primary"
            data-testid="new-agent"
            disabled={!canCreate}
            title={!canCreate && hermes ? `${hermes.reason} ${hermes.deploy_hint}` : 'Create a new Agent'}
            onClick={() => navigate('/agents/new')}
          >
            <Plus size={15} /> New Agent
          </button>
        }
      />

      {hermes !== null && !hermes.available && (
        <div className="alert-banner alert-partial" data-testid="agents-create-blocked">
          <AlertTriangle className="alert-icon" />
          <div>
            <strong>New Agents cannot be created on this deploy</strong>
            <div>{hermes.reason}</div>
            <div className="wizard-deploy-hint mono">{hermes.deploy_hint}</div>
          </div>
        </div>
      )}

      {active.voice_agent_env && (
        <div className="alert-banner alert-partial" data-testid="voice-agent-env-banner">
          <AlertTriangle className="alert-icon" />
          <div>
            <strong>VOICE_AGENT overrides every assignment below</strong>
            <div>
              <span className="mono">VOICE_AGENT={active.voice_agent_env}</span> is set in this
              service's environment. While it is set the stored per-Outlet assignments are inert:
              every Outlet and both directions resolve that agent instead.
            </div>
          </div>
        </div>
      )}

      {pageWarnings.map((warning) => (
        <div className="alert-banner alert-unreachable" data-testid="active-page-warning" key={warning}>
          <AlertTriangle className="alert-icon" />
          <div>{warning}</div>
        </div>
      ))}

      <section className="panel" aria-labelledby="outlets-heading">
        <div className="panel-head">
          <h2 id="outlets-heading" className="panel-title">
            Outlets
          </h2>
          <p className="panel-copy">
            Where calls arrive and leave. An unassigned slot keeps the bridge defaults; it is not an
            Agent. To speak to Hermes itself, give an Agent the Hermes directly type on its Voice tab.
          </p>
        </div>
        <div className="outlet-grid">
          {outletOrder.map((outlet) => (
            <OutletRow
              key={outlet}
              outlet={outlet}
              active={active}
              agents={agents}
              pending={pending}
              errors={slotErrors}
              onAssign={onAssign}
            />
          ))}
        </div>
      </section>

      <section className="panel" aria-labelledby="roster-heading">
        <div className="panel-head">
          <h2 id="roster-heading" className="panel-title">
            Agent roster <Pill>{agents.length}</Pill>
          </h2>
        </div>
        <div className="agent-grid">
          {agents.map((agent) => (
            <AgentRow key={agent.id} agent={agent} outletOrder={outletOrder} />
          ))}
        </div>
        {agents.length === 0 && (
          <p className="agents-empty" data-testid="agents-empty">
            No Agents yet. Create an Agent, then assign it to an Outlet.
          </p>
        )}
      </section>
    </div>
  );
}
