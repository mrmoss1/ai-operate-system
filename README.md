# operate-system

![CI](https://github.com/OWNER/operate-system/actions/workflows/ci.yml/badge.svg)
![Zone-1](https://img.shields.io/badge/Zone--1-operator--agnostic-2ea44f)
![Python](https://img.shields.io/badge/python-3.11-blue)

The engine layer of **Operate** — a model-neutral, governed operating system for professional
practices. This public repository is the **Zone-1 core**: the shareable engines plus the
confidentiality guardrail. It contains **no client, tenant, or operator data** — by construction,
not by promise (see below).

## Why a two-zone split

Operate is delivered to individual practices (e.g. a solo law firm) as separate installs, each
holding privileged client data. The confidentiality boundary is therefore the **repository**
boundary:

- **Zone 1 — this repo (public):** engines, templates, CI, the guardrail. Operator-agnostic.
- **Zone 2 — private, per practice:** that practice's configuration, matters, and work product.
  Never ships, never public.

## The guardrail — leaks are structurally hard, not just discouraged

A pre-commit hook (`.githooks/pre-commit`) plus the CI workflow refuse any commit that carries a
secret-like file or an operator/tenant identity token. Install it with:

```bash
git config core.hooksPath .githooks
```

One deliberate design detail: a linter that *forbids* identity tokens must *list* them — which
would itself leak identity if shipped. So the token list lives in a local, git-ignored
`.zone-tokens` file: **the mechanism is public, the identities are not.**

## Engines

All Python, standard-library only, each with a runnable self-test where a self-test makes sense.

| Engine | Does | Self-test |
| --- | --- | --- |
| `atomic_write.py` | Atomic write-then-validate: a file is replaced only if the new bytes pass a validator, so a partial write never lands | `--self-test` |
| `atomic_write_cli.py` | Shell-callable wrapper around the atomic writer (stdin → validated atomic replace) | — |
| `board_ids.py` | Global monotonic ID ratchet with collision detection — stable identity across a work board | no-arg |
| `board_add.py` | The only sanctioned constructor for a work-board card: resolves linkage, mints a fresh global ID, self-validates before returning | no-arg |
| `board_lint.py` | The executable contract for a work board (schema + linkage invariants) | — |
| `agent_fleet_lint.py` | Structural conformance scanner for an agent fleet (six-folder layout, co-location, no stray artifacts) | — |
| `agent_form_migrate.py` | Migrates a flat-file agent to the six-folder form by pure rename, rewriting and re-validating intra-agent links | `--self-test` |

## Design principles

- **Atomic write, then validate** — every write is a validated, atomic replace; a truncated or
  half-written file never reaches disk.
- **Linters as the contract** — structure and identity are enforced by executable checks, not by
  convention.
- **Model-neutral** — the engines are plain Python; the system runs under Claude, Codex, or Copilot
  via generated per-tool instruction files. The logic is the product; the AI is the driver.

## Running

```bash
cd engines
python3 atomic_write.py --self-test
python3 board_ids.py
python3 agent_form_migrate.py --self-test
```

---

*This repository is the shareable core of a larger, privately-governed system. Replace `OWNER` in
the CI badge with the repository owner once published.*
