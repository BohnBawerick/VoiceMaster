/* Settings, in three sections (T1): Defaults, Speed dial, Advanced.
 *
 * Ticket 14 made this one consolidated page. The proven/untested labels are
 * SERVED by the backend (settings_catalog.py) - this screen renders them, it
 * never invents them. Every provider and voice stays reachable under Advanced
 * (VC7: nothing is removed, only reorganised).
 *
 * Two things that used to live here moved to where they belong:
 *   - an Agent's voice is edited on that Agent's own page (/agents/<id>/voice);
 *   - Outlet assignment is the Agents screen's alone (ticket 02), so this page
 *     states it read-only and links there rather than offering a second
 *     control that could disagree with the first. */
import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import { Bot, Check, ChevronRight, Loader2, Plus, Save, Settings2, X } from 'lucide-react';
import { fetchAgentsScreen, fetchSettings, saveSpeedDial } from './api';
import { DIRECTIONS, outletCopy } from './format';
import { OutletIcon, Pill, ProvenTag } from './ui';
import { agentStack } from './agents';
import { Link } from './Link';
import { navigate } from './router';
import { PageHeader } from './Shell';
import type { ActiveResponse, Agent, SettingsData, SettingsSpeedDial } from './types';

const SECTIONS = [
  { id: 'defaults', label: 'Defaults', blurb: 'What already works, and where each setting lives' },
  { id: 'speed-dial', label: 'Speed dial', blurb: 'Numbers a call can be aimed at' },
  { id: 'advanced', label: 'Advanced', blurb: 'Every provider and voice, with its evidence' },
] as const;

type SectionId = (typeof SECTIONS)[number]['id'];

function roleLabel(role: string): string {
  const labels: Record<string, string> = { realtime: 'Realtime', llm: 'LLM', stt: 'STT', tts: 'TTS' };
  return labels[role] || role;
}

function Row({
  label,
  help,
  children,
  testId,
}: {
  label: string;
  help?: React.ReactNode;
  children: React.ReactNode;
  testId?: string;
}) {
  return (
    <div className="form-row" data-testid={testId}>
      <div className="form-row-label">
        {label}
        {help && <span className="form-row-help">{help}</span>}
      </div>
      <div className="form-row-control">{children}</div>
    </div>
  );
}

