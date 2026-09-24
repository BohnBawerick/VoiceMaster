# Issue tracker: GitHub Issues

Issues and feature requests live in this repository's GitHub Issues. Use `gh issue` to read and
create them. Security problems go through private vulnerability reporting, never a public issue
(see `SECURITY.md`).

## Conventions

- One issue per problem or feature. Link related issues rather than combining them.
- Triage state is a label (see `triage-labels.md` for the role strings).
- Never paste secrets, real phone numbers, recordings or transcripts into an issue.

## Deployment changes

An install's own compose files, hostnames and deploy notes are not part of this repository.
An issue that needs an operator step (a new env var, a new mount) says so in its body, and the
pull request that closes it updates `.env.example`, `docker-compose.yml` and
`docs/configuration.md`.
