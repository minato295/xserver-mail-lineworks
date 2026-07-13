# Immutable validator deployment compatibility report

## Root cause

Commit `ab2a785` correctly made the Python stable-manifest builder inspect verified
source bytes and omit PHP-named templates such as PHP-DI's
`src/Compiler/Template.php`. It also added the same filtering to
`src/ReleaseValidator.php`. That second change was unnecessary because the
bootstrap and validator consume the already-filtered stable manifest, and it
changed a file in the production fixed runtime. `provision_fixed_runtime()`
intentionally requires that existing fixed runtime to match byte-for-byte, so a
normal deployment could not replace the immutable validator.

## Change

- Restored `src/ReleaseValidator.php` exactly to its pre-feature bytes (SHA-256
  `c026b6d5b12ad1f89622abc1904aef0bf21ed6f9432554e2dadc876cc4073766`).
- Removed only the PHP validator fixtures/assertions that depended on its
  redundant executable-source filtering.
- Added a workflow regression that pins the fixed validator to that immutable
  baseline for this feature.
- Retained Python stable-manifest filtering and verified-source binding, the
  exact PHP-DI template regression, strict entrypoint/preload rejection, stable
  bootstrap invalid-opening tests, workflow coverage, and existing security
  checks.

## TDD evidence

The new immutable-baseline regression first failed against `ab2a785`, reporting
the expected baseline digest `c026…3766` and current digest `cd17…9535`. After
removing only the validator filtering branch/helper, the focused test passed.

## Verification

- Focused workflow/deployer: 38 Python tests passed.
- Focused PHP validator and stable bootstrap suites passed.
- Full `bash tests/run-all.sh`: 329 tests passed, 1 explicit opt-in real-Mac SSH
  contract test skipped.
- PHP syntax checks, Python compile checks, Composer metadata validation, and the
  public secret scan passed.
- `git diff --check` passed.

## Concerns

The SHA-256 regression deliberately makes any future `ReleaseValidator.php`
upgrade explicit. Such an upgrade still requires a separate fixed-runtime
generation/migration design; normal release deployment must not overwrite it.
