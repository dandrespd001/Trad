# Codex supervisor / MiniMax-M3 API executor v0.3.5

## Decision

Keep the primary Codex as architect and call exact model `MiniMax-M3` directly through the official Token Plan Responses endpoint. Send a small allowlisted bundle after best-effort DLP scanning and accept only a unified patch. Do not run MiniMax locally or start Claude Code, another Codex process, MCP, or a plugin wrapper.

```text
Primary Codex
  -> repository-relative spec + explicit narrow allowlist
  -> best-effort DLP scan + exact selected working-tree snapshot
  -> sealed `job status` review of spec, files, skips, hashes, and billing
  -> POST api.minimax.io/v1/responses (no tools, one attempt)
  -> locally validated, frozen, deterministic patch export
  -> Codex review + expected patch hash
  -> Codex-only Bubblewrap verifier over a private read-only projection
  -> accept, one bounded repair, or reject
```

## Why direct Responses

- MiniMax documents `MiniMax-M3`, a 1,000,000-token context, `https://api.minimax.io/v1`, Bearer authentication, and `wire_api = "responses"` for Codex: <https://platform.minimax.io/docs/token-plan/codex>.
- MiniMax documents `POST /v1/responses`, reasoning control, token usage, `prompt_cache_key`, and text output: <https://platform.minimax.io/docs/api-reference/responses-create>.
- A nested Codex or Claude process exposes unrelated agent features and may make auxiliary requests. A direct no-tools client removes that surface.
- MiniMax's current Responses schema documents text output but not JSON Schema. Require plain text, accept only `diff --git` or `NO_CHANGE`, and validate locally.

## Process and credential boundary

Invoke the runner with `/usr/bin/python3 -I` so user-site packages and Python startup customizations cannot extend it.

If the optional `~/.local/bin/minimax-api-worker` command is installed, make it
a private regular copy of the audited runner, never a symlink. This preserves
the runner's self-hash/provenance checks and prevents link retargeting from
changing the executable after review.

Keep the Token Plan key in the user's Fish `claude-minimax` function as `ANTHROPIC_AUTH_TOKEN`. Accept exactly one literal from that function, require the `sk-cp-` prefix, and read it into memory only for the HTTPS Authorization header. Never put it in Codex config, subprocess environment, job manifest, log, patch, or output. Never substitute a standard pay-as-you-go API key.

MiniMax's current FAQ states that a Subscription Key uses included Token Plan quota first and automatically uses purchased Credits for eligible overflow when Credits exist. Responses documents no request field that disables this. Record that billing contract in every job. If zero additional Credit consumption is required, enforce it in the MiniMax account or an external quota guard; the runner cannot prove or enforce it.

Give Git subprocesses a fixed minimal environment without parent `KEY`, `TOKEN`, or `SECRET` variables and without global/system Git config. Require the Fish config to be user-owned and mode `0600`.

Keep test execution in the separate `minimax_patch_verify.py` process. That
verifier has no endpoint, Fish, token, or cloud operation. It invokes only the
adjacent audited runner's read-only `job status` and `job diff` commands, binds
the exact reviewed patch SHA-256, and never exposes a verification command to
MiniMax.

## Egress boundary

Accept only the canonical global endpoint:

```text
https://api.minimax.io/v1
```

Disable environment proxies and redirects. Include only:

- the stable worker instruction contract;
- the repository-relative task specification;
- allowed relative paths;
- selected UTF-8 file contents and hashes;
- non-sensitive job metadata.

Give MiniMax no shell, tools, MCP servers, plugins, browser, checkout path, Git history, credentials, broker state, or live-trading capability. Deny credential/state paths, the delegation trust boundary, financial and risk controls, broker/execution/live/promotion code, and deployment paths before snapshot creation.

Default to exact files. Directory selection requires explicit
`--allow-directory`, and delegable inputs are limited to pure `.py` plus
documentation text (`.md`, `.rst`, `.txt`). Structured fixtures/configuration
and other runtime languages are outside automated egress. Production credential
loading is fixed to `~/.config/fish/config.fish`; a different path is test-only.

For this repository, the authority closure also includes the complete
`configs/` and `scripts/` trees, the central CLI/config loader, approved-data
policy, `pyproject.toml`, Dockerfile, and Compose manifest. Deny them even when
their generic names would otherwise evade a token-based path rule. Reject
quoted/truthy live flags, protected financial-mode assignments, generated
secrets, and executable-file patches as defense in depth.

Treat content scanning as best-effort DLP. It reduces accidental disclosure but cannot prove that an arbitrary selected text is non-sensitive. Require Codex to inspect the spec, allowlist, skipped-file list, and egress manifest. Keep host-level filtering to `api.minimax.io:443` as defense in depth.

## State, provenance, and integrity

