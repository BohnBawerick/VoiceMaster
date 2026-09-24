# Agents are Hermes profiles

An Agent in Voice Control is not a voice configuration that references Hermes; it **is** a Hermes
profile (`~/.hermes/profiles/<name>/`, with its own `config.yaml`, `SOUL.md`, `.env`, memory,
sessions, skills and cron), to which this app adds voice settings and an Outlet assignment. We
chose this because the thing we want, agents that are genuinely separate beings with their own
personality, tools and memory, is a capability Hermes already has and has run in production since
2026-05-17 (a second profile). Building a parallel lightweight persona concept in this app would
have been a shallow imitation of it.

## Considered options

- **Voice persona only.** Prompt, voice and provider, all sharing one Hermes brain and one memory
  pool. This is what the previous build did, and it is why its three "agents" were really three
  configurations of the same being.
- **Voice persona with its own memory scope.** Cheap and needs no infrastructure change, but it
  imitates profile separation without delivering it: same tools, same soul, same skills.
- **Agent is a Hermes profile.** Chosen.

## Consequences

- **Creating an Agent creates a Hermes gateway process.** Each profile runs its own gateway
  (`hermes -p <name> gateway run`), with `API_SERVER_*` unset so only the default profile owns
  port 18789.
- **This repo now changes the Hermes repo.** Profile discovery has to become dynamic, or creating
  an Agent would require a stack redeploy and therefore bounce the live phone line. The
  supervisor moves from reading `HERMES_PROFILES` to globbing `~/.hermes/profiles/*/`, and the
  bridges read gateway URLs from a file this app writes instead of `HERMES_PROFILE_GATEWAY_URLS`.
  Both repos are in scope for a single change; the two-repos-one-stack discipline applies.
- **Profile state is container-master.** Hermes mutates it at runtime, so profiles are not
  reconstructible from this repo and must not be treated as such.
- **The agent creation wizard is Hermes administration.** It covers souls, models, skills, memory
  and Telegram pairing, not just voice, because that is what a profile is. Its surface therefore
  tracks Hermes's own surface and will need maintenance when Hermes changes.
