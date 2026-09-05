import importlib.util
from pathlib import Path
import unittest


class PublicPathPolicyTests(unittest.TestCase):
    def test_actual_account_paths_are_rejected_even_outside_web_root(self):
        spec = importlib.util.spec_from_file_location(
            "public_policy", Path(__file__).parents[1] / "public_policy.py"
        )
        policy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(policy)
        for value in (
            "/home/" + "s" + "1234/private/log/events.jsonl",
            "/virtual/" + "tenant" + "/private/config.json",
            "/Users/" + "actual-person" + "/Applications/Example.app",
        ):
            with self.subTest(value=value):
                self.assertTrue(policy.contains_private_environment_path(value))
                self.assertTrue(policy.contains_private_environment_path(value, test_fixture=True))
        for value in (
            "/home/example/private/log/events.jsonl",
            "/Users/example/Applications/Example.app",
            "/usr/bin/php",
        ):
            with self.subTest(value=value):
                self.assertFalse(policy.contains_private_environment_path(value))
