---
name: delegate-minimax-api
description: Delegate bounded coding changes to MiniMax-M3 through the official Token Plan Responses API while the primary Codex remains architect, reviewer, tester, and final decision-maker. Use for cloud delegation only when the user has authorized the minimal allowlisted source bundle and the active platform permits that egress. Also use for local diagnosis of the API bridge without source egress. Do not use for secrets, trust-boundary code, financial controls, raw/approved financial data, broker state, deployments, or live order execution.
---

# Delegate to MiniMax API

Keep the current Codex session in charge of design, scope, patch review, testing, and acceptance. Treat MiniMax-M3 only as a text patch generator with no tools. Never give it the checkout, shell, MCP, plugins, broker, or credentials. Use the API-backed worker; local inference is out of scope.

## Check preconditions

1. Confirm that the user authorized sending the selected files to MiniMax API and that current platform policy permits the network/data-egress request. Treat workflow-level authorization as eliminating a separate per-job human step only inside the runner; never use it to bypass a host-platform approval or denial.
2. Resolve `scripts/minimax_api_worker.py` and the Codex-only
   `scripts/minimax_patch_verify.py` relative to this `SKILL.md`; invoke both
   with `/usr/bin/python3 -I`.
   If exposing `~/.local/bin/minimax-api-worker`, install it as a private regular copy of that audited runner, never as a symlink, so its self-hash and provenance remain stable.
3. Require the Fish config to be user-owned and mode `0600`. Let the runner read exactly one literal `ANTHROPIC_AUTH_TOKEN` from `function claude-minimax`; require a Token Plan Subscription Key beginning with `sk-cp-`. Never print or persist it, and never substitute a standard pay-as-you-go API key.
4. Account for MiniMax billing semantics: the Subscription Key uses included Token Plan quota first and can automatically consume purchased Credits when they exist. Responses exposes no per-request switch to disable that overflow. If zero incremental Credit consumption is required, require an external account/quota guard or verify that the Team has no purchased Credits before running; do not claim the runner enforces this.
5. Run a local check:

```bash
/usr/bin/python3 -I /absolute/path/to/minimax_api_worker.py \
  --json --repo /absolute/path/to/repo doctor
```

Run `doctor --probe` only when a credential/model connectivity check is useful and platform policy permits it. The probe sends no repository content. Only this read-only GET may retry.
Before job creation, compare the source and installed worker/verifier against
`references/bridge-lock.json`, require `installation.status=READY`, and run
`job list` to recover or block on existing/ambiguous work. Never invoke a stale
installed revision merely because its command is already present.

## Create a bounded job

Write a small UTF-8 implementation spec inside the repository with invariants, exact acceptance tests, and explicit exclusions. Pass it as a repository-relative regular-file path. Reject absolute paths, `..`, the repository root, and symlinks. Select only the files MiniMax needs; never select `.`.

```bash
/usr/bin/python3 -I /absolute/path/to/minimax_api_worker.py \
  --json --repo /absolute/path/to/repo job create \
  --cloud-approved \
  --spec task-spec.md \
  --allow-path src/relevant_package/module.py \
  --allow-path tests/test_relevant_package.py
```

Exact files are the default. A directory is rejected unless Codex adds
`--allow-directory` explicitly after reviewing the traversal. Delegable
suffixes are limited to `.py`, `.md`, `.rst`, and `.txt`. Expect state under
`/tmp/minimax-api-worker-$UID/state/<repo-hash>/<job-id>`. The runner accepts at most 80 allowlisted UTF-8 files and 300 KB, with a 400 KB serialized-input ceiling. Every selected source and the spec must be non-executable; each file seals `source_executable=false` into `source_snapshot_sha256`, and any later mode-bit change invalidates the job. Treat its name/content scan as best-effort DLP, not proof that selected files are safe. Inspect the spec, allowlist, skipped files, and egress manifest before sending.

Read the sealed manifest before `job run`:

```bash
/usr/bin/python3 -I /absolute/path/to/minimax_api_worker.py \
  --json --repo /absolute/path/to/repo job list

/usr/bin/python3 -I /absolute/path/to/minimax_api_worker.py \
  --json --repo /absolute/path/to/repo job status JOB_ID
```

Use `job list` to recover pending job IDs without inspecting private state by
hand. It is read-only, does not create a state directory or HMAC key when none
exists, and returns only bounded public summaries. Treat a listing-limit or
manifest-integrity error as a stop condition; do not bypass it by scanning the
state tree yourself.

Require the expected `spec_source_path`, spec hash, `allowed_selections`, selected `files` paths/bytes/hashes, `skipped_files`, billing contract, runner hash, and prompt hash. Codex performs this review without a separate person.

Verify that the sealed job identifies the exact selected working-tree snapshot with `source_head_commit`, `source_snapshot_sha256`, the spec hash, and a local HMAC. Allow selected uncommitted files only deliberately. If HEAD, the spec, or any selected source changes, create a new job.

## Run MiniMax

Use thinking-off first:

```bash
/usr/bin/python3 -I /absolute/path/to/minimax_api_worker.py \
  --json --repo /absolute/path/to/repo job run JOB_ID \
  --reasoning none --max-output-tokens 8000
```

