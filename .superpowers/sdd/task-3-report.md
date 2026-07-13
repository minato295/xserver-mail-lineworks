# Task 3 Report: Notification previews

## Status

Complete. Implemented notification-only quote trimming, sanitized notification titles, attachment ordering, and delivery title propagation.

## TDD evidence

- Quote fixtures first failed because `XserverMail\QuoteTrimmer` did not exist.
- Title/order fixtures first failed because `NotificationFormatter::title()` did not exist.
- A self-review regression fixture for tag-like text inside quoted HTML attributes failed before the tokenizer correction.
- A whitespace-only title fallback fixture failed before title normalization was tightened.

## Implementation

- Added bounded plain-text and HTML quote trimming in `src/QuoteTrimmer.php`.
- Kept `MailMessage::body` as the normalized full body and added `notificationBody` immediately after it.
- Plain notifications remove standalone `>` lines and stop at qualified reply boundaries; HTML notifications remove only `blockquote` and exact whitespace-delimited `gmail_quote` class tokens.
- Added fixed quote-only fallback `（引用部分は省略しました）`.
- Added `NotificationFormatter::title()` with fixed empty-value fallbacks, control removal, valid UTF-8 replacement, and a 100-code-point limit.
- Formatter now uses `notificationBody` and places attachment metadata after Bcc and before subject.
- Delivery now passes the formatter-produced title to `WebhookClient`.
- Updated every `MailMessage` constructor call site.

## Verification

- `php tests/php/test_mail_parser.php` — PASS
- `php tests/php/test_delivery.php` — PASS
- `bash tests/run-all.sh` — PASS; 359 Python tests, one explicit opt-in skip, PHP suites, syntax checks, and public secret scan all passed.

## Concerns

None. No real webhook URL, account data, credential, or other secret was added.

## Reviewer follow-up

- RED: a 2k/4k malformed repeated-tag scaling fixture failed because quote matching rescanned the remaining suffix for each candidate and lost text after the quote.
- RED: a script raw-text decoy containing `"<blockquote>"` hid the following notification text.
- RED: ordinary `On project wrote:` prose was incorrectly treated as an English reply boundary.
- GREEN: HTML trimming now uses one forward tokenizer pass, treats comments and script/style bodies as opaque, and recovers from malformed attributes without rescanning suffixes.
- GREEN: the retained scaling fixture was expanded to 20k/40k malformed units with a deliberately loose linear-scaling ratio bound.
- GREEN: English reply boundaries now require a recognizable date, time, and sender segment while preserving the existing dated positive fixture.
- RED: `textarea` and `title` RCDATA containing a tag-like `<blockquote>` each hid the following notification text; both failures were confirmed independently by changing fixture order.
- GREEN: the forward tokenizer now treats `textarea` and `title` through the same opaque matching-end path as script/style, while the existing HTML-to-plain conversion remains unchanged.
- Test stability: the 20k/40k scaling check now takes the fastest of three runs at each size, retaining the 3.5x bound while avoiding scheduler-only failures.
- RED: parameterized `xmp`/`iframe`/`noembed`/`noframes`/`noscript` fixtures all lost text following tag-like `<blockquote>` content; the `plaintext` EOF fixture independently lost content that must remain text.
- GREEN: tokenizer classification now explicitly fixes script data, RAWTEXT (including conservative scripting-enabled `noscript` handling), RCDATA, and the distinct `plaintext`-through-EOF state as opaque to quote recognition.