Root all state at `/tmp/minimax-api-worker-$UID/state`. Use `/tmp/minimax-api-worker-$UID/state/<repo-hash>` as the default per-repository state and a private `<job-id>` child for each job. Accept caller-selected state only as a direct marked child of that fixed state parent; never honor `TMPDIR`, `TEMP`, or `TMP` for state placement.

Record `source_basis=selected_working_tree_snapshot`, `source_head_commit`, and `source_snapshot_sha256`. Compute the latter from the exact selected-file manifest, including deliberately selected uncommitted contents. Seal the manifest, spec hash, state, and result with a per-state HMAC.

Publish creation only by atomically renaming a complete private `.creating-*`
sibling to its 20-hex job ID. Purge by first atomically renaming the job to a
private `.purging-*` tombstone and then deleting that tombstone. A read-only
`job list` operation may inspect only direct 20-hex job children, must not
create state or an HMAC key, and returns only public status/timestamps/version
and bounded usage. Cap listing at 512 direct entries, 256 sealed jobs, 16 MB of
manifest input, and JSON nesting depth 128. Fail closed on excess, tampering,
or a malformed manifest rather than exposing partial state.

Before API execution and patch export, require HEAD, spec hash, selected paths, file metadata, content hashes, and non-executable mode to match the sealed job. Each selected-file manifest entry contains `source_executable=false`, which participates in `source_snapshot_sha256`; chmod after creation invalidates run, diff, and verification. Treat any mismatch as tampering or stale provenance and require a new snapshot.

Apply provider output only to the isolated snapshot. Reject binary patches, symlinks, mode changes, renames, out-of-scope paths, excessive files, and oversized output. Create `provider-output.txt`, `provider.patch`, and `changes.patch` as exclusive no-follow private artifacts with directory/file identity checks and atomic no-replace publication. A prepositioned name, including a symlink, fails closed without truncating its referent. Freeze `changes.patch` and seal its SHA-256 in the job manifest.

Before credential loading, reject any prepositioned reserved result name and
disable core dumps/process dumpability. Reassert those controls in the provider
child, clear its environment, and arm Linux `PDEATHSIG=SIGKILL`. The key never
enters disk state, IPC envelopes, or errors. A transient private
`.response-watchdog-*` contains only sanitized response/error data; it is
normally unlinked and can remain only after a hard crash until job purge.

While holding the global API lock, create the global private
`.provider-request-unconfirmed.json` with no-follow/exclusive `0600` semantics
and fsync both the file and worker base before recording `RUNNING`, loading the
Fish key, forking, or permitting a POST. It contains only job/attempt,
endpoint/model, a canonical request fingerprint, the SHA-256 of the exact
`POST` method+URL+body, supervisor PID, and timestamp—never the token or source
body. Fsync the terminal sealed job manifest and job directory with
`request.outcome=confirmed|not_sent` before unlinking the marker and fsyncing
the base. An ambiguous or unconfirmed outcome keeps it durable.

Deny package initializers, startup hooks, plugin/entry-point surfaces, and
protected references to execution/risk, broker/live, environment credentials,
networking, subprocesses, filesystem I/O, or dynamic code execution. Delegation
is limited to pure Python modules: if either the sealed baseline or generated
version of a changed Python file contains a protected capability, reject the
entire candidate. The AST and regex surfaces explicitly include
`__builtins__`, reflection calls, `site`, `inspect`, `pickle`, `marshal`, and `pathlib`
(`Path.home`, `joinpath`, `read_text` and related operations), `io`, built-in
`open`, and `urllib3` managers/requests. This blocks activation of an existing
helper or inactive branch but is deliberately conservative, not complete
semantic or taint analysis. Before any candidate can execute,
Codex must review it and run validation in a network-denied sandbox with an
allowlisted environment containing no Fish, cloud, broker, or trading secrets.
If that runtime boundary is unavailable, reject rather than test the patch.

Let `job diff` export only from `PATCH_READY`. Accept no `--out`; create or verify exactly `/tmp/minimax-api-worker-$UID/exports/<job-id>.patch` without overwriting different content. Purge both job state and export after review. Never edit the source checkout in the runner.

## Codex-only verification boundary

After line-by-line review, require the verifier caller to repeat the exact
sealed patch SHA-256. Rebuild the test tree without copying the dirty checkout:

1. Materialize only tracked blobs from `source_head_commit` through one fixed
   `git cat-file --batch` process. Do not use checkout, clone, worktree, archive
   filters, Git hooks, fsmonitor, smudge/clean filters, or the real `.git`.
   Set `GIT_NO_LAZY_FETCH=1`, verify every returned OID/type/size/frame, and
   enforce a deadline while reading the batch stream so a partial/promisor
   repository cannot trigger network or hang verification.
2. Overlay only the selected working-tree files in the sealed manifest and
   recheck their byte counts and SHA-256 values. Unrelated dirty/untracked files
   remain absent; required uncommitted dependencies must have been selected.
