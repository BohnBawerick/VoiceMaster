import '@testing-library/jest-dom/vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { NewCallView } from './NewCallView';

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
  {
    id: 'asleep',
    description: 'paused',
    hermes_profile: 'asleep',
    enabled: false,
    pipeline: 'realtime',
    direction: null,
    outlets: { phone: { inbound: false, outbound: false }, talk: { inbound: false, outbound: false } },
    valid: true,
    errors: [],
  },
];

const speedDial = {
  owner: '+61491570156',
  numbers: [
    { label: 'Me', number: '+61491570156', owner: true },
    { label: 'Dentist', number: '+61390000000', owner: false },
  ],
};

const active = {
  outlets: {
    phone: { inbound: null, outbound: null },
    talk: { inbound: null, outbound: null },
  },
  outlet_order: ['phone', 'talk'],
  warnings: [],
  slot_warnings: {
    phone: { inbound: null, outbound: null },
    talk: { inbound: null, outbound: null },
  },
  voice_agent_env: null,
};

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

describe('Place a call', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('offers runnable Agents and the owner on speed dial, not a disabled Agent', async () => {
    mockFetch({
      '/api/agents': () => agents,
      '/api/active': () => active,
      '/api/speed-dial': () => speedDial,
    });
    render(<NewCallView initialWhen="now" />);

    await waitFor(() => {
      expect(screen.getByTestId('place-agent')).toBeInTheDocument();
    });
    const select = screen.getByTestId('place-agent') as HTMLSelectElement;
    const values = Array.from(select.options).map((o) => o.value);
    expect(values).toContain('scout');
    expect(values).not.toContain('asleep');
    expect(screen.getByTestId('speed-dial-owner')).toHaveTextContent('+61491570156');
    expect(screen.getByLabelText('Disclose that the Agent is an AI')).not.toBeChecked();
  });

  it('posts Agent, number, Mission and disclose, and does not mention a dry-run', async () => {
    const posted: unknown[] = [];
    mockFetch({
      '/api/agents': () => agents,
      '/api/active': () => active,
      '/api/speed-dial': () => speedDial,
      '/api/calls/place': (init) => {
        posted.push(JSON.parse(String(init?.body || '{}')));
        return {
          placed: true,
          call_sid: 'CAui1',
          call_id: 'cid-ui',
          agent: 'scout',
          to: '+61390000000',
          mission: 'Ask if Friday still works.',
          disclose: true,
        };
      },
    });
    render(<NewCallView initialWhen="now" />);
    await waitFor(() => expect(screen.getByTestId('place-agent')).toBeInTheDocument());

    fireEvent.change(screen.getByTestId('place-agent'), { target: { value: 'scout' } });
    fireEvent.click(screen.getByTestId('speed-dial-+61390000000'));
    fireEvent.change(screen.getByTestId('place-mission'), {
      target: { value: 'Ask if Friday still works.' },
    });
    fireEvent.click(screen.getByLabelText('Disclose that the Agent is an AI'));
    fireEvent.click(screen.getByTestId('place-submit'));

    await waitFor(() => {
      expect(screen.getByTestId('place-success')).toBeInTheDocument();
    });
    expect(posted).toEqual([
      {
        agent: 'scout',
        to: '+61390000000',
        mission: 'Ask if Friday still works.',
        disclose: true,
      },
    ]);
    expect(screen.getByTestId('place-success')).toHaveTextContent('Ask if Friday still works.');
    expect(screen.queryByText(/dry.?run/i)).not.toBeInTheDocument();
  });

  it('refuses to fire without a Mission', async () => {
    mockFetch({
      '/api/agents': () => agents,
      '/api/active': () => active,
      '/api/speed-dial': () => speedDial,
      '/api/calls/place': () => {
        throw new Error('should not have placed');
      },
    });
    render(<NewCallView initialWhen="now" />);
    await waitFor(() => expect(screen.getByTestId('place-submit')).toBeInTheDocument());
    fireEvent.change(screen.getByTestId('place-agent'), { target: { value: 'scout' } });
    fireEvent.click(screen.getByTestId('place-submit'));
    await waitFor(() => {
      expect(screen.getByTestId('place-error')).toHaveTextContent('what this Call is for');
    });
  });

  it('puts an expanded Mission in the editable field and does not place', async () => {
    const posted: string[] = [];
    mockFetch({
      '/api/agents': () => agents,
      '/api/active': () => active,
      '/api/speed-dial': () => speedDial,
      '/api/missions/expand': () => ({
        mission: 'Call the dentist and move Thursday to Friday.',
        agent: 'scout',
        hermes_profile: 'scout',
      }),
      '/api/calls/place': () => {
        posted.push('placed');
        throw new Error('must not place from expand');
      },
    });
    render(<NewCallView initialWhen="now" />);
    await waitFor(() => expect(screen.getByTestId('place-expand')).toBeInTheDocument());
    fireEvent.change(screen.getByTestId('place-agent'), { target: { value: 'scout' } });
    fireEvent.change(screen.getByTestId('place-mission'), { target: { value: 'move thursday' } });
    fireEvent.click(screen.getByTestId('place-expand'));
    await waitFor(() => {
      expect(screen.getByTestId('place-mission')).toHaveValue(
        'Call the dentist and move Thursday to Friday.'
      );
    });
    expect(posted).toEqual([]);
    expect(screen.queryByTestId('place-success')).not.toBeInTheDocument();
    expect(screen.getByTestId('place-mission')).not.toHaveAttribute('readOnly');
    expect(screen.getByTestId('place-mission')).not.toBeDisabled();
  });

  it('cannot get from assist to a placed call without the text sitting in the field', async () => {
    /* THE never-sent-unseen test. Expand writes the textarea. The operator
     * edits it. Place posts what is in the field, not what expand returned.
     * Sabotage: place with the expand payload and this goes red. */
    const placed: unknown[] = [];
    mockFetch({
      '/api/agents': () => agents,
      '/api/active': () => active,
      '/api/speed-dial': () => speedDial,
      '/api/missions/expand': () => ({
        mission: 'EXPANDED UNSEEN',
        agent: 'scout',
        hermes_profile: 'scout',
      }),
      '/api/calls/place': (init) => {
        placed.push(JSON.parse(String(init?.body || '{}')));
        return {
          placed: true,
          call_sid: 'CAui2',
          call_id: 'cid-ui2',
          agent: 'scout',
          to: '+61491570156',
          mission: 'the operator edited this',
          disclose: false,
        };
      },
    });
    render(<NewCallView initialWhen="now" />);
    await waitFor(() => expect(screen.getByTestId('place-expand')).toBeInTheDocument());
    fireEvent.change(screen.getByTestId('place-agent'), { target: { value: 'scout' } });
    fireEvent.change(screen.getByTestId('place-mission'), { target: { value: 'short' } });
    fireEvent.click(screen.getByTestId('place-expand'));
    await waitFor(() => {
      expect(screen.getByTestId('place-mission')).toHaveValue('EXPANDED UNSEEN');
    });
    fireEvent.change(screen.getByTestId('place-mission'), {
      target: { value: 'the operator edited this' },
    });
    fireEvent.click(screen.getByTestId('place-submit'));
    await waitFor(() => expect(screen.getByTestId('place-success')).toBeInTheDocument());
    expect(placed).toEqual([
      {
        agent: 'scout',
        to: '+61491570156',
        mission: 'the operator edited this',
        disclose: false,
      },
    ]);
  });

  it.each([
    ['expand', '/api/missions/expand'],
  ])('leaves the typed Mission intact when %s fails', async (_name, path) => {
    mockFetch({
      '/api/agents': () => agents,
      '/api/active': () => active,
      '/api/speed-dial': () => speedDial,
      [path]: () => {
        throw new Error('gateway mock should 502');
      },
    });
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = input.toString();
      if (url.includes('/api/agents')) return new Response(JSON.stringify(agents), { status: 200 });
      if (url.includes('/api/active')) return new Response(JSON.stringify(active), { status: 200 });
      if (url.includes('/api/speed-dial')) return new Response(JSON.stringify(speedDial), { status: 200 });
      if (url.includes(path)) {
        return new Response(JSON.stringify({ detail: ['the Agent could not write a Mission'] }), {
          status: 502,
        });
      }
      return new Response('not found', { status: 404 });
    });
    render(<NewCallView initialWhen="now" />);
    await waitFor(() => expect(screen.getByTestId('place-expand')).toBeInTheDocument());
    fireEvent.change(screen.getByTestId('place-agent'), { target: { value: 'scout' } });
    fireEvent.change(screen.getByTestId('place-mission'), { target: { value: 'keep this typed line' } });
    fireEvent.click(screen.getByTestId('place-expand'));
    await waitFor(() => {
      expect(screen.getByTestId('place-assist-error')).toBeInTheDocument();
    });
    expect(screen.getByTestId('place-mission')).toHaveValue('keep this typed line');
    expect(screen.queryByTestId('place-success')).not.toBeInTheDocument();
  });

  it('leaves the typed Mission intact when the microphone is denied', async () => {
    mockFetch({
      '/api/agents': () => agents,
      '/api/active': () => active,
      '/api/speed-dial': () => speedDial,
      '/api/missions/dictate': () => {
        throw new Error('must not dictate after a denied mic');
      },
    });
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: {
        getUserMedia: async () => {
          throw Object.assign(new Error('Permission denied'), { name: 'NotAllowedError' });
        },
      },
    });
    window.MediaRecorder = class {} as unknown as typeof MediaRecorder;
    render(<NewCallView initialWhen="now" />);
    await waitFor(() => expect(screen.getByTestId('place-record')).toBeInTheDocument());
    fireEvent.change(screen.getByTestId('place-agent'), { target: { value: 'scout' } });
    fireEvent.change(screen.getByTestId('place-mission'), { target: { value: 'keep this typed line' } });
    fireEvent.click(screen.getByTestId('place-record'));
    await waitFor(() => {
      expect(screen.getByTestId('place-assist-error')).toHaveTextContent(/permission denied/i);
    });
    expect(screen.getByTestId('place-mission')).toHaveValue('keep this typed line');
  });

  it('leaves the typed Mission intact when recording is unavailable', async () => {
    mockFetch({
      '/api/agents': () => agents,
      '/api/active': () => active,
      '/api/speed-dial': () => speedDial,
    });
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: undefined,
    });
    render(<NewCallView initialWhen="now" />);
    await waitFor(() => expect(screen.getByTestId('place-record')).toBeInTheDocument());
    fireEvent.change(screen.getByTestId('place-agent'), { target: { value: 'scout' } });
    fireEvent.change(screen.getByTestId('place-mission'), { target: { value: 'keep this typed line' } });
    fireEvent.click(screen.getByTestId('place-record'));
    await waitFor(() => {
      expect(screen.getByTestId('place-assist-error')).toHaveTextContent(/cannot record/i);
    });
    expect(screen.getByTestId('place-mission')).toHaveValue('keep this typed line');
  });

  it('leaves the typed Mission intact when dictation is silent', async () => {
    mockFetch({
      '/api/agents': () => agents,
      '/api/active': () => active,
      '/api/speed-dial': () => speedDial,
      '/api/missions/dictate': () => {
        throw new Error('must not POST a silent recording');
      },
    });
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: {
        getUserMedia: async () => ({ getTracks: () => [{ stop: () => undefined }] }),
      },
    });
    class SilentRecorder {
      state = 'inactive';
      ondataavailable: ((ev: { data: Blob }) => void) | null = null;
      onstop: (() => void) | null = null;
      mimeType = 'audio/webm';
      start() {
        this.state = 'recording';
      }
      stop() {
        this.state = 'inactive';
        this.ondataavailable?.({ data: new Blob([]) });
        this.onstop?.();
      }
    }
    window.MediaRecorder = SilentRecorder as unknown as typeof MediaRecorder;
    render(<NewCallView initialWhen="now" />);
    await waitFor(() => expect(screen.getByTestId('place-record')).toBeInTheDocument());
    fireEvent.change(screen.getByTestId('place-agent'), { target: { value: 'scout' } });
    fireEvent.change(screen.getByTestId('place-mission'), { target: { value: 'keep this typed line' } });
    fireEvent.click(screen.getByTestId('place-record'));
    await waitFor(() => {
      expect(screen.getByTestId('place-record')).toHaveTextContent('Stop');
    });
    fireEvent.click(screen.getByTestId('place-record'));
    await waitFor(() => {
      expect(screen.getByTestId('place-assist-error')).toHaveTextContent(/silent/i);
    });
    expect(screen.getByTestId('place-mission')).toHaveValue('keep this typed line');
  });

  it('leaves the typed Mission intact when dictate fails at the gateway', async () => {
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: {
        getUserMedia: async () => ({ getTracks: () => [{ stop: () => undefined }] }),
      },
    });
    class LoudRecorder {
      state = 'inactive';
      ondataavailable: ((ev: { data: Blob }) => void) | null = null;
      onstop: (() => void) | null = null;
      mimeType = 'audio/webm';
      start() {
        this.state = 'recording';
      }
      stop() {
        this.state = 'inactive';
        this.ondataavailable?.({ data: new Blob([new Uint8Array(400)], { type: 'audio/webm' }) });
        this.onstop?.();
      }
    }
    window.MediaRecorder = LoudRecorder as unknown as typeof MediaRecorder;
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = input.toString();
      if (url.includes('/api/agents')) return new Response(JSON.stringify(agents), { status: 200 });
      if (url.includes('/api/active')) return new Response(JSON.stringify(active), { status: 200 });
      if (url.includes('/api/speed-dial')) return new Response(JSON.stringify(speedDial), { status: 200 });
      if (url.includes('/api/missions/dictate')) {
        return new Response(JSON.stringify({ detail: ['the Agent could not write a Mission'] }), {
          status: 502,
        });
      }
      return new Response('not found', { status: 404 });
    });
    render(<NewCallView initialWhen="now" />);
    await waitFor(() => expect(screen.getByTestId('place-record')).toBeInTheDocument());
    fireEvent.change(screen.getByTestId('place-agent'), { target: { value: 'scout' } });
    fireEvent.change(screen.getByTestId('place-mission'), { target: { value: 'keep this typed line' } });
    fireEvent.click(screen.getByTestId('place-record'));
    await waitFor(() => {
      expect(screen.getByTestId('place-record')).toHaveTextContent('Stop');
    });
    fireEvent.click(screen.getByTestId('place-record'));
    await waitFor(() => {
      expect(screen.getByTestId('place-assist-error')).toBeInTheDocument();
    });
    expect(screen.getByTestId('place-mission')).toHaveValue('keep this typed line');
  });

  it('a second Record click while the mic is opening does not leak a stream', async () => {
    /* THE N1 test. getUserMedia takes 150 ms (real device-open latency).
     * Two clicks inside that window used to open two streams and orphan
     * the first: Stop stopped only the second, and the mic stayed live.
     * Sabotage: drop the openingRef guard and this goes red (opens === 2). */
    const tracks: { stop: ReturnType<typeof vi.fn> }[] = [];
    let opens = 0;
    mockFetch({
      '/api/agents': () => agents,
      '/api/active': () => active,
      '/api/speed-dial': () => speedDial,
      '/api/missions/dictate': () => ({
        mission: 'Call the dentist.',
        agent: 'scout',
        hermes_profile: 'scout',
      }),
    });
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: {
        getUserMedia: async () => {
          opens += 1;
          await new Promise((r) => setTimeout(r, 150));
          const track = { stop: vi.fn() };
          tracks.push(track);
          return { getTracks: () => [track] };
        },
      },
    });
    class Rec {
      state = 'inactive';
      ondataavailable: ((ev: { data: Blob }) => void) | null = null;
      onstop: (() => void) | null = null;
      mimeType = 'audio/webm';
      start() {
        this.state = 'recording';
      }
      stop() {
        this.state = 'inactive';
        this.ondataavailable?.({ data: new Blob([new Uint8Array(400)], { type: 'audio/webm' }) });
        this.onstop?.();
      }
    }
    window.MediaRecorder = Rec as unknown as typeof MediaRecorder;
    render(<NewCallView initialWhen="now" />);
    await waitFor(() => expect(screen.getByTestId('place-record')).toBeInTheDocument());
    fireEvent.change(screen.getByTestId('place-agent'), { target: { value: 'scout' } });
    fireEvent.click(screen.getByTestId('place-record'));
    fireEvent.click(screen.getByTestId('place-record'));
    await waitFor(() => {
      expect(screen.getByTestId('place-record')).toHaveTextContent('Stop');
    });
    expect(opens).toBe(1);
    expect(tracks).toHaveLength(1);
    fireEvent.click(screen.getByTestId('place-record'));
    await waitFor(() => {
      expect(tracks[0].stop).toHaveBeenCalled();
    });
    expect(tracks.every((t) => t.stop.mock.calls.length > 0)).toBe(true);
  });

  it('hand-typed Mission still places exactly as ticket 09', async () => {
    const posted: unknown[] = [];
    mockFetch({
      '/api/agents': () => agents,
      '/api/active': () => active,
      '/api/speed-dial': () => speedDial,
      '/api/calls/place': (init) => {
        posted.push(JSON.parse(String(init?.body || '{}')));
        return {
          placed: true,
          call_sid: 'CAhand',
          call_id: 'cid-hand',
          agent: 'scout',
          to: '+61491570156',
          mission: 'typed in full by hand',
          disclose: false,
        };
      },
    });
    render(<NewCallView initialWhen="now" />);
    await waitFor(() => expect(screen.getByTestId('place-agent')).toBeInTheDocument());
    fireEvent.change(screen.getByTestId('place-agent'), { target: { value: 'scout' } });
    fireEvent.change(screen.getByTestId('place-mission'), { target: { value: 'typed in full by hand' } });
    fireEvent.click(screen.getByTestId('place-submit'));
    await waitFor(() => expect(screen.getByTestId('place-success')).toBeInTheDocument());
    expect(posted[0]).toMatchObject({ mission: 'typed in full by hand' });
  });
});
