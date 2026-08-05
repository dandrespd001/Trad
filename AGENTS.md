# Codex orchestration for this repository

Codex is the architect, supervisor, reviewer, and final decision-maker. MiniMax-M3 may act only as a bounded implementation worker through the installed `$delegate-minimax-api` skill and the official Token Plan Responses API.

The repository owner has granted standing workflow-level authorization to use that bridge for non-sensitive, explicitly allowlisted development material. Codex may therefore run the bounded workflow without asking a person to approve each job. This standing authorization does not permit secrets, financial controls, raw/approved financial data, or any request blocked by the active platform's network/data-egress policy.

For every concrete coding task, Codex must first classify MiniMax eligibility.
If the task is non-sensitive, bounded, expected to reduce primary-model work,
covered by the standing authorization, and permitted by active platform policy,
Codex must use `$delegate-minimax-api` without asking a person to approve that
job. If any condition fails, Codex works directly and records the exclusion;
it must not weaken the boundary to force delegation.

Before creating a job, Codex must compare the installed Skill, private worker,
verifier, and `references/bridge-lock.json`; require exact versions/hashes and
`installation.status=READY`, then run offline `doctor` and `job list`. A stale,
missing, ambiguous, poisoned, or `HOLD` installation is a stop condition, not a
reason to call the old runner.

For an eligible task, automate the bounded workflow without a separate person
approving each job:

1. Write a narrow repository-relative spec with invariants, tests, and exclusions.
2. Select the smallest source/test allowlist and create the best-effort DLP-scanned job with `--cloud-approved`.
3. Invoke the runner with `/usr/bin/python3 -I`; start narrow jobs with
   `--reasoning none --max-output-tokens 8000`, raising the cap only when the
   reviewed scope requires it. Use `high` only for complex work or one repair.
4. Review the sealed deterministic patch export, run `git apply --check`, apply it only after review, and execute focused plus release tests in a network-denied sandbox with a minimal allowlisted environment and no Fish, cloud, broker, or trading credentials. Reject the candidate if that isolation cannot be established.
5. Accept, perform at most one bounded repair, or reject. Purge the worker snapshot and export afterward.

Worker state is confined to `/tmp/minimax-api-worker-$UID/state`, and only one MiniMax Responses job POST may run at a time for that user. `--cloud-approved` records an existing authorization; it does not bypass platform network/data-egress controls. Never send credentials, `.env`, datasets, reports, broker state, raw/approved financial data, trust-boundary code, financial controls, or deployment paths. Never let MiniMax use tools, edit the real checkout, commit, push, deploy, approve promotion, enable live trading, or move money. No LLM output guarantees profitability; `live_trading_allowed` remains `false`, and platform policy plus deterministic financial gates always override delegation.

Reject startup hooks, package initializers, plugin/entry-point changes, executable files, new network/process/credential access, and imports into protected execution/risk capabilities before applying or running any candidate. A green test may not override these static boundaries.

Use only a `sk-cp-` Subscription Key. MiniMax may automatically consume purchased Credits after included Token Plan quota; the runner records but cannot disable that provider-side behavior. If zero incremental Credit spend is required, require an external account/quota guard before delegation.
