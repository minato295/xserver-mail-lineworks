# Runtime manifest PHP-template fix report

## Root cause

The stable-manifest builder and `ReleaseValidator::fdRuntime()` selected runtime
files solely by the `.php` suffix. Composer package completeness legitimately
includes PHP-named templates such as
`vendor/php-di/php-di/src/Compiler/Template.php`, whose first bytes are template
text rather than an executable PHP opening tag. The builder therefore placed
that file in the stable runtime manifest, while the stable bootstrap correctly
rejected it under its existing opening contract.

## Opening contract and change

Executable runtime PHP must begin at byte zero with `<?php`, followed by PHP
whitespace or EOF. This deliberately rejects UTF-8 BOMs, leading whitespace,
short echo tags, and template/text prefixes. It is the same contract already
enforced by `bin/stable-mail-entrypoint.php`.

- `build_stable_manifest()` now receives the release source root and verifies
  each candidate's regular-file identity, size, and SHA-256 against the
  canonical upload manifest before inspecting its bytes.
- PHP-named non-code files are omitted only from the stable execution manifest;
  `build_manifest()` remains unchanged, so those files are still uploaded and
  validated for package completeness.
- Entrypoints and explicitly requested preloads remain strict and fail closed
  unless they satisfy the executable opening contract.
- `ReleaseValidator::fdRuntime()` applies the same byte-opening predicate before
  token parsing and class discovery. Non-runtime templates are skipped, while
  valid source files (including `XserverMail\\NotifierConfig`) remain available
  through the verified-FD class map.

## TDD evidence

The initial focused Python regression run failed with two `TypeError` errors
because the builder had no content-aware `source_root` contract. After the
minimal implementation, the four focused stable-manifest tests passed.

Coverage now includes the exact PHP-DI `Compiler/Template.php` path, generic
template text, UTF-8 BOM, leading whitespace, valid byte-zero PHP, strict
entrypoint/preload rejection, canonical upload completeness, stable-bootstrap
runtime rejection, and release-validator class discovery with non-code files
present.

## Verification

- Focused bootstrap, validator, deployer, and workflow regression tests passed.
- `git diff --check` passed.
- `bash tests/run-all.sh` passed: 328 tests, 1 explicit opt-in SSH contract test
  skipped; PHP syntax/tests, Python compile checks, and the public secret scan
  all passed.

## Concerns

No known correctness concern remains. The byte-zero policy intentionally does
not support BOM-prefixed or leading-whitespace PHP source; changing that policy
would require coordinated changes to the builder, release validator, and stable
bootstrap contract.
