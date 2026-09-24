/* One Call, at its own URL (/calls/<call_id>), as a drawer over the list on a
 * desktop and a full page on a phone. It opens on what the owner came for:
 * who, which Agent, how it ended, then the recording, then the summary. The
 * transcript is one tab away. Prev/next walk the list behind it. */
import { useEffect, useRef, useState } from 'react';
import {
  AlertTriangle,
  AudioLines,
  Bot,
  Check,
  ChevronDown,
  ChevronUp,
  Clock,
  Copy,
  FileText,
  X,
} from 'lucide-react';
import { fetchCallDetail } from './api';
import { formatDate, formatDuration, NOT_RETAINED, outletCopy, summaryAbsence } from './format';
import { DirectionIcon, OutletIcon, OutcomePill, Pill } from './ui';
import { Link } from './Link';
import { navigate } from './router';
import { Transcript } from './Transcript';
import type { Call, CallRecording } from './types';
import { Waveform } from './Waveform';

/* Ticket 07. Three states, and only one of them is a player:
 *
 *  - a recording exists  -> the stereo waveform player (the <audio> element
 *                           underneath scrubs because the API answers range
 *                           requests);
 *  - capture FAILED      -> a sentence saying so, because a call whose audio was lost
 *                           must not look like a call that was never recorded;
 *  - anything else       -> nothing at all. An older call, or one never captured, gets
 *                           no player rather than a broken one.
 */
export function RecordingBlock({ recording }: { recording: CallRecording | null }) {
  if (!recording) return null;

  if (!recording.available) {
    if (recording.status !== 'failed') return null;
    return (
      <section className="detail-section recording-card" aria-label="Recording">
        <h3 className="detail-section-title">
          <AudioLines size={16} /> Recording
        </h3>
        <div className="recording-absent">
          Capturing this call&apos;s audio failed, so there is no recording to play.
          {recording.error ? ` (${recording.error})` : ''}
        </div>
      </section>
    );
  }

  return (
    <section className="detail-section recording-card" aria-label="Recording">
      <h3 className="detail-section-title">
        <AudioLines size={16} /> Recording
      </h3>
      <Waveform url={recording.url ?? ''} durationHint={recording.duration_s ?? null} />
      {recording.dropped_frames ? (
        <p className="recording-warning">
          <AlertTriangle size={14} />
          {recording.dropped_frames} audio frame
          {recording.dropped_frames === 1 ? '' : 's'} could not be written, so this recording has gaps
          the call did not.
        </p>
      ) : null}
    </section>
  );
}

function MetaRow({ label, children, absent }: { label: string; children: React.ReactNode; absent?: boolean }) {
  return (
    <div className="meta-row">
      <dt className="meta-item-label">{label}</dt>
      <dd className={'meta-item-value' + (absent ? ' meta-item-absent' : '')}>{children}</dd>
    </div>
  );
}

function CopyId({ id }: { id: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="copy-id mono"
      title="Copy the call ID"
      data-testid="call-id"
      onClick={() => {
        navigator.clipboard?.writeText(id).then(
          () => {
            setCopied(true);
            window.setTimeout(() => setCopied(false), 1500);
          },
          () => undefined
        );
      }}
    >
      <span className="copy-id-text">{id}</span>
      {copied ? <Check size={13} /> : <Copy size={13} />}
    </button>
  );
}

interface Props {
  callId: string;
  position: { index: number; total: number } | null;
  prevHref: string | null;
  nextHref: string | null;
  closeHref: string;
}

