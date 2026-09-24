import '@testing-library/jest-dom/vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { App } from './App';

/* A Call exactly as /api/calls builds one from what the retainers write:
 * platform, direction, target and a date, and nothing else. No producer writes
 * an outcome, a summary, a duration or a turn count -- see
 * services/voice/server.py, talk-voice-bridge/realtime_bridge.py and
 * voicecore/cascade_live.py, and the docstring on
 * services/voice-control/tests/hindsight_producer_fixtures.py. */
const retainedCall = {
  call_id: 'voice-twilio-inbound-real',
  when: '2026-08-17T09:05:00Z',
  direction: 'inbound',
  who: 'Not retained',
  target: '',
  agent: '',
  outlet: 'voice_twilio',
  outcome: null,
  summary: null,
  transcript:
    'Them: exact original transcript voice-twilio-inbound-real\nAI: exact reply voice-twilio-inbound-real',
};

const listBody = (extra: Record<string, unknown> = {}) => ({
  calls: [retainedCall],
  total: 1,
  page: 1,
  page_size: 20,
  has_more: false,
  unreachable: false,
  partial: false,
  warning: null,
  error: null,
  ...extra,
});

/* The list addresses a Call by its id, which lives on the row rather than in
 * its text: the id moved to the detail view. */
const rowOf = (callId: string) =>
  document.querySelector(`[data-call-id="${callId}"]`) as HTMLElement | null;

const fieldOf = (callId: string, field: string) =>
  rowOf(callId)?.querySelector(`[data-field="${field}"]`) as HTMLElement;

const waitForRow = (callId: string) =>
  waitFor(() => {
    expect(rowOf(callId)).not.toBeNull();
  });

const openCall = async (callId: string) => {
  fireEvent.click(rowOf(callId) as HTMLElement);
  await waitFor(() => {
    expect(screen.getByTestId('call-detail')).toBeInTheDocument();
  });
};

const verbatim = () => {
  fireEvent.click(screen.getByTestId('call-tab-transcript'));
  fireEvent.click(screen.getByTestId('transcript-verbatim'));
};

beforeEach(() => {
  window.history.replaceState({}, '', '/');
});

