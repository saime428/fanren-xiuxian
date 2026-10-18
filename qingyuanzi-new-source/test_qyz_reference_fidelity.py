"""Guard the user-supplied reference bodies and auditable local adaptations."""
import ast
import hashlib
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).parent / 'qyz_reference'


class ReferenceFidelityTests(unittest.TestCase):
    def test_three_telegram_files_and_preserved_function_bodies_match_manifest(self):
        manifest = json.loads((ROOT/'SOURCE.json').read_text())
        self.assertEqual(set(manifest['files']), {'world_boss_browser.py','world_boss_turnstile.py','world_boss_features.py'})
        count = 0
        for filename, info in manifest['files'].items():
            content = (ROOT/filename).read_bytes()
            source = content.decode('utf-8')
            self.assertEqual(hashlib.sha256(content).hexdigest(), info['adapted_sha256'],filename)
            nodes = {}
            for node in ast.parse(content).body:
                if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)):
                    nodes[node.name] = node
                elif isinstance(node,ast.ClassDef):
                    for member in node.body:
                        if isinstance(member,(ast.FunctionDef,ast.AsyncFunctionDef)):
                            nodes[node.name+'.'+member.name] = member
            for name, expected in info['preserved_symbols'].items():
                self.assertEqual(hashlib.sha256(ast.get_source_segment(source,nodes[name]).encode()).hexdigest(),expected,filename+':'+name)
                count += 1
        self.assertGreater(count,100)
        self.assertIn('WorldBossMonitor._begin_with_turnstile',manifest['files']['world_boss_features.py']['preserved_symbols'])
        self.assertIn('NativeTurnstileBrowser.verify',manifest['files']['world_boss_browser.py']['preserved_symbols'])
        browser = manifest['files']['world_boss_browser.py']
        self.assertIn('AutomaticTurnstileWorker.run_once', browser['adapted_symbols'])
        self.assertEqual(browser['adapted_symbol_original_sha256']['AutomaticTurnstileWorker.run_once'],
                         '2e7338769434340f5b474e4da0979d89bd4dd96cb27e12069ff3b9094255c585')