export function CallDetail({ callId, position, prevHref, nextHref, closeHref }: Props) {
  const [call, setCall] = useState<Call | null>(null);
  const [recording, setRecording] = useState<CallRecording | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<'overview' | 'transcript'>('overview');
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let isMounted = true;
    setLoading(true);
    setError(null);
    setCall(null);
    setRecording(null);
    fetchCallDetail(callId)
      .then((res) => {
        if (!isMounted) return;
        if (res.unreachable) {
          setError('Call archive unreachable');
        } else if (res.error) {
          setError(res.error);
        } else {
          setCall(res.call);
        }
        setRecording(res.recording ?? null);
      })
      .catch(() => {
        if (isMounted) setError('Failed to fetch call transcript');
      })
      .finally(() => {
        if (isMounted) setLoading(false);
      });
    return () => {
      isMounted = false;
    };
  }, [callId]);

  useEffect(() => {
    panelRef.current?.focus();
  }, [callId]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target && /^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName)) return;
      if (event.key === 'Escape') navigate(closeHref, { keepScroll: true });
      else if ((event.key === 'ArrowUp' || event.key === 'k') && prevHref) {
        event.preventDefault();
        navigate(prevHref, { keepScroll: true });
      } else if ((event.key === 'ArrowDown' || event.key === 'j') && nextHref) {
        event.preventDefault();
        navigate(nextHref, { keepScroll: true });
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [closeHref, prevHref, nextHref]);

  const duration = call ? formatDuration(call.duration_s) : null;
  const absence = summaryAbsence(call?.summary_state);

  return (
    <div className="drawer-layer">
      <Link className="drawer-scrim" href={closeHref} aria-label="Close the call" tabIndex={-1} />
      <div
        className="drawer"
        role="dialog"
        aria-modal="false"
        aria-label="Call detail"
        tabIndex={-1}
        ref={panelRef}
        data-testid="call-detail"
      >
        <div className="drawer-top">
          <div className="drawer-stepper">
            <Link
              className={'icon-btn' + (prevHref ? '' : ' is-disabled')}
              href={prevHref ?? '#'}
              aria-disabled={!prevHref}
              aria-label="Previous call"
              data-testid="call-prev"
              onClick={(e) => {
                if (!prevHref) e.preventDefault();
              }}
            >
              <ChevronUp size={17} />
            </Link>
            <Link
              className={'icon-btn' + (nextHref ? '' : ' is-disabled')}
              href={nextHref ?? '#'}
              aria-disabled={!nextHref}
              aria-label="Next call"
              data-testid="call-next"
              onClick={(e) => {
                if (!nextHref) e.preventDefault();
              }}
            >
              <ChevronDown size={17} />
            </Link>
            {position && (
              <span className="drawer-position mono-num" data-testid="call-position">
                {position.index + 1} / {position.total}
              </span>
            )}
          </div>
          <Link className="icon-btn detail-back-btn" href={closeHref} aria-label="Back to Calls list">
            <X size={18} />
          </Link>
        </div>

        {loading && <div className="spinner" />}

        {!loading && error && (
          <div className="alert-banner alert-unreachable">
            <AlertTriangle className="alert-icon" />
            <div>
              <strong>Error loading transcript</strong>
              <div>{error}</div>
            </div>
          </div>
        )}

        {!loading && call && (
          <div className="drawer-body">
            <header className="detail-header">
              <div className="detail-title-row">
                <h2 className="detail-title">{call.who}</h2>
                <Pill
                  tone={call.direction === 'inbound' ? 'accent' : 'info'}
                  icon={<DirectionIcon direction={call.direction} size={12} />}
                >
                  {call.direction}
                </Pill>
              </div>
              <div className="detail-subline">
                <span className="detail-sub">
                  <Bot size={14} />
                  {call.agent || <span className="meta-item-absent">{NOT_RETAINED}</span>}
                </span>
                <span className="detail-sub">
                  {call.outlet ? (
                    <>
                      <OutletIcon outlet={call.outlet} size={14} />
                      {outletCopy(call.outlet).label}
                    </>
                  ) : (
                    <span className="meta-item-absent">{NOT_RETAINED}</span>
                  )}
                </span>
                <span className="detail-sub">
                  <Clock size={14} />
                  {formatDate(call.when, call.when_precision)}
                </span>
              </div>
              <CopyId id={call.call_id} />
            </header>

            <RecordingBlock recording={recording} />

            <div className="tabs" role="tablist" aria-label="Call">
              <button
                type="button"
                role="tab"
                aria-selected={tab === 'overview'}
                className={'tab' + (tab === 'overview' ? ' active' : '')}
                data-testid="call-tab-overview"
                onClick={() => setTab('overview')}
              >
                Overview
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={tab === 'transcript'}
                className={'tab' + (tab === 'transcript' ? ' active' : '')}
                data-testid="call-tab-transcript"
                onClick={() => setTab('transcript')}
              >
                <FileText size={14} /> Transcript
              </button>
            </div>

            {tab === 'overview' && (
              <div className="detail-overview" role="tabpanel">
                <section className="detail-section">
                  <h3 className="detail-section-title">Summary</h3>
                  {call.summary ? (
                    <p className="detail-prose" data-testid="summary-detail">
                      {call.summary}
                    </p>
                  ) : (
                    <p
                      className="detail-prose meta-item-absent"
                      data-testid="summary-detail-absent"
                      title={absence.title}
                    >
                      {absence.text}
                    </p>
                  )}
                </section>

                <section className="detail-section">
                  <h3 className="detail-section-title">Mission</h3>
                  {call.mission ? (
                    <p className="detail-prose" data-testid="mission-detail">
                      {call.mission}
                    </p>
                  ) : call.direction === 'inbound' ? (
                    /* An inbound call has no Mission by construction -- that is a fact
                       about the call, not a gap in the record, and saying "not retained"
                       would suggest something went missing. */
                    <p className="detail-prose meta-item-absent">Not applicable (inbound call)</p>
                  ) : (
                    <p className="detail-prose meta-item-absent">{NOT_RETAINED}</p>
                  )}
                </section>

                {/* Agent, Outlet, Outcome and Duration are ALWAYS rendered, even when
                    the store holds none of them. Hiding them reads as "this call had no
                    Agent" rather than "nobody wrote one down" -- and hides from the owner
                    that anything is missing. Calls retained before ticket 05 have all four
                    absent. */}
                <dl className="meta-rows" data-testid="call-meta">
                  <MetaRow label="How the call ended" absent={!call.outcome}>
                    {call.outcome ? <OutcomePill outcome={call.outcome} /> : NOT_RETAINED}
                  </MetaRow>
                  <MetaRow label="Duration" absent={!duration}>
                    <span className="mono-num">{duration || NOT_RETAINED}</span>
                  </MetaRow>
                  <MetaRow label="Agent" absent={!call.agent}>
                    {call.agent ? (
                      <Link href={`/agents/${encodeURIComponent(call.agent)}`}>{call.agent}</Link>
                    ) : (
                      NOT_RETAINED
                    )}
                  </MetaRow>
                  <MetaRow label="Outlet" absent={!call.outlet}>
                    {call.outlet ? outletCopy(call.outlet).label : NOT_RETAINED}
                  </MetaRow>
                  <MetaRow label="Other Party">{call.who}</MetaRow>
                  <MetaRow label="When">{formatDate(call.when, call.when_precision)}</MetaRow>
                </dl>
              </div>
            )}

            {tab === 'transcript' && (
              <div role="tabpanel">
                <Transcript text={call.transcript ?? ''} />
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