3. Copy the frozen patch into private verifier state, run `git apply --check`,
   and apply it only to that projection. The source checkout is never a write
   target.
4. Mount the patched projection read-only at `/work` in Bubblewrap. Unshare the
   user, PID, IPC, UTS, cgroup, and network namespaces; disable nested user
   namespaces, drop all capabilities, expose only loopback, use a new `/proc`,
   minimal `/dev`, bounded tmpfs, synthetic HOME, fixed environment, read-only
   system/runtime paths, and no real home, repo, `/run`, sockets, Fish
   configuration, or credentials.
5. Accept direct JSON argv arrays only, never a shell command string. Run every
   focused/release command in a fresh sandbox and run release only after all
   focused commands pass. Enforce time/output limits and never fall back if
   Bubblewrap or network namespaces are unavailable.

Compile a mandatory sealed seccomp BPF with pinned libseccomp and pass its FD
to Bubblewrap. Fail closed if the library/filter is unavailable. The policy
denies keyring, ptrace/BPF/perf/io_uring/userfaultfd, cross-process memory,
mount/namespace/module/kexec and related high-risk syscalls; socket/socketpair
families are limited to UNIX, IPv4, and IPv6. Mask `/proc/keys` and
`/proc/key-users` with `/dev/null`. The result reports the deterministic policy
version/hash. This is kernel attack-surface reduction, not VM isolation.

For a trusted venv, mount only a validated base Python distribution and
site-packages through a private venv skeleton. Require every focused/release
Python command to use exactly `/runtime/venv/bin/python`, with `-S` and `-P`
before `-m`, `-c`, `--`, or a script. `.pth` files may remain physically in
the trusted dependency tree, but `-S` prevents their automatic loading; only
explicit `sitecustomize.py` and `usercustomize.py` hooks are denied by name.
Set `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`. With `--venv`, deny every other
executable. Without it, allow only exact `/usr/bin/python3` with `-I -S -P`.
Validate all mount-source parents against
symlink escape, then walk at most 100,000 site-packages entries and 16 GiB of
regular-file sizes while rejecting symlinks, cross-device `st_dev` boundaries,
sockets, FIFOs,
devices, `sitecustomize.py`, and `usercustomize.py`.
The `st_dev` scan detects ordinary cross-device mounts but does not prove the
absence of a same-device bind mount; host ownership/immutability remains required.

The verifier pins the adjacent v0.3.5 worker to SHA-256
`5fdd4dcf32e47ca5c27b169d7472ecd036cd7fcf7b90b0de6d15b4ea43b7498d`;
a self-declared version/hash in a job cannot substitute another adjacent
binary. Before any execution, the verifier copies the already-open audited
bytes into a mode-0400 sealed memfd and uses that same inherited FD for status
and diff; an adjacent-path swap cannot change executed bytes. The venv remains a trusted host dependency and can mutate after the
bounded walk but before/during the read-only bind. Pinning or copying its full
6+ GiB dependency tree is outside this bridge revision, so that residual must
be handled by host ownership, immutability, and environment lifecycle controls.

Captured command output remains inside the private projection lifetime. The
result exposes only phase, exit/timing status, byte count, and output hash; raw
output is not a repair delta and must never be forwarded to MiniMax
automatically. Delete the projection after every outcome. If deletion fails,
report the retained private path and reject the candidate.

Bubblewrap is not a VM and does not eliminate kernel-exploit or total resource
exhaustion risk. The namespace, capability, tmpfs, file-size, CPU, timeout, and
output controls are the minimum local boundary for this bounded LLM-generated
patch workflow. Host/platform policy can still require an exact elevated
execution approval; `sandbox_unavailable` is the only valid fallback.

Linux charges `RLIMIT_NPROC` across the host UID, not the new PID namespace.
Read the aggregate cgroup-v2 `pids.current` for the exact systemd UID slice;
that kernel counter includes threads and remains host-wide when a nested PID
namespace hides them from `/proc/*/task`. Set the child hard limit to that
current count plus 256, capped by 4096 and the inherited hard limit. If the
counter cannot be established reliably, the current count reaches the cap, or
the limit cannot be applied, fail before candidate execution. This is an
incremental host-UID guard, not a per-command cgroup quota.

Apply `RLIMIT_AS` at 4 GiB per process or the lower inherited hard limit, and
fail closed if it cannot be inspected or applied. It bounds one process's
virtual address space; together with the incremental task budget and timeout it
reduces, but does not eliminate, aggregate memory-exhaustion risk from children.

Because `/work` is read-only, tests that write caches, snapshots, reports, or
fixtures under the repository will fail. Redirect those writes to `/tmp` or the
synthetic HOME; do not solve them by mounting the real or projected checkout
writable.

