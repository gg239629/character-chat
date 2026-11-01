import asyncio,json,unittest
from types import SimpleNamespace as S
from unittest.mock import patch,AsyncMock
import test_home_settings as fixtures
import model_json,role_actions,server

class JsonTests(unittest.TestCase):
    setUp=fixtures.HomeSettingsTests.setUp
    tearDown=fixtures.HomeSettingsTests.tearDown
    def response(self,text,finish='stop'):return S(choices=[S(message=S(content=text),finish_reason=finish)])
    def test_valid_wrappers_and_reasoning_are_supported(self):
        for raw in ('{"decision":"none"}','```json\n{"decision":"none"}\n```','<think>internal {wrong}</think>\n{"decision":"none"}','结果：{"decision":"none"}'):
            self.assertEqual(model_json.decode(raw)['decision'],'none')
    def test_truncated_or_plain_prose_is_never_guessed_as_send(self):
        for raw in ('我已经发了','{"decision":"send",','{"message":"你好}'):
            with self.assertRaises((json.JSONDecodeError,ValueError)):model_json.decode(raw)
        with self.assertRaises(model_json.EmptyModelResponse):model_json.decode('')
    def test_json_mode_is_used_and_bad_format_is_not_counted_as_success(self):
        create=AsyncMock(return_value=self.response('{"decision":"none"}'))
        client=S(chat=S(completions=S(create=create)),close=AsyncMock())
        with patch.object(server,'make_client',return_value=client):
            result=asyncio.run(role_actions.ask_json({'model':'json-test','api_key':'fake'},'输出JSON',{}))
        self.assertEqual(result['decision'],'none');self.assertEqual(create.call_args.kwargs['response_format'],{'type':'json_object'})
        client.close.assert_awaited_once()
    def test_unsupported_json_mode_falls_back_once_and_is_cached(self):
        class Unsupported(Exception):status_code=400
        create=AsyncMock(side_effect=[Unsupported('response_format unsupported'),self.response('{"decision":"none"}'),self.response('{"decision":"none"}')])
        client=S(chat=S(completions=S(create=create)),close=AsyncMock())
        try:
            with patch.object(server,'make_client',return_value=client):
                for i in range(2):asyncio.run(role_actions.ask_json({'model':'no-json-test','api_key':'fake'},'输出JSON',{}))
            self.assertEqual(create.await_count,3)
            self.assertNotIn('response_format',create.call_args_list[-1].kwargs)
        finally:role_actions._NO_JSON_MODE.discard(('', 'no-json-test'))
    def test_length_is_classified_separately(self):
        client=S(chat=S(completions=S(create=AsyncMock(return_value=self.response('{"decision":','length')))),close=AsyncMock())
        with patch.object(server,'make_client',return_value=client),self.assertRaises(model_json.ModelOutputTruncated):
            asyncio.run(role_actions.ask_json({'model':'json-test','api_key':'fake'},'输出JSON',{}))
    def test_old_error_is_marked_recovered_only_after_task_succeeds(self):
        import peer_chat,task_health
        from sqlalchemy.orm import Session
        task=peer_chat.queue('owner','default','wan','followup','recovery',{})
        task_health.record('owner','default',task,'peer_followup',json.JSONDecodeError('invalid','',0),2,5300)
        with Session(self.store.engine) as db:
            row=db.get(peer_chat.Job,task);row.status='done';db.commit()
        self.assertEqual(task_health.report('owner','default')[0]['status'],'done')
