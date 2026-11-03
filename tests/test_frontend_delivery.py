import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import server


class FrontendDeliveryTests(unittest.TestCase):
    def test_all_pages_use_the_same_vue_entry_and_report_missing_build(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            with patch.object(server,'STATIC',root):
                for route in (server.home,server.index,server.login_page):
                    response=route()
                    self.assertEqual(response.status_code,503)
                    self.assertIn('前端尚未构建',json.loads(response.body)['error'])
                entry=root/'web'/'index.html'
                entry.parent.mkdir();entry.write_text('<div id="app"></div>',encoding='utf-8')
                for route in (server.home,server.index,server.login_page):
                    response=route()
                    self.assertEqual(response.status_code,200)
                    self.assertEqual(response.path,entry)
                    self.assertEqual(response.headers['cache-control'],'no-store')
