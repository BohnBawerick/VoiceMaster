"""Playwright driver for Mode V — headed Chromium (under Xvfb) as the Nextcloud Talk client.

This module owns Chromium lifecycle, ai-agent login-session persistence, and call
join/leave. It does not touch OpenAI or PulseAudio directly: Chromium's default audio
input/output devices ARE the PulseAudio virtual sink/source set up by entrypoint.sh, so
once the browser is streaming in a call, audio capture/injection is implicit.

Selectors + flow below were validated live against Nextcloud 34.0.0 / Talk (spreed) 24.0.1
on 2026-07-01. Five things had to be right, and none were in the first cut:
  1. This instance has NO URL rewriting — every route needs the `/index.php/` prefix
     (bare `/login`, `/apps/spreed/`, `/call/{token}` all return 404).
  2. Nextcloud rejects app-passwords at the web login FORM; the browser must use the
     real account password (`cfg.login_password`). App-passwords are OCS/DAV-only.
  3. The auth probe must check `OC.currentUser`, not the URL — a 404 page's URL contains
     no `/login`, which fooled the old `"/login" not in url` heuristic into "authenticated".
  4. Talk shows an "unsupported browser" toast for Playwright's default UA and that toast
     overlay intercepts the join click — so we spoof a normal Chrome User-Agent.
  5. Joining is TWO steps behind a media-settings dialog, and a residual overlay still
     intercepts native clicks, so we drive the buttons via JS `.click()`.
"""
import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

from playwright.async_api import (Browser, BrowserContext, Page, Playwright,
                                  TimeoutError as PlaywrightTimeoutError, async_playwright)

from config import Config

logger = logging.getLogger("mode-v.browser")

# Container default, overridable so the bridge can boot outside a container (s14a-2a: the
# readiness endpoint has to be drivable as a REAL process, and a hardcoded /app made that
# impossible anywhere but the NAS). Same env-driven pattern as config.py's HERMES_CONFIG_DIR;
# unset — i.e. in the image — the path is byte-identical to what it always was.
STATE_DIR = Path(os.environ.get("VOICE_STATE_DIR", "/app/state"))
STATE_FILE = STATE_DIR / "ai-agent-session.json"

# Talk flags Playwright's default UA ("HeadlessChrome") as unsupported and pops a toast
# that overlays — and intercepts clicks on — the join button. A plain Chrome UA clears it.
CHROME_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
             "Chrome/131.0.0.0 Safari/537.36")

# This Nextcloud has pretty-URLs OFF: every app route must go through the front controller.
IDX = "/index.php"

# Audio-only capture (Chromium runs headed under Xvfb). `--use-fake-ui-for-media-stream` auto-accepts the
# getUserMedia permission prompt WITHOUT synthesizing devices, so Chromium uses the real
# PulseAudio default source/sink (talk_mic / talk_speaker) that entrypoint.sh created.
# We deliberately do NOT pass `--use-fake-device-for-media-stream` (it would replace the
# Pulse mic with a synthetic test tone, breaking the OpenAI audio bridge), nor `--mute-audio`
# (output must reach the talk_speaker sink so OpenAI's replies are audible to the caller).
LAUNCH_ARGS = [
    "--use-fake-ui-for-media-stream",
    "--autoplay-policy=no-user-gesture-required",
]

# Injected before any page script runs. Wraps three constructors so we can, post-join,
# (a) query getStats() on every RTCPeerConnection Talk makes, (b) reach every `new Audio()`
# element Talk uses to play remote participants (they're NOT in the DOM), and (c) reach any
# Web Audio AudioContext. Talk never exposes these globally.
MEDIA_HOOK_JS = """
(() => {
  const OrigPC = window.RTCPeerConnection;
  if (OrigPC) {
    window.__pcs = [];
    const W = function(...a){ const pc = new OrigPC(...a); window.__pcs.push(pc); return pc; };
    W.prototype = OrigPC.prototype; window.RTCPeerConnection = W;
  }
  window.__audios = [];
  const OrigAudio = window.Audio;
  if (OrigAudio) {
    const W = function(...a){ const el = new OrigAudio(...a); window.__audios.push(el); return el; };
    W.prototype = OrigAudio.prototype; window.Audio = W;
  }
  window.__acs = [];
  for (const key of ['AudioContext','webkitAudioContext']) {
    const Orig = window[key];
    if (Orig) {
      const W = function(...a){ const c = new Orig(...a); window.__acs.push(c); return c; };
      W.prototype = Orig.prototype; window[key] = W;
    }
  }
})();
"""