function Defaults({
  settings,
  agents,
  active,
}: {
  settings: SettingsData;
  agents: Agent[];
  active: ActiveResponse | null;
}) {
  const { proven } = settings;
  return (
    <>
      {/* The proven default, stated first so an operator is never asked to
          decide before being told what already works. */}
      <section className="panel" data-testid="settings-proven">
        <div className="panel-head">
          <h2 className="panel-title">The proven path</h2>
          <p className="panel-copy">
            The live phone line runs{' '}
            <span className="pill pill-accent settings-tag settings-tag-proven">{proven.pipeline}</span> on{' '}
            <strong>{proven.realtime_provider}</strong>, speaking <strong>{proven.voice}</strong>. A new
            Agent needs no decision about any of these - the wizard preselects them.
          </p>
        </div>
        <div className="form-rows">
          <Row label="Pipeline" help="How a call is carried.">
            <span className="mono">{proven.pipeline}</span> <ProvenTag proven />
          </Row>
          <Row label="Realtime provider" help="The speech-to-speech model on the live line.">
            <span className="mono">{proven.realtime_provider}</span> <ProvenTag proven />
          </Row>
          <Row label="Voice" help="What an Agent that sets no voice sounds like.">
            <span className="mono">{proven.voice}</span> <ProvenTag proven />
          </Row>
        </div>
      </section>

      <section className="panel" data-testid="settings-agent-voice-moved">
        <div className="panel-head">
          <h2 className="panel-title">Agent voice</h2>
          <p className="panel-copy">
            Each Agent&apos;s voice is set on its own page, one Agent at a time. A change applies to
            that Agent&apos;s next call only.
          </p>
        </div>
        <ul className="link-rows">
          {agents.map((agent) => (
            <li key={agent.id}>
              <Link className="link-row" href={`/agents/${encodeURIComponent(agent.id)}/voice`}>
                <Bot size={15} />
                <strong>{agent.id}</strong>
                <span className="mono muted">{agentStack(agent)}</span>
                {agent.knobs && typeof agent.knobs.voice === 'string' && (
                  <span className="muted">voice {agent.knobs.voice}</span>
                )}
                <ChevronRight size={15} className="link-row-chevron" />
              </Link>
            </li>
          ))}
        </ul>
      </section>

      {/* T2: read-only. The Agents screen is the ONLY surface that writes an
          assignment (ticket 02); a second control here could disagree with it. */}
      <section className="panel" data-testid="settings-outlets">
        <div className="panel-head">
          <h2 className="panel-title">Outlet assignment</h2>
          <p className="panel-copy">
            Who answers each Outlet is set on the Agents screen.{' '}
            <Link href="/agents" className="inline-link">
              Change it on Agents <ChevronRight size={13} />
            </Link>
          </p>
        </div>
        {active && (
          <ul className="link-rows">
            {active.outlet_order.map((outlet) => {
              const slots = active.outlets[outlet] || { inbound: null, outbound: null };
              const warnings = active.slot_warnings[outlet] || { inbound: null, outbound: null };
              return (
                <li key={outlet} className="link-row is-static" data-testid={`settings-outlet-${outlet}`}>
                  <OutletIcon outlet={outlet} size={15} />
                  <strong>{outletCopy(outlet).label}</strong>
                  {DIRECTIONS.map((direction) => (
                    <span key={direction} className="settings-slot-summary">
                      <span className="muted">{direction === 'inbound' ? 'in' : 'out'}</span>{' '}
                      {warnings[direction] ? (
                        <Pill tone="danger" title={warnings[direction] as string}>
                          {slots[direction] ?? 'default'} · broken
                        </Pill>
                      ) : (
                        <span className={slots[direction] ? '' : 'muted'}>
                          {slots[direction] ?? 'No Agent assigned'}
                        </span>
                      )}
                    </span>
                  ))}
                </li>
              );
            })}
          </ul>
        )}
      </section>
    </>
  );
}

