/* The Agent creation wizard (ticket 13).
 *
 * You answer questions about who this new Agent is, press Create, and you have
 * a new Agent you can put on an Outlet. Because an Agent IS a Hermes profile
 * (ADR 0001), pressing Create makes a new being: its own soul, its own tools,
 * its own memory, its own model.
 *
 * Two properties this file is built around, both of them load-bearing:
 *
 * 1. NOTHING IS WRITTEN UNTIL CREATE. Every answer lives in this component's
 *    state and nowhere else -- there is no draft endpoint, no autosave, no
 *    server-side wizard session. Closing the tab halfway therefore cannot leave
 *    a partial Agent on disk, because halfway through there is nothing on disk
 *    to be partial. The backend covers the remaining case (a crash DURING the
 *    one write) with ticket 12's `.incomplete` marker; see
 *    hermes_profiles.create_profile.
 *
 * 2. INHERITANCE IS THE QUICK PATH. With a profile to inherit from, the wizard
 *    is four steps: who it is, its voice, Telegram, review. Declining
 *    inheritance -- or asking to change what it inherits -- adds the
 *    substantive questions (its soul, the model it thinks with, its skills and
 *    tools, where its memory lives) as three more steps. The step list is
 *    computed from that choice rather than hidden with CSS, so "quick" is a
 *    fact about the flow and not a claim about it.
 *
 * Depth belongs under Advanced, which is ticket 14. What is here is the
 * proven default preselected, with everything else still selectable and
 * honestly labelled (VC7).
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { PageHeader } from './Shell';
import {
  createAgent,
  fetchHermesState,
  fetchInheritable,
  fetchProviders,
  fetchVoices,
} from './api';
import type {
  CreateAgentRequest,
  HermesState,
  Inheritable,
  ProviderRow,
  VoiceCatalog,
} from './types';
import {
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  Bot,
  Check,
  Loader2,
  Sparkles,
  X,
} from 'lucide-react';

type StepId = 'identity' | 'soul' | 'mind' | 'memory' | 'voice' | 'telegram' | 'review';

const STEP_TITLE: Record<StepId, string> = {
  identity: 'Who is it',
  soul: 'Its soul',
  mind: 'What it thinks with',
  memory: 'Where its memory lives',
  voice: 'Its voice',
  telegram: 'Telegram',
  review: 'Review',
};

/* The realtime lane has no voice-catalog API, so the voice list is a suggestion,
 * not a contract -- the field stays free text and a blank means "whatever the
 * bridge already uses". The ids come from the ONE backend source
 * (settings_catalog, served as `state.realtime_voices`), never a copy kept in
 * this file. */

interface Draft {
  name: string;
  description: string;
  inheritFrom: string;
  inheritSkills: boolean;
  customise: boolean;
  identityName: string;
  soul: string;
  model: string;
  modelProvider: string;
  toolsProfile: string;
  memory: string;
  pipeline: string;
  realtimeProvider: string;
  sttProvider: string;
  llmProvider: string;
  ttsProvider: string;
  voice: string;
  persona: string;
  telegramConnect: boolean;
  telegramToken: string;
  telegramDmPolicy: string;
}

/* The draft before /api/hermes has answered. `pipeline` and `realtimeProvider`
 * are deliberately BLANK here rather than carrying a copy of the proven
 * default: which lane is proven is the backend's answer (`proven`), and a
 * second copy of it in this file is a copy that can drift from the one the API
 * serves while every screenshot still looks right. Nothing renders from this
 * draft - the component shows a spinner until the state arrives and fills
 * both. */
function emptyDraft(): Draft {
  return {
    name: '',
    description: '',
    inheritFrom: '',
    inheritSkills: false,
    customise: false,
    identityName: '',
    soul: '',
    model: '',
    modelProvider: '',
    toolsProfile: '',
    memory: 'shared',
    pipeline: '',
    realtimeProvider: '',
    sttProvider: 'deepgram',
    llmProvider: 'gpt-4.1',
    ttsProvider: 'elevenlabs',
    voice: '',
    persona: '',
    telegramConnect: false,
    telegramToken: '',
    telegramDmPolicy: 'pairing',
  };
}

