import '@testing-library/jest-dom/vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { NewCallView } from './NewCallView';
import { ScheduleView } from './ScheduleView';
import { formatLocalTime } from './scheduleTime';
import type { Schedule } from './types';

const agents = [
  {
    id: 'scout',
    description: 'the live one',
    hermes_profile: 'scout',
    enabled: true,
    pipeline: 'realtime',
    direction: null,
    outlets: { phone: { inbound: false, outbound: false }, talk: { inbound: false, outbound: false } },
    valid: true,
    errors: [],
  },
];

const speedDial = {
  owner: '+61491570156',
  numbers: [{ label: 'Me', number: '+61491570156', owner: true }],
};

const active = {
  outlets: { phone: { inbound: null, outbound: null }, talk: { inbound: null, outbound: null } },
  outlet_order: ['phone', 'talk'],
  warnings: [],
  slot_warnings: {
    phone: { inbound: null, outbound: null },
    talk: { inbound: null, outbound: null },
  },
  voice_agent_env: null,
};

function schedule(overrides: Partial<Schedule> = {}): Schedule {
  return {
    id: 'sch-000000000001',
    status: 'pending',
    created_at: '2026-08-19T01:00:00Z',
    due_at: '2026-08-20T07:00:00Z',
    timezone: 'Australia/Perth',
    local_time: '2026-08-20T15:00:00',
    utc_offset: '+08:00',
    agent: 'scout',
    to: '+61491570156',
    mission: 'Ask if Friday still works.',
    disclose: false,
    ...overrides,
  };
}

function mockFetch(handlers: Record<string, (init?: RequestInit) => unknown>) {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const url = input.toString();
    for (const [key, fn] of Object.entries(handlers)) {
      if (url.includes(key)) {
        const body = fn(init);
        return new Response(JSON.stringify(body), { status: 200 });
      }
    }
    return new Response('not found', { status: 404 });
  });
}

function listOf(schedules: Schedule[]) {
  return { schedules, now: '2026-08-19T02:00:00Z', timezone: 'Australia/Perth', grace_s: 300 };
}

