import type {
  ActiveResponse,
  Agent,
  AgentsScreenData,
  AuthoredMission,
  CallsResponse,
  CallDetailResponse,
  CreateAgentRequest,
  CreateAgentResponse,
  Direction,
  HermesState,
  Inheritable,
  PlaceCallRequest,
  PlaceCallResponse,
  ProviderRow,
  Schedule,
  SchedulesResponse,
  CreateScheduleRequest,
  SettingsData,
  SpeedDial,
  UpdateVoiceRequest,
  UpdateHermesProfileResponse,
  UpdateVoiceResponse,
  VoiceCatalog,
} from './types';

/* The Agents screen reads two endpoints and never a third that flattens them:
 * `/api/agents` is who exists, `/api/active` is who is on which Outlet. Both
 * are the same shapes the bridges resolve, so the screen cannot claim an
 * assignment the call path does not make. A failure here throws -- an Agents
 * screen that renders half its truth is the F1/F2 defect class. */
export async function fetchAgentsScreen(): Promise<AgentsScreenData> {
  const [agentsRes, activeRes] = await Promise.all([
    fetch('/api/agents'),
    fetch('/api/active'),
  ]);
  if (!agentsRes.ok) throw new Error(`GET /api/agents failed (HTTP ${agentsRes.status})`);
  if (!activeRes.ok) throw new Error(`GET /api/active failed (HTTP ${activeRes.status})`);
  const agents: Agent[] = await agentsRes.json();
  const active: ActiveResponse = await activeRes.json();
  return { agents, active };
}

/* Assign (or clear) ONE slot: one Outlet, one direction.
 *
 * The body carries exactly the slot that was touched. `PUT /api/active` only
 * writes outlets present in the map and directions present in the entry, so an
 * Outlet the operator did not touch is not in the request at all and cannot be
 * changed by it. This is the whole point of the rebuilt screen: the old screen's
 * buttons sent a flat `{inbound: ...}` key, which named no Outlet and so meant
 * BOTH of them, silently undoing a per-Outlet split. Ticket 17 deleted that
 * shape from the API, so sending it now is a 422 rather than a quiet 200.
 *
 * Resolves to the new ActiveResponse, or rejects with the API's own refusal
 * text (422) -- the same refusal the live call path would make. */
export async function assignOutletSlot(
  outlet: string,
  direction: Direction,
  agentId: string | null
): Promise<ActiveResponse> {
  const res = await fetch('/api/active', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ outlets: { [outlet]: { [direction]: agentId } } }),
  });
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = body && body.detail;
    const text = Array.isArray(detail) ? detail.join(' ') : detail;
    throw new Error(text || `PUT /api/active failed (HTTP ${res.status})`);
  }
  return body as ActiveResponse;
}

/* ---------------------------------------------------------------------------
 * The Agent creation wizard (ticket 13).
 * ------------------------------------------------------------------------ */

/* Can this dashboard create a real Hermes profile, and what already exists.
 * A failure here throws: a wizard that opened against an unknown answer would
 * either offer creation it cannot do, or hide creation that works. */
export async function fetchHermesState(): Promise<HermesState> {
  const res = await fetch('/api/hermes');
  if (!res.ok) throw new Error(`GET /api/hermes failed (HTTP ${res.status})`);
  return (await res.json()) as HermesState;
}

/* What inheriting from this profile would actually take. */
export async function fetchInheritable(name: string): Promise<Inheritable> {
  const res = await fetch(`/api/hermes/profiles/${encodeURIComponent(name)}`);
  if (!res.ok) throw new Error(`could not read the profile '${name}' (HTTP ${res.status})`);
  return (await res.json()) as Inheritable;
}

export async function fetchProviders(): Promise<ProviderRow[]> {
  const res = await fetch('/api/providers');
  if (!res.ok) throw new Error(`GET /api/providers failed (HTTP ${res.status})`);
  return (await res.json()) as ProviderRow[];
}

export async function fetchVoices(providerId: string): Promise<VoiceCatalog> {
  const res = await fetch(`/api/providers/${encodeURIComponent(providerId)}/voices`);
  if (!res.ok) throw new Error(`GET /api/providers/${providerId}/voices failed (HTTP ${res.status})`);
  return (await res.json()) as VoiceCatalog;
}

