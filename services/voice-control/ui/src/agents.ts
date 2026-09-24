/* What the screens say about an Agent, derived only from its roster row. */
import { DIRECTIONS } from './format';
import type { Agent, Direction } from './types';

/* An agent the call path would refuse: broken schema, or paused. Both make
 * every call on a slot naming it refuse to start, so both read as broken. */
export function agentIsBroken(agent: Agent): boolean {
  return !agent.valid || !agent.enabled;
}

/* "deepgram · hermes-agent · elevenlabs", or the realtime provider: what the
 * Agent is built from, as the Agent's document says. */
export function agentStack(agent: Agent): string {
  const p = agent.providers || {};
  if (agent.pipeline === 'cascade') {
    return [p.stt, p.llm, p.tts].filter(Boolean).join(' · ') || 'cascade';
  }
  return p.realtime || agent.pipeline || 'realtime';
}

export const AGENT_TYPE_LABEL: Record<string, string> = {
  'hermes-direct': 'Hermes directly',
  realtime: 'OpenAI Realtime',
  custom: 'Advanced',
};

/* Where this Agent sits, from the roster's per-Outlet flags. */
export function heldSlots(agent: Agent, outletOrder: string[]): { outlet: string; direction: Direction }[] {
  const held: { outlet: string; direction: Direction }[] = [];
  for (const outlet of outletOrder) {
    const flags = agent.outlets?.[outlet];
    if (!flags) continue;
    for (const direction of DIRECTIONS) if (flags[direction]) held.push({ outlet, direction });
  }
  return held;
}

