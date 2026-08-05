# Paper executor systemd package

These files describe the intended **paper-only** deployment. They are not
installed or enabled by the repository. There is deliberately no socket unit,
timer, `EnvironmentFile`, plaintext credential path, stale-socket deletion, or
`open` capability.

Provisioning must run as root on a staging host:

1. Install `trading-ai-paper.conf` under `/usr/lib/sysusers.d/` and run
   `systemd-sysusers`. Keep the resulting UIDs stable for the lifetime of the
   executor ledger.
2. Render `executor-authz.yml.in` with the actual values of
   `id -u trading-ai-monitor` and `id -u trading-ai-safety`. Install the result
   as `/etc/trading-ai-paper/executor-authz.yml`, `root:root`, mode `0444`.
   Install the exact reviewed risk and universe files in the same root-owned,
   non-writable directory.
3. Install the Python package and all locked dependencies under a root-owned
   release. The unit assumes that the reviewed package is importable by
   `/usr/bin/python3 -I`; adjust this path only in the packaged unit, never at
   runtime.
4. Create the two `LoadCredentialEncrypted=` artifacts with `systemd-creds` in
   the system encrypted credential store. Do not copy Alpaca credentials from
   Fish, zsh, `.env`, an `EnvironmentFile`, or a caller service.
5. Run `systemd-analyze verify` and `systemd-analyze security` against the
   installed unit. Test with real distinct UIDs and paper credentials before
   any cutover.

`RuntimeDirectoryPreserve=no` is part of the safety contract. The daemon never
guesses whether an existing socket is stale and never unlinks it during
startup. systemd owns cleanup between service instances.

The monitor and safety callers must not receive any `LoadCredential*` entry.
The safety identity can reduce, cancel, or latch the kill switch; neither
identity can open or increase a position. Production/live activation remains
out of scope and `live_trading_allowed=false` remains mandatory.
