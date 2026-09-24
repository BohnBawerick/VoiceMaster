/* New call (tickets 09, 10 and 11, as one form: N3).
 *
 * Pick an Agent, pick a number from speed dial or type one, write the Mission,
 * choose whether the Agent discloses that it is an AI, then say WHEN: now, and
 * the phone rings; later, and a Schedule is written that this app places at
 * that time through the same placement (`place_call.place_from_request`). The
 * form shows the rule the backend already keeps: one placement, used now or
 * later.
 *
 * Firing is one shot either way: this screen never writes an Outlet
 * assignment. Two calls with different Agents cannot interfere because nothing
 * global is mutated.
 *
 * /place opens it with Now selected, /schedule/new with Later. */
import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import {
  createSchedule,
  dictateMission,
  expandMission,
  fetchAgentsScreen,
  fetchSpeedDial,
  placeCall,
  saveSpeedDial,
} from './api';
import { Link } from './Link';
import { navigate, useLocation } from './router';
import { browserTimeZone, defaultWhen, formatLocalTime } from './scheduleTime';
import { PageHeader } from './Shell';
import type { Agent, PlaceCallResponse, Schedule, SpeedDialEntry } from './types';
import {
  AlertTriangle,
  CalendarClock,
  CheckCircle2,
  Loader2,
  Mic,
  PhoneOutgoing,
  Plus,
  Square,
  WandSparkles,
} from 'lucide-react';

type When = 'now' | 'later';

function agentCanRun(agent: Agent): boolean {
  return agent.valid && agent.enabled;
}

