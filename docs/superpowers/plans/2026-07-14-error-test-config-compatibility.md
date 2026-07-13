# Error Test Config Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** エラーメールテストが本番PHPで必ず読み込める設定を生成し、通常通知を停止させずに障害・復旧通知を検証できるようにする。

**Architecture:** 管理CLI側で本番 `NotifierConfig` と同じトークン長・日時正規形・件名完全一致を生成し、CAS前に同等の検証を行う。Pythonの失敗テストで本番障害を再現し、既存PHPテストの有効設定契約と揃える。

**Tech Stack:** Python 3 `unittest`, PHP 8.5, Xserver private-config CAS

## Global Constraints

- テストトークンは `ERRTEST-` で始まり、全体が `[A-Za-z0-9_-]{32,128}` に一致する。
- UTC期限は `YYYY-MM-DDTHH:MM:SS+00:00` の正規形で保存し、末尾 `Z` は保存しない。
- 利用者へ案内する件名は `[Error Test TOKEN]` の完全一致とする。
- 無効なテスト設定をリモートCASへ送らない。
- Webhook URL、通知対象、エラー通知先、公開領域には変更を加えない。

---

### Task 1: エラーテスト設定の本番互換化

**Files:**
- Modify: `manager/manage.py`
- Modify: `tests/python/test_manager.py`

**Interfaces:**
- Consumes: `MailManager._default_error_mail_test()`, `MailManager._validate_runtime_config()`
- Produces: 本番 `NotifierConfig::fromArray()` が受理する期限・トークン、完全一致件名の案内

- [ ] **Step 1: Write the failing tests**

`tests/python/test_manager.py` に、実ジェネレータが32〜128文字の許可文字だけを返すこと、期限が `+00:00` になること、案内が `[Error Test TOKEN]` の完全一致であること、短い固定トークンをCAS前に拒否することを追加する。

- [ ] **Step 2: Run tests to verify RED**

Run: `python3 -m unittest tests.python.test_manager.ManagerTest.test_error_mail_test_sets_expiring_subject_scoped_failure_after_exact_confirmation tests.python.test_manager.ManagerTest.test_error_mail_test_default_generator_is_runtime_compatible tests.python.test_manager.ManagerTest.test_error_mail_test_rejects_invalid_token_before_cas -v`

Expected: 既存の `Z`、24文字生成、曖昧な案内、CAS前検証不足のためFAIL。

- [ ] **Step 3: Write minimal implementation**

`manager/manage.py` で既定生成を `"ERRTEST-" + secrets.token_urlsafe(24)` に変更し、期限は `expires_at.isoformat(timespec="seconds")` をそのまま保存する。`_validate_runtime_config()` にtestフィールドのペア、正規表現、UTC正規日時を追加し、`_default_error_mail_test()` はCAS前に検証済み設定を用いる。案内には完全な件名 `[Error Test TOKEN]` を表示する。

- [ ] **Step 4: Run tests to verify GREEN**

Run: `python3 -m unittest tests.python.test_manager -v`

Expected: PASS。

- [ ] **Step 5: Run cross-runtime verification**

Run: `php tests/php/test_delivery.php`

Expected: PASS。続けて `bash tests/run-all.sh` が全体PASS（明示的なlive SSH opt-in skipのみ許可）。

- [ ] **Step 6: Commit**

```bash
git add manager/manage.py tests/python/test_manager.py docs/superpowers/plans/2026-07-14-error-test-config-compatibility.md
git commit -m "fix: keep error test config runtime compatible"
```