Runtime states:

```text
CREATED -> RUNNING -> PATCH_READY | NO_CHANGE
                    -> RATE_LIMITED | FAILED
                    -> POLICY_REJECTED | RESPONSE_INVALID
```

## Concurrency and retry policy

Allow one MiniMax Responses job POST per user at a time through `/tmp/minimax-api-worker-$UID/.api-call.lock`. Wait only for the bounded queue timeout. If the slot is still busy, return an error while leaving the job `CREATED` and retriable; do not start a provider request or consume provider tokens. A later supervisor attempt may retry the same sealed job after revalidating it.

Every generative attempt also owns the durable global request guard described
above. Reconcile or remove it only while holding the global lock. A marker whose
referenced job is still `CREATED` proves the marker-first process died before
the durable `RUNNING` transition and may be cleared as `not_sent`. A terminal
sealed job may clear it only when the matching attempt/fingerprint records
`confirmed` or `not_sent`. `RUNNING`, missing, corrupt, mismatched, or
`ambiguous` state blocks all later POSTs; never auto-clear based on a dead PID.
`doctor` and `job list` expose only sanitized guard metadata, while `job purge`
refuses the referenced job. A PID-only poison remains an additional fail-closed
signal if watchdog termination cannot be confirmed.

Require every parsed or internal float timeout to be finite and in its bounded
range, with provider timeouts capped at 1800 seconds. Reject `nan`, positive/negative infinity, and non-positive execution
timeouts before lock waiting, provider I/O, resource-limit arithmetic, or
Bubblewrap startup; do not serialize non-finite numbers into JSON results.

Run provider I/O in a killable child under one parent monotonic wall deadline,
covering DNS, slow reads, retries, and backoff. Attempt `POST /v1/responses`
exactly once. Do not retry 429, timeout, ambiguous transport failure, or
provider error; an automatic replay can duplicate token consumption or work
whose remote completion is unknown. Allow bounded retries only inside the
total deadline for read-only `GET /v1/models` probes because they send no repository content and create no response.

This design prevents automatic replay after a same-boot supervisor crash, but
it cannot prove remote exactly-once execution because the provider contract has
no idempotency key. State under `/tmp` is not durable across host reboot; require
external provider/account reconciliation if cross-reboot ambiguity matters.

## Token policy

- Limit a snapshot to 80 files and 300 KB, serialized input to 400 KB, and output to 50,000 tokens; split larger work. These conservative limits keep normal requests well below the 1M context and long-context pricing region.
- Default to reasoning `none`; use `high` only for complex work or one repair.
- Keep `prompt_cache_key=codex-supervised-minimax-patch-v1` stable.
- Keep service tier `standard`; do not use priority or a standard pay-as-you-go key. Disclose possible purchased-Credit overflow on the Subscription Key.
- Record input, cached, output, and reasoning token usage without storing model reasoning.

MiniMax documents `POST /v1/responses/input_tokens`, but invoking it with a private job would transmit the same bundle a second time. v0.3.5 therefore uses conservative local byte/output ceilings and the actual usage returned by the single generative POST. This is less exact than the provider tokenizer but minimizes egress and request count.

MiniMax documents automatic caching and cached-token reporting for repeated
prefixes, with a 512-token minimum cacheable prefix:
<https://platform.minimax.io/docs/api-reference/text-prompt-caching>. Token Plan
deduction and its 5-hour/weekly windows remain provider-side account state; the
runner records actual response usage but does not predict remaining allowance.

For the audited v0.3.5 revision, the repository runner SHA-256 is
`5fdd4dcf32e47ca5c27b169d7472ecd036cd7fcf7b90b0de6d15b4ea43b7498d`.
The v0.3.1 verifier SHA-256 is
`84b233f55ecdea3e9219e16a9466609cf1c11044f45560d8d92d27edfc9dabef`.
Recompute and revalidate this evidence
after any runner or verifier edit; never treat a historical hash as an upgrade
bypass.

## Authorization and validation status

Automate the bounded workflow without a separate person per job only while user authorization and active platform network/data-egress policy permit it. Treat `--cloud-approved` as an audit record, not a way to bypass a managed approval or denial.

Tests use simulated provider responses and the non-sensitive synthetic canary passed. On
2026-07-14 a private-bundle POST attempt ended in `provider_unreachable`/`URLError` with
no provider response, usage, or patch. Delivery is unconfirmed, so the sealed job was
purged and must never be retried. A future private job requires a fresh sealed snapshot,
manifest review, and an active platform policy that explicitly permits the egress.

## Non-goals

Treat this bridge as a development worker, not a production-trading dependency and not evidence of profitability. Never let it approve research evidence, promotion gates, live deployment, orders, or money movement. Preserve `live_trading_allowed=false`. Govern any production service path through a separate commercial, security, and financial approval process.
