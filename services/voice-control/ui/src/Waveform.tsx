/* The recording as two lanes on one time axis (C4).
 *
 * The bridges write one stereo Opus file per call: the caller on the LEFT
 * channel, the Agent on the RIGHT (ticket 07, voicecore/recording.py). Drawing
 * the channels apart shows at a glance who talked when, where the silences are
 * and where one voice ran over the other.
 *
 * Playback is an ordinary <audio> element, which scrubs through the API's range
 * requests. The lanes are drawn from the same file decoded once with Web Audio.
 * When decoding is not possible (no Web Audio, or a browser that cannot decode
 * the container), the plain player with its own controls is what remains:
 * the recording is still playable, only the picture is missing. */
import { useCallback, useEffect, useRef, useState } from 'react';
import type { KeyboardEvent, PointerEvent } from 'react';
import { Pause, Play } from 'lucide-react';
import { formatClockTime } from './format';

const BUCKETS = 480;
const SPEEDS = [1, 1.5, 2];

type Peaks = { left: Float32Array; right: Float32Array; duration: number; stereo: boolean };

function peaksOf(buffer: AudioBuffer): Peaks {
  const left = buffer.getChannelData(0);
  const right = buffer.numberOfChannels > 1 ? buffer.getChannelData(1) : left;
  const size = Math.max(1, Math.floor(left.length / BUCKETS));
  const out = (data: Float32Array) => {
    const peaks = new Float32Array(BUCKETS);
    for (let b = 0; b < BUCKETS; b++) {
      let max = 0;
      const start = b * size;
      const end = Math.min(data.length, start + size);
      for (let i = start; i < end; i++) {
        const v = Math.abs(data[i]);
        if (v > max) max = v;
      }
      peaks[b] = max;
    }
    let top = 0;
    for (const v of peaks) top = Math.max(top, v);
    if (top > 0) for (let b = 0; b < BUCKETS; b++) peaks[b] = peaks[b] / top;
    return peaks;
  };
  return { left: out(left), right: out(right), duration: buffer.duration, stereo: buffer.numberOfChannels > 1 };
}

function cssVar(el: Element, name: string, fallback: string): string {
  return getComputedStyle(el).getPropertyValue(name).trim() || fallback;
}

function draw(canvas: HTMLCanvasElement, peaks: Peaks, progress: number) {
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);

  const caller = cssVar(canvas, '--caller', '#e8893b');
  const agent = cssVar(canvas, '--accent', '#3cc4a5');
  const rest = cssVar(canvas, '--wave-rest', 'rgba(255,255,255,0.18)');
  const lane = height / 2;
  const step = width / BUCKETS;
  const bar = Math.max(1, step * 0.62);
  const played = progress * width;

  const lanes: [Float32Array, string, number][] = [
    [peaks.left, caller, lane / 2],
    [peaks.right, agent, lane + lane / 2],
  ];
  for (const [data, colour, mid] of lanes) {
    ctx.fillStyle = rest;
    ctx.fillRect(0, mid - 0.5, width, 1);
    for (let b = 0; b < BUCKETS; b++) {
      const x = b * step;
      const h = Math.max(1, data[b] * (lane * 0.84));
      ctx.globalAlpha = x <= played ? 1 : 0.42;
      ctx.fillStyle = colour;
      ctx.fillRect(x, mid - h / 2, bar, h);
    }
    ctx.globalAlpha = 1;
  }
}

function ticks(duration: number): number[] {
  if (!isFinite(duration) || duration <= 0) return [];
  const steps = [5, 10, 15, 30, 60, 120, 300, 600];
  const step = steps.find((s) => duration / s <= 8) ?? 1200;
  const out: number[] = [];
  for (let t = step; t < duration; t += step) out.push(t);
  return out;
}

function audioContextClass(): typeof AudioContext | undefined {
  const w = window as unknown as { AudioContext?: typeof AudioContext; webkitAudioContext?: typeof AudioContext };
  return w.AudioContext || w.webkitAudioContext;
}

