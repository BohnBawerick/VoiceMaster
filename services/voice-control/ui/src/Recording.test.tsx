import '@testing-library/jest-dom/vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { App } from './App';

/* Ticket 07 on the screen. Three states, and only one of them is a player:
 * a recording that exists, a capture that failed, and a call that simply has none. */

const call = {
  call_id: 'voice-twilio-with-audio',
  when: '2026-08-18T09:05:00Z',
  direction: 'inbound',
  who: 'Not retained',
  outlet: 'voice_twilio',
  outcome: null,
  summary: null,
  transcript: 'Them: hello\nAI: hello back',
};

const listBody = {
  calls: [call],
  total: 1,
  page: 1,
  page_size: 20,
  has_more: false,
  unreachable: false,
  partial: false,
  warning: null,
  error: null,
};

function mockApi(recording: unknown) {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (url) => {
    if (url.toString().includes(`/api/calls/${call.call_id}`)) {
      return new Response(
        JSON.stringify({ call, unreachable: false, partial: false, error: null, recording }),
        { status: 200 }
      );
    }
    return new Response(JSON.stringify(listBody), { status: 200 });
  });
}

/* jsdom has no Web Audio, so these tests see the player the waveform falls
 * back to: the plain <audio> element with its own controls. The waveform itself
 * is exercised in a real browser (tests/test_calls_browser.py). */
async function openTheCall() {
  window.history.replaceState({}, '', '/');
  render(<App />);
  const row = () => document.querySelector(`[data-call-id="${call.call_id}"]`) as HTMLElement;
  await waitFor(() => expect(row()).not.toBeNull());
  fireEvent.click(row());
  await waitFor(() => expect(screen.getByTestId('call-tab-overview')).toBeInTheDocument());
}

describe('Call recording playback', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('plays the recording inline, from the range-serving endpoint', async () => {
    mockApi({
      available: true,
      status: 'ok',
      url: `/api/calls/${call.call_id}/recording`,
      duration_s: 92.5,
      size_bytes: 372000,
      dropped_frames: 0,
    });
    await openTheCall();

    const player = document.querySelector('audio.recording-player');
    expect(player).toBeInTheDocument();
    expect(player).toHaveAttribute('src', `/api/calls/${call.call_id}/recording`);
    expect(player).toHaveAttribute('controls');
    // preload="metadata" is what makes the scrub bar know the duration before play.
    expect(player).toHaveAttribute('preload', 'metadata');
    expect(screen.getByText('1:33')).toBeInTheDocument();
    expect(
      screen.getByText(/Caller on the left channel, Agent on the right/i)
    ).toBeInTheDocument();
  });

  it('shows NO player for a call that has no recording', async () => {
    mockApi({ available: false, status: null, error: null, url: null });
    await openTheCall();

    expect(document.querySelector('audio')).toBeNull();
    expect(screen.queryByText(/Recording/)).toBeNull();
  });

  it('shows no player when the API says nothing about recordings at all', async () => {
    /* An older dashboard response, or a build where the field is absent. The screen
     * must not render an empty <audio> that spins forever. */
    mockApi(undefined);
    await openTheCall();
    expect(document.querySelector('audio')).toBeNull();
  });

  it('says so — without a player — when capture failed', async () => {
    mockApi({
      available: false,
      status: 'failed',
      error: 'OSError: No space left on device',
      url: null,
    });
    await openTheCall();

    expect(document.querySelector('audio')).toBeNull();
    expect(screen.getByText(/Capturing this call/i)).toBeInTheDocument();
    expect(screen.getByText(/No space left on device/)).toBeInTheDocument();
  });

  it('warns when the recording has gaps the call did not', async () => {
    mockApi({
      available: true,
      status: 'ok',
      url: `/api/calls/${call.call_id}/recording`,
      duration_s: 12,
      dropped_frames: 41,
    });
    await openTheCall();

    expect(document.querySelector('audio.recording-player')).toBeInTheDocument();
    expect(screen.getByText(/41 audio frames could not be written/i)).toBeInTheDocument();
  });
});