describe('Voice Control App Frontend', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('says "Not retained" instead of inventing an outcome', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (url) => {
      if (url.toString().includes('/api/calls/voice-twilio-inbound-real')) {
        return new Response(
          JSON.stringify({ call: retainedCall, unreachable: false, partial: false, error: null }),
          { status: 200 }
        );
      }
      return new Response(JSON.stringify(listBody()), { status: 200 });
    });

    render(<App />);

    await waitForRow('voice-twilio-inbound-real');
    // the list: other party and outcome are both absent, and both say so
    expect(fieldOf('voice-twilio-inbound-real', 'who')).toHaveTextContent('Not retained');
    expect(fieldOf('voice-twilio-inbound-real', 'outcome')).toHaveTextContent('Not retained');
    expect(screen.queryByText(/incomplete/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/did not finish cleanly/i)).not.toBeInTheDocument();

    await openCall('voice-twilio-inbound-real');
    // the detail: Outcome and Summary are shown as fields, and shown as absent
    expect(screen.getByText('How the call ended')).toBeInTheDocument();
    expect(screen.getByText('Summary')).toBeInTheDocument();
    expect(screen.getByTestId('summary-detail-absent')).toHaveTextContent('Not retained');
    expect(screen.queryByText(/incomplete/i)).not.toBeInTheDocument();
  });

  it('shows the calls it did fetch when one bank fails, with a banner naming it', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(
      async () =>
        new Response(
          JSON.stringify(
            listBody({
              partial: true,
              warning:
                "Bank 'hermes' could not be read (HTTP 503). Calls retained there are not shown.",
            })
          ),
          { status: 200 }
        )
    );

    render(<App />);

    await waitForRow('voice-twilio-inbound-real');
    expect(screen.getByText('Partial call history')).toBeInTheDocument();
    expect(screen.getByText(/Bank 'hermes' could not be read/)).toBeInTheDocument();
    // a failing bank is not a store outage
    expect(screen.queryByText('Call Archive Unreachable')).not.toBeInTheDocument();
  });

  it('does not claim there are no calls when part of the history failed', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(
      async () =>
        new Response(
          JSON.stringify(
            listBody({
              calls: [],
              total: 0,
              partial: true,
              warning: "Bank 'hermes' could not be read (HTTP 503).",
            })
          ),
          { status: 200 }
        )
    );

    render(<App />);

    await waitFor(() => {
      expect(screen.getByText('Some call history could not be read')).toBeInTheDocument();
    });
    expect(screen.queryByText('No calls recorded yet')).not.toBeInTheDocument();
  });

  it('renders call list with pagination and details', async () => {
    const mockCalls = [
      {
        call_id: 'voice-talk-call-101',
        when: '2026-08-17T14:30:00Z',
        direction: 'outbound',
        who: '+61491570157',
        agent: 'hermes-default',
        outlet: 'talk',
        mission: 'Check server status',
        outcome: 'completed',
        summary: 'Agent checked server.',
        transcript: 'Speaker 1: Status ok.',
      },
    ];

    vi.spyOn(globalThis, 'fetch').mockImplementation(async (url) => {
      const urlStr = url.toString();
      if (urlStr.includes('/api/calls/voice-talk-call-101')) {
        return new Response(
          JSON.stringify({
            call: mockCalls[0],
            unreachable: false,
            error: null,
          }),
          { status: 200 }
        );
      }
      if (urlStr.includes('/api/calls')) {
        return new Response(
          JSON.stringify({
            calls: mockCalls,
            total: 1,
            page: 1,
            page_size: 20,
            has_more: false,
            unreachable: false,
            error: null,
          }),
          { status: 200 }
        );
      }
      return new Response(null, { status: 404 });
    });

    render(<App />);

    // Wait for call list to load
    await waitFor(() => {
      expect(screen.getByText('+61491570157')).toBeInTheDocument();
    });

    expect(fieldOf('voice-talk-call-101', 'agent')).toHaveTextContent('hermes-default');

    // Open the call: it gets its own URL, with the id on the detail view
    await openCall('voice-talk-call-101');
    expect(window.location.pathname).toBe('/calls/voice-talk-call-101');
    expect(screen.getByTestId('call-id')).toHaveTextContent('voice-talk-call-101');
    verbatim();
    expect(screen.getByText('Speaker 1: Status ok.')).toBeInTheDocument();

    // Close it: back on the list, at the list's URL
    fireEvent.click(screen.getByLabelText('Back to Calls list'));
    await waitFor(() => {
      expect(screen.queryByTestId('call-detail')).not.toBeInTheDocument();
    });
    expect(window.location.pathname).toBe('/');
    expect(rowOf('voice-talk-call-101')).not.toBeNull();
  });

  it('renders honest empty state when store is empty', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => {
      return new Response(
        JSON.stringify({
          calls: [],
          total: 0,
          page: 1,
          page_size: 20,
          has_more: false,
          unreachable: false,
          error: null,
        }),
        { status: 200 }
      );
    });

    render(<App />);

    await waitFor(() => {
      expect(screen.getByText('No calls recorded yet')).toBeInTheDocument();
    });
  });

  it('renders honest unreachable store state on network/HTTP error', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => {
      return new Response(
        JSON.stringify({
          calls: [],
          total: 0,
          page: 1,
          page_size: 20,
          has_more: false,
          unreachable: true,
          error: 'Hindsight store error (HTTP 503)',
        }),
        { status: 200 }
      );
    });

    render(<App />);

    await waitFor(() => {
      expect(screen.getByText('Call Archive Unreachable')).toBeInTheDocument();
      expect(screen.getByText('Store Unreachable')).toBeInTheDocument();
    });
  });

  it('performs recall search and opens matching call detail', async () => {
    const searchHitCall = {
      call_id: 'voice-twilio-call-024',
      when: '2026-08-17T15:00:00Z',
      direction: 'inbound',
      who: '+61491570110',
      agent: 'support',
      outlet: 'phone',
      summary: 'Coffee machine issue.',
      transcript: 'Customer: Coffee machine is leaking.',
    };

    vi.spyOn(globalThis, 'fetch').mockImplementation(async (url) => {
      const urlStr = url.toString();
      if (urlStr.includes('/api/calls/voice-twilio-call-024')) {
        return new Response(
          JSON.stringify({
            call: searchHitCall,
            unreachable: false,
            error: null,
          }),
          { status: 200 }
        );
      }
      if (urlStr.includes('q=coffee')) {
        return new Response(
          JSON.stringify({
            calls: [searchHitCall],
            total: 1,
            page: 1,
            page_size: 20,
            has_more: false,
            unreachable: false,
            error: null,
          }),
          { status: 200 }
        );
      }
      return new Response(
        JSON.stringify({
          calls: [],
          total: 0,
          page: 1,
          page_size: 20,
          has_more: false,
          unreachable: false,
          error: null,
        }),
        { status: 200 }
      );
    });

    render(<App />);

    const searchInput = screen.getByPlaceholderText('Search what was said');
    fireEvent.change(searchInput, { target: { value: 'coffee' } });
    fireEvent.submit(searchInput);

    await waitForRow('voice-twilio-call-024');
    expect(window.location.search).toBe('?q=coffee');

    // Click matching call
    await openCall('voice-twilio-call-024');
    verbatim();
    expect(screen.getByText('Customer: Coffee machine is leaking.')).toBeInTheDocument();
  });
});

