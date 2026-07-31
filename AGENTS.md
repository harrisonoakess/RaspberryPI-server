# AGENTS.md

## Scope
Applies to the whole repository and every PRD in `prd/`.

## Stack
- Server: FastAPI on Railway, SQLite on a mounted volume. Root `/server`.
- Frontend: React + TypeScript + Vite dashboard. Root `/frontend`. Built into
  the server image by the repo-root `Dockerfile`; same process, same origin.
- Pi: Python systemd daemon, Raspberry Pi OS 64-bit (Pi 3B+). Root `/pi`.
- The Pi deploys independently of the server; they are coupled only by the HTTP
  contract in the active PRD. Server and frontend deploy together.
- Tests: pytest. Canonical command from the repo root is
  `.venv/bin/python -m pytest tests -q` (see README "Tests"). The frontend adds
  `npm run typecheck`, `npm test`, and `npm run build` from `/frontend`.

## Roles (multi-agent)
- CTO (Opus 5): sets acceptance criteria, slices work by file ownership, approves.
- Implementer (Sonnet 5): owns one slice, stays in its files.
- Tester (GPT-5.6 Sol, or any non-Claude model): verifies against the PRD's success criteria.
- No agent approves its own code.

## Always
- Read this file, the active PRD, and relevant code before editing.
- Choose the smallest design that fully satisfies the PRD.
- Derive tests from the PRD's success criteria; cover failure and boundary cases.
- Update tests and docs with behavior changes.
- Report commands run, results, skipped checks, and risks.

## If / Then
- If ambiguity affects security, data loss, cost, or a public contract, ask the user.
- If an interface changes, update every producer, consumer, test, and doc together.
- If slices touch the same files, sequence them instead of editing in parallel.
- If a test can't run locally (Pi hardware, Railway deploy), say why and give manual steps.

## Never
- Commit secrets, local state, build output, or unrelated changes.
- Weaken auth, validation, or durability to make a test pass.
- Hide failures, delete failing tests, or claim an unexecuted check passed.
- Mix unrelated refactors with a PRD implementation.
