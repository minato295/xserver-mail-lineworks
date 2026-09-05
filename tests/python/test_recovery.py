import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch


class RecoveryTest(unittest.TestCase):
    def test_startup_check_is_fixed_read_only_command_and_rejects_output(self):
        from manager.recovery import RecoverySsh
        with patch('manager.remote_validator.RemoteValidator') as validator:
            client = RecoverySsh('safe-alias', '/home/example', expected_hosts=['host.example.invalid'])
        remote = validator.return_value
        remote.run_trusted.return_value = b''
        self.assertIsNone(client.check_startup())
        remote.run_trusted.assert_called_once_with(
            '/usr/bin/php8.5 /home/example/private/xserver-mail-lineworks/bootstrap/mail-forward-command.php --check-config',
            b'', expected_hosts=['host.example.invalid'], output_limit=1024)
        remote.run_trusted.return_value = b'unexpected'
        with self.assertRaises(RuntimeError):
            client.check_startup()

    def test_cold_recovery_import_during_mocked_config_bootstrap_does_not_poison_client(self):
        # Reproduce the discovery-order failure in a fresh interpreter. Only the
        # private-config client is mocked during first import; recovery must still
        # use its own real validation and the trusted SSH boundary afterward.
        script = '''
import json
from unittest.mock import patch
with patch('manager.private_config_ssh.PrivateConfigSsh', side_effect=[object()]):
    from manager.recovery import RecoverySsh
with patch('manager.remote_validator.RemoteValidator') as validator:
    client = RecoverySsh('safe-alias', '/home/example', expected_hosts=['host.example.invalid'])
validator.return_value.run_trusted.return_value = b'{"schema_version":1,"items":[]}'
assert client.list_items() == []
assert validator.return_value.run_trusted.call_args.args[0] == '/usr/bin/php8.5 /home/example/private/xserver-mail-lineworks/bootstrap/mail-forward-command.php --outbox-list'
'''
        result = subprocess.run([sys.executable, '-c', script],
            cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_invalid_connection_metadata_is_rejected_before_ssh(self):
        from manager.recovery import RecoverySsh
        with patch('manager.remote_validator.RemoteValidator') as validator:
            for home, hosts in [('/home/example;id', ['host.example.invalid']),
                                ('/home/example', []),
                                ('/home/example', ['host.example.invalid'] * 2),
                                ('/home/example', ['Host.example.invalid'])]:
                with self.subTest(home=home, hosts=hosts), self.assertRaises(ValueError):
                    RecoverySsh('safe-alias', home, expected_hosts=hosts)
        validator.assert_not_called()

    def test_manager_menu_16_lists_without_sending(self):
        from manager.manage import MailManager
        class Client:
            def list_items(self): return []
        answers=iter(['16','0']); output=[]
        manager=MailManager(object(),object(),'/home/example/private/xserver-mail-lineworks/bootstrap/mail-forward-command-701.php',
            input_fn=lambda _:next(answers),output_fn=output.append,recovery_client=Client())
        manager.run()
        self.assertIn('保存された未送信通知はありません。',output)

    def test_selected_retry_uses_trusted_fixed_command_and_no_automatic_retry(self):
        from manager.recovery import RecoverySsh
        with patch('manager.remote_validator.RemoteValidator') as validator:
            client = RecoverySsh('safe-alias', '/home/example', expected_hosts=['host.example.invalid'])
        remote = validator.return_value
        remote.run_trusted.return_value = json.dumps({'schema_version': 1, 'items': []}).encode()
        self.assertEqual([], client.list_items())
        remote.run_trusted.assert_called_once_with('/usr/bin/php8.5 /home/example/private/xserver-mail-lineworks/bootstrap/mail-forward-command.php --outbox-list', b'', expected_hosts=['host.example.invalid'], output_limit=131072)
        remote.run_trusted.reset_mock()
        with self.assertRaises(ValueError):
            client.retry_selected('A' * 64, 'b' * 64)
        remote.run_trusted.assert_not_called()
        remote.run_trusted.side_effect = RuntimeError('timeout')
        with self.assertRaises(RuntimeError):
            client.retry_selected('a' * 64, 'b' * 64)
        self.assertEqual(1, remote.run_trusted.call_count)

    def test_menu_requires_exact_selected_confirmation_and_refuses_review_only(self):
        from manager.recovery import show_recovery_menu
        class Client:
            calls = []
            def list_items(self): return [dict(id='a'*64, revision='b'*64, state='review_only', retryable=False,created_at=1700000000), dict(id='c'*64,revision='d'*64,state='pending',retryable=True,created_at=1700000000)]
            def retry_selected(self, ident, revision): self.calls.append((ident,revision)); return {'status':'delivered'}
        client=Client(); output=[]
        show_recovery_menu(client, iter(['1']).__next__, output.append)
        self.assertEqual([],client.calls)
        self.assertTrue(any('再送不可' in line for line in output))
        self.assertTrue(any('2023年11月15日 07:13:20 JST' in line for line in output))
        show_recovery_menu(client, iter(['2','yes']).__next__, output.append)
        self.assertEqual([],client.calls)
        show_recovery_menu(client, iter(['2','再送 '+ 'c'*64]).__next__, output.append)
        self.assertEqual([('c'*64,'d'*64)],client.calls)

    def test_metadata_rejects_secret_fields_and_invalid_retryability(self):
        from manager.recovery import RecoverySsh
        with patch('manager.remote_validator.RemoteValidator') as validator:
            client=RecoverySsh('safe-alias','/home/example',expected_hosts=['host.example.invalid'])
        remote=validator.return_value
        for item in [dict(title='secret'), dict(id='a'*64,revision='b'*64,state='review_only',created_at=1,expires_at=604801,completed_chunks=None,total_chunks=None,classification='review_required',retryable=True)]:
            remote.run_trusted.return_value=json.dumps({'schema_version':1,'items':[item]}).encode()
            with self.assertRaises(RuntimeError): client.list_items()

if __name__ == '__main__': unittest.main()