/* s5 (ticket 05): the Calls screen shows what was recorded, says so when nothing
 * was, and can filter by Agent and Outlet. */
describe('Calls screen metadata and filters', () => {
  const recorded = {
    call_id: 'voice-twilio-out-1',
    when: '2026-08-18T09:00:00+00:00',
    when_precision: 'datetime',
    direction: 'outbound',
    who: '+61400000000',
    agent: 'hermes-main',
    outlet: 'phone',
    mission: 'Book a table for 7pm.',
    outcome: 'ok',
    duration_s: 63.4,
    summary: null,
    transcript: 'Them: hi\nAI: hello',
  };

  /* Exactly what a call retained before this ticket comes back as. */
  const legacy = {
    call_id: 'voice-twilio-legacy',
    when: '2026-08-17',
    when_precision: 'date',
    direction: 'inbound',
    who: 'Not retained',
    agent: '',
    outlet: '',
    mission: '',
    outcome: null,
    duration_s: null,
    summary: null,
    transcript: 'Them: hi',
  };

  const body = (calls: unknown[], extra: Record<string, unknown> = {}) => ({
    calls,
    total: calls.length,
    page: 1,
    page_size: 20,
    has_more: false,
    unreachable: false,
    partial: false,
    warning: null,
    error: null,
    agents: ['hermes-main', '(not retained)'],
    outlets: ['phone', '(not retained)'],
    filters: { agent: null, outlet: null },
    ...extra,
  });

  let requested: string[] = [];

  const serve = (pick: (url: string) => unknown[]) => {
    requested = [];
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (url) => {
      const urlStr = url.toString();
      requested.push(urlStr);
      if (urlStr.includes('/api/calls/')) {
        const id = urlStr.split('/api/calls/')[1];
        const call = [recorded, legacy].find((c) => c.call_id === id) || null;
        return new Response(
          JSON.stringify({ call, unreachable: false, partial: false, error: null }),
          { status: 200 }
        );
      }
      return new Response(JSON.stringify(body(pick(urlStr))), { status: 200 });
    });
  };

  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('shows the agent, outlet, mission, outcome and duration of a recorded call', async () => {
    serve(() => [recorded]);
    render(<App />);

    await waitForRow('voice-twilio-out-1');
    expect(fieldOf('voice-twilio-out-1', 'agent')).toHaveTextContent('hermes-main');
    expect(fieldOf('voice-twilio-out-1', 'outlet')).toHaveTextContent('Phone');
    expect(fieldOf('voice-twilio-out-1', 'outcome')).toHaveTextContent('ok');
    expect(fieldOf('voice-twilio-out-1', 'duration')).toHaveTextContent('1m 03s');
    // no summary was written, so the row says so and shows the Mission beside it
    expect(rowOf('voice-twilio-out-1')).toHaveTextContent('Mission: Book a table for 7pm.');

    await openCall('voice-twilio-out-1');
    expect(screen.getByTestId('mission-detail')).toHaveTextContent('Book a table for 7pm.');
  });

  it('renders every unrecorded field as not retained, never as zero or a dash', async () => {
    serve(() => [legacy]);
    render(<App />);

    await waitForRow('voice-twilio-legacy');
    // Assert the FIELDS, not the row: "Not retained" appearing somewhere in a row
    // stays true while one field lies.
    const text = (field: string) => fieldOf('voice-twilio-legacy', field).textContent;
    expect(text('agent')).toBe('Not retained');
    expect(text('outlet')).toBe('Not retained');
    expect(text('outcome')).toBe('Not retained');
    expect(text('duration')).toBe('Not retained');
    expect(text('duration')).not.toMatch(/0/);
    // an inbound call has no Mission, so the row offers none
    expect(rowOf('voice-twilio-legacy')).not.toHaveTextContent('Mission');
    expect(rowOf('voice-twilio-legacy')).not.toHaveTextContent('—');
  });

  it('says an inbound call has no mission rather than that one went missing', async () => {
    serve(() => [legacy]);
    render(<App />);

    await waitForRow('voice-twilio-legacy');
    await openCall('voice-twilio-legacy');
    expect(screen.getByText('Not applicable (inbound call)')).toBeInTheDocument();
    // and the fields that genuinely ARE missing still say so
    const meta = screen.getByTestId('call-meta');
    for (const label of ['Agent', 'Outlet', 'Duration', 'How the call ended']) {
      const row = Array.from(meta.querySelectorAll('.meta-row')).find(
        (r) => r.querySelector('dt')?.textContent === label
      ) as HTMLElement;
      expect(row.querySelector('dd')).toHaveTextContent('Not retained');
    }
  });

  it('offers the filters the API found and sends the chosen one to the API', async () => {
    serve((url) =>
      url.includes('agent=hermes-main') ? [recorded] : [recorded, legacy]
    );
    render(<App />);

    await waitForRow('voice-twilio-legacy');

    fireEvent.click(screen.getByTestId('filter-agent'));
    const options = screen.getAllByRole('option').map((o) => o.getAttribute('data-testid'));
    expect(options).toEqual(['filter-agent-option-hermes-main', 'filter-agent-option-(not retained)']);

    fireEvent.click(screen.getByTestId('filter-agent-option-hermes-main'));

    await waitFor(() => {
      expect(rowOf('voice-twilio-legacy')).toBeNull();
    });
    expect(requested.some((u) => u.includes('agent=hermes-main'))).toBe(true);
    expect(rowOf('voice-twilio-out-1')).not.toBeNull();
    // the filter is in the URL, so the filtered view is linkable
    expect(window.location.search).toBe('?agent=hermes-main');
    expect(screen.getByTestId('filter-agent-set')).toHaveTextContent('hermes-main');
  });

  it('can filter to the calls whose field was never retained', async () => {
    serve((url) =>
      url.includes('outlet=%28not+retained%29') || url.includes('outlet=(not retained)')
        ? [legacy]
        : [recorded, legacy]
    );
    render(<App />);

    await waitForRow('voice-twilio-out-1');

    fireEvent.click(screen.getByTestId('filter-outlet'));
    fireEvent.click(screen.getByTestId('filter-outlet-option-(not retained)'));

    await waitFor(() => {
      expect(rowOf('voice-twilio-out-1')).toBeNull();
    });
    expect(rowOf('voice-twilio-legacy')).not.toBeNull();
    expect(screen.getByTestId('filter-outlet-set')).toHaveTextContent('Not retained');
  });

  it('does not claim the history is empty when a filter matched nothing', async () => {
    serve((url) => (url.includes('agent=') ? [] : [recorded, legacy]));
    render(<App />);

    await waitForRow('voice-twilio-out-1');

    fireEvent.click(screen.getByTestId('filter-agent'));
    fireEvent.click(screen.getByTestId('filter-agent-option-hermes-main'));

    await waitFor(() => {
      expect(screen.getByText('No calls match these filters')).toBeInTheDocument();
    });
    expect(screen.queryByText('No calls recorded yet')).not.toBeInTheDocument();
  });

  /* Ticket 15: the deleted legacy screen was the only place this was asserted.
   * It could be opened on a call id directly, so it had a test for "the bank
   * holding this call is down -- say why". This screen opens a Call at its own
   * URL too, and this is what it does with the answer the API gives for exactly
   * that case: `call: null`, `partial: true`, and an error saying the call MAY exist.
   * Rendering nothing there reads as "there is no such call", which is the claim
   * a bank we could not read is not entitled to make. */
  it('relays why a call could not be read instead of silently showing nothing', async () => {
    const downBank =
      "bank 'hermes' could not be read (503) - this call may exist in it";
    requested = [];
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (url) => {
      const urlStr = url.toString();
      requested.push(urlStr);
      if (urlStr.includes('/api/calls/')) {
        return new Response(
          JSON.stringify({
            call: null,
            unreachable: false,
            partial: true,
            error: downBank,
          }),
          { status: 200 }
        );
      }
      return new Response(JSON.stringify(body([recorded])), { status: 200 });
    });
    render(<App />);

    await waitForRow('voice-twilio-out-1');
    await openCall('voice-twilio-out-1');

    await waitFor(() => {
      expect(screen.getByText(downBank)).toBeInTheDocument();
    });
    // ...and it does not present the absence as the call's own content
    expect(screen.queryByTestId('call-tab-transcript')).not.toBeInTheDocument();
    expect(screen.queryByText(/not found/i)).not.toBeInTheDocument();
  });
});