function SpeedDialSection({ initial }: { initial: SettingsSpeedDial }) {
  const [speedDial, setSpeedDial] = useState<SettingsSpeedDial>(initial);
  const [saved, setSaved] = useState<SettingsSpeedDial>(initial);
  const [newLabel, setNewLabel] = useState('');
  const [newNumber, setNewNumber] = useState('');
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const dirty = JSON.stringify(speedDial.numbers) !== JSON.stringify(saved.numbers);

  async function save(e: FormEvent) {
    e.preventDefault();
    setSaving(true);
    setMessage(null);
    setError(null);
    try {
      const next = await saveSpeedDial(speedDial.numbers);
      setSpeedDial(next);
      setSaved(next);
      setMessage('Speed dial saved.');
    } catch (err) {
      setError(String(err instanceof Error ? err.message : err));
    } finally {
      setSaving(false);
    }
  }

  function add() {
    if (!newLabel.trim() || !newNumber.trim()) return;
    setMessage(null);
    setSpeedDial((prev) => ({
      ...prev,
      numbers: [...prev.numbers, { label: newLabel.trim(), number: newNumber.trim(), owner: false }],
    }));
    setNewLabel('');
    setNewNumber('');
  }

  function remove(index: number) {
    setMessage(null);
    setSpeedDial((prev) => ({ ...prev, numbers: prev.numbers.filter((_, i) => i !== index) }));
  }

  return (
    <section className="panel" data-testid="settings-speed-dial">
      <div className="panel-head">
        <h2 className="panel-title">Speed dial</h2>
        <p className="panel-copy">
          Numbers a call can be aimed at without typing one. The New call form offers them first.
          {!speedDial.owner &&
            ' Seed it with the owner number by setting VOICE_OWNER_NUMBER in the voice-control service.'}
        </p>
      </div>

      {speedDial.error ? (
        <p className="settings-error" data-testid="settings-dial-error">
          {speedDial.error}
        </p>
      ) : (
        <form onSubmit={save} className="settings-speed-form">
          <ul className="settings-speed-rows">
            {speedDial.numbers.length === 0 && <li className="settings-empty">No numbers saved yet.</li>}
            {speedDial.numbers.map((entry, index) => (
              <li className="settings-speed-row" key={`${entry.number}-${index}`}>
                <span className="settings-speed-label">
                  {entry.label} {entry.owner && <Pill tone="accent">owner</Pill>}
                </span>
                <span className="settings-speed-number mono-num">{entry.number}</span>
                {!entry.owner ? (
                  <button
                    type="button"
                    className="icon-btn settings-speed-remove"
                    aria-label={`Remove ${entry.label}`}
                    data-testid={`settings-speed-remove-${index}`}
                    onClick={() => remove(index)}
                  >
                    <X size={14} />
                  </button>
                ) : (
                  <span className="settings-speed-lock muted" title="Set by VOICE_OWNER_NUMBER">
                    from env
                  </span>
                )}
              </li>
            ))}
          </ul>

          <div className="settings-speed-add">
            <input
              className="field-input"
              placeholder="Label"
              aria-label="New speed dial label"
              data-testid="settings-speed-label"
              value={newLabel}
              onChange={(e) => setNewLabel(e.target.value)}
            />
            <input
              className="field-input mono"
              placeholder="+15551234567"
              aria-label="New speed dial number"
              data-testid="settings-speed-number"
              value={newNumber}
              onChange={(e) => setNewNumber(e.target.value)}
            />
            <button type="button" className="btn btn-secondary" data-testid="settings-speed-add" onClick={add}>
              <Plus size={14} /> Add
            </button>
          </div>

          <div className={'save-bar' + (dirty ? ' is-dirty' : '')}>
            <span className="save-bar-state">
              {message ? (
                <span className="settings-success" data-testid="settings-dial-saved">
                  <Check size={14} /> {message}
                </span>
              ) : error ? (
                <span className="settings-error" data-testid="settings-dial-error">
                  {error}
                </span>
              ) : dirty ? (
                'Unsaved changes'
              ) : (
                'No changes'
              )}
            </span>
            <span className="save-bar-actions">
              <button
                type="button"
                className="btn btn-ghost"
                disabled={!dirty || saving}
                onClick={() => setSpeedDial(saved)}
              >
                Discard
              </button>
              <button
                type="submit"
                className="btn btn-primary"
                data-testid="settings-save-speed-dial"
                disabled={saving}
              >
                {saving ? <Loader2 className="spin" size={16} /> : <Save size={16} />}
                Save speed dial
              </button>
            </span>
          </div>
        </form>
      )}
    </section>
  );
}