function stepsFor(draft: Draft): StepId[] {
  const substantive: StepId[] = ['soul', 'mind', 'memory'];
  const scratch = draft.inheritFrom === '';
  return [
    'identity',
    ...(scratch || draft.customise ? substantive : []),
    'voice',
    'telegram',
    'review',
  ];
}

function toRequest(draft: Draft): CreateAgentRequest {
  const providers: Record<string, string> =
    draft.pipeline === 'cascade'
      ? { stt: draft.sttProvider, llm: draft.llmProvider, tts: draft.ttsProvider }
      : { realtime: draft.realtimeProvider };
  const knobs: Record<string, string> = {};
  if (draft.voice.trim()) knobs.voice = draft.voice.trim();
  const scratch = draft.inheritFrom === '';
  const answered = scratch || draft.customise;
  return {
    name: draft.name.trim(),
    description: draft.description.trim(),
    inherit_from: draft.inheritFrom || null,
    inherit_skills: draft.inheritSkills,
    identity_name: draft.identityName.trim() || undefined,
    soul: answered && draft.soul.trim() ? draft.soul : undefined,
    model: answered && draft.model.trim() ? draft.model.trim() : undefined,
    model_provider:
      answered && draft.modelProvider.trim() ? draft.modelProvider.trim() : undefined,
    tools_profile: answered && draft.toolsProfile ? draft.toolsProfile : undefined,
    memory: answered ? draft.memory : undefined,
    telegram_connect: draft.telegramConnect,
    telegram_bot_token: draft.telegramConnect ? draft.telegramToken.trim() : undefined,
    telegram_dm_policy: draft.telegramConnect ? draft.telegramDmPolicy : undefined,
    pipeline: draft.pipeline,
    providers,
    knobs,
    persona: draft.persona.trim() || undefined,
  };
}

/* The two naming rules an Agent has to satisfy at once -- voicecore's agent id
 * and the Hermes profile directory name. Checked here only to say so early;
 * the backend refuses independently and is the authority. */
const NAME_RE = /^[a-z0-9][a-z0-9_-]{0,62}$/;

/* The shape @BotFather issues, mirroring hermes_profiles.TELEGRAM_TOKEN_RE. The
 * backend refuses independently and is the authority; this exists so the
 * operator is told at the step where they typed it, rather than after pressing
 * Create. A token this rejects would be written verbatim into the profile's
 * .env and only surface as a bot that never comes online. */
const TELEGRAM_TOKEN_RE = /^\d+:[A-Za-z0-9_-]{35}$/;

/* Never interpolates the value into the message: this string is rendered on
 * screen, and a credential does not belong on a screen or in a screenshot. */
function telegramTokenProblem(token: string): string | null {
  const text = token.trim();
  if (!text) return 'Paste the new bot\u2019s token from @BotFather, or turn Telegram off.';
  if (!TELEGRAM_TOKEN_RE.test(text))
    return 'That is not the shape of a Telegram bot token. @BotFather issues <bot_id>:<auth_token> \u2014 digits, a colon, then 35 characters of letters, digits, - and _.';
  return null;
}

function nameProblem(name: string): string | null {
  if (!name) return 'An Agent needs a name.';
  if (!NAME_RE.test(name))
    return 'Lowercase letters, digits, - and _ only, starting with a letter or digit. The name is the Hermes profile directory as well as the agent id, so a dot is not allowed in it.';
  if (name === 'default')
    return '"default" is the container\'s own Hermes profile. Pick another name.';
  return null;
}

function Blocked({ state }: { state: HermesState }) {
  return (
    <div className="alert-banner alert-unreachable" data-testid="wizard-unavailable">
      <AlertTriangle className="alert-icon" />
      <div>
        <strong>This dashboard cannot create a Hermes profile yet</strong>
        <div data-testid="wizard-unavailable-reason">{state.reason}</div>
        <div className="wizard-deploy-hint mono">{state.deploy_hint}</div>
      </div>
    </div>
  );
}