export function Waveform({ url, durationHint }: { url: string; durationHint: number | null }) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [peaks, setPeaks] = useState<Peaks | null>(null);
  const [failed, setFailed] = useState(() => !audioContextClass() || !url);
  const [playing, setPlaying] = useState(false);
  const [time, setTime] = useState(0);
  const [duration, setDuration] = useState<number>(durationHint ?? 0);
  const [speed, setSpeed] = useState(1);

  useEffect(() => {
    let alive = true;
    setPeaks(null);
    const Ctx = audioContextClass();
    setFailed(!Ctx || !url);
    if (!Ctx || !url) {
      setFailed(true);
      return;
    }
    const context = new Ctx();
    fetch(url)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.arrayBuffer();
      })
      .then((data) => context.decodeAudioData(data))
      .then((buffer) => {
        if (!alive) return;
        const next = peaksOf(buffer);
        setPeaks(next);
        setDuration((d) => d || next.duration);
      })
      .catch(() => {
        if (alive) setFailed(true);
      })
      .finally(() => {
        context.close().catch(() => undefined);
      });
    return () => {
      alive = false;
    };
  }, [url]);

  const progress = duration > 0 ? Math.min(1, time / duration) : 0;

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !peaks) return;
    draw(canvas, peaks, progress);
    const onResize = () => draw(canvas, peaks, progress);
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, [peaks, progress]);

  useEffect(() => {
    if (audioRef.current) audioRef.current.playbackRate = speed;
  }, [speed]);

  const toggle = useCallback(() => {
    const audio = audioRef.current;
    if (!audio) return;
    if (audio.paused) audio.play().catch(() => undefined);
    else audio.pause();
  }, []);

  const seekTo = (fraction: number) => {
    const audio = audioRef.current;
    if (!audio || !duration) return;
    const t = Math.max(0, Math.min(1, fraction)) * duration;
    audio.currentTime = t;
    setTime(t);
  };

  const onPointer = (event: PointerEvent<HTMLDivElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    seekTo((event.clientX - rect.left) / rect.width);
  };

  const onKey = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!duration) return;
    if (event.key === 'ArrowRight') seekTo((time + 5) / duration);
    else if (event.key === 'ArrowLeft') seekTo((time - 5) / duration);
    else if (event.key === ' ' || event.key === 'Enter') toggle();
    else return;
    event.preventDefault();
    event.stopPropagation();
  };

  const audio = (
    <audio
      ref={audioRef}
      className="recording-player"
      controls={failed}
      preload="metadata"
      src={url || undefined}
      onPlay={() => setPlaying(true)}
      onPause={() => setPlaying(false)}
      onEnded={() => setPlaying(false)}
      onTimeUpdate={(e) => setTime(e.currentTarget.currentTime)}
      onLoadedMetadata={(e) => {
        const d = e.currentTarget.duration;
        if (isFinite(d) && d > 0) setDuration(d);
      }}
    >
      Your browser cannot play this recording.
    </audio>
  );

  if (failed) {
    return (
      <div className="waveform waveform-fallback" data-testid="waveform-fallback">
        {audio}
        <div className="recording-meta">
          <span>Caller on the left channel, Agent on the right.</span>
          {duration > 0 && <span className="mono-num">{formatClockTime(Math.round(duration))}</span>}
        </div>
      </div>
    );
  }

  return (
    <div className="waveform" data-testid="waveform">
      {audio}
      <div className="waveform-legend">
        <span className="legend-item legend-caller">
          <span className="legend-swatch" /> Caller <span className="legend-note">left channel</span>
        </span>
        <span className="legend-item legend-agent">
          <span className="legend-swatch" /> Agent <span className="legend-note">right channel</span>
        </span>
        {peaks && !peaks.stereo && <span className="legend-note">This file is mono, so both lanes are the same.</span>}
      </div>
      <div
        className="waveform-lanes"
        role="slider"
        tabIndex={0}
        aria-label="Seek in the recording"
        aria-valuemin={0}
        aria-valuemax={Math.round(duration)}
        aria-valuenow={Math.round(time)}
        aria-valuetext={`${formatClockTime(time)} of ${formatClockTime(duration)}`}
        onPointerDown={onPointer}
        onKeyDown={onKey}
      >
        {peaks ? <canvas ref={canvasRef} className="waveform-canvas" /> : <div className="waveform-loading" />}
        <div className="waveform-playhead" style={{ left: `${progress * 100}%` }} />
      </div>
      <div className="waveform-axis" aria-hidden="true">
        {ticks(duration).map((t) => (
          <span key={t} className="waveform-tick mono-num" style={{ left: `${(t / duration) * 100}%` }}>
            {formatClockTime(t)}
          </span>
        ))}
      </div>
      <div className="waveform-controls">
        <button
          type="button"
          className="btn btn-primary btn-icon play-btn"
          onClick={toggle}
          aria-label={playing ? 'Pause' : 'Play'}
          data-testid="recording-play"
        >
          {playing ? <Pause size={16} /> : <Play size={16} />}
        </button>
        <span className="waveform-time mono-num">
          {formatClockTime(time)} / {formatClockTime(duration)}
        </span>
        <div className="speed-toggle" role="group" aria-label="Playback speed">
          {SPEEDS.map((s) => (
            <button
              key={s}
              type="button"
              className={'speed-btn' + (speed === s ? ' active' : '')}
              aria-pressed={speed === s}
              onClick={() => setSpeed(s)}
            >
              {s}x
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