export function NewCallView({ initialWhen }: { initialWhen: When }) {
  const { search } = useLocation();
  const wantedAgent = search.get('agent') || '';

  const [agents, setAgents] = useState<Agent[]>([]);
  const [speedDial, setSpeedDial] = useState<SpeedDialEntry[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const [when, setWhen] = useState<When>(initialWhen);
  const [at, setAt] = useState(defaultWhen);
  const [agentId, setAgentId] = useState('');
  const [to, setTo] = useState('');
  const [mission, setMission] = useState('');
  const [disclose, setDisclose] = useState(false);
  const [saveLabel, setSaveLabel] = useState('');

  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [placed, setPlaced] = useState<PlaceCallResponse | null>(null);
  const [created, setCreated] = useState<Schedule | null>(null);

  const [assisting, setAssisting] = useState(false);
  const [recording, setRecording] = useState(false);
  const [assistError, setAssistError] = useState<string | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<BlobPart[]>([]);
  const tracksRef = useRef<MediaStreamTrack[]>([]);
  // State `recording` is only true AFTER getUserMedia resolves. A second
  // click in that window used to open another stream and orphan the first
  // (hot mic until the page closed). This ref is sync.
  const openingRef = useRef(false);

  useEffect(() => setWhen(initialWhen), [initialWhen]);

  useEffect(() => {
    let alive = true;
    Promise.all([fetchAgentsScreen(), fetchSpeedDial()])
      .then(([roster, dial]) => {
        if (!alive) return;
        setAgents(roster.agents);
        setSpeedDial(dial.numbers);
        const runnable = roster.agents.filter(agentCanRun);
        if (wantedAgent && runnable.some((a) => a.id === wantedAgent)) setAgentId(wantedAgent);
        else if (runnable.length === 1) setAgentId(runnable[0].id);
        if (dial.numbers.length > 0) setTo((current) => current || dial.numbers[0].number);
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
    // Seeds the first load only; the wanted Agent is read once from the URL.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    return () => {
      const rec = recorderRef.current;
      if (rec && rec.state !== 'inactive') {
        try {
          rec.stop();
        } catch {
          /* unmounting */
        }
      }
      for (const track of tracksRef.current) {
        try {
          track.stop();
        } catch {
          /* already stopped */
        }
      }
    };
  }, []);

  const runnable = agents.filter(agentCanRun);

  const clearOutcome = () => {
    setPlaced(null);
    setCreated(null);
    setError(null);
  };

  const pickWhen = (next: When) => {
    setWhen(next);
    clearOutcome();
    navigate(next === 'now' ? '/place' + (search.toString() ? `?${search}` : '') : '/schedule/new' + (search.toString() ? `?${search}` : ''), {
      replace: true,
      keepScroll: true,
    });
  };

  const pickNumber = (number: string) => {
    setTo(number);
    clearOutcome();
  };

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    clearOutcome();
    if (!agentId) {
      setError('Pick an Agent.');
      return;
    }
    if (!to.trim()) {
      setError('Pick a number from speed dial or type one.');
      return;
    }
    if (!mission.trim()) {
      setError('Write what this Call is for.');
      return;
    }
    if (when === 'later' && !at) {
      setError('Pick when the phone should ring.');
      return;
    }
    setSubmitting(true);
    try {
      if (when === 'now') {
        const result = await placeCall({ agent: agentId, to: to.trim(), mission: mission.trim(), disclose });
        setPlaced(result);
      } else {
        const record = await createSchedule({
          agent: agentId,
          to: to.trim(),
          mission: mission.trim(),
          disclose,
          at,
          tz: browserTimeZone(),
        });
        setCreated(record);
        setMission('');
      }
    } catch (err) {
      const fallback = when === 'now' ? 'Could not place the call.' : 'Could not write the Schedule.';
      setError(err instanceof Error ? err.message : fallback);
    } finally {
      setSubmitting(false);
    }
  };

  const onSaveNumber = async () => {
    const number = to.trim();
    if (!number) return;
    const label = saveLabel.trim() || number;
    const next = speedDial.filter((entry) => !entry.owner).concat([{ label, number, owner: false }]);
    try {
      const saved = await saveSpeedDial(next.map((entry) => ({ label: entry.label, number: entry.number })));
      setSpeedDial(saved.numbers);
      setSaveLabel('');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not save the number.');
    }
  };

  const stopTracks = () => {
    for (const track of tracksRef.current) {
      try {
        track.stop();
      } catch {
        /* already stopped */
      }
    }
    tracksRef.current = [];
  };

  const applyAuthored = (text: string) => {
    // The generated Mission lands ONLY in the editable field. Submit reads
    // that field. There is no side channel from assist to the dial.
    setMission(text);
    setAssistError(null);
  };

  const failAssist = (message: string) => {
    // THE load-bearing failure rule: do not touch `mission`.
    setAssistError(message);
  };

  const onExpand = async () => {
    if (assisting || recording || submitting) return;
    setAssistError(null);
    clearOutcome();
    if (!agentId) {
      failAssist('Pick an Agent before asking it to write the Mission.');
      return;
    }
    if (!mission.trim()) {
      failAssist('Type a short line for the Agent to turn into a Mission.');
      return;
    }
    setAssisting(true);
    try {
      const authored = await expandMission(agentId, mission);
      applyAuthored(authored.mission);
    } catch (err) {
      failAssist(err instanceof Error ? err.message : 'The Agent could not write a Mission.');
    } finally {
      setAssisting(false);
    }
  };

  const onRecord = async () => {
    if (assisting || submitting || openingRef.current) return;
    if (recording || recorderRef.current) {
      const rec = recorderRef.current;
      if (rec && rec.state !== 'inactive') rec.stop();
      return;
    }
    setAssistError(null);
    clearOutcome();
    if (!agentId) {
      failAssist('Pick an Agent before dictating a Mission.');
      return;
    }
    const devices = navigator.mediaDevices;
    const Recorder = window.MediaRecorder;
    if (!devices || typeof devices.getUserMedia !== 'function' || typeof Recorder !== 'function') {
      failAssist(
        'This browser cannot record audio (needs getUserMedia and MediaRecorder, on a secure origin). The typed Mission is unchanged.'
      );
      return;
    }
    openingRef.current = true;
    let stream: MediaStream;
    try {
      stream = await devices.getUserMedia({ audio: true });
    } catch (err) {
      openingRef.current = false;
      const name = err instanceof Error ? err.name : '';
      if (name === 'NotAllowedError' || name === 'PermissionDeniedError') {
        failAssist('Microphone permission denied. The typed Mission is unchanged.');
      } else {
        failAssist('Could not open the microphone. The typed Mission is unchanged.');
      }
      return;
    }
    tracksRef.current = stream.getTracks();
    chunksRef.current = [];
    let rec: MediaRecorder;
    try {
      rec = new Recorder(stream);
    } catch {
      openingRef.current = false;
      stopTracks();
      failAssist('This browser cannot record audio. The typed Mission is unchanged.');
      return;
    }
    rec.ondataavailable = (ev) => {
      if (ev.data && ev.data.size > 0) chunksRef.current.push(ev.data);
    };
    rec.onstop = () => {
      stopTracks();
      recorderRef.current = null;
      openingRef.current = false;
      setRecording(false);
      const blob = new Blob(chunksRef.current, { type: rec.mimeType || 'audio/webm' });
      chunksRef.current = [];
      void sendDictation(blob);
    };
    recorderRef.current = rec;
    setRecording(true);
    rec.start();
    openingRef.current = false;
  };

  const sendDictation = async (blob: Blob) => {
    if (!blob.size) {
      failAssist('The recording was silent or empty. The typed Mission is unchanged.');
      return;
    }
    setAssisting(true);
    try {
      const authored = await dictateMission(agentId, blob);
      applyAuthored(authored.mission);
    } catch (err) {
      failAssist(err instanceof Error ? err.message : 'The Agent could not write a Mission from the recording.');
    } finally {
      setAssisting(false);
    }
  };

  if (loading) return <div className="spinner" />;

  if (loadError) {
    return (
      <div className="page page-narrow">
        <div className="alert-banner alert-unreachable">
          <AlertTriangle className="alert-icon" />
          <div>
            <strong>Could not load New call</strong>
            <div>{loadError}</div>
          </div>
        </div>
      </div>
    );
  }

  const later = when === 'later';

  return (
    <div className="page page-narrow place-call" data-testid="place-call">
      <PageHeader
        title="New call"
        icon={<PhoneOutgoing size={20} />}
        lede="Pick an Agent, a number, and what the Call is for, then call now or at a set time. Either way it is one call, one attempt: it does not change who is assigned to any Outlet."
      />

      {placed && (
        <div className="alert-banner alert-placed" data-testid="place-success">
          <CheckCircle2 className="alert-icon" />
          <div>
            <strong>Call placed</strong>
            <div>
              {placed.agent} is ringing {placed.to}.{placed.call_sid ? ` Twilio sid ${placed.call_sid}.` : ''}
            </div>
            <div className="place-call-mission-echo">{placed.mission}</div>
            <Link className="place-call-after" href="/">
              Open Calls
            </Link>
          </div>
        </div>
      )}

      {created && (
        <div className="alert-banner alert-placed" data-testid="schedule-success">
          <CheckCircle2 className="alert-icon" />
          <div>
            <strong>Scheduled</strong>
            <div>
              {created.agent} will ring {created.to} at {formatLocalTime(created)} ({created.timezone}).
            </div>
            {created.ambiguous_local_time && (
              <div className="field-help" data-testid="schedule-ambiguous">
                That clock time happens twice that day (daylight saving ends). The earlier one was
                taken.
              </div>
            )}
            <Link className="place-call-after" href="/schedule">
              Open the Schedule
            </Link>
          </div>
        </div>
      )}

      {error && (
        <div className="alert-banner alert-unreachable" data-testid={later ? 'schedule-error' : 'place-error'}>
          <AlertTriangle className="alert-icon" />
          <div>
            <strong>{later ? 'Not scheduled' : 'Call not placed'}</strong>
            <div>{error}</div>
          </div>
        </div>
      )}

      <form className="place-call-form panel" data-testid={later ? 'schedule-form' : 'place-form'} onSubmit={onSubmit}>
        <fieldset className="field">
          <legend className="field-label">When</legend>
          <div className="seg-tabs seg-tabs-block" role="radiogroup" aria-label="When">
            <button
              type="button"
              role="radio"
              aria-checked={!later}
              className={'seg-tab' + (!later ? ' active' : '')}
              data-testid="when-now"
              onClick={() => pickWhen('now')}
            >
              <PhoneOutgoing size={15} /> Call now
            </button>
            <button
              type="button"
              role="radio"
              aria-checked={later}
              className={'seg-tab' + (later ? ' active' : '')}
              data-testid="when-later"
              onClick={() => pickWhen('later')}
            >
              <CalendarClock size={15} /> Schedule for later
            </button>
          </div>
          {later && (
            <div className="when-later">
              <input
                className="field-input"
                data-testid="schedule-at"
                aria-label="When the phone rings"
                type="datetime-local"
                value={at}
                onChange={(e) => setAt(e.target.value)}
              />
              <span className="field-help" data-testid="schedule-zone">
                Your clock, {browserTimeZone()}. The phone rings at that local time, daylight saving
                included. One attempt, no retries.
              </span>
            </div>
          )}
        </fieldset>

        <label className="field">
          <span className="field-label">Agent</span>
          <select
            className="field-input"
            data-testid="place-agent"
            aria-label="Agent"
            value={agentId}
            onChange={(e) => setAgentId(e.target.value)}
          >
            <option value="">Select an Agent</option>
            {runnable.map((agent) => (
              <option key={agent.id} value={agent.id}>
                {agent.id}
                {agent.description ? ` — ${agent.description}` : ''}
              </option>
            ))}
          </select>
          {runnable.length === 0 && (
            <span className="field-help">No Agent can run an outbound Call. Create one on the Agents screen.</span>
          )}
        </label>

        <fieldset className="field">
          <legend className="field-label">Number</legend>
          <div className="speed-dial" data-testid="speed-dial">
            {speedDial.map((entry) => (
              <button
                key={entry.number}
                type="button"
                className={'speed-dial-chip' + (to === entry.number ? ' speed-dial-chip-on' : '')}
                aria-pressed={to === entry.number}
                data-testid={`speed-dial-${entry.owner ? 'owner' : entry.number}`}
                onClick={() => pickNumber(entry.number)}
              >
                <span className="speed-dial-label">{entry.label}</span>
                <span className="speed-dial-number mono-num">{entry.number}</span>
              </button>
            ))}
          </div>
          <input
            className="field-input"
            data-testid="place-to"
            aria-label="Number"
            type="tel"
            placeholder="+61…"
            value={to}
            onChange={(e) => setTo(e.target.value)}
            autoComplete="tel"
          />
          <span className="field-help">Pick from speed dial or type any number. Outbound is allow-any.</span>
          <div className="speed-dial-save">
            <input
              className="field-input"
              data-testid="speed-dial-label"
              aria-label="Speed dial label"
              type="text"
              placeholder="Label (optional)"
              value={saveLabel}
              onChange={(e) => setSaveLabel(e.target.value)}
            />
            <button type="button" className="btn btn-secondary" data-testid="speed-dial-save" onClick={onSaveNumber}>
              <Plus size={14} /> Save to speed dial
            </button>
          </div>
        </fieldset>

        <label className="field">
          <span className="field-label">Mission</span>
          <textarea
            className="field-input wizard-textarea"
            data-testid="place-mission"
            aria-label="Mission"
            rows={5}
            placeholder="What should the Agent accomplish on this Call?"
            value={mission}
            onChange={(e) => setMission(e.target.value)}
          />
          <span className="field-help">
            Belongs to this Call, not to the Agent. Type it in full, type a short line and press
            Elaborate, or press Record and speak. The Agent writes, and you review it before the call
            goes out.
          </span>
          <div className="mission-assist">
            <button
              type="button"
              className="btn btn-secondary"
              data-testid="place-expand"
              disabled={assisting || recording || submitting || runnable.length === 0}
              onClick={onExpand}
            >
              {assisting && !recording ? <Loader2 size={14} className="wizard-spin" /> : <WandSparkles size={14} />}
              Elaborate
            </button>
            <button
              type="button"
              className={'btn btn-secondary' + (recording ? ' mission-record-on' : '')}
              data-testid="place-record"
              disabled={assisting || submitting || runnable.length === 0}
              onClick={onRecord}
            >
              {recording ? <Square size={14} /> : <Mic size={14} />}
              {recording ? 'Stop' : 'Record'}
            </button>
          </div>
          {recording && (
            <span className="mission-assist-status" data-testid="place-assist-status">
              Listening. The Agent will write, not talk back.
            </span>
          )}
          {assisting && !recording && (
            <span className="mission-assist-status" data-testid="place-assist-status">
              The Agent is writing the Mission…
            </span>
          )}
          {assistError && (
            <div className="alert-banner alert-unreachable mission-assist-error" data-testid="place-assist-error">
              <AlertTriangle className="alert-icon" />
              <div>
                <strong>Mission not written</strong>
                <div>{assistError}</div>
              </div>
            </div>
          )}
        </label>

        <label className="wizard-check place-disclose">
          <input
            type="checkbox"
            data-testid="place-disclose"
            aria-label="Disclose that the Agent is an AI"
            checked={disclose}
            onChange={(e) => setDisclose(e.target.checked)}
          />
          <span>
            Disclose that the Agent is an AI at the start of the Call. Off by default: a choice for
            this Call, not a hardcoded behaviour.
          </span>
        </label>

        <div className="place-call-actions">
          {later ? (
            <button
              type="submit"
              className="btn btn-primary btn-lg"
              data-testid="schedule-submit"
              disabled={submitting || assisting || recording || runnable.length === 0}
            >
              {submitting ? <Loader2 size={16} className="wizard-spin" /> : <CalendarClock size={16} />}
              {submitting ? 'Scheduling…' : 'Schedule the call'}
            </button>
          ) : (
            <button
              type="submit"
              className="btn btn-primary btn-lg"
              data-testid="place-submit"
              disabled={submitting || assisting || recording || runnable.length === 0}
            >
              {submitting ? <Loader2 size={16} className="wizard-spin" /> : <PhoneOutgoing size={16} />}
              {submitting ? 'Placing…' : 'Place call'}
            </button>
          )}
        </div>
      </form>
    </div>
  );
}
