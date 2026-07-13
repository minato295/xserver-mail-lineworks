# Dedup Empty-State Repair Design

## Problem

`DeliveryDeduplicator::readClaims()` correctly requires a JSON object whose
keys are message-id hashes. `writeClaims()` currently applies `json_encode()`
directly to a PHP array. When releasing the last reservation, the empty array
is serialized as `[]`; the next operation rejects that value because it is not
a JSON object. Delivery then fails open, logging `dedup_store_failure` and
sending the webhook without duplicate suppression.

## Design

Keep the strict reader unchanged and serialize the claim map as a JSON object
on every write by encoding `(object) $claims`. Non-empty hash maps retain the
same JSON representation; only the empty state changes from invalid `[]` to
valid `{}`. No existing claim, webhook payload, configuration, mailbox, filter,
or log format changes.

The production empty-list state is repaired only after the new code is active,
using an exact SHA-256/size/mode/owner guard and an atomic private-file replace.
The repair is refused for every other state. A no-webhook reserve/suppress/
release cycle must finish with a valid empty object and remain reusable.

## Tests and completion

- A new store, release of the last reservation, and expiry/prune paths always
  persist a JSON object.
- Strict rejection of externally supplied JSON lists remains unchanged.
- Existing concurrency, mode, symlink, and atomic-write tests remain green.
- Full suite and public secret scan pass before deployment.
- Production repair and a no-webhook dedup cycle pass before the final external
  mail observation.
