# Voice Control

The phone layer for Hermes. Hermes profiles get a voice and a way to be reached, and this is
where you see, schedule and review the calls they make. Hermes itself is the usual driver; this
is the interface layer and the manual override.

Named **Voice Control**. The repo directory is still `VoiceMaster`, which is a separate
mechanical rename with cross-repo references.

## Language

**Agent**:
A Hermes profile that can be reached on an Outlet. Its identity, tools, model and memory are the
profile's own (`~/.hermes/profiles/<name>/`); this app adds the voice settings and makes it
callable.
_Avoid_: voice agent, bot, assistant, persona (a persona is part of an Agent, not the whole)

**Profile**:
A Hermes home directory with its own `config.yaml`, `SOUL.md`, `.env`, memory, sessions, skills
and cron. The durable identity behind an Agent. Only ever means the Hermes concept.
_Avoid_: using "profile" for the old `agents/*.yaml` voice-config files

**Outlet**:
A channel calls arrive on and leave from. There are two: the phone number, and Nextcloud Talk.
Exactly one Agent is assigned to each Outlet at a time, and the site shows that assignment at a
glance.
_Avoid_: lane, mode, bridge, channel

**Mission**:
The instruction for one outbound Call: who to reach and what to accomplish. Belongs to the Call,
never to the Agent, so the same Agent can be sent on many different Missions.

Inbound Calls have **no** Mission and no standing brief. An Agent answering its Outlet is simply
itself: honest about what it is and what it can do, and it works out what is needed once the
caller speaks.

A Mission is authored three ways: typed out in full; typed as a short prompt and elaborated by
the Agent into its own Mission; or spoken aloud to the Agent, which listens without replying and
writes its own Mission from what it heard.
_Avoid_: task, brief, prompt, instruction

**Call**:
One conversation between an Agent and a person on an Outlet, with a verbatim transcript, a
stereo recording, a summary and an outcome.
_Avoid_: session, conversation

**Schedule**:
A Call that has not happened yet, with a time, an Agent and a Mission attached.
_Avoid_: job, task, cron

**Speed dial**:
The saved list of numbers a Call can be aimed at without typing one. The owner's own number is in
it by default.
_Avoid_: contacts, address book, allow-list (the outbound allow-any policy is a separate thing)

**Disclosure**:
The per-call toggle deciding whether the Agent tells the person it is an AI. A choice, not a
hardcoded behaviour.
_Avoid_: consent, compliance

## Where a Call lives

The call archive holds one document per Call: the verbatim transcript, and the Agent, Outlet,
Mission, outcome and summary as metadata. It is a SQLite file beside the event log by default, or
a dedicated `voice` bank in Hindsight when `HINDSIGHT_URL` is set; the document is the same
shape in both, and there is no second copy. Audio is the exception: recordings are stereo Opus
files on a volume, referenced from the Call's metadata and played in the page.

The summary is written at teardown by the Agent's own profile gateway, by the being that was
actually on the call.

## Retired language

Implementation terms from the control-plane build. They do not appear in the product and should
not be reintroduced into user-facing language.

**Mode C / Mode V / Mode T**, **lane**, **pipeline** (as a user-facing word), **twin**,
**bench**, **eval**, **dry-run**, **provider registry** (the settings page is just Settings).

Bench, eval and the dry-run are retired in code as well as in language: ticket 15 deleted the
screens, the routes and the modules. There is no dry-run in this product - the way to find out
whether a configuration works is to place a call.

Cascade and realtime survive as real choices, but live under Advanced with honest labels saying
which combinations are proven and which are untested.