/* The wizard's ONE write. Nothing before this call touches the disk, which is
 * the first half of the abandonment guarantee: a closed tab leaves nothing.
 * Rejects with the API's own refusal text, which is the same refusal the live
 * call path would make about the same document. */
export async function createAgent(body: CreateAgentRequest): Promise<CreateAgentResponse> {
  const res = await fetch('/api/agents/create', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const payload = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = payload && payload.detail;
    const text = Array.isArray(detail) ? detail.join(' ') : detail;
    throw new Error(text || `POST /api/agents/create failed (HTTP ${res.status})`);
  }
  return payload as CreateAgentResponse;
}

/* ---------------------------------------------------------------------------
 * Settings screen (ticket 14).
 * ------------------------------------------------------------------------ */

/* The consolidated Settings payload: proven defaults, labelled pipelines,
 * labelled providers, the realtime voice options and the speed dial. Outlet
 * assignment is still read through fetchAgentsScreen - the SAME endpoints the
 * Agents screen uses, so the two pages cannot disagree about who answers. */
export async function fetchSettings(): Promise<SettingsData> {
  const res = await fetch('/api/settings');
  if (!res.ok) throw new Error(`GET /api/settings failed (HTTP ${res.status})`);
  return (await res.json()) as SettingsData;
}

/* Edit ONE Agent's voice settings (pipeline, providers, knobs). Per-Agent by
 * construction: the endpoint reads that Agent's own document and merges only
 * the keys present, so this Agent's next call speaks the new voice and no other
 * Agent is touched. */
export async function updateAgentVoice(
  agentId: string,
  body: UpdateVoiceRequest
): Promise<UpdateVoiceResponse> {
  const res = await fetch(`/api/agents/${encodeURIComponent(agentId)}/voice`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const payload = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = payload && payload.detail;
    const text = Array.isArray(detail) ? detail.join(' ') : detail;
    throw new Error(text || `PUT /api/agents/${agentId}/voice failed (HTTP ${res.status})`);
  }
  return payload as UpdateVoiceResponse;
}

/* VC24: which Hermes profile this Agent drives. ONE field, through its own narrow
 * endpoint - never the whole-document PUT, which no screen calls. Rejects with the
 * API's own sentence: a profile nobody is running, on an Agent that holds a slot, is
 * refused rather than saved into a quiet outage. */
export async function updateAgentHermesProfile(
  agentId: string,
  hermesProfile: string
): Promise<UpdateHermesProfileResponse> {
  const res = await fetch(`/api/agents/${encodeURIComponent(agentId)}/hermes-profile`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ hermes_profile: hermesProfile }),
  });
  const payload = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = payload && payload.detail;
    const text = Array.isArray(detail) ? detail.join(' ') : detail;
    throw new Error(text || `PUT /api/agents/${agentId}/hermes-profile failed (HTTP ${res.status})`);
  }
  return payload as UpdateHermesProfileResponse;
}

export async function fetchCalls(
  page: number = 1,
  pageSize: number = 20,
  query?: string,
  agent?: string,
  outlet?: string
): Promise<CallsResponse> {
  try {
    const params = new URLSearchParams({
      page: page.toString(),
      page_size: pageSize.toString(),
    });
    if (query && query.trim()) {
      params.append('q', query.trim());
    }
    if (agent) {
      params.append('agent', agent);
    }
    if (outlet) {
      params.append('outlet', outlet);
    }

    const res = await fetch(`/api/calls?${params.toString()}`);
    if (!res.ok) {
      return {
        calls: [],
        total: 0,
        page,
        page_size: pageSize,
        has_more: false,
        unreachable: true,
        error: `HTTP error ${res.status}`,
      };
    }
    const data = await res.json();
    return data;
  } catch {
    return {
      calls: [],
      total: 0,
      page,
      page_size: pageSize,
      has_more: false,
      unreachable: true,
      error: 'Call archive unreachable',
    };
  }
}

export async function fetchSpeedDial(): Promise<SpeedDial> {
  const res = await fetch('/api/speed-dial');
  if (!res.ok) throw new Error(`GET /api/speed-dial failed (HTTP ${res.status})`);
  return (await res.json()) as SpeedDial;
}