interface Props {
  onClose: () => void;
  onCreated: (name: string) => void;
}

export function CreateAgentWizard({ onClose, onCreated }: Props) {
  const [state, setState] = useState<HermesState | null>(null);
  const [providers, setProviders] = useState<ProviderRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft>(emptyDraft);
  const [index, setIndex] = useState(0);
  const [inherited, setInherited] = useState<Inheritable | null>(null);
  const [voices, setVoices] = useState<VoiceCatalog | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    fetchHermesState()
      .then((next) => {
        if (!alive) return;
        setState(next);
        const inheritable = next.profiles.filter((row) => row.complete);
        setDraft((previous) => ({
          ...previous,
          /* Inheritance is the DEFAULT path, so it is chosen for the operator
           * whenever there is something to inherit from -- not offered as the
           * first of two equal radios that both start unselected. With nothing
           * complete on disk, from-scratch is the only truthful default. */
          inheritFrom: inheritable.length ? inheritable[0].name : '',
          pipeline: next.proven.pipeline,
          realtimeProvider: next.proven.realtime_provider,
        }));
      })
      .catch((reason: Error) => alive && setLoadError(reason.message))
      .finally(() => alive && setLoading(false));
    fetchProviders()
      .then((rows) => alive && setProviders(rows))
      /* The provider list is a convenience: without it the selects fall back to
       * the ids the backend already defaults to, and creation still works. */
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, []);

  /* What inheritance would take, fetched when a source is chosen so the screen
   * shows the real values rather than the word "inherited". */
  useEffect(() => {
    if (!draft.inheritFrom) {
      setInherited(null);
      return;
    }
    let alive = true;
    fetchInheritable(draft.inheritFrom)
      .then((next) => {
        if (!alive) return;
        setInherited(next);
        setDraft((previous) => ({
          ...previous,
          soul: previous.soul || next.soul,
          model: previous.model || next.model || '',
          modelProvider: previous.modelProvider || next.model_provider || '',
          toolsProfile: previous.toolsProfile || next.tools_profile || '',
          memory: next.memory_shared ? 'shared' : 'private',
        }));
      })
      .catch(() => alive && setInherited(null));
    return () => {
      alive = false;
    };
  }, [draft.inheritFrom]);

  const ttsProvider = draft.pipeline === 'cascade' ? draft.ttsProvider : '';
  useEffect(() => {
    if (!ttsProvider) {
      setVoices(null);
      return;
    }
    let alive = true;
    fetchVoices(ttsProvider)
      .then((next) => alive && setVoices(next))
      .catch(() => alive && setVoices(null));
    return () => {
      alive = false;
    };
  }, [ttsProvider]);

  const steps = useMemo(() => stepsFor(draft), [draft]);
  const step = steps[Math.min(index, steps.length - 1)];
  const set = useCallback(
    <K extends keyof Draft>(key: K, value: Draft[K]) =>
      setDraft((previous) => ({ ...previous, [key]: value })),
    []
  );

  const problem = nameProblem(draft.name.trim());
  const telegramProblem = draft.telegramConnect
    ? telegramTokenProblem(draft.telegramToken)
    : null;
  const blockingProblem =
    step === 'identity' ? problem : step === 'telegram' ? telegramProblem : null;

  const submit = () => {
    setSubmitting(true);
    setError(null);
    createAgent(toRequest(draft))
      .then((res) => onCreated(res.profile.name))
      .catch((reason: Error) => setError(reason.message))
      .finally(() => setSubmitting(false));
  };

  if (loading) return <div className="spinner" />;

  if (loadError || state === null) {
    return (
      <div className="alert-banner alert-unreachable" data-testid="wizard-error">
        <AlertTriangle className="alert-icon" />
        <div>
          <strong>The wizard could not be loaded</strong>
          <div>{loadError || 'No answer from /api/hermes.'}</div>
        </div>
      </div>
    );
  }

  const complete = state.profiles.filter((row) => row.complete);
  const byRole = (role: string) => providers.filter((row) => row.role === role);

  return (
    <div className="page page-wide wizard" data-testid="agent-wizard">
      <PageHeader
        title="New Agent"
        icon={<Bot size={20} />}
        lede="An Agent is a Hermes profile (ADR 0001). Creating one creates a new being: its own soul, its own tools, its own memory, its own model."
      />

      {!state.available && <Blocked state={state} />}

      <ol className="wizard-steps" data-testid="wizard-steps">
        {steps.map((id, position) => (
          <li
            key={id}
            className={
              'wizard-step' +
              (position === index ? ' wizard-step-current' : '') +
              (position < index ? ' wizard-step-done' : '')
            }
            data-testid={`wizard-step-${id}`}
          >
            <span className="wizard-step-index">
              {position < index ? <Check size={13} aria-label="done" /> : position + 1}
            </span>
            <span className="wizard-step-title">{STEP_TITLE[id]}</span>
          </li>
        ))}
      </ol>

      <div className="wizard-body">
      <div className="wizard-panel">
        {step === 'identity' && (
          <section data-testid="wizard-panel-identity">
            <label className="field">
              <span className="field-label">Name</span>
              <input
                className="field-input mono"
                data-testid="wizard-name"
                value={draft.name}
                placeholder="nora"
                onChange={(event) => set('name', event.target.value)}
              />
              <span className="field-help">
                This is the Hermes profile directory as well as the agent id. One Agent, one
                name.
              </span>
            </label>
            {draft.name.trim() !== '' && problem && (
              <p className="field-error" data-testid="wizard-name-error">
                {problem}
              </p>
            )}

            <label className="field">
              <span className="field-label">Description</span>
              <input
                className="field-input"
                data-testid="wizard-description"
                value={draft.description}
                placeholder="What is it for?"
                onChange={(event) => set('description', event.target.value)}
              />
            </label>

            <fieldset className="field wizard-inherit">
              <legend className="field-label">Where it starts from</legend>
              <label className="wizard-radio">
                <input
                  type="radio"
                  name="inherit"
                  data-testid="wizard-inherit-yes"
                  checked={draft.inheritFrom !== ''}
                  disabled={complete.length === 0}
                  onChange={() =>
                    set('inheritFrom', complete.length ? complete[0].name : '')
                  }
                />
                <span>
                  <strong>Inherit from an existing Agent</strong> — take everything and change
                  only what you want. The quick path.
                </span>
              </label>
              {draft.inheritFrom !== '' && (
                <select
                  className="field-input"
                  data-testid="wizard-inherit-source"
                  value={draft.inheritFrom}
                  onChange={(event) => set('inheritFrom', event.target.value)}
                >
                  {complete.map((row) => (
                    <option key={row.name} value={row.name}>
                      {row.name}
                    </option>
                  ))}
                </select>
              )}
              {complete.length === 0 && (
                <p className="field-help" data-testid="wizard-no-sources">
                  There is no complete Hermes profile to inherit from yet, so this one is built
                  from scratch.
                </p>
              )}
              <label className="wizard-radio">
                <input
                  type="radio"
                  name="inherit"
                  data-testid="wizard-inherit-no"
                  checked={draft.inheritFrom === ''}
                  onChange={() => set('inheritFrom', '')}
                />
                <span>
                  <strong>Start from scratch</strong> — answer the substantive questions: its
                  soul, its model, its skills and tools, where its memory lives.
                </span>
              </label>
            </fieldset>

            {inherited && (
              <div className="wizard-inherited" data-testid="wizard-inherited-summary">
                <div className="wizard-inherited-title">
                  <Sparkles size={14} /> Taking from {inherited.profile}
                </div>
                <dl className="wizard-inherited-grid">
                  <dt>Model</dt>
                  <dd className="mono">{inherited.model || 'the Hermes default'}</dd>
                  <dt>Tools</dt>
                  <dd className="mono">{inherited.tools_profile || 'none'}</dd>
                  <dt>Memory</dt>
                  <dd>
                    {inherited.memory_shared
                      ? 'the shared Hindsight banks'
                      : 'its own, private'}
                  </dd>
                  <dt>Skills</dt>
                  <dd>{inherited.skills.length} in that profile</dd>
                </dl>
                <label className="wizard-check">
                  <input
                    type="checkbox"
                    data-testid="wizard-inherit-skills"
                    checked={draft.inheritSkills}
                    onChange={(event) => set('inheritSkills', event.target.checked)}
                  />
                  Copy its skills across as well
                </label>
                <label className="wizard-check">
                  <input
                    type="checkbox"
                    data-testid="wizard-customise"
                    checked={draft.customise}
                    onChange={(event) => set('customise', event.target.checked)}
                  />
                  Change what it inherits (adds the soul, model and memory steps)
                </label>
                <p className="field-help">
                  Not inherited, ever: its name, and the source&apos;s gateway port. Two beings
                  introducing themselves as the same person, or racing for one port, is not
                  something &ldquo;take everything&rdquo; should take.
                </p>
              </div>
            )}
          </section>
        )}

        {step === 'soul' && (
          <section data-testid="wizard-panel-soul">
            <label className="field">
              <span className="field-label">What it calls itself</span>
              <input
                className="field-input"
                data-testid="wizard-identity-name"
                value={draft.identityName}
                placeholder={draft.name || 'Nora'}
                onChange={(event) => set('identityName', event.target.value)}
              />
            </label>
            <label className="field">
              <span className="field-label">Its soul (SOUL.md)</span>
              <textarea
                className="field-input wizard-textarea"
                data-testid="wizard-soul"
                rows={12}
                value={draft.soul}
                placeholder={'# ' + (draft.name || 'Nora') + '\nWho it is, and how it behaves.'}
                onChange={(event) => set('soul', event.target.value)}
              />
              <span className="field-help">
                Written to the profile&apos;s SOUL.md. It is who the being is everywhere — on
                Telegram as much as on a call.
              </span>
            </label>
            <label className="field">
              <span className="field-label">Call persona (optional)</span>
              <textarea
                className="field-input wizard-textarea"
                data-testid="wizard-persona"
                rows={4}
                value={draft.persona}
                onChange={(event) => set('persona', event.target.value)}
              />
              <span className="field-help">
                Appended to the bridge&apos;s prompt on a call only. The soul above is the
                being; this is how it speaks on the phone.
              </span>
            </label>
          </section>
        )}

        {step === 'mind' && (
          <section data-testid="wizard-panel-mind">
            <label className="field">
              <span className="field-label">The model it thinks with</span>
              <input
                className="field-input mono"
                data-testid="wizard-model"
                value={draft.model}
                placeholder="minimax/minimax-m2.7"
                onChange={(event) => set('model', event.target.value)}
              />
              <span className="field-help">
                Blank leaves the Hermes default. This is the profile&apos;s own brain, not the
                voice pipeline&apos;s.
              </span>
            </label>
            <label className="field">
              <span className="field-label">Model provider</span>
              <input
                className="field-input mono"
                data-testid="wizard-model-provider"
                value={draft.modelProvider}
                placeholder="openrouter"
                onChange={(event) => set('modelProvider', event.target.value)}
              />
            </label>
            <label className="field">
              <span className="field-label">Tools it gets</span>
              <select
                className="field-input"
                data-testid="wizard-tools"
                value={draft.toolsProfile}
                onChange={(event) => set('toolsProfile', event.target.value)}
              >
                <option value="">inherit the default tool profile</option>
                {state.tool_profiles.map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </select>
              <span className="field-help">
                Skills live in the profile&apos;s own <span className="mono">skills/</span>{' '}
                directory and start empty unless you copied them across on the first step.
              </span>
            </label>
          </section>
        )}

        {step === 'memory' && (
          <section data-testid="wizard-panel-memory">
            <fieldset className="field">
              <legend className="field-label">Where its memory lives</legend>
              <label className="wizard-radio">
                <input
                  type="radio"
                  name="memory"
                  data-testid="wizard-memory-shared"
                  checked={draft.memory === 'shared'}
                  onChange={() => set('memory', 'shared')}
                />
                <span>
                  <strong>The shared Hindsight banks</strong> — it can consult everything the
                  other profiles remember.
                </span>
              </label>
              <label className="wizard-radio">
                <input
                  type="radio"
                  name="memory"
                  data-testid="wizard-memory-private"
                  checked={draft.memory === 'private'}
                  onChange={() => set('memory', 'private')}
                />
                <span>
                  <strong>Its own, private</strong> — only the profile&apos;s own{' '}
                  <span className="mono">memory/</span> and sessions. It remembers its own
                  conversations and nothing else.
                </span>
              </label>
              <p className="field-help">
                A Hindsight bank of its own is not offered here: a new bank has to be
                provisioned in the memory stack first, so a choice here would promise something
                this app cannot deliver.
              </p>
            </fieldset>
          </section>
        )}

        {step === 'voice' && (
          <section data-testid="wizard-panel-voice">
            <fieldset className="field">
              <legend className="field-label">How it speaks</legend>
              <label className="wizard-radio">
                <input
                  type="radio"
                  name="pipeline"
                  data-testid="wizard-pipeline-realtime"
                  checked={draft.pipeline === 'realtime'}
                  onChange={() => set('pipeline', 'realtime')}
                />
                <span>
                  <strong>Realtime</strong> — speech to speech.{' '}
                  <span className="wizard-proven">Proven</span> on both Outlets, and what this
                  wizard preselects.
                </span>
              </label>
              <label className="wizard-radio">
                <input
                  type="radio"
                  name="pipeline"
                  data-testid="wizard-pipeline-cascade"
                  checked={draft.pipeline === 'cascade'}
                  onChange={() => set('pipeline', 'cascade')}
                />
                <span>
                  <strong>Cascade</strong> — transcribe, think, speak.{' '}
                  <span className="wizard-unproven">Outbound only</span>, and the Talk Outlet
                  refuses it.
                </span>
              </label>
            </fieldset>

            {draft.pipeline === 'realtime' ? (
              <label className="field">
                <span className="field-label">Realtime provider</span>
                <select
                  className="field-input"
                  data-testid="wizard-realtime-provider"
                  value={draft.realtimeProvider}
                  onChange={(event) => set('realtimeProvider', event.target.value)}
                >
                  {(byRole('realtime').length
                    ? byRole('realtime')
                    : [
                        {
                          id: state.proven.realtime_provider,
                          display_name: state.proven.realtime_provider,
                          role: 'realtime',
                          status: '',
                          default_knobs: {},
                        },
                      ]
                  ).map((row) => (
                    <option key={row.id} value={row.id}>
                      {row.display_name}
                      {row.id === state.proven.realtime_provider
                        ? ' — proven'
                        : ' — not wired on this build'}
                    </option>
                  ))}
                </select>
              </label>
            ) : (
              <div className="wizard-grid">
                <label className="field">
                  <span className="field-label">Hearing (STT)</span>
                  <select
                    className="field-input"
                    data-testid="wizard-stt-provider"
                    value={draft.sttProvider}
                    onChange={(event) => set('sttProvider', event.target.value)}
                  >
                    {byRole('stt').map((row) => (
                      <option key={row.id} value={row.id}>
                        {row.display_name}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="field">
                  <span className="field-label">Thinking (LLM)</span>
                  <select
                    className="field-input"
                    data-testid="wizard-llm-provider"
                    value={draft.llmProvider}
                    onChange={(event) => set('llmProvider', event.target.value)}
                  >
                    {byRole('llm').map((row) => (
                      <option key={row.id} value={row.id}>
                        {row.display_name}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="field">
                  <span className="field-label">Speaking (TTS)</span>
                  <select
                    className="field-input"
                    data-testid="wizard-tts-provider"
                    value={draft.ttsProvider}
                    onChange={(event) => set('ttsProvider', event.target.value)}
                  >
                    {byRole('tts').map((row) => (
                      <option key={row.id} value={row.id}>
                        {row.display_name}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
            )}

            <label className="field">
              <span className="field-label">Voice</span>
              <input
                className="field-input mono"
                data-testid="wizard-voice"
                list="wizard-voice-options"
                value={draft.voice}
                placeholder="leave blank for the bridge's own default"
                onChange={(event) => set('voice', event.target.value)}
              />
              <datalist id="wizard-voice-options">
                {(voices && voices.voices
                  ? voices.voices.map((entry) => entry.id)
                  : (state.realtime_voices || []).map((v) => v.id)
                ).map((id) => (
                  <option key={id} value={id} />
                ))}
              </datalist>
              <span className="field-help" data-testid="wizard-voice-help">
                {voices && voices.voices
                  ? `${voices.voices.length} voices, ${
                      voices.source === 'account' ? 'from the account' : 'a curated list'
                    }.`
                  : voices && voices.detail
                    ? voices.detail
                    : 'Suggestions only — this lane has no voice catalog to read, so an id here is not verified until a call is placed.'}
              </span>
            </label>
            <p className="field-help">
              Everything else — VAD, temperature, keyterms — lives under Advanced, which is not
              built yet. Nothing is removed; it is just not asked here.
            </p>
          </section>
        )}

        {step === 'telegram' && (
          <section data-testid="wizard-panel-telegram">
            <label className="wizard-check">
              <input
                type="checkbox"
                data-testid="wizard-telegram-connect"
                checked={draft.telegramConnect}
                onChange={(event) => set('telegramConnect', event.target.checked)}
              />
              <span>
                <strong>Give it its own Telegram bot</strong> — a profile is reachable on
                Telegram as much as on a call, so this is part of what it is.
              </span>
            </label>
            {draft.telegramConnect && (
              <>
                <label className="field">
                  <span className="field-label">Bot token from @BotFather</span>
                  <input
                    className="field-input mono"
                    data-testid="wizard-telegram-token"
                    type="password"
                    autoComplete="off"
                    value={draft.telegramToken}
                    onChange={(event) => set('telegramToken', event.target.value)}
                  />
                  <span className="field-help">
                    Written to the profile&apos;s own <span className="mono">.env</span>, mode
                    0600. It is never shown again by this dashboard and never comes back out of
                    any endpoint — keep your own copy.
                  </span>
                </label>
                <label className="field">
                  <span className="field-label">Who may DM it</span>
                  <select
                    className="field-input"
                    data-testid="wizard-telegram-policy"
                    value={draft.telegramDmPolicy}
                    onChange={(event) => set('telegramDmPolicy', event.target.value)}
                  >
                    <option value="pairing">pairing — a code has to be exchanged first</option>
                    <option value="allowlist">allowlist — only known chats</option>
                    <option value="open">open — anybody who finds the bot</option>
                  </select>
                </label>
                {telegramProblem && (
                  <p className="field-error" data-testid="wizard-telegram-error">
                    {telegramProblem}
                  </p>
                )}
              </>
            )}
            {!draft.telegramConnect && (
              <p className="field-help" data-testid="wizard-telegram-off">
                Not connected. It answers on the Outlets you assign it to, and nowhere else. An
                inherited Telegram setting is switched off rather than copied — two beings
                cannot share one bot.
              </p>
            )}
          </section>
        )}

        {step === 'review' && (
          <section data-testid="wizard-panel-review">
            <dl className="wizard-review" data-testid="wizard-review">
              <dt>Name</dt>
              <dd className="mono" data-testid="review-name">
                {draft.name.trim() || '—'}
              </dd>
              <dt>Hermes profile</dt>
              <dd className="mono" data-testid="review-profile">
                {state.profiles_dir}/{draft.name.trim() || '…'}
              </dd>
              <dt>Starts from</dt>
              <dd data-testid="review-inherit">
                {draft.inheritFrom ? `${draft.inheritFrom} (inherited)` : 'scratch'}
              </dd>
              <dt>Model</dt>
              <dd className="mono">{draft.model || 'the Hermes default'}</dd>
              <dt>Memory</dt>
              <dd data-testid="review-memory">
                {draft.memory === 'shared' ? 'the shared Hindsight banks' : 'its own, private'}
              </dd>
              <dt>Voice</dt>
              <dd className="mono" data-testid="review-voice">
                {draft.pipeline}
                {' · '}
                {draft.pipeline === 'cascade'
                  ? `${draft.sttProvider} / ${draft.llmProvider} / ${draft.ttsProvider}`
                  : draft.realtimeProvider}
                {draft.voice ? ` · ${draft.voice}` : ''}
              </dd>
              <dt>Telegram</dt>
              <dd data-testid="review-telegram">
                {draft.telegramConnect ? 'its own bot' : 'not connected'}
              </dd>
            </dl>
            <p className="field-help">
              Nothing has been written yet. Pressing Create writes the profile directory and the
              Agent together; if any part of it fails, neither is left behind.
            </p>
            {error && (
              <p className="field-error" data-testid="wizard-submit-error">
                <AlertTriangle size={14} /> {error}
              </p>
            )}
          </section>
        )}
      </div>

      <aside className="wizard-summary" aria-label="Your choices so far" data-testid="wizard-summary">
        <h2 className="wizard-summary-title">So far</h2>
        <dl className="wizard-summary-list">
          <dt>Name</dt>
          <dd className="mono">{draft.name.trim() || <span className="muted">not yet</span>}</dd>
          <dt>Starts from</dt>
          <dd>{draft.inheritFrom ? `${draft.inheritFrom} (inherited)` : 'scratch'}</dd>
          <dt>Model</dt>
          <dd className="mono">{draft.model || (inherited?.model ?? 'the Hermes default')}</dd>
          <dt>Memory</dt>
          <dd>{draft.memory === 'shared' ? 'the shared Hindsight banks' : 'its own, private'}</dd>
          <dt>Voice</dt>
          <dd className="mono">
            {draft.pipeline === 'cascade'
              ? `cascade · ${draft.sttProvider} / ${draft.llmProvider} / ${draft.ttsProvider}`
              : `realtime · ${draft.realtimeProvider}`}
            {draft.voice ? ` · ${draft.voice}` : ''}
          </dd>
          <dt>Telegram</dt>
          <dd>{draft.telegramConnect ? 'its own bot' : 'not connected'}</dd>
        </dl>
        <p className="wizard-footnote">
          <Bot size={13} /> Nothing is saved while you are answering. Close this and no Agent, and no
          half-made profile, is left behind.
        </p>
      </aside>
      </div>

      <div className="wizard-nav">
        <button className="btn btn-ghost wizard-close" onClick={onClose} data-testid="wizard-cancel">
          <X size={15} /> Cancel
        </button>
        <span className="wizard-nav-spacer" />
        <button
          className="btn btn-secondary wizard-btn"
          data-testid="wizard-back"
          disabled={index === 0 || submitting}
          onClick={() => setIndex((position) => Math.max(0, position - 1))}
        >
          <ArrowLeft size={15} /> Back
        </button>
        {step === 'review' ? (
          <button
            className="btn btn-primary wizard-btn wizard-btn-primary"
            data-testid="wizard-create"
            disabled={submitting || !state.available || problem !== null || telegramProblem !== null}
            onClick={submit}
          >
            {submitting ? <Loader2 size={15} className="wizard-spin" /> : <Check size={15} />}
            Create Agent
          </button>
        ) : (
          <button
            className="btn btn-primary wizard-btn wizard-btn-primary"
            data-testid="wizard-next"
            disabled={blockingProblem !== null || submitting}
            onClick={() => setIndex((position) => Math.min(steps.length - 1, position + 1))}
          >
            Next <ArrowRight size={15} />
          </button>
        )}
      </div>

    </div>
  );
}