# Inbound-audio belt-and-suspenders + observability. The ROOT fix for inbound audio is
# running Chromium HEADED under Xvfb (see start()/entrypoint.sh) — that makes Talk's native
# auto-playout render the remote WebRTC track to the `talk_speaker` Pulse sink → parec →
# OpenAI. On top of that, this also explicitly attaches every remote RTCRtpReceiver audio
# track to its own <audio> element (srcObject) and plays it (idempotent per track id) and
# resumes any suspended AudioContext — harmless reinforcement of the playout path. Its main
# job now is REPORTING: it returns inbound getStats so we can confirm the caller's voice is
# actually reaching Chromium (rising totalAudioEnergy/audioLevel) during a live call.
FIX_AND_REPORT_JS = """
async () => {
  window.__attached = window.__attached || {};
  window.__remoteEls = window.__remoteEls || [];
  let attached = 0;
  for (const pc of (window.__pcs||[])) {
    let receivers = [];
    try { receivers = pc.getReceivers(); } catch (e) {}
    for (const r of receivers) {
      const t = r.track;
      if (t && t.kind === 'audio' && !window.__attached[t.id]) {
        window.__attached[t.id] = true;
        try {
          const el = new Audio();
          el.srcObject = new MediaStream([t]);
          el.autoplay = true; el.muted = false; el.volume = 1;
          const p = el.play(); if (p && p.catch) p.catch(()=>{});
          window.__remoteEls.push(el);
          attached++;
        } catch (e) {}
      }
    }
  }
  // Keep our explicit remote-audio elements alive + audible.
  for (const el of window.__remoteEls) {
    try { el.muted = false; el.volume = 1; if (el.paused) el.play().catch(()=>{}); } catch (e) {}
  }
  for (const c of (window.__acs||[])) { try { if (c.state === 'suspended') c.resume(); } catch (e) {} }

  const inbound = [];
  const path = [];
  for (const pc of (window.__pcs||[])) {
    let s; try { s = await pc.getStats(); } catch (e) { continue; }
    const byId = new Map();
    s.forEach(r => byId.set(r.id, r));
    s.forEach(r => {
      if (r.type === 'inbound-rtp' && r.kind === 'audio')
        inbound.push({packets:r.packetsReceived||0, level:r.audioLevel, energy:r.totalAudioEnergy});
    });
    // Which ICE candidate pair actually carries the media — this is what tells us tailnet vs coturn.
    let sel = null;
    s.forEach(r => { if (r.type === 'transport' && r.selectedCandidatePairId) sel = byId.get(r.selectedCandidatePairId) || sel; });
    if (!sel) s.forEach(r => { if (r.type === 'candidate-pair' && r.state === 'succeeded' && (r.nominated || !sel)) sel = r; });
    if (sel) {
      const loc = byId.get(sel.localCandidateId) || {};
      const rem = byId.get(sel.remoteCandidateId) || {};
      // local.type 'relay' (or a relay protocol) => media is going through coturn/TURN.
      // 'host' with a 100.x addr => tailnet P2P; 'srflx'/'prflx' with a public IP => direct-over-internet.
      path.push({
        state: sel.state,
        local:  {type: loc.candidateType, addr: loc.address || loc.ip, proto: loc.protocol, relay: loc.relayProtocol || null},
        remote: {type: rem.candidateType, addr: rem.address || rem.ip, proto: rem.protocol},
      });
    }
  }
  return {attached_now: attached, remote_els: window.__remoteEls.length, inbound, path};
}
"""

# Named timeouts (ms).
LOGIN_TIMEOUT_MS = 20000          # wait for OC.currentUser after submitting the login form
START_BTN_TIMEOUT_MS = 15000      # wait for the top-bar "Start call" control to appear (outbound)
JOIN_BTN_TIMEOUT_MS = 15000       # wait for the top-bar "Join call" control to appear
MEDIA_SETTINGS_TIMEOUT_MS = 8000  # wait for the pre-join media-settings dialog
JOIN_CONFIRM_TIMEOUT_MS = 15000   # wait for the in-call "Leave call" control (join confirmed)
LEAVE_CLICK_TIMEOUT_MS = 5000     # click the hang-up control before giving up


