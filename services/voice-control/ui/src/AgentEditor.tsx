/* One Agent's voice and tools, edited on that Agent's own page (A2).
 *
 * This is the form that lived on Settings as "Agent voice" behind an Agent
 * picker (ticket 14, VC24, ticket 19). Only its place changed: the drafts, the
 * two narrow write endpoints and their refusal guards are exactly as they were.
 * Every option stays selectable, with the honest labels the backend serves
 * (VC7: nothing is removed, only reorganised).
 *
 * The drafts are seeded from THIS Agent's own document, never from the
 * catalog's proven default: opening an Agent must show that Agent's voice, and
 * saving without touching a field must not rewrite it. */
import { useEffect, useMemo, useState } from 'react';
import type { FormEvent } from 'react';
import { Check, Loader2, RotateCcw, Save } from 'lucide-react';
import { fetchAgentsScreen, updateAgentHermesProfile, updateAgentVoice } from './api';
import { ProvenTag } from './ui';
import type { Agent, ProviderRow, SelectableProfile, SettingsData, VoiceCatalog } from './types';

/* VC24: the agent type that is not a preset. Every outside-vendor combination lives
   here, behind the same pipeline and provider controls this page has always had. */
const CUSTOM_TYPE = 'custom';

/* Deepgram models offered on the Listening card. A suggestion list, not a gate: the
   field is free text, because Deepgram adds models faster than this page ships. */
const DEEPGRAM_MODELS = ['nova-3', 'nova-2', 'nova-2-phonecall'];

function knobString(knobs: Record<string, unknown>, key: string): string {
  const value = knobs[key];
  return typeof value === 'string' ? value : '';
}

/* A boolean knob has three states, and the third matters: 'inherit' sends nothing, so
   an Agent that never touched the Listening card dials the URL it always dialled. */
function knobTriState(knobs: Record<string, unknown>, key: string): string {
  const value = knobs[key];
  return value === true ? 'on' : value === false ? 'off' : 'inherit';
}

function triStateKnob(state: string): boolean | null {
  return state === 'on' ? true : state === 'off' ? false : null;
}

interface Draft {
  pipeline: string;
  realtimeProvider: string;
  voice: string;
  stt: string;
  llm: string;
  tts: string;
  type: string;
  profile: string;
  tools: boolean;
  sttModel: string;
  language: string;
  keyterms: string;
  smartFormat: string;
  numerals: string;
}

/* Pipeline and provider fall back to the proven catalog only when the Agent
   carries neither. Voice does not: a missing knobs.voice is a deliberate
   inherit-the-stack default. Show that as placeholder text, never as a value -
   prefilling the proven voice and sending it on Save would pin a wizard-default
   Agent forever. */
function seed(row: Agent, proven: SettingsData['proven']): Draft {
  const providers = row.providers || {};
  const knobs = row.knobs || {};
  const tools = row.guardrails?.on_call_tools;
  return {
    pipeline: row.pipeline || proven.pipeline || 'realtime',
    realtimeProvider: providers.realtime || proven.realtime_provider || '',
    stt: providers.stt || '',
    llm: providers.llm || '',
    tts: providers.tts || '',
    voice: knobString(knobs, 'voice'),
    type: row.agent_type || CUSTOM_TYPE,
    profile: row.hermes_profile || 'default',
    tools: row.valid && row.guardrails !== null && (tools === undefined || tools === true),
    sttModel: knobString(knobs, 'transcription_model'),
    language: knobString(knobs, 'language'),
    keyterms: Array.isArray(knobs.keyterms) ? knobs.keyterms.join(', ') : '',
    smartFormat: knobTriState(knobs, 'smart_format'),
    numerals: knobTriState(knobs, 'numerals'),
  };
}

interface Props {
  agent: Agent;
  settings: SettingsData;
  selectable: SelectableProfile[];
  elevenVoices: VoiceCatalog | null;
  auraVoices?: VoiceCatalog | null;
  tab: 'voice' | 'tools';
  onSaved: (agents: Agent[]) => void;
}

