"""Local model calls must not be sent to an environment-configured proxy."""
import asyncio
import json
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from unittest.mock import patch
from backend.app.characters import engine
from backend.app.infrastructure import model_transport


@contextmanager
def endpoints():
    calls={'gateway':[],'proxy':[]}
    def handler(kind):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length',0)))
                calls[kind].append(self.path)
                status=502 if kind=='proxy' else 200
                data={'error':{'message':'Environment proxy intercepted the request'}} if status==502 else {
                    'id':'test','object':'chat.completion','created':0,'model':'test',
                    'choices':[{'index':0,'message':{'role':'assistant','content':'好'},'finish_reason':'stop'}]}
                body=json.dumps(data).encode()
                self.send_response(status);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        return Handler
    gateway=ThreadingHTTPServer(('127.0.0.1',0),handler('gateway'))
    proxy=ThreadingHTTPServer(('127.0.0.1',0),handler('proxy'))
    threads=[threading.Thread(target=s.serve_forever,daemon=True) for s in (gateway,proxy)]
    for t in threads:t.start()
    try:
        proxy_url='http://127.0.0.1:'+str(proxy.server_port)
        with patch.dict('os.environ',{'HTTP_PROXY':proxy_url,'HTTPS_PROXY':proxy_url,
            'ALL_PROXY':proxy_url,'NO_PROXY':'','http_proxy':proxy_url,'https_proxy':proxy_url,
            'all_proxy':proxy_url,'no_proxy':''}):
            yield 'http://127.0.0.1:'+str(gateway.server_port)+'/v1',calls
    finally:
        for s in (gateway,proxy):s.shutdown();s.server_close()
        for t in threads:t.join(timeout=3)


class ModelTransportTests(unittest.TestCase):
    def test_local_addresses_bypass_but_public_providers_keep_environment_behavior(self):
        for url in ('http://192.168.95.1:56828/v1','http://10.2.3.4/v1','http://172.16.0.1/v1',
                    'http://localhost:11434/v1','http://127.0.0.1/v1','http://[::1]/v1','http://[::ffff:192.168.1.1]/v1'):
            self.assertTrue(model_transport.is_local_gateway(url),url)
        for url in ('https://api.deepseek.com/v1','https://api.openai.com/v1',
                    'https://8.8.8.8/v1','https://localhost.example.com/v1','invalid','file:///tmp/model'):
            self.assertFalse(model_transport.is_local_gateway(url),url)
            with patch.object(model_transport,'DefaultHttpxClient') as factory:
                self.assertEqual(model_transport.client_options(url),{})
                factory.assert_not_called()

    def test_sdk_environment_base_url_uses_same_routing_policy(self):
        with patch.dict('os.environ',{'OPENAI_BASE_URL':'http://192.168.95.1:56828/v1'}):
            self.assertTrue(model_transport.is_local_gateway(None))
            self.assertFalse(model_transport.is_local_gateway('https://api.openai.com/v1'))

    def test_sync_helpers_bypass_proxy_for_local_gateway(self):
        from openai import OpenAI
        with endpoints() as (url,calls):
            with OpenAI(base_url=url,api_key='test-only',**model_transport.client_options(url)) as client:
                response=client.chat.completions.create(model='test',messages=[{'role':'user','content':'好'}])
            self.assertEqual(response.choices[0].message.content,'好')
            self.assertEqual(calls['gateway'],['/v1/chat/completions'])
            self.assertEqual(calls['proxy'],[])

    def test_character_and_connection_test_client_bypass_proxy_for_local_gateway(self):
        with endpoints() as (url,calls):
            async def request():
                client=engine.make_client({'base_url':url,'api_key':'test-only','timeout':3})
                try:
                    return await client.chat.completions.create(model='test',messages=[{'role':'user','content':'好'}])
                finally:await client.close()
            response=asyncio.run(request())
            self.assertEqual(response.choices[0].message.content,'好')
            self.assertEqual(calls['gateway'],['/v1/chat/completions'])
            self.assertEqual(calls['proxy'],[])
