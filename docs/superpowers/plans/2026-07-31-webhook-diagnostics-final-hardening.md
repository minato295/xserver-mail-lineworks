# Webhook Diagnostics Final Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Webhook診断のFTPS読取り、retry、producer契約、ログ容量、consumer表示を同じfail-closed契約へ揃える。

**Architecture:** FTPSはRFC reply envelopeとfact entryを分離して解析する。Webhook送信は1 payload最大2回の状態機械、OperationalLoggerは書込み前のDTO検証とEX lock下の240 KiB compactionを担い、Mac consumerは同じ制御文字・相関契約で読む。

**Tech Stack:** PHP 8.2、Python 3.11+、`unittest`、JSON Lines、Explicit FTPS。

## Global Constraints

- 外部通信、本番配備、Webhook送信、メール送信を行わない。
- 親directory `0700`、log `0600`、実効UID owner、通常ファイル、`nlink=1`を維持する。
- Macの読取り上限256 KiBに対し、server logは240 KiB以下、1 eventは120 KiB以下とする。
- 不正診断は一切追記せず、ログ障害時もメール配送はfail-openとする。

---

### Task 1: RFC MLST reply parser

**Files:**
- Modify: `manager/ftps_deployer.py`
- Test: `tests/python/test_ftps_deployer.py`

**Interfaces:**
- Consumes: `_verify_mlst_mode(response: str, remote_path: str, expected_mode: str)`
- Produces: 任意250 reply textを許容し、fact entryを厳密に1件へ束縛するparser。

- [ ] **Step 1: Write failing tests** — `250-Status follows` / `250 Complete`を持つ複数の正規replyを受理し、複数entry、fact-like malformed行、途中の偽250 envelopeを拒否するfixtureを追加する。
- [ ] **Step 2: Verify RED** — `python3 -m unittest tests.python.test_ftps_deployer -v`で任意reply textの正規fixtureが失敗することを確認する。
- [ ] **Step 3: Implement parser** — reply code構造を先に検証し、内部行のうちfact grammarを満たす行だけをentry化する。`=`または`;`を含むfact-like malformed行は拒否し、path/type/modeを完全一致させる。
- [ ] **Step 4: Verify GREEN** — 同じmoduleの全testを通す。

### Task 2: Two-attempt Webhook state machine and echo defense

**Files:**
- Modify: `src/WebhookClient.php`
- Test: `tests/php/test_delivery.php`

**Interfaces:**
- Produces: 初回429または5xxから最大1回だけ同一payloadへ遷移する`requestWithRateLimitRetry()`。

- [ ] **Step 1: Write failing tests** — `429→500`、`500→429`、不正reset、provider descriptionによる8文字以上のpayload断片echoを追加し、送信回数、sleep、保存descriptionをliteralで検証する。
- [ ] **Step 2: Verify RED** — `php tests/php/test_delivery.php`でcross-statusが3回送信されることとechoが残ることを確認する。
- [ ] **Step 3: Implement state machine** — 初回statusだけからretry種別とdelayを決定し、2回目結果を無条件でterminalにする。固定provider説明以外のpayload断片echoはdiagnostic上`null`にする。
- [ ] **Step 4: Verify GREEN** — delivery testを通す。

### Task 3: OperationalLogger strict contract

**Files:**
- Modify: `src/OperationalLogger.php`
- Test: `tests/php/test_delivery.php`

**Interfaces:**
- Produces: `WebhookDiagnostic`のattempt全件とevent相関をJSON化前に検証するprivate validator。

- [ ] **Step 1: Write failing tests** — status範囲、負寸法、空/異型attempt、format/hash/metadata相関、outcome/classification、recovered retryの不正tableを追加し、各呼出し前後のlog bytes完全一致を検証する。
- [ ] **Step 2: Verify RED** — delivery testで不正DTOが追記されることを確認する。
- [ ] **Step 3: Implement validator** — consumerと同じ分類・成功・failure・transport・invalid JSON・retry相関を検証し、選択済provider値だけを再boundする。
- [ ] **Step 4: Verify GREEN** — 既存producer全patternを含むdelivery testを通す。

### Task 4: Owner-safe bounded compaction

**Files:**
- Modify: `src/OperationalLogger.php`
- Test: `tests/php/test_delivery.php`

**Interfaces:**
- Produces: EX lock下で240 KiB preflightと完全JSONL tail compactionを行うappend path。

- [ ] **Step 1: Write failing tests** — 240 KiB境界、最新既存行＋新規行の保持、malformed/partial行の除外、mode/inode/owner/nlink維持、truncate前faultで旧tailが残ることを追加する。
- [ ] **Step 2: Verify RED** — 現行unbounded appendが240 KiBを超えることを確認する。
- [ ] **Step 3: Implement compaction** — tailを最大240 KiBだけbounded readし、有効な完全JSON object行を予算内で選ぶ。先頭write→flush→truncate→flushの順とし、各境界でidentityを再検証する。
- [ ] **Step 4: Verify GREEN** — delivery testとlogger fail-open integrationを通す。

### Task 5: Consumer and operations documentation

**Files:**
- Modify: `manager/manage.py`
- Modify: `tests/python/test_manager.py`
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-07-31-webhook-failure-diagnostics-design.md`

**Interfaces:**
- Produces: descriptionのC0/DEL/C1 fail-closed、保持・retry・trust boundary・日中preflightの日本語運用契約。

- [ ] **Step 1: Write failing consumer test** — C0、DEL、C1の各descriptionを正規17項目fixtureへ入れ、固定安全エラーを期待する。
- [ ] **Step 2: Verify RED** — C0 fixtureだけが受理される現状を確認する。
- [ ] **Step 3: Implement and document** — consumer validatorを`[\\x00-\\x1f\\x7f-\\x9f]`拒否へ統一し、READMEへ240 KiB保持、日中preflight、at-least-onceの5xx重複可能性、provider応答trust boundaryを追記する。
- [ ] **Step 4: Verify all** — `tests/run-all.sh`、`git diff --check`、public secret scanを通す。