Use the lowest output cap compatible with the reviewed patch; unused capacity is
not evidence of spend, but a smaller cap limits runaway output. Raise it only
once rather than retrying a truncated broad job. Use `--reasoning high` only for
a complex first pass or one bounded repair. Expect one global API slot per user. If the bounded queue is busy, the job remains `CREATED` and may be retried automatically; no provider request occurred. Under that lock, the runner creates and fsyncs a global `0600` no-follow/exclusive `.provider-request-unconfirmed.json` before it records `RUNNING`, loads the Fish credential, forks, or can issue the POST. The marker contains no token: it records the job, attempt, endpoint/model, a canonical fingerprint, and the SHA-256 of the exact method+URL+body. A terminal job manifest with `request.outcome=confirmed|not_sent` is fsynced before the marker is removed and the base directory is fsynced. Timeout, transport ambiguity, `SIGKILL`, malformed child output, a missing/corrupt referenced job, or `RUNNING/unconfirmed` retains the marker and blocks every later POST without replay; `doctor` and `job list` report it, and `job purge` refuses the referenced job. Never auto-clear it merely because a PID is gone. A killable child enforces a monotonic total deadline (maximum 1800 seconds) for model probes and the single generative POST. Require MiniMax to return a unified patch or `NO_CHANGE`.

This is fail-closed duplicate-cost protection within the current boot, not a remote exactly-once guarantee: MiniMax exposes no idempotency key for this contract, and `/tmp` does not survive reboot. An unresolved marker therefore requires provider/account reconciliation or an explicit abandonment decision before further POSTs.

## Review and accept

Treat `PATCH_READY` as a candidate, never an acceptance.

1. Export the sealed artifact without choosing an output path:

```bash
/usr/bin/python3 -I /absolute/path/to/minimax_api_worker.py \
  --json --repo /absolute/path/to/repo job diff JOB_ID
```

2. Read the returned `patch` field. Require `/tmp/minimax-api-worker-$UID/exports/JOB_ID.patch`; refuse an unexpected path. Verify its SHA-256, `source_head_commit`, and `source_snapshot_sha256`.
3. Inspect every changed line. Reject scope expansion, weakened fail-closed behavior, secrets, remote dependencies, broker/live operations, deployment changes, or missing tests.
4. Reject startup hooks, package initializers, executable files, imports into
   protected execution/risk code, and new network/process/credential
   capabilities before execution. Then invoke the separate verifier with the
   exact reviewed patch hash and direct JSON argv arrays:

```bash
/usr/bin/python3 -I /absolute/path/to/minimax_patch_verify.py \
  --json --repo /absolute/path/to/repo verify JOB_ID \
  --expect-patch-sha256 REVIEWED_SHA256 \
  --venv .venv312 \
  --focused-argv-json '["/runtime/venv/bin/python","-S","-P","-m","unittest","tests.test_relevant","-v"]' \
  --release-argv-json '["/runtime/venv/bin/python","-S","-P","-m","unittest","discover","-s","tests","-q"]'
```

   The verifier must rebuild a private projection from the tracked sealed HEAD
   plus only the exact selected snapshot, apply the patch only there, mount
   `/work` read-only, and run focused before release in Bubblewrap with a new
   network namespace, synthetic HOME, minimal environment, no real checkout,
   Fish configuration, broker/cloud credentials, or host sockets. It must fail
   closed if Bubblewrap/user/network namespaces are unavailable; never fall
   back to an unsandboxed command. Set `RLIMIT_NPROC` to the reliable current
   host-UID task count (including threads) plus 256, capped by 4096 and the
   existing hard limit; fail before execution when that count/budget is
   unavailable. Apply a per-process `RLIMIT_AS` of 4 GiB or the lower existing
   hard limit. With `--venv`, every Python command must use exactly
   `/runtime/venv/bin/python` with `-S` and `-P` before the module, command, or
   script boundary. With a venv, no other executable is accepted; without one,
   the only accepted executable is exact `/usr/bin/python3` with `-I -S -P`.
   A sealed seccomp filter denies keyrings, high-risk kernel/process/mount
   syscalls, and every socket family except UNIX/IPv4/IPv6; `/proc/keys` and
   `/proc/key-users` are masked. The verifier sets `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, rejects
   symlinks, special files, and startup hooks in a bounded site-packages walk,
   and freezes the hash-pinned adjacent worker into one read-only sealed memfd.
   Status and diff run from that same inherited descriptor. Only after `PASS` may
   Codex apply the same reviewed patch to the real checkout.
5. If verification fails, Codex may create one minimal repair job containing
   only relevant source and a failure delta that Codex explicitly sanitizes.
   This does not require operator intervention. The verifier emits
   phase, exit status, byte count, and output hash but deliberately omits raw
   test output; never forward its captured output automatically. Stop after two
   worker attempts.
6. Purge the isolated snapshot and deterministic export after acceptance or rejection:

```bash
/usr/bin/python3 -I /absolute/path/to/minimax_api_worker.py \
  --json --repo /absolute/path/to/repo job purge JOB_ID
