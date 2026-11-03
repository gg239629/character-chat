import unittest
from pathlib import Path
from backend.app.characters import avatar_assets


class AvatarTests(unittest.TestCase):
    def test_approved_assets_and_custom_priority(self):
        root=Path(__file__).resolve().parents[1]
        for role,path in avatar_assets.DEFAULTS.items():
            self.assertTrue((root/path.lstrip('/')).is_file())
            self.assertEqual(avatar_assets.display(role),path)
            self.assertEqual(avatar_assets.display(role,'data:image/png;base64,custom'),'data:image/png;base64,custom')
        self.assertEqual(avatar_assets.display('group'),'')