class TalkBrowser:
    """Drives a persistent headed Chromium session (under Xvfb) logged in as the Talk voice user."""

    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._started = False

    async def start(self) -> None:
        """Launch headed Chromium (under Xvfb) and restore a saved session if one exists.

        Only a fully-successful run (through _ensure_session) marks the object as
        started; any failure tears down whatever was allocated and re-raises, so the
        caller can safely retry start() on a clean object.
        """
        if self._started:
            logger.warning("start() called on an already-started TalkBrowser — ignoring")
            return

        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)

            self._playwright = await async_playwright().start()
            # HEADED (headless=False) under the Xvfb virtual display that entrypoint.sh starts.
            # Headless Chromium decodes the remote WebRTC audio track but never renders the live
            # MediaStream to a real output device, so talk_speaker stays silent and the caller is
            # never heard. Headed+Xvfb is the proven path (Nextcloud's own recording backend does
            # the same). DISPLAY is inherited from entrypoint.sh.
            self._browser = await self._playwright.chromium.launch(headless=False, args=LAUNCH_ARGS)

            storage_state = str(STATE_FILE) if STATE_FILE.exists() else None
            if storage_state:
                logger.info("Restoring saved session from %s", STATE_FILE)

            self._context = await self._browser.new_context(
                storage_state=storage_state,
                permissions=["microphone", "camera"],
                user_agent=CHROME_UA,
            )
            await self._context.add_init_script(MEDIA_HOOK_JS)
            self._page = await self._context.new_page()

            await self._ensure_session()
        except Exception:
            logger.exception("start() failed — tearing down for a clean retry")
            await self.stop()
            raise

        self._started = True

    async def _js_click(self, selector: str) -> bool:
        """Click the first element matching `selector` via JS (`el.click()`).

        Native/force clicks are unreliable here: Talk overlays (toasts, tooltips)
        intercept pointer events even when the button is visible/enabled. A JS click
        fires the handler directly, bypassing hit-testing. Returns True if found.
        """
        if self._page is None:
            raise RuntimeError("start() must be called first")
        return await self._page.evaluate(
            "(sel) => { const el = document.querySelector(sel);"
            " if (el) { el.click(); return true; } return false; }", selector)

    async def _js_click_aria(self, aria_substr: str) -> bool:
        """JS-click the first button whose aria-label contains `aria_substr` (case-insensitive)."""
        if self._page is None:
            raise RuntimeError("start() must be called first")
        return await self._page.evaluate(
            "(s) => { const b = [...document.querySelectorAll('button')]"
            ".find(e => (e.getAttribute('aria-label')||'').toLowerCase().includes(s.toLowerCase()));"
            " if (b) { b.click(); return true; } return false; }", aria_substr)

    async def _ensure_session(self) -> None:
        """Log in as cfg.talk_user unless the restored session is already authenticated."""
        if self._page is None:
            raise RuntimeError("start() must be called before _ensure_session()")

        if await self._is_authenticated():
            logger.info("Existing session for %s is still valid — skipping login", self._cfg.talk_user)
            return

        if not self._cfg.login_password:
            raise RuntimeError(
                "NEXTCLOUD_VOICE_LOGIN_PASSWORD is empty — the browser needs the real "
                "ai-agent account password (app-passwords are rejected at the web login form)")

        logger.info("No valid session — logging in as %s", self._cfg.talk_user)
        await self._page.goto(f"{self._cfg.nextcloud_base_url}{IDX}/login")
        await self._page.fill('input[name="user"]', self._cfg.talk_user)
        await self._page.fill('input[name="password"]', self._cfg.login_password)
        await self._page.click('button[type="submit"]')

        # Authenticated iff the SPA reports a current user. Robust against 404 pages and
        # redirects that the old `"/login" not in url` heuristic misread as success.
        try:
            await self._page.wait_for_function(
                "() => !!(window.OC && OC.currentUser)", timeout=LOGIN_TIMEOUT_MS)
        except PlaywrightTimeoutError as exc:
            raise RuntimeError(
                "login did not authenticate (OC.currentUser never set) — check "
                "NEXTCLOUD_VOICE_LOGIN_PASSWORD is the real account password") from exc

        if self._context is None:
            raise RuntimeError("browser context missing after login")
        await self._context.storage_state(path=str(STATE_FILE))
        logger.info("Session saved to %s", STATE_FILE)

    async def _is_authenticated(self) -> bool:
        """Best-effort probe: load the Talk app and confirm the SPA has a current user."""
        if self._page is None:
            raise RuntimeError("start() must be called before _is_authenticated()")
        try:
            await self._page.goto(f"{self._cfg.nextcloud_base_url}{IDX}/apps/spreed/",
                                  wait_until="domcontentloaded")
            await self._page.wait_for_timeout(1500)  # let the OC bootstrap run
        except Exception as exc:
            logger.warning("Auth probe navigation failed: %s", exc)
            return False
        try:
            return await self._page.evaluate("() => !!(window.OC && OC.currentUser)")
        except Exception:
            return False

    async def join_call(self, token: str) -> None:
        """Navigate to a Talk conversation and join its active call audio-only.

        Two-step flow (validated on Talk 24): the top-bar `.join-call.call-button` opens a
        media-settings pre-join dialog; the dialog's `.join-call.action-button` confirms.
        All clicks go through JS (`_js_click`) because Talk overlays intercept native ones.
        """
        if self._page is None:
            raise RuntimeError("start() must be called before join_call()")

        url = f"{self._cfg.nextcloud_base_url}{IDX}/call/{token}"
        logger.info("Joining call %s", token)
        await self._page.goto(url, wait_until="domcontentloaded")

        # Wait for the top-bar join control to render (the call must be active).
        await self._page.wait_for_selector("button.join-call", timeout=JOIN_BTN_TIMEOUT_MS)

        # 1) Open the pre-join media-settings dialog.
        if not await self._js_click("button.join-call.call-button"):
            await self._js_click("button.join-call")  # fallback: any join-call button

        # 2) In the media-settings dialog, join audio-only, then confirm.
        try:
            await self._page.wait_for_selector(".media-settings", state="visible",
                                               timeout=MEDIA_SETTINGS_TIMEOUT_MS)
            await self._js_click_aria("Disable video")  # camera off → audio-only (no-op if already off)
            if not await self._js_click(".media-settings button.join-call.action-button"):
                await self._js_click(".media-settings button.join-call")
        except PlaywrightTimeoutError:
            logger.info("No media-settings dialog appeared — assuming direct join")

        # 3) Confirm we actually entered the call (the Leave control only exists in-call).
        await self._page.wait_for_selector(
            'button[aria-label*="Leave call" i], button.leave-call-button--split, button.leave-call',
            state="visible", timeout=JOIN_CONFIRM_TIMEOUT_MS)

        logger.info("Joined call %s", token)

        # Kick off the remote-audio monitor: it reports inbound getStats (so we can see the
        # caller's voice reaching Chromium) and, belt-and-suspenders, reinforces the remote
        # `new Audio()` playout + resumes any suspended AudioContext. Detached — self-limits.
        if self._monitor_task is not None:
            self._monitor_task.cancel()
        self._monitor_task = asyncio.create_task(self._audio_monitor(token))

    async def start_call(self, token: str) -> None:
        """Navigate to a Talk conversation and START (place) a call in it, audio-only.

        The OUTBOUND analogue of ``join_call``. When no call is active a room shows a
        "Start call" control instead of the ``.join-call`` control; clicking it opens the SAME
        pre-join media-settings dialog, so the dialog/confirm/leave-confirm/monitor steps are
        identical to ``join_call`` and reuse the same overlay-safe JS clicks. Starting a call
        rings every other participant of the room (for a 1:1 room, the target user).

        Selectors confirmed live 2026-07-03 against this instance (home room, no active call):
        the top-bar control is ``button.join-call`` with **aria-label/text "Start call"** — the
        SAME Vue component (and media-settings dialog) as the inbound "Join call" button, only the
        label differs. There is NO ``.start-call`` or ``.call-button`` class here. The media-settings
        confirm button was NOT inspected live (that would place a real call); it reuses join's proven
        ``.media-settings button.join-call.action-button`` with aria/generic fallbacks.
        """
        if self._page is None:
            raise RuntimeError("start() must be called before start_call()")

        url = f"{self._cfg.nextcloud_base_url}{IDX}/call/{token}"
        logger.info("Starting (placing) call in %s", token)
        await self._page.goto(url, wait_until="domcontentloaded")

        # The "Start call" control is `button.join-call` (aria/text "Start call") when no call is
        # active; match on either the class or the aria label.
        await self._page.wait_for_selector(
            'button.join-call, button[aria-label*="Start call" i]',
            timeout=START_BTN_TIMEOUT_MS)

        # 1) Open the pre-call media-settings dialog. Prefer the "Start call" aria (semantic),
        #    fall back to the join-call class (same control).
        if not await self._js_click_aria("Start call"):
            await self._js_click("button.join-call")

        # 2) In the media-settings dialog, go audio-only, then confirm the start.
        try:
            await self._page.wait_for_selector(".media-settings", state="visible",
                                               timeout=MEDIA_SETTINGS_TIMEOUT_MS)
            await self._js_click_aria("Disable video")  # camera off → audio-only (no-op if already off)
            # Confirm: reuse join's proven dialog button (shared component); then aria/generic.
            if not await self._js_click(".media-settings button.join-call.action-button"):
                if not await self._js_click_aria("Start call"):
                    await self._js_click(".media-settings button.action-button")
        except PlaywrightTimeoutError:
            logger.info("No media-settings dialog appeared — assuming direct start")

        # 3) Confirm we actually entered the call (the Leave control only exists in-call).
        await self._page.wait_for_selector(
            'button[aria-label*="Leave call" i], button.leave-call-button--split, button.leave-call',
            state="visible", timeout=JOIN_CONFIRM_TIMEOUT_MS)

        logger.info("Started call %s", token)

        # Same remote-audio monitor as join_call (inbound getStats + playout reinforcement).
        if self._monitor_task is not None:
            self._monitor_task.cancel()
        self._monitor_task = asyncio.create_task(self._audio_monitor(token))

    async def _audio_monitor(self, token: str) -> None:
        """Every 3s for ~1 min: force remote-audio playout + log inbound-audio health.

        Fire-and-forget; ends on the first page/context-closed error (call teardown)."""
        for _ in range(20):
            try:
                await asyncio.sleep(3)
                report = await self._page.evaluate(FIX_AND_REPORT_JS)
                logger.info("audio-monitor %s: %s", token, report)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.info("audio-monitor %s stopping: %s", token, str(exc)[:80])
                return

    async def leave_call(self) -> None:
        """Click the hang-up control and navigate away from the call."""
        if self._page is None:
            raise RuntimeError("start() must be called before leave_call()")

        # JS-click the leave control (overlay-safe); fall back to navigating away.
        try:
            left = await self._page.evaluate(
                "() => { const b = document.querySelector("
                "'button[aria-label*=\"Leave call\" i], button.leave-call-button--split, button.leave-call');"
                " if (b) { b.click(); return true; } return false; }")
            if not left:
                logger.info("No leave-call control found — navigating away")
        except Exception as exc:
            logger.warning("Leave-call click failed (%s) — navigating away anyway", exc)

        await self._page.goto(f"{self._cfg.nextcloud_base_url}{IDX}/apps/spreed/",
                              wait_until="domcontentloaded")
        logger.info("Left call")

    async def stop(self) -> None:
        """Close the context/browser and stop Playwright. Idempotent — safe to call twice."""
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            self._monitor_task = None
        # context.close() closes the pages it owns, so _page needs no explicit close —
        # just drop our reference to it.
        self._page = None
        for attr in ("_context", "_browser"):
            closer = getattr(self, attr)
            if closer is not None:
                try:
                    await closer.close()
                except Exception as exc:
                    logger.warning("Error closing %s: %s", attr, exc)
                setattr(self, attr, None)

        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as exc:
                logger.warning("Error stopping Playwright: %s", exc)
            self._playwright = None

        self._started = False
        logger.info("TalkBrowser stopped")
