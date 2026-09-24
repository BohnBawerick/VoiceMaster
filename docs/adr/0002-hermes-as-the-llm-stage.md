# Hermes is the LLM stage of a cascade Agent

To let a caller talk to Hermes itself, the Agent's own Hermes profile becomes the llm stage of the
existing cascade pipeline (Deepgram, then Hermes, then ElevenLabs), selected by the registry
provider `hermes-agent`. We chose this over a third pipeline because the cascade engine already
owns everything a call needs that is not thinking: turn-taking, barge-in, paced streaming speech,
recording, the call record and the archive. Only the llm round changes, so only it was replaced.

## Considered options

- **A third pipeline, `hermes`, beside `realtime` and `cascade`.** A second copy of turn-taking,
  barge-in, recording and teardown to keep in step with the first. The two cascade lanes already
  drifted once (Smart-Turn was wired on the phone and inert on Talk for weeks).
- **Keep Realtime in front and make it always call the `hermes_agent` tool.** This is what the
  owner asked to get away from: a second model paraphrasing Hermes in both directions, and
  answering as "Robot" with the default soul whatever profile the Agent is bound to.
- **Hermes as the cascade's llm stage.** Chosen.

## Consequences

- **Hermes holds the conversation, so VoiceMaster stops resending it.** Every turn carries
  `X-Hermes-Session-Id: voice-<call id>` and one new user message. The vendor-LLM round still
  resends its history; the two rounds are separate code paths in `cascade_live` on purpose.
- **The client streams `/v1/chat/completions`, not `/v1/responses`.** Read against the pinned
  upstream source, `/v1/responses` never reports a failed turn, and a dead model chain completes
  as ordinary text. Chat completions end with `finish_reason: "error"` and `hermes.failed`. The
  client holds the last sentence until the stream ends cleanly, so an error body is dropped, not
  spoken. This is a separate module from `hermes_gateway.ask_chat`, whose listen-only guard must
  keep standing.
- **The Agent's tools setting is `tool_choice` on the wire.** The first build could not cut
  Hermes's tools (upstream ignored `tool_choice`), so it sent no switch and refused an outbound
  call unless the Agent said `guardrails.on_call_tools: true`. Hermes now enforces `tool_choice`.
  Every turn of a direct call sends `none` (setting off) or `auto` (setting on, the profile's full
  tool set), and a tools-off Agent is valid inbound and outbound. A listed caller of a tools-on
  Agent gets full tools by decision (VC24). On Talk a guest never reaches this lane.
- **Speech starts before Hermes has finished writing.** Replies are spoken sentence by sentence.
  A barge before the reply starts is soft, so a cough cannot kill an agent turn that is half way
  through acting. A barge over the reply is hard: the stream is closed, which makes upstream
  interrupt the agent, and the next turn tells Hermes how much of its reply was heard.
- **Profile routing has one owner.** `voicecore.hermes_gateway.gateway_url_for_profile` reads the
  env map, then `default`, then the supervisor's `gateways/gateways.json`. The two bridge copies
  are gone. This is the half of VC15 that ADR 0001 promised and the code had not delivered: a new
  profile is reachable on a call with no compose edit and no stack redeploy.
- **The cascade capability names an Outlet and a direction.** `profiles.CASCADE_CAPABILITY`
  replaced a boolean that could not say "inbound", which is why inbound cascade had to be refused
  in three separate places.
