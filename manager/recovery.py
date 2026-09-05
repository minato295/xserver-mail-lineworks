"""Explicit selected recovery through the existing authenticated SSH boundary."""
import json
import re
import shlex
from datetime import datetime, timedelta, timezone

try:
    from manager import private_config_ssh, remote_validator
except ModuleNotFoundError:
    import private_config_ssh
    import remote_validator

_HASH = re.compile(r'[a-f0-9]{64}\Z')
_FIELDS = {'id', 'revision', 'state', 'created_at', 'expires_at', 'completed_chunks',
           'total_chunks', 'classification', 'retryable'}


class RecoverySsh:
    def __init__(self, ssh_alias, filesystem_home, *, expected_hosts, runner=None):
        # Recovery is not a private-config operation. Reuse only the non-secret
        # validation policy and trusted SSH boundary, not the mutable config client.
        if (type(filesystem_home) is not str
                or private_config_ssh._HOME.fullmatch(filesystem_home) is None):
            raise ValueError('filesystem_home is invalid')
        if (type(expected_hosts) is not list or not expected_hosts
                or len(set(expected_hosts)) != len(expected_hosts)
                or any(type(host) is not str or private_config_ssh._HOST.fullmatch(host) is None
                       for host in expected_hosts)):
            raise ValueError('expected_hosts is invalid')
        bootstrap = filesystem_home + '/private/xserver-mail-lineworks/bootstrap/mail-forward-command.php'
        self.remote_command = '/usr/bin/php8.5 ' + shlex.quote(bootstrap)
        self.expected_hosts = list(expected_hosts)
        self.validator = remote_validator.RemoteValidator(
            ssh_alias, runner if runner is not None else remote_validator.bounded_subprocess_run)

    @staticmethod
    def _unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('duplicate')
            value[key] = item
        return value

    def _call(self, arguments):
        # Every argument is either a fixed literal or prevalidated lowercase hex.
        output = self.validator.run_trusted(self.remote_command + ' ' + ' '.join(arguments), b'',
            expected_hosts=self.expected_hosts, output_limit=131072)
        try:
            if type(output) is not bytes or len(output) > 131072: raise ValueError()
            value = json.loads(output.decode('utf-8'), object_pairs_hook=self._unique_object)
            if type(value) is not dict or type(value.get('schema_version')) is not int or value['schema_version'] != 1: raise ValueError()
            return value
        except (ValueError, TypeError, UnicodeDecodeError):
            raise RuntimeError('未送信通知の応答を確認できません。') from None

    def list_items(self):
        value = self._call(['--outbox-list'])
        if set(value) != {'schema_version', 'items'} or type(value['items']) is not list or len(value['items']) > 100:
            raise RuntimeError('未送信通知の応答を確認できません。')
        seen = set()
        for row in value['items']:
            valid = (type(row) is dict and set(row) == _FIELDS
                and all(type(row.get(k)) is str and _HASH.fullmatch(row[k]) for k in ('id','revision'))
                and type(row.get('state')) is str and row['state'] in {'pending','in_flight','review_only','delivered','expired'}
                and type(row.get('created_at')) is int and row['created_at'] >= 0
                and type(row.get('expires_at')) is int and row['expires_at'] == row['created_at'] + 604800
                and (row['completed_chunks'] is None or type(row['completed_chunks']) is int and row['completed_chunks'] == 0)
                and row['total_chunks'] is None
                and type(row.get('classification')) is str and row['classification'] in {'definite_rejection','success','review_required'}
                and type(row.get('retryable')) is bool and row['retryable'] == (row['state'] == 'pending'))
            if not valid or row['id'] in seen:
                raise RuntimeError('未送信通知の応答を確認できません。')
            if row['retryable'] and (row['completed_chunks'] != 0 or row['classification'] != 'definite_rejection'):
                raise RuntimeError('未送信通知の応答を確認できません。')
            seen.add(row['id'])
        return value['items']

    def retry_selected(self, ident, revision):
        if any(type(v) is not str or not _HASH.fullmatch(v) for v in (ident, revision)):
            raise ValueError('再送の選択が不正です。')
        # Never retry this SSH operation automatically, even on a timeout.
        value = self._call(['--outbox-retry', ident, revision])
        if set(value) != {'schema_version', 'id', 'status'} or value['id'] != ident or value['status'] not in {'delivered','not_delivered'}:
            raise RuntimeError('再送結果が不明です。一覧で状態を再確認してください。')
        return value


def show_recovery_menu(client, input_fn, output_fn):
    """No send occurs without listing, selecting a safe row, and exact confirmation."""
    try:
        rows = client.list_items()
        if not rows:
            output_fn('保存された未送信通知はありません。')
            return
        for index, row in enumerate(rows, 1):
            label = ('未送信確定・選択再送可' if row['retryable'] else
                     {'delivered':'送信済み・再送不可','expired':'保持期限切れ・再送不可'}.get(row['state'], '一部送信または結果不明・再送不可'))
            created = datetime.fromtimestamp(row['created_at'], timezone(timedelta(hours=9)))
            timestamp = created.strftime('%Y年%m月%d日 %H:%M:%S JST')
            output_fn(f"{index}. {timestamp} ID:{row['id'][:12]} {label}")
        output_fn('再送する番号を入力（空欄で戻る）:')
        selection = input_fn().strip()
        if not selection: return
        if not selection.isascii() or not selection.isdigit() or not 1 <= int(selection) <= len(rows):
            output_fn('番号が正しくありません。')
            return
        row = rows[int(selection)-1]
        if not row['retryable']:
            output_fn('この通知は再送不可です。送信先で実際の状態を確認してください。')
            return
        phrase = '再送 ' + row['id']
        output_fn('この1件を現在の設定で送るには「' + phrase + '」と入力:')
        if input_fn() != phrase:
            output_fn('再送を中止しました。')
            return
        result = client.retry_selected(row['id'], row['revision'])
        output_fn('LINE WORKSの受付成功を確認しました。' if result['status'] == 'delivered' else '再送は成功していません。一覧で状態を再確認してください。')
    except (RuntimeError, ValueError, OSError):
        output_fn('未送信通知の操作を完了できませんでした。自動再試行せず、一覧で状態を再確認してください。')
