import unittest
from types import SimpleNamespace

from observe_return import migrate_observer_legacy_cursor


class CursorMigrationTests(unittest.TestCase):
    def test_observer_legacy_cursor_uses_resolved_entity_id(self) -> None:
        cursors = migrate_observer_legacy_cursor({}, 51, SimpleNamespace(id=888), "@group")
        self.assertEqual({"888": 51}, cursors)

    def test_observer_root_legacy_cursor_uses_resolved_entity_id(self) -> None:
        cursors = migrate_observer_legacy_cursor({}, 52, SimpleNamespace(id=889), "@root")
        self.assertEqual({"889": 52}, cursors)


if __name__ == "__main__":
    unittest.main()