/* The small components every screen shares: absence, Outlet icons, pills.
 * Their wording comes from format.ts. */
import type { ReactNode } from 'react';
import { MessageSquare, Phone, PhoneCall, PhoneIncoming, PhoneOutgoing } from 'lucide-react';
import { NOT_RETAINED } from './format';
import type { Tone } from './format';

/* A value that may not have been recorded. */
export function Value({ text, title }: { text?: string | null; title?: string }) {
  if (text) return <span title={title}>{text}</span>;
  return <span className="meta-item-absent">{NOT_RETAINED}</span>;
}

export function OutletIcon({ outlet, size = 16 }: { outlet: string; size?: number }) {
  return outlet === 'talk' ? <MessageSquare size={size} /> : <PhoneCall size={size} />;
}


/* A status pill. The tone carries meaning; the text always says it too. */
export function Pill({
  tone = 'neutral',
  children,
  icon,
  title,
  testId,
}: {
  tone?: Tone;
  children: ReactNode;
  icon?: ReactNode;
  title?: string;
  testId?: string;
}) {
  return (
    <span className={`pill pill-${tone}`} title={title} data-testid={testId}>
      {icon}
      {children}
    </span>
  );
}

/* The honest evidence label the backend serves, as a pill. */
export function ProvenTag({ proven }: { proven: boolean }) {
  return proven ? (
    <span className="pill pill-accent settings-tag settings-tag-proven">Proven</span>
  ) : (
    <span className="pill pill-warning settings-tag settings-tag-unproven">Untested</span>
  );
}

export function DirectionIcon({ direction, size = 15 }: { direction: string; size?: number }) {
  if (direction === 'inbound') return <PhoneIncoming size={size} />;
  if (direction === 'outbound') return <PhoneOutgoing size={size} />;
  return <Phone size={size} />;
}

/* The retained outcome, as retained. "ok" and "error" are what the bridges
 * write; the pill colours them and never renames them into a claim. */
export function OutcomePill({ outcome }: { outcome?: string | null }) {
  if (!outcome) {
    return <span className="meta-item-absent">{NOT_RETAINED}</span>;
  }
  const tone = outcome === 'ok' ? 'accent' : outcome === 'error' ? 'danger' : 'neutral';
  return <Pill tone={tone}>{outcome}</Pill>;
}

