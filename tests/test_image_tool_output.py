import asyncio
import unittest
from types import SimpleNamespace as S
from unittest.mock import AsyncMock,patch
from backend.app.characters import image_tools

class ImageToolOutputTests(unittest.TestCase):
    def test_internal_prompt_is_never_visible_but_normal_actions_remain(self):
        self.assertEqual(image_tools.visible_text('等我一下。\n[[action:image]]\n173cm的年轻女性'), '等我一下。')
        self.assertEqual(image_tools.visible_text('[[image:abc]]描述'), '')
        self.assertEqual(image_tools.visible_text('（拿起手机）行，等一下。'),'（拿起手机）行，等一下。')

    def test_repair_requires_real_tool_call_and_keeps_auto_choice(self):
        response=S(choices=[S(message=S(content=None,tool_calls=[S(id='real-call',function=S(name='generate_image',arguments='{"prompt":"厨房自拍"}'))]))])
        client=S(close=AsyncMock())
        with patch('backend.app.core.services.models') as models, patch('backend.app.infrastructure.model_usage.complete',new=AsyncMock(return_value=response)) as complete:
            models.return_value.make_client.return_value=client
            raw,calls=asyncio.run(image_tools.repair({'model':'fake'},[],'[[action:image]]描述'))
            self.assertEqual(calls[0]['name'],'generate_image')
            self.assertEqual(complete.call_args.kwargs['tool_choice'],'auto')
            client.close.assert_awaited_once()