```

Automate these steps only while user authorization and platform policy remain valid. Never claim the workflow suppresses platform-level network approval. Never let MiniMax apply to the real checkout, commit, push, deploy, or control financial execution.

The verifier ignores unrelated dirty/untracked files. Any uncommitted file
required by focused or release tests must therefore be part of the sealed
allowlist. It does not run `checkout`, `clone`, `worktree`, Git hooks, smudge
filters, or package startup hooks. Its temporary projection is deleted after
every result; any cleanup failure is a rejection that reports retained private
state.

## Control token use

- Delegate one coherent implementation package at a time.
- Prefer individual files over directories.
- Start with `--reasoning none`; escalate once only when justified.
- Keep the system contract and prompt-cache key stable.
- Expect provider caching only once the reusable prefix reaches MiniMax's 512-token minimum; very small jobs may report zero cached tokens.
- Send failure deltas rather than conversation history.
- Track input, cached, output, and reasoning tokens from each result.
- Split work before the file or snapshot caps.
- Keep the local 400 KB input and 50,000-token output ceilings. The runner deliberately does not call `/responses/input_tokens` for private jobs because that would send the same bundle a second time; use smaller bundles instead of relying on the 1M context limit.

## Enforce hard boundaries

- Use only `https://api.minimax.io/v1`, exact model `MiniMax-M3`, service tier `standard`, and a Token Plan `sk-cp-` Subscription Key. Keep redirects and proxies disabled. Never substitute the standard pay-as-you-go key. Explicitly report that purchased Credits, if present, may cover quota overflow automatically.
- Add `--cloud-approved` only for already-authorized external processing. Never treat it as a platform-policy override.
- Treat DLP as best effort. Never include `.env`, credentials, keys, notebooks, datasets, reports, logs, raw/approved financial data, `AGENTS.md`, `CLAUDE.md`, `.agents`, `.claude`, `.codex`, this delegation skill, or its integration/audit contract.
- Production credential loading is fixed to `~/.config/fish/config.fish`; the
  non-default path switch is test-only. Core dumps and process dumpability must
  be disabled before loading the credential/private bundle and rechecked in the child.
- Never delegate broker, execution, live, promotion, risk-control, deployment, or credential paths. Minimize the allowlist even though the runner denies these path classes.
- In this financial repository, also deny all `configs/`, all `scripts/`, the central CLI/config loader, package/deployment manifests, and approved-data policy code. Those are transitive authority surfaces even when their filenames do not contain `risk` or `live`.
- Delegate code only as pure Python modules; `.md`, `.rst`, and `.txt` are
  documentation-only exceptions. For every selected and changed Python file, reject the
  candidate if either the sealed baseline or generated file contains protected
  imports/references/calls, including environment, network, subprocess,
  dynamic loading/reflection (`__builtins__`, `getattr`, `site`, `inspect`,
  `pickle`, `marshal`), filesystem I/O (`pathlib`, `io`, `open`), `urllib3`,
  broker/live, or execution/risk capabilities. This conservative AST/regex
  policy blocks activation of a pre-existing helper or `if False` branch; it is
  an explicit allowability boundary, not a claim of complete program analysis.
- Never wrap the call in MCP, plugins, hooks, Claude Code, a nested Codex, or a tunnel.
- Preserve `live_trading_allowed=false`. Never approve promotion or money movement, and never claim this worker establishes or guarantees profitability.
- The non-sensitive synthetic canary passed. A later private-bundle POST attempt ended
  with an ambiguous transport failure before any provider result was available; delivery
  is unconfirmed. Never retry that sealed job. Send a new private job only when the active
  platform explicitly permits that egress and after repeating the manifest review.

- Job creation publishes a complete sealed directory by atomically renaming a
  private `.creating-*` sibling; purge first atomically renames the job to a
  private `.purging-*` tombstone. `job list` considers only direct 20-hex job
  children and is capped at 512 direct entries, 256 jobs, 16 MB of manifests,
  and JSON nesting depth 128.
- All float timeouts must be finite and provider timeouts cannot exceed 1800
  seconds. Reserved result names are checked before token loading. Provider
  output and patch artifacts are created privately with no-follow/exclusive, identity-checked publication;
  a prepositioned symlink or file is never truncated or replaced.
- The repository runner v0.3.5 currently has SHA-256
  `5fdd4dcf32e47ca5c27b169d7472ecd036cd7fcf7b90b0de6d15b4ea43b7498d`.
  The Codex-only verifier v0.3.1 currently has SHA-256
  `84b233f55ecdea3e9219e16a9466609cf1c11044f45560d8d92d27edfc9dabef`.
  Recompute the hash after every source
  change and require the installed Skill, verifier, and private worker copy to
  match before use; this is revision evidence, not a permanent trust exception.
  Never copy `__pycache__`, `.pyc`, test caches, or temporary job artifacts into
  the global Skill or private command installation.

Read [references/architecture.md](references/architecture.md) before changing the trust boundary, API contract, token policy, state layout, or failure states.
Use [references/bridge-lock.json](references/bridge-lock.json) as the
machine-readable source/hash/test/install record; installation remains blocked
while its `installation.status` is `HOLD`.