/* Everything that is not proven, with its evidence beside it (T3). */
function Advanced({ settings }: { settings: SettingsData }) {
  const { proven, providers, realtime_voices } = settings;
  const roles = Array.from(new Set(providers.map((row) => row.role)));
  return (
    <section className="panel settings-advanced" data-testid="settings-advanced">
      <div className="panel-head">
        <h2 className="panel-title">Providers</h2>
        <p className="panel-copy">
          Every provider stays selectable on each Agent&apos;s Voice tab. The label is the evidence: a
          wired cascade client is not a proven call.
        </p>
      </div>
      {roles.map((role) => (
        <div className="provider-group" key={role}>
          <h3 className="provider-group-title">{roleLabel(role)}</h3>
          <ul className="provider-rows">
            {providers
              .filter((row) => row.role === role)
              .map((row) => (
                <li key={row.id} className="provider-row" data-testid={`settings-provider-${row.id}`}>
                  <span className="provider-name">
                    <strong>{row.display_name}</strong>
                    <span className="settings-provider-id mono">{row.id}</span>
                  </span>
                  <span className="provider-tags">
                    <ProvenTag proven={row.proven} />
                    {row.wired && <span className="pill pill-info settings-tag settings-tag-wired">wired</span>}
                    <span className="settings-provider-status mono">{row.status}</span>
                  </span>
                  {row.evidence && <span className="settings-evidence provider-evidence">{row.evidence}</span>}
                </li>
              ))}
          </ul>
        </div>
      ))}

      <div className="panel-head panel-head-sub">
        <h2 className="panel-title">Voices</h2>
        <p className="panel-copy">
          The realtime lane&apos;s voice options. Only <strong>{proven.voice}</strong> has the evidence of
          a real call. The cascade providers&apos; voice catalogs are fetched per provider from their
          accounts; every one of them is untested by the same bar.
        </p>
      </div>
      <ul className="provider-rows settings-voice-list">
        {realtime_voices.map((voice) => (
          <li key={voice.id} className="provider-row" data-testid={`settings-voice-${voice.id}`}>
            <span className="provider-name">
              <strong className="mono">{voice.id}</strong>
            </span>
            <span className="provider-tags">
              <ProvenTag proven={voice.proven} />
            </span>
            <span className="settings-evidence provider-evidence">{voice.evidence}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

export function SettingsView({ section }: { section: string }) {
  const [settings, setSettings] = useState<SettingsData | null>(null);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [active, setActive] = useState<ActiveResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    fetchSettings()
      .then((data) => alive && setSettings(data))
      .catch((err) => alive && setError(String(err.message || err)));
    fetchAgentsScreen()
      .then((data) => {
        if (!alive) return;
        setAgents(data.agents);
        setActive(data.active);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, []);

  const current: SectionId = (SECTIONS.find((s) => s.id === section)?.id as SectionId) ?? 'defaults';
  const hrefFor = (id: SectionId) => (id === 'defaults' ? '/settings' : `/settings/${id}`);

  if (error && !settings) {
    return (
      <div className="page">
        <div className="alert-banner alert-unreachable">
          <div>
            <strong>Settings could not be loaded</strong>
            <div>{error}</div>
          </div>
        </div>
      </div>
    );
  }

  if (!settings) return <div className="spinner" />;

  return (
    <div className="page page-wide settings-page" data-testid="settings-page">
      <PageHeader title="Settings" icon={<Settings2 size={20} />} lede="How calls sound by default, and the numbers they can reach." />

      <div className="settings-layout">
        <nav className="section-nav" aria-label="Settings sections">
          {SECTIONS.map((s) => (
            <Link
              key={s.id}
              href={hrefFor(s.id)}
              className={'section-nav-link' + (current === s.id ? ' active' : '')}
              aria-current={current === s.id ? 'page' : undefined}
              data-testid={`settings-nav-${s.id}`}
            >
              <span>{s.label}</span>
              <span className="section-nav-blurb">{s.blurb}</span>
            </Link>
          ))}
        </nav>
        <label className="section-select">
          <span className="sr-only">Settings section</span>
          <select className="field-input" value={current} onChange={(e) => navigate(hrefFor(e.target.value as SectionId))}>
            {SECTIONS.map((s) => (
              <option key={s.id} value={s.id}>
                {s.label}
              </option>
            ))}
          </select>
        </label>

        <div className="settings-content">
          {current === 'defaults' && <Defaults settings={settings} agents={agents} active={active} />}
          {current === 'speed-dial' && <SpeedDialSection initial={settings.speed_dial} />}
          {current === 'advanced' && <Advanced settings={settings} />}
        </div>
      </div>
    </div>
  );
}