describe('Schedule screen', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('lists an upcoming Call in the wall clock it was written in', async () => {
    mockFetch({
      '/api/agents': () => agents,
      '/api/active': () => active,
      '/api/speed-dial': () => speedDial,
      '/api/schedules': () => listOf([schedule()]),
    });
    render(<ScheduleView />);

    await waitFor(() => {
      expect(screen.getByTestId('schedule-row-sch-000000000001')).toBeInTheDocument();
    });
    const when = screen.getByTestId('schedule-when-sch-000000000001');
    expect(when).toHaveTextContent('15:00');
    // The date is printed in the viewer's locale; the day it names is the one
    // the Schedule was written for, which is what must not drift.
    expect(when.textContent).toMatch(/Thu/);
    expect(when.textContent).toMatch(/20/);
    expect(when.textContent).toMatch(/2026/);
    expect(screen.getByTestId('schedule-row-sch-000000000001')).toHaveTextContent(
      'Australia/Perth'
    );
    expect(screen.getByTestId('schedule-status-sch-000000000001')).toHaveTextContent(
      'Upcoming'
    );
    expect(screen.getByTestId('schedule-cancel-sch-000000000001')).toBeInTheDocument();
  });

  it('posts the local time with the browser zone, not an instant it invented', async () => {
    const posted: unknown[] = [];
    mockFetch({
      '/api/agents': () => agents,
      '/api/active': () => active,
      '/api/speed-dial': () => speedDial,
      '/api/schedules': (init) => {
        if (init?.method === 'POST') {
          posted.push(JSON.parse(String(init?.body || '{}')));
          return schedule();
        }
        return listOf([]);
      },
    });
    render(<NewCallView initialWhen="later" />);
    await waitFor(() => expect(screen.getByTestId('place-agent')).toBeInTheDocument());

    fireEvent.change(screen.getByTestId('place-agent'), {
      target: { value: 'scout' },
    });
    fireEvent.change(screen.getByTestId('schedule-at'), {
      target: { value: '2026-08-20T15:00' },
    });
    fireEvent.change(screen.getByTestId('place-mission'), {
      target: { value: 'Ask if Friday still works.' },
    });
    fireEvent.click(screen.getByLabelText('Disclose that the Agent is an AI'));
    fireEvent.click(screen.getByTestId('schedule-submit'));

    await waitFor(() => expect(screen.getByTestId('schedule-success')).toBeInTheDocument());
    expect(posted).toHaveLength(1);
    const body = posted[0] as Record<string, unknown>;
    expect(body.at).toBe('2026-08-20T15:00');
    expect(body.tz).toBe(Intl.DateTimeFormat().resolvedOptions().timeZone);
    expect(body.agent).toBe('scout');
    expect(body.to).toBe('+61491570156');
    expect(body.mission).toBe('Ask if Friday still works.');
    expect(body.disclose).toBe(true);
  });

  it('shows the API refusal for a local time that does not exist', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url.includes('/api/agents')) return new Response(JSON.stringify(agents));
      if (url.includes('/api/active')) return new Response(JSON.stringify(active));
      if (url.includes('/api/speed-dial')) return new Response(JSON.stringify(speedDial));
      if (url.includes('/api/schedules') && init?.method === 'POST') {
        return new Response(
          JSON.stringify({
            detail: [
              'at: 2026-10-04T02:30:00 does not exist in Australia/Sydney — ' +
                'daylight saving moves the clocks forward 1:00 that day',
            ],
          }),
          { status: 422 }
        );
      }
      return new Response(JSON.stringify(listOf([])));
    });
    render(<NewCallView initialWhen="later" />);
    await waitFor(() => expect(screen.getByTestId('place-agent')).toBeInTheDocument());
    fireEvent.change(screen.getByTestId('place-agent'), {
      target: { value: 'scout' },
    });
    fireEvent.change(screen.getByTestId('place-mission'), { target: { value: 'M' } });
    fireEvent.click(screen.getByTestId('schedule-submit'));

    await waitFor(() =>
      expect(screen.getByTestId('schedule-error')).toHaveTextContent('does not exist')
    );
  });

  it('cancels one Schedule by id and shows a refusal when it is too late', async () => {
    const deleted: string[] = [];
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url.includes('/api/agents')) return new Response(JSON.stringify(agents));
      if (url.includes('/api/active')) return new Response(JSON.stringify(active));
      if (url.includes('/api/speed-dial')) return new Response(JSON.stringify(speedDial));
      if (init?.method === 'DELETE') {
        deleted.push(url);
        return new Response(
          JSON.stringify({ detail: ['too late — this Call is already being placed'] }),
          { status: 409 }
        );
      }
      return new Response(JSON.stringify(listOf([schedule()])));
    });
    render(<ScheduleView />);
    await waitFor(() =>
      expect(screen.getByTestId('schedule-cancel-sch-000000000001')).toBeInTheDocument()
    );
    fireEvent.click(screen.getByTestId('schedule-cancel-sch-000000000001'));

    await waitFor(() =>
      expect(screen.getByTestId('schedule-error')).toHaveTextContent('already being placed')
    );
    expect(deleted).toEqual([expect.stringContaining('/api/schedules/sch-000000000001')]);
  });

  it('shows a failed Schedule with its honest reason and no cancel button', async () => {
    mockFetch({
      '/api/agents': () => agents,
      '/api/active': () => active,
      '/api/speed-dial': () => speedDial,
      '/api/schedules': () =>
        listOf([
          schedule({
            id: 'sch-00000000000f',
            status: 'failed',
            settled_at: '2026-08-20T07:00:02Z',
            reason: 'the phone bridge is unreachable at http://127.0.0.1:3336 — no call placed',
          }),
        ]),
    });
    render(<ScheduleView />);
    // Nothing is upcoming; the failed one is under Past.
    await waitFor(() => expect(screen.getByTestId('schedule-upcoming-empty')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('schedule-tab-past'));
    await waitFor(() =>
      expect(screen.getByTestId('schedule-status-sch-00000000000f')).toHaveTextContent('Failed')
    );
    expect(screen.getByTestId('schedule-reason-sch-00000000000f')).toHaveTextContent(
      'no call placed'
    );
    expect(screen.queryByTestId('schedule-cancel-sch-00000000000f')).not.toBeInTheDocument();
  });

  it('says which of the two 2:30s it took when the clock reads it twice', async () => {
    mockFetch({
      '/api/agents': () => agents,
      '/api/active': () => active,
      '/api/speed-dial': () => speedDial,
      '/api/schedules': (init) => {
        if (init?.method === 'POST') {
          return schedule({
            timezone: 'Australia/Sydney',
            local_time: '2027-04-04T02:30:00',
            due_at: '2027-04-03T15:30:00Z',
            utc_offset: '+11:00',
            ambiguous_local_time: true,
          });
        }
        return listOf([]);
      },
    });
    render(<NewCallView initialWhen="later" />);
    await waitFor(() => expect(screen.getByTestId('place-agent')).toBeInTheDocument());
    fireEvent.change(screen.getByTestId('place-agent'), {
      target: { value: 'scout' },
    });
    fireEvent.change(screen.getByTestId('place-mission'), { target: { value: 'M' } });
    fireEvent.click(screen.getByTestId('schedule-submit'));

    await waitFor(() => expect(screen.getByTestId('schedule-ambiguous')).toBeInTheDocument());
    expect(screen.getByTestId('schedule-ambiguous')).toHaveTextContent('twice');
  });

  it('prints the wall clock that was promised, whatever the viewer clock says', () => {
    /* formatLocalTime reads local_time, never due_at: a viewer in another zone
       must still see the time the Schedule was written for. */
    expect(formatLocalTime(schedule())).toContain('15:00');
    expect(
      formatLocalTime(schedule({ local_time: '2026-12-24T09:05:00', due_at: '2026-12-24T01:05:00Z' }))
    ).toContain('09:05');
  });
});
