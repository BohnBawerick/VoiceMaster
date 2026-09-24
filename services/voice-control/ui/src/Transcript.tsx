/* The transcript as speaker turns (C5), with the verbatim text one click away.
 *
 * The bridges write one line per turn, prefixed "AI:" for the Agent and
 * "Them:" for the other party (voice/server.py, talk-voice-bridge,
 * voicecore/cascade_live.py). Older documents may carry "Agent:" / "Caller:".
 * A line with no known prefix continues the turn above it; one before any
 * turn is shown unattributed rather than guessed at.
 *
 * There are no per-turn timestamps in the archive, so there is no click to
 * seek: offering one would mean inventing where each turn starts. */
import { useMemo, useState } from 'react';
import { Check, Copy, FileText } from 'lucide-react';

type Speaker = 'agent' | 'caller' | 'unknown';

const PREFIXES: [RegExp, Speaker][] = [
  [/^(AI|Agent|Assistant)\s*:\s?/i, 'agent'],
  [/^(Them|Caller|User)\s*:\s?/i, 'caller'],
];

function parseTurns(text: string): { speaker: Speaker; text: string }[] {
  const turns: { speaker: Speaker; text: string }[] = [];
  for (const raw of text.split('\n')) {
    const line = raw.trimEnd();
    if (!line.trim()) continue;
    const match = PREFIXES.find(([pattern]) => pattern.test(line));
    if (match) {
      turns.push({ speaker: match[1], text: line.replace(match[0], '') });
    } else if (turns.length) {
      turns[turns.length - 1].text += '\n' + line;
    } else {
      turns.push({ speaker: 'unknown', text: line });
    }
  }
  return turns;
}

const LABEL: Record<Speaker, string> = { agent: 'Agent', caller: 'Caller', unknown: 'Unattributed' };

export function Transcript({ text }: { text: string }) {
  const [verbatim, setVerbatim] = useState(false);
  const [copied, setCopied] = useState(false);
  const turns = useMemo(() => parseTurns(text), [text]);

  if (!text.trim()) {
    return <p className="detail-prose meta-item-absent">No transcript text retained for this call.</p>;
  }

  return (
    <section className="transcript" aria-label="Transcript">
      <div className="transcript-toolbar">
        <div className="seg-tabs seg-tabs-sm" role="group" aria-label="Transcript view">
          <button
            type="button"
            className={'seg-tab' + (!verbatim ? ' active' : '')}
            aria-pressed={!verbatim}
            data-testid="transcript-turns"
            onClick={() => setVerbatim(false)}
          >
            Turns
          </button>
          <button
            type="button"
            className={'seg-tab' + (verbatim ? ' active' : '')}
            aria-pressed={verbatim}
            data-testid="transcript-verbatim"
            onClick={() => setVerbatim(true)}
          >
            <FileText size={13} /> Verbatim
          </button>
        </div>
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          onClick={() =>
            navigator.clipboard?.writeText(text).then(
              () => {
                setCopied(true);
                window.setTimeout(() => setCopied(false), 1500);
              },
              () => undefined
            )
          }
        >
          {copied ? <Check size={14} /> : <Copy size={14} />} Copy verbatim
        </button>
      </div>

      {verbatim ? (
        <pre className="transcript-body">{text}</pre>
      ) : (
        <ol className="turns" data-testid="transcript-turn-list">
          {turns.map((turn, i) => (
            <li key={i} className={`turn turn-${turn.speaker}`}>
              <span className="turn-speaker">{LABEL[turn.speaker]}</span>
              <p className="turn-text">{turn.text}</p>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