export function AgentEditor({
  agent,
  settings,
  selectable,
  elevenVoices,
  auraVoices = null,
  tab,
  onSaved,
}: Props) {
  const { proven, pipelines, providers, realtime_voices } = settings;
  const agentTypes = settings.agent_types || [];
  const initial = useMemo(() => seed(agent, proven), [agent, proven]);
  const [draft, setDraft] = useState<Draft>(initial);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  /* A save re-reads the roster, which re-seeds from the document just written;
     the saved message stays. Another Agent is another editor (the parent keys it
     by id), so nothing here carries across Agents. */
  useEffect(() => {
    setDraft(initial);
  }, [initial]);

  const set = <K extends keyof Draft>(key: K, value: Draft[K]) =>
    setDraft((prev) => ({ ...prev, [key]: value }));

  const dirty = JSON.stringify(draft) !== JSON.stringify(initial);
  useEffect(() => {
    if (dirty) setMessage(null);
  }, [dirty]);
  const isHermes = draft.pipeline === 'cascade' && draft.llm === 'hermes-agent';
  const usesElevenLabs = draft.pipeline === 'cascade' && draft.tts === 'elevenlabs';
  const usesDeepgram = draft.pipeline === 'cascade' && draft.stt === 'deepgram';
  const usesScribe = draft.pipeline === 'cascade' && draft.stt === 'elevenlabs-scribe';
  const usesAura = draft.pipeline === 'cascade' && draft.tts === 'deepgram-aura';
  const typeRow = agentTypes.find((t) => t.id === draft.type);
  const sttOptions = typeRow?.stt_options || [];
  const ttsOptions = typeRow?.tts_options || [];
  const providerName = (id: string) => providers.find((row) => row.id === id)?.display_name || id;
  const realtimeRows = providers.filter((row) => row.role === 'realtime');

  /* The picker always contains the Agent's current profile, even when the listing did
     not (no profiles directory mounted, or a profile deleted out of band). Dropping it
     would make the select silently show a different profile than the Agent has. */
  const profileOptions = [...selectable];
  for (const name of new Set(['default', draft.profile])) {
    if (!profileOptions.some((r) => r.name === name)) {
      profileOptions.push({
        name,
        complete: true,
        gateway_status: null,
        gateway_url: null,
        routable: name === agent.hermes_profile && Boolean(agent.hermes_routable),
      });
    }
  }
  const chosenProfile = profileOptions.find((r) => r.name === draft.profile);

  function firstProviderId(role: string): string {
    return providers.find((row) => row.role === role)?.id || '';
  }

  /* Picking a type sets the SAME pipeline and provider drafts the Advanced controls
     edit, so the two views of one Agent cannot disagree about what Save will send. */
  function pickType(id: string) {
    const preset = agentTypes.find((t) => t.id === id);
    setDraft((prev) => {
      if (!preset) return { ...prev, type: id };
      return {
        ...prev,
        type: id,
        pipeline: preset.pipeline,
        realtimeProvider: preset.providers.realtime || prev.realtimeProvider,
        stt: preset.providers.stt || '',
        llm: preset.providers.llm || '',
        tts: preset.providers.tts || '',
        /* A voice id belongs to ONE provider. Carrying an OpenAI voice name into an
           ElevenLabs Agent (or the reverse) would save a voice that cannot be spoken. */
        voice: preset.id !== (agent.agent_type || CUSTOM_TYPE) ? '' : prev.voice,
      };
    });
  }

  /* A realtime Agent has no cascade trio, so the three selects would open on ''
     with no matching option. Seed empty drafts from the first registry row of
     each role so tick-and-Save sends real ids and the screen matches the payload. */
  function pickPipeline(id: string) {
    setDraft((prev) =>
      id !== 'cascade'
        ? { ...prev, pipeline: id }
        : {
            ...prev,
            pipeline: id,
            stt: prev.stt || firstProviderId('stt'),
            llm: prev.llm || firstProviderId('llm'),
            tts: prev.tts || firstProviderId('tts'),
          }
    );
  }

  async function save(e: FormEvent) {
    e.preventDefault();
    setSaving(true);
    setMessage(null);
    setError(null);
    try {
      const sent: Record<string, string | null> =
        draft.pipeline === 'cascade'
          ? { stt: draft.stt, llm: draft.llm, tts: draft.tts, realtime: null }
          : { realtime: draft.realtimeProvider, stt: null, llm: null, tts: null };
      const voice = draft.voice.trim();
      /* A null DELETES the knob. The voice is cleared only when the type or the voice
         provider changed (its old id belongs to another provider); otherwise blank still
         means "leave it". */
      const typeChanged = draft.type !== (agent.agent_type || CUSTOM_TYPE);
      const knobs: Record<string, string | boolean | string[] | null> = {};
      const ttsChanged = draft.tts !== (agent.providers?.tts || '');
      if (voice) knobs.voice = voice;
      else if (typeChanged || ttsChanged) knobs.voice = null;
      const terms = draft.keyterms.split(',').map((t) => t.trim()).filter(Boolean);
      if (usesDeepgram) {
        knobs.transcription_model = draft.sttModel.trim() || null;
        knobs.language = draft.language.trim() || null;
        knobs.keyterms = terms.length ? terms : null;
        knobs.smart_format = triStateKnob(draft.smartFormat);
        knobs.numerals = triStateKnob(draft.numerals);
      } else if (usesScribe) {
        /* Scribe has one realtime model and no formatting switches: a Deepgram model
           left behind would be sent to ElevenLabs as its model, so it is deleted. */
        knobs.transcription_model = null;
        knobs.smart_format = null;
        knobs.numerals = null;
        knobs.language = draft.language.trim() || null;
        knobs.keyterms = terms.length ? terms : null;
      }
      /* The profile goes first, through its own narrow endpoint. If it is refused (a
         profile nobody is running, on an Agent that holds a slot) nothing else is
         written, so a half-applied change cannot exist. */
      if (isHermes && draft.profile !== (agent.hermes_profile || 'default')) {
        await updateAgentHermesProfile(agent.id, draft.profile);
      }
      await updateAgentVoice(agent.id, {
        pipeline: draft.pipeline,
        providers: sent,
        ...(Object.keys(knobs).length ? { knobs } : {}),
        ...(isHermes ? { guardrails: { on_call_tools: draft.tools } } : {}),
      });
      setMessage(
        voice
          ? `Saved - ${agent.id} will use '${voice}' on its next call.`
          : `Saved - ${agent.id} will inherit the stack default voice on its next call.`
      );
      const { agents } = await fetchAgentsScreen();
      onSaved(agents);
    } catch (err) {
      setError(String(err instanceof Error ? err.message : err));
    } finally {
      setSaving(false);
    }
  }

  const voiceFields = (
    <>
      <div className="form-row" role="group" aria-label="What you are talking to" data-testid="settings-agent-type">
        <span className="form-row-label">
          What you are talking to
          <span className="form-row-help">
            The change applies to <strong>this Agent&apos;s next call only</strong>. No other Agent
            is touched.
          </span>
        </span>
        <div className="form-row-control choice-list">
          {agentTypes.map((t) => (
            <label className="wizard-radio choice" key={t.id}>
              <input
                type="radio"
                name="settings-agent-type"
                data-testid={`settings-type-${t.id}`}
                checked={draft.type === t.id}
                onChange={() => pickType(t.id)}
              />
              <span>
                <span className="choice-title">
                  <strong>{t.name}</strong> <ProvenTag proven={t.proven} />
                </span>
                <span className="settings-evidence">{t.summary}</span>
                <span className="settings-evidence">{t.evidence}</span>
              </span>
            </label>
          ))}
          <label className="wizard-radio choice">
            <input
              type="radio"
              name="settings-agent-type"
              data-testid={`settings-type-${CUSTOM_TYPE}`}
              checked={draft.type === CUSTOM_TYPE}
              onChange={() => pickType(CUSTOM_TYPE)}
            />
            <span>
              <span className="choice-title">
                <strong>Advanced</strong>
              </span>
              <span className="settings-evidence">
                Any other pipeline and provider combination, including an outside LLM that calls
                Hermes as a tool. Nothing has been removed; it lives here.
              </span>
            </span>
          </label>
        </div>
      </div>

      {isHermes && (
        <label className="form-row">
          <span className="form-row-label">
            Hermes profile
            <span className="form-row-help">
              The Hermes profile that holds the conversation, the tools and the memory on this
              Agent&apos;s calls.
            </span>
          </span>
          <span className="form-row-control">
            <select
              className="field-input"
              data-testid="settings-hermes-profile"
              value={draft.profile}
              onChange={(e) => set('profile', e.target.value)}
            >
              {profileOptions.map((row) => (
                <option key={row.name} value={row.name} disabled={!row.complete}>
                  {row.name}
                  {!row.complete
                    ? ' (still being created)'
                    : row.routable
                      ? ''
                      : ` (not running${row.gateway_status ? `: ${row.gateway_status}` : ''})`}
                </option>
              ))}
            </select>
            {chosenProfile && !chosenProfile.routable && (
              <span className="settings-error" data-testid="settings-profile-unroutable">
                No gateway is running for &apos;{chosenProfile.name}&apos; right now. An inbound call
                on it would be answered by OpenAI Realtime instead, and the dashboard refuses the
                change while this Agent holds an Outlet.
              </span>
            )}
          </span>
        </label>
      )}

      {isHermes && sttOptions.length > 1 && (
        <label className="form-row">
          <span className="form-row-label">
            Hearing
            <span className="form-row-help">
              Who turns the caller&apos;s speech into text. ElevenLabs also says when the caller
              trailed off mid-thought, and the Agent waits for the rest.
            </span>
          </span>
          <span className="form-row-control">
            <select
              className="field-input"
              data-testid="settings-hermes-stt"
              value={draft.stt}
              onChange={(e) => {
                const stt = e.target.value;
                /* A language written for one provider can be one the other refuses
                   (Deepgram's `multi` ends a Scribe session), so it starts blank. */
                setDraft((prev) => ({ ...prev, stt, language: '' }));
              }}
            >
              {sttOptions.map((id) => (
                <option key={id} value={id}>
                  {providerName(id)}
                </option>
              ))}
            </select>
          </span>
        </label>
      )}

      {isHermes && ttsOptions.length > 1 && (
        <label className="form-row">
          <span className="form-row-label">
            Voice provider
            <span className="form-row-help">Who speaks the Agent&apos;s replies.</span>
          </span>
          <span className="form-row-control">
            <select
              className="field-input"
              data-testid="settings-hermes-tts"
              value={draft.tts}
              onChange={(e) => {
                const tts = e.target.value;
                /* A voice id belongs to ONE provider; carried across it cannot be spoken. */
                setDraft((prev) => ({ ...prev, tts, voice: '' }));
              }}
            >
              {ttsOptions.map((id) => (
                <option key={id} value={id}>
                  {providerName(id)}
                </option>
              ))}
            </select>
          </span>
        </label>
      )}

      {draft.type === CUSTOM_TYPE && (
        <>
          <div className="form-row" role="group" aria-label="Pipeline">
            <span className="form-row-label">Pipeline</span>
            <div className="form-row-control choice-list">
              {pipelines.map((p) => (
                <label className="wizard-radio choice" key={p.id}>
                  <input
                    type="radio"
                    name="settings-pipeline"
                    data-testid={`settings-pipeline-${p.id}`}
                    checked={draft.pipeline === p.id}
                    onChange={() => pickPipeline(p.id)}
                  />
                  <span>
                    <span className="choice-title">
                      <strong>{p.id}</strong> <ProvenTag proven={p.proven} />
                    </span>
                    <span className="settings-evidence">{p.evidence}</span>
                  </span>
                </label>
              ))}
            </div>
          </div>

          {draft.pipeline === 'realtime' ? (
            <label className="form-row">
              <span className="form-row-label">Realtime provider</span>
              <span className="form-row-control">
                <select
                  className="field-input"
                  data-testid="settings-realtime-provider"
                  value={draft.realtimeProvider}
                  onChange={(e) => set('realtimeProvider', e.target.value)}
                >
                  {(realtimeRows.length
                    ? realtimeRows
                    : [
                        {
                          id: proven.realtime_provider,
                          display_name: proven.realtime_provider,
                          role: 'realtime',
                          status: '',
                          proven: true,
                          wired: true,
                          evidence: '',
                          default_knobs: {},
                        } as ProviderRow,
                      ]
                  ).map((row) => (
                    <option key={row.id} value={row.id}>
                      {row.display_name} — {row.proven ? 'proven' : 'not wired on this build'}
                    </option>
                  ))}
                </select>
              </span>
            </label>
          ) : (
            <div className="form-row">
              <span className="form-row-label">
                Providers
                <span className="form-row-help">Hearing, thinking and speaking, one provider each.</span>
              </span>
              <div className="form-row-control wizard-grid">
                {(
                  [
                    ['stt', 'Hearing (STT)', 'settings-stt-provider'],
                    ['llm', 'Thinking (LLM)', 'settings-llm-provider'],
                    ['tts', 'Speaking (TTS)', 'settings-tts-provider'],
                  ] as const
                ).map(([role, label, testId]) => (
                  <label className="field" key={role}>
                    <span className="field-label">{label}</span>
                    <select
                      className="field-input"
                      data-testid={testId}
                      value={draft[role]}
                      onChange={(e) => set(role, e.target.value)}
                    >
                      <option value="">Choose…</option>
                      {providers
                        .filter((r) => r.role === role)
                        .map((row) => (
                          <option key={row.id} value={row.id}>
                            {row.display_name}
                          </option>
                        ))}
                    </select>
                  </label>
                ))}
              </div>
            </div>
          )}
        </>
      )}

      {usesElevenLabs ? (
        <label className="form-row">
          <span className="form-row-label">
            ElevenLabs voice
            <span className="form-row-help" data-testid="settings-eleven-voice-source">
              {elevenVoices === null
                ? 'Loading this account’s voices…'
                : elevenVoices.voices?.length
                  ? 'The voices on this ElevenLabs account. Blank inherits ELEVENLABS_VOICE_ID, then the registry default.'
                  : `No voice list: ${elevenVoices.detail || 'the ElevenLabs catalog is unavailable'}. Enter a voice id by hand.`}
            </span>
          </span>
          <span className="form-row-control">
            {elevenVoices?.voices?.length ? (
              <select
                className="field-input"
                data-testid="settings-eleven-voice"
                value={draft.voice}
                onChange={(e) => set('voice', e.target.value)}
              >
                <option value="">Inherit the stack default</option>
                {/* The Agent's current voice stays selectable even when the account
                    no longer lists it, so opening the page never changes it. */}
                {draft.voice && !elevenVoices.voices.some((v) => v.id === draft.voice) && (
                  <option value={draft.voice}>{draft.voice} (not in this account&apos;s list)</option>
                )}
                {elevenVoices.voices.map((v) => (
                  <option key={v.id} value={v.id}>
                    {v.name}
                  </option>
                ))}
              </select>
            ) : (
              <input
                className="field-input mono"
                data-testid="settings-eleven-voice"
                value={draft.voice}
                placeholder="an ElevenLabs voice id, or blank to inherit"
                onChange={(e) => set('voice', e.target.value)}
              />
            )}
          </span>
        </label>
      ) : usesAura ? (
        <label className="form-row">
          <span className="form-row-label">
            Deepgram Aura voice
            <span className="form-row-help">Blank inherits the registry default voice.</span>
          </span>
          <span className="form-row-control">
            <select
              className="field-input"
              data-testid="settings-aura-voice"
              value={draft.voice}
              onChange={(e) => set('voice', e.target.value)}
            >
              <option value="">Inherit the registry default</option>
              {draft.voice && !(auraVoices?.voices || []).some((v) => v.id === draft.voice) && (
                <option value={draft.voice}>{draft.voice}</option>
              )}
              {(auraVoices?.voices || []).map((v) => (
                <option key={v.id} value={v.id}>
                  {v.name} ({v.id})
                </option>
              ))}
            </select>
          </span>
        </label>
      ) : (
        <label className="form-row">
          <span className="form-row-label">
            Voice
            <span className="form-row-help">Leave blank to inherit the stack default ({proven.voice}).</span>
          </span>
          <span className="form-row-control">
            <input
              className="field-input mono"
              data-testid="settings-voice"
              list="settings-voice-options"
              value={draft.voice}
              placeholder={`inherit (${proven.voice})`}
              onChange={(e) => set('voice', e.target.value)}
            />
            <datalist id="settings-voice-options">
              {realtime_voices.map((v) => (
                <option key={v.id} value={v.id} />
              ))}
            </datalist>
            <span className="voice-options">
              {realtime_voices.map((v) => (
                <button
                  type="button"
                  key={v.id}
                  className={'chip chip-choice settings-voice-option' + (draft.voice === v.id ? ' is-on' : '')}
                  onClick={() => set('voice', v.id)}
                >
                  <span className="mono">{v.id}</span> {v.proven && <ProvenTag proven />}
                </button>
              ))}
            </span>
          </span>
        </label>
      )}

      {usesDeepgram && (
        <div className="form-row" role="group" aria-label="Listening (Deepgram)" data-testid="settings-listening">
          <span className="form-row-label">
            Listening (Deepgram)
            <span className="form-row-help">
              Deepgram&apos;s own endpointing is not offered: VoiceMaster decides when a turn has
              ended, so it would be a setting with no effect.
            </span>
          </span>
          <div className="form-row-control listening-grid">
            <label className="field">
              <span className="field-label">Model</span>
              <input
                className="field-input mono"
                data-testid="settings-stt-model"
                list="settings-stt-models"
                value={draft.sttModel}
                placeholder="inherit (nova-3)"
                onChange={(e) => set('sttModel', e.target.value)}
              />
              <datalist id="settings-stt-models">
                {DEEPGRAM_MODELS.map((m) => (
                  <option key={m} value={m} />
                ))}
              </datalist>
            </label>
            <label className="field">
              <span className="field-label">Language</span>
              <input
                className="field-input mono"
                data-testid="settings-stt-language"
                value={draft.language}
                placeholder="inherit (en)"
                onChange={(e) => set('language', e.target.value)}
              />
            </label>
            <label className="field listening-wide">
              <span className="field-label">Names to listen for</span>
              <input
                className="field-input"
                data-testid="settings-stt-keyterms"
                value={draft.keyterms}
                placeholder="Hermes, Alex, Acme"
                onChange={(e) => set('keyterms', e.target.value)}
              />
              <span className="field-help">
                Comma separated. Deepgram biases its transcript toward these words, which is what
                stops a proper noun being heard as something else.
              </span>
            </label>
            <label className="field">
              <span className="field-label">Smart formatting</span>
              <select
                className="field-input"
                data-testid="settings-stt-smart-format"
                value={draft.smartFormat}
                onChange={(e) => set('smartFormat', e.target.value)}
              >
                <option value="inherit">Inherit (not sent)</option>
                <option value="on">On</option>
                <option value="off">Off</option>
              </select>
            </label>
            <label className="field">
              <span className="field-label">Numerals</span>
              <select
                className="field-input"
                data-testid="settings-stt-numerals"
                value={draft.numerals}
                onChange={(e) => set('numerals', e.target.value)}
              >
                <option value="inherit">Inherit (not sent)</option>
                <option value="on">On</option>
                <option value="off">Off</option>
              </select>
            </label>
          </div>
        </div>
      )}
      {usesScribe && (
        <div className="form-row" role="group" aria-label="Listening (ElevenLabs)" data-testid="settings-listening-scribe">
          <span className="form-row-label">
            Listening (ElevenLabs)
            <span className="form-row-help">
              VoiceMaster decides when a turn has ended, from its own silence detector and from
              ElevenLabs marking speech that trailed off.
            </span>
          </span>
          <div className="form-row-control listening-grid">
            <label className="field">
              <span className="field-label">Language</span>
              <input
                className="field-input mono"
                data-testid="settings-scribe-language"
                value={draft.language}
                placeholder="inherit (en)"
                onChange={(e) => set('language', e.target.value)}
              />
            </label>
            <label className="field listening-wide">
              <span className="field-label">Names to listen for</span>
              <input
                className="field-input"
                data-testid="settings-scribe-keyterms"
                value={draft.keyterms}
                placeholder="Hermes, Alex, Acme"
                onChange={(e) => set('keyterms', e.target.value)}
              />
              <span className="field-help">
                Comma separated. ElevenLabs biases its transcript toward these words.
              </span>
            </label>
          </div>
        </div>
      )}
    </>
  );

  const toolsFields = isHermes ? (
    <label className="form-row toggle-row">
      <span className="form-row-label">
        Allow Hermes to use tools on this Agent&apos;s calls
        <span className="form-row-help">
          {draft.tools
            ? 'Hermes may use the tools available to its profile.'
            : 'Hermes cannot use tools on calls with this Agent.'}
        </span>
      </span>
      <span className="form-row-control">
        <span className="switch">
          <input
            type="checkbox"
            role="switch"
            data-testid="settings-hermes-tools"
            checked={draft.tools}
            onChange={(e) => set('tools', e.target.checked)}
          />
          <span className="switch-track" aria-hidden="true" />
        </span>
      </span>
    </label>
  ) : (
    <div className="form-row">
      <span className="form-row-label">
        On-call tools
        <span className="form-row-help">
          This setting applies when an Agent talks to Hermes directly. {agent.id} is not set up
          that way{draft.type !== (agent.agent_type || CUSTOM_TYPE) ? ' in the saved document' : ''},
          so there is nothing to set here. Choose Hermes directly on the Voice tab to use it.
        </span>
      </span>
    </div>
  );

  return (
    <form onSubmit={save} className="agent-editor" data-testid="settings-agent-voice">
      <div className="form-rows">{tab === 'voice' ? voiceFields : toolsFields}</div>

      <div className={'save-bar' + (dirty ? ' is-dirty' : '')} data-testid="save-bar">
        <span className="save-bar-state">
          {message ? (
            <span className="settings-success" data-testid="settings-voice-saved">
              <Check size={14} /> {message}
            </span>
          ) : error ? (
            <span className="settings-error" data-testid="settings-voice-error">
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
            onClick={() => {
              setDraft(initial);
              setError(null);
            }}
          >
            <RotateCcw size={14} /> Discard
          </button>
          <button type="submit" className="btn btn-primary" data-testid="settings-save-voice" disabled={saving}>
            {saving ? <Loader2 className="spin" size={16} /> : <Save size={16} />}
            Save voice settings
          </button>
        </span>
      </div>
    </form>
  );
}
