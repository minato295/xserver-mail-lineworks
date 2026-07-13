# Xserver Fixed Runtime Migration Design

## Problem

The fixed bootstrap and release validators were originally publish-once. `provision_fixed_runtime()` therefore rejects an existing tree whose files differ from the new source. The stdin-frame repair changes three fixed files, so the normal release workflow stops before staging and leaves the active locator unchanged.

## Compatibility bootstrap

The stable bootstrap selects the protocol only after completely validating the active locator, release manifest, entrypoint, and runtime records. If that verified active release contains the exact manifest record for `src/StdinFrame.php`, it sends the verified config and raw mail as the stdin frame. An older verified release without that record uses the legacy config-FD path. A release that declares the frame decoder but cannot pass framed remote validation is never switched active; frame construction or decoding errors fail closed and never downgrade to legacy.

This marker is deliberately limited to the known migration from the exact legacy release generation to the reviewed frame generation. A future manifest schema should replace it with an explicit `entrypoint_protocol` field.

## Resumable fixed-tree migration

Only these fixed files may change, in this order:

1. `src/ReleaseValidator.php`
2. `bootstrap/validate-release.php`
3. `bootstrap/mail-forward-command.php` (dual-protocol bootstrap, last)

Before the first live replacement, the complete legacy fixed tree plus the pinned private-config helper must match its signed manifest. The three legacy bytes are read only after hash/mode verification and published to a generation-specific private backup tree outside `public_html`, with directories 700 and files preserving 600/700. The backup tree is fully read back before live mutation and remains the durable rollback source.

For each live file, the current whole-tree state must equal exactly one allowed prefix state. The file is verified against the expected old hash immediately before atomic replacement, the replacement bytes are read back, and the resulting whole tree is verified against the next prefix manifest. Process termination leaves one complete prefix state; rerunning detects that state and resumes forward. Unknown hashes, non-prefix combinations, missing backup, symlinks, loose modes, or extra files stop without further mutation. An indeterminate rename result is resolved by readback, never blind retry.

Mixed validator states are not used for release deployment. The old stable bootstrap remains live until both validators are new. Existing managers see a non-exact fixed tree and fail closed. Concurrent identical migration attempts are convergent because every transition is an idempotent old-or-new hash check and atomic same-file replacement; the generation backup is the durable migration marker.

## Rollout and rollback

After the full new fixed tree is exact, the ordinary workflow stages and remotely validates the immutable new release. It then switches the application locator atomically. The permanent mail filters, mailbox, config, dedup state, and logs are outside this migration and remain unchanged.

Rollback first switches the application locator to the previous release. The dual bootstrap then selects the legacy path. If fixed files must also be rolled back, restore the stable bootstrap first from the verified backup, then the validator script and validator class, verifying every prefix in reverse.

## Tests and completion

Tests cover old active release with dual bootstrap, new active release with frame, frame capability validation failure without downgrade, every forward interruption point and resume, unknown/non-prefix state rejection, backup permissions/hash/readback, reverse rollback order, permanent filter/config/mailbox path invariance, and full new-tree exact verification. Production completion additionally requires normal permanent-filter E2E with a new Message-ID and a new HTTP 200 operational event.

## Production-discovered classifier boundary

The system-PHP fixed-runtime inspector deliberately treats a known path whose
hash, size, type, or mode differs from the caller's expected manifest as a
failed inspection, not `PARTIAL`. `PARTIAL` means only that the actual tree is
an exact subset of the expected tree. The migration workflow must preserve this
strict inspector contract.

When the initial comparison with the new fixed manifest fails, the workflow may
enter only the pinned migration classifier. It compares the live tree against
all four signed prefix manifests, treating an inspection failure as “not this
candidate.” Exactly one `EXACT` candidate is required before any backup,
upload, deletion, or live replacement. Zero or multiple exact candidates fail
closed. Before every live replacement the workflow rechecks the current whole
tree against its expected prefix, in addition to the existing per-file and
backup checks. Transport failures, unsafe paths, symlinks, unknown files, and
unapproved byte combinations therefore cannot authorize mutation.
