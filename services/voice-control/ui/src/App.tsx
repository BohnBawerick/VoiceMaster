import { AgentDetail } from './AgentDetail';
import { AgentsView } from './AgentsView';
import { CallsView } from './CallsView';
import { NewCallView } from './NewCallView';
import { useLocation } from './router';
import { ScheduleView } from './ScheduleView';
import { SettingsView } from './SettingsView';
import { Shell } from './Shell';
import type { Section } from './Shell';

/* Screens distinguished by path. The service serves index.html for any
 * non-API path (the SPA fallback in app.py), so every one of these is a real,
 * linkable, reloadable URL:
 *
 *   /                      Calls            /calls/<call_id>   one Call, over the list
 *   /agents                Agents           /agents/new        the creation wizard
 *   /agents/<id>[/<tab>]   one Agent        /place             New call, now
 *   /schedule              Schedule         /schedule/new      New call, later
 *   /settings[/<section>]  Settings
 */
type Screen = 'calls' | 'agents' | 'agent' | 'new-agent' | 'place' | 'schedule' | 'settings';

interface Route {
  screen: Screen;
  params: string[];
}

function decode(part: string): string {
  try {
    return decodeURIComponent(part);
  } catch {
    return part;
  }
}

function route(path: string): Route {
  const parts = path.split('/').filter(Boolean).map(decode);
  const [head, ...rest] = parts;
  if (head === 'calls' && rest.length) return { screen: 'calls', params: [rest.join('/')] };
  if (head === 'agents') {
    if (rest[0] === 'new') return { screen: 'new-agent', params: [] };
    if (rest.length) return { screen: 'agent', params: rest };
    return { screen: 'agents', params: [] };
  }
  if (head === 'place') return { screen: 'place', params: [] };
  if (head === 'schedule') return { screen: 'schedule', params: rest };
  if (head === 'settings') return { screen: 'settings', params: rest };
  return { screen: 'calls', params: [] };
}

const SECTION: Record<Screen, Section> = {
  calls: 'calls',
  agents: 'agents',
  agent: 'agents',
  'new-agent': 'agents',
  place: 'new-call',
  schedule: 'schedule',
  settings: 'settings',
};

function Screen({ current }: { current: Route }) {
  const { screen, params } = current;
  if (screen === 'agents' || screen === 'new-agent') {
    return <AgentsView wizard={screen === 'new-agent'} />;
  }
  if (screen === 'agent') {
    return <AgentDetail agentId={params[0]} tab={params[1] || 'overview'} />;
  }
  if (screen === 'place') {
    return <NewCallView initialWhen="now" />;
  }
  if (screen === 'schedule') {
    if (params[0] === 'new') return <NewCallView initialWhen="later" />;
    return <ScheduleView />;
  }
  if (screen === 'settings') {
    return <SettingsView section={params[0] || 'defaults'} />;
  }
  return <CallsView callId={params[0] ?? null} />;
}

export function App() {
  const { path } = useLocation();
  const current = route(path);
  const section = current.screen === 'schedule' && current.params[0] === 'new' ? 'new-call' : SECTION[current.screen];
  return (
    <Shell section={section}>
      <Screen current={current} />
    </Shell>
  );
}
