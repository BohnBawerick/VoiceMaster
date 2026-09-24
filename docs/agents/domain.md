# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

This repo is **single-context**: one `CONTEXT.md` plus `docs/adr/` at the root. The dirs under
`services/` are deployment units, not separate bounded contexts, and they share one vocabulary.

## Before exploring, read these

- **`CONTEXT.md`** at the repo root. The glossary, and the shortest path to understanding the
  product. Read it first.
- **`docs/decisions.md`** - decisions `VC1` onward. Binding.
- **`docs/adr/`** - read ADRs that touch the area you are about to work in.
- **`docs/configuration.md`** - when the task touches settings, the archive or deployment.

If any of these files do not exist, **proceed silently**. Do not flag their absence; do not
suggest creating them upfront. The `/domain-modeling` skill (reached via `/grill-with-docs` and
`/improve-codebase-architecture`) creates them lazily when terms or decisions actually get
resolved.

## New decisions: ADR or `decisions.md`?

`docs/decisions.md` is the log. Append the next `VC*` number for an ordinary decision. Write a
numbered ADR in `docs/adr/` when a decision is hard to reverse, surprising without context, **and**
the result of a real trade-off, and reference it from the `VC*` entry rather than restating it.
Never renumber or reuse a `D*` number; that series belonged to an earlier, abandoned build.

## File structure

```
/
├── CONTEXT.md         <- glossary, read first
├── docs/
│   ├── decisions.md   <- VC1 onward
│   └── adr/           <- numbered ADRs
├── hermes/            <- the Hermes-side add-ons
└── services/
```

## Use the glossary's vocabulary

When your output names a domain concept (in an issue title, a refactor proposal, a hypothesis, a
test name), use the term as defined in `CONTEXT.md`, falling back to `README.md`. Do not drift to
synonyms the glossary explicitly avoids.

If the concept you need is not in the glossary yet, that is a signal - either you are inventing
language the project does not use (reconsider) or there is a real gap (note it for
`/domain-modeling`).

## Flag ADR conflicts

If your output contradicts an existing ADR or a `decisions.md` entry, surface it explicitly
rather than silently overriding:

> _Contradicts ADR-0007 (event-sourced orders) - but worth reopening because..._

The rules listed in `AGENTS.md` ("The rules that keep this from breaking") are the hardest of
these. Each one cost real debugging time. Contradicting one needs an explicit argument, not a
passing mention.