export async function saveSpeedDial(numbers: { label: string; number: string }[]): Promise<SpeedDial> {
  const res = await fetch('/api/speed-dial', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ numbers }),
  });
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = body && body.detail;
    const text = Array.isArray(detail) ? detail.join(' ') : detail;
    throw new Error(text || `PUT /api/speed-dial failed (HTTP ${res.status})`);
  }
  return body as SpeedDial;
}

function apiError(payload: unknown, fallback: string): Error {
  const body = payload as { detail?: unknown } | null;
  const detail = body && body.detail;
  const text = Array.isArray(detail) ? detail.join(' ') : detail;
  return new Error(typeof text === 'string' && text ? text : fallback);
}

export async function expandMission(agent: string, prompt: string): Promise<AuthoredMission> {
  const res = await fetch('/api/missions/expand', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ agent, prompt }),
  });
  const payload = await res.json().catch(() => null);
  if (!res.ok) throw apiError(payload, `POST /api/missions/expand failed (HTTP ${res.status})`);
  return payload as AuthoredMission;
}

export async function dictateMission(agent: string, audio: Blob): Promise<AuthoredMission> {
  const body = new FormData();
  body.append('agent', agent);
  body.append('audio', audio, 'recording.webm');
  const res = await fetch('/api/missions/dictate', { method: 'POST', body });
  const payload = await res.json().catch(() => null);
  if (!res.ok) throw apiError(payload, `POST /api/missions/dictate failed (HTTP ${res.status})`);
  return payload as AuthoredMission;
}

export async function placeCall(req: PlaceCallRequest): Promise<PlaceCallResponse> {
  const res = await fetch('/api/calls/place', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req),
  });
  const payload = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = payload && payload.detail;
    const text = Array.isArray(detail) ? detail.join(' ') : detail;
    throw new Error(text || `POST /api/calls/place failed (HTTP ${res.status})`);
  }
  return payload as PlaceCallResponse;
}

export async function fetchCallDetail(callId: string): Promise<CallDetailResponse> {
  try {
    const res = await fetch(`/api/calls/${encodeURIComponent(callId)}`);
    if (!res.ok) {
      if (res.status === 404) {
        return {
          call: null,
          unreachable: false,
          error: `Call '${callId}' not found`,
        };
      }
      return {
        call: null,
        unreachable: true,
        error: `HTTP error ${res.status}`,
      };
    }
    const data = await res.json();
    return data;
  } catch {
    return {
      call: null,
      unreachable: true,
      error: 'Call archive unreachable',
    };
  }
}

/* ---------------------------------------------------------------------------
 * Schedules (ticket 11).
 *
 * Every one of these rejects with the API's own refusal text. That text is the
 * same sentence the manual Place-a-call path produces for the same fault
 * (both are graded by one validator server-side), so the screen never has to
 * invent a second wording for "that Agent cannot run outbound".
 * ------------------------------------------------------------------------ */

export async function fetchSchedules(): Promise<SchedulesResponse> {
  const res = await fetch('/api/schedules');
  if (!res.ok) throw new Error(`GET /api/schedules failed (HTTP ${res.status})`);
  return (await res.json()) as SchedulesResponse;
}

async function refusal(res: Response, fallback: string): Promise<string> {
  const body = await res.json().catch(() => null);
  const detail = body && body.detail;
  const text = Array.isArray(detail) ? detail.join(' ') : detail;
  return text || fallback;
}

export async function createSchedule(req: CreateScheduleRequest): Promise<Schedule> {
  const res = await fetch('/api/schedules', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(req),
  });
  if (!res.ok) {
    throw new Error(await refusal(res, `POST /api/schedules failed (HTTP ${res.status})`));
  }
  return (await res.json()) as Schedule;
}

/* Cancelling races the scheduler for the same claim, so a 409 here is a real
 * answer and not a retryable error: the Call is already being placed. */
export async function cancelSchedule(id: string): Promise<Schedule> {
  const res = await fetch(`/api/schedules/${encodeURIComponent(id)}`, {
    method: 'DELETE',
  });
  if (!res.ok) {
    throw new Error(await refusal(res, `could not cancel ${id} (HTTP ${res.status})`));
  }
  return (await res.json()) as Schedule;
}