/* Ticket 06: the per-call summary, and - the part that actually needs testing - the
 * three DIFFERENT absences behind an empty Summary cell.
 *
 * The producer (services/voicecore/summary.py) never stores an empty or placeholder
 * summary, so a cell with no prose in it always means one of:
 *
 *   nothing_to_summarise  the call held no conversation to describe
 *   unavailable           the Agent was asked and could not answer
 *   (no state)            nobody was asked
 *
 * A screen that renders all three the same way tells the reader something it does not
 * know. There is deliberately no fourth "still coming" state: the summary is settled
 * before the call's document exists, so nothing on this screen can be waiting for one -
 * which is asserted here, because a spinner would wait forever. */
describe('Calls screen summary', () => {
  const base = {
    when: '2026-08-18T09:00:00+00:00',
    when_precision: 'datetime',
    direction: 'outbound',
    who: '+61400000000',
    agent: 'hermes-main',
    outlet: 'phone',
    mission: 'Book a table for 7pm.',
    outcome: 'ok',
    duration_s: 63.4,
    transcript: 'Them: hi\nAI: hello',
  };

  const written = {
    ...base,
    call_id: 'sum-written',
    summary: 'Chased the Tuesday delivery; it shipped Monday and lands tomorrow.',
    summary_state: 'written',
  };
  const nothing = {
    ...base,
    call_id: 'sum-nothing',
    summary: null,
    summary_state: 'nothing_to_summarise',
  };
  const unavailable = {
    ...base,
    call_id: 'sum-unavailable',
    summary: null,
    summary_state: 'unavailable',
  };
  const neverAsked = {
    ...base,
    call_id: 'sum-never-asked',
    summary: null,
    summary_state: null,
  };

  const ALL = [written, nothing, unavailable, neverAsked];

  const serveSummaries = () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (url) => {
      const urlStr = url.toString();
      if (urlStr.includes('/api/calls/')) {
        const id = urlStr.split('/api/calls/')[1].split('?')[0];
        const call = ALL.find((c) => c.call_id === id) || null;
        return new Response(
          JSON.stringify({ call, unreachable: false, partial: false, error: null }),
          { status: 200 }
        );
      }
      return new Response(
        JSON.stringify({
          calls: ALL,
          total: ALL.length,
          page: 1,
          page_size: 20,
          has_more: false,
          unreachable: false,
          partial: false,
          warning: null,
          error: null,
          agents: [],
          outlets: [],
          filters: { agent: null, outlet: null },
        }),
        { status: 200 }
      );
    });
  };

  const summaryCellOf = (callId: string) =>
    rowOf(callId)?.querySelector('[data-testid="summary-cell"]') as HTMLElement;

  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('shows the summary the Agent wrote', async () => {
    serveSummaries();
    render(<App />);
    await waitForRow('sum-written');
    const cell = summaryCellOf('sum-written');
    expect(cell).toHaveTextContent('Chased the Tuesday delivery');
    /* A long summary is cut to one line by the stylesheet (an ellipsis), but the
       whole text is in the element and in its title, so the list never quietly
       holds half a sentence as if it were the summary. */
    expect(cell.textContent).toBe('Chased the Tuesday delivery; it shipped Monday and lands tomorrow.');
    expect(cell.getAttribute('title')).toBe(
      'Chased the Tuesday delivery; it shipped Monday and lands tomorrow.'
    );
  });

  it('tells the three absences apart instead of showing one blank cell', async () => {
    serveSummaries();
    render(<App />);
    await waitForRow('sum-nothing');

    /* Assert the CELL, not the row: "Not retained" somewhere in a row stays true
       while the Summary cell itself says the wrong thing. */
    const wordings = ['sum-nothing', 'sum-unavailable', 'sum-never-asked'].map((id) => {
      const cell = summaryCellOf(id);
      expect(cell.textContent?.trim()).toBeTruthy();
      return cell.textContent?.trim();
    });

    expect(new Set(wordings).size).toBe(3);
    /* ...and none of them reads like a summary of the call. */
    for (const wording of wordings) {
      expect(wording).not.toContain('delivery');
    }
  });

  it('never says a summary is on its way, because none ever is', async () => {
    serveSummaries();
    render(<App />);
    await waitForRow('sum-unavailable');
    for (const id of ['sum-nothing', 'sum-unavailable', 'sum-never-asked']) {
      const wording = (summaryCellOf(id).textContent || '').toLowerCase();
      expect(wording).not.toContain('pending');
      expect(wording).not.toContain('loading');
      expect(wording).not.toMatch(/coming|in progress|being written|\.\.\./);
    }
  });

  it('says on the detail view which absence this call has', async () => {
    serveSummaries();
    render(<App />);
    await waitForRow('sum-nothing');

    await openCall('sum-nothing');
    await waitFor(() => {
      expect(screen.getByTestId('summary-detail-absent')).toBeInTheDocument();
    });
    const absent = screen.getByTestId('summary-detail-absent');
    expect(absent).toHaveTextContent('No conversation to summarise');
    expect(screen.queryByTestId('summary-detail')).not.toBeInTheDocument();
  });

  it('shows the written summary on the detail view, not an absence', async () => {
    serveSummaries();
    render(<App />);
    await waitForRow('sum-written');

    await openCall('sum-written');
    await waitFor(() => {
      expect(screen.getByTestId('summary-detail')).toBeInTheDocument();
    });
    expect(screen.getByTestId('summary-detail')).toHaveTextContent(
      'Chased the Tuesday delivery'
    );
    expect(screen.queryByTestId('summary-detail-absent')).not.toBeInTheDocument();
  });
});
