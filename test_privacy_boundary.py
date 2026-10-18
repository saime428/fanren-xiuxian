import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.privacy_guard import violations
from scripts.materialize_private_checkout import materialize


class PrivacyBoundaryTests(unittest.TestCase):
    def test_real_account_and_runtime_records_are_rejected(self):
        self.assertIn('non-demo-account-id', violations('example.py', ('tg' + '5555555555').encode()))
        self.assertIn('runtime-record', violations('.maintenance/observed.json', b'{}'))
        self.assertIn('credential-file', violations('config/accounts/example.env', b''))
        self.assertEqual(set(), violations('config/accounts/account.env.example', b''))
        self.assertEqual(set(), violations('example.py', b'tg9100000001 @demo_player_01'))

    def test_private_value_check_covers_content_and_filenames_without_echo(self):
        self.assertIn('private-mapping-match', violations('private-alias.py', b'', (b'private-alias',)))
        self.assertIn('private-mapping-match', violations('app.py', b'PRIVATE-ALIAS', (b'private-alias',)))

    def test_materialize_creates_private_copy_and_never_overwrites_source(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / 'source'
            source.mkdir()
            subprocess.run(['git', 'init', '-q', str(source)], check=True)
            (source / 'demo-player.py').write_text('identity = "demo-player"\n')
            subprocess.run(['git', '-C', str(source), 'add', '.'], check=True)
            mapping = base / 'values.json'
            mapping.write_text(json.dumps({'demo-player': 'private-player'}))
            output = base / 'runtime'
            self.assertEqual(1, materialize(source, mapping, output))
            self.assertIn('private-player', (output / 'private-player.py').read_text())
            self.assertIn('demo-player', (source / 'demo-player.py').read_text())
            self.assertEqual(0o700, output.stat().st_mode & 0o777)
            self.assertEqual(0o600, (output / 'private-player.py').stat().st_mode & 0o777)
            self.assertNotEqual(0, subprocess.run(['sh', str(output / '.git/hooks/pre-push')], capture_output=True).returncode)
            with self.assertRaises(FileExistsError):
                materialize(source, mapping, output)

    def test_materialize_rejects_path_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / 'source'
            source.mkdir()
            subprocess.run(['git', 'init', '-q', str(source)], check=True)
            (source / 'example.txt').write_text('example')
            subprocess.run(['git', '-C', str(source), 'add', '.'], check=True)
            mapping = base / 'values.json'
            mapping.write_text(json.dumps({'example.txt': '../escape.txt'}))
            with self.assertRaisesRegex(ValueError, 'unsafe_or_duplicate_private_path'):
                materialize(source, mapping, base / 'runtime')
            self.assertFalse((base / 'escape.txt').exists())


if __name__ == '__main__':
    unittest.main()
