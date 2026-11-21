"""Replay the same synthetic transcript against a Chat Completions endpoint."""
import argparse
from contextlib import ExitStack
import getpass
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path.cwd())
    parser.add_argument('--url', required=True)
    parser.add_argument('--model', default='gpt-6-luna')
    parser.add_argument('--max-output', type=int, default=320)
    parser.add_argument('--trial', default='comparison')
    parser.add_argument('--turns', type=int, choices=(12, 20), default=12)
    parser.add_argument('--layout', choices=('baseline', 'tail-first', 'append'), required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    key = os.getenv('CONTEXT_BENCHMARK_API_KEY') or getpass.getpass('Model API key: ')
    sys.path.insert(0, str(args.repo.resolve()))

    import server
    from backend.app.conversations import chat_history
    from backend.app.infrastructure import model_usage, storage
    from backend.app.memory import memory_autosave
    from backend.app.runtime import role_actions
    from backend.app.world import actor_context

    cfg = {'api_key': key, 'base_url': args.url, 'model': args.model, 'temperature': 0,
           'max_tokens': args.max_output, 'timeout': 45}
    transcript = [
        ('请记住，周六团建原计划在植物园，预约编号是 Q7M9。', '记住了。'),
        ('通知：植物园团建已经取消，先不安排新地点。', '我已记录取消。'),
    ]
    topics = ['排版', '停车', '茶歇', '签到', '音响', '灯光', '清洁', '设备']
    if args.turns == 20:
        topics += ['门禁', '桌椅', '布景', '摄影', '饮水', '指引', '物资', '收尾']
    for index, topic in enumerate(topics, 1):
        user = f'记录第{index}批{topic}事项。' + ''.join(
            f'{topic}第{item}项要核对负责人、交付日期和验收清单，提交后留档。'
            for item in range(1, 23)
        )
        transcript.append((user, f'第{index}批{topic}任务已记录，交付前核对清单。'))
    transcript += [
        ('之前周六植物园的团建还举行吗？只说明最新状态。', '已取消。'),
        ('请复核植物园原预约编号。只回复编号。', 'Q7M9'),
    ]
    result = {'model': args.model, 'layout': args.layout, 'input_budget': os.getenv('CHAT_CONTEXT_INPUT_TOKENS'),
              'max_output': args.max_output, 'trial': args.trial,
              'turns': [], 'summary_calls': []}

    with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
        root = Path(folder)
        fixture = root / 'system.md'
        fixture.write_text(
            f'合成测试标识 {args.trial}/{args.layout}\n'
            '你是团队的会议纪要助理。以最新确认的信息回答。普通记录只回复“收到”。'
            '追问既往记录时可读取历史与摘要。如果没有证据，回复“不确定”。\n'
            + '\n'.join(f'固定规程 {i:03d}：保留资料来源，确认后再记档，记录与当日状态分开管理。' for i in range(85)),
            encoding='utf-8',
        )
        store = storage.Store('sqlite:///' + (root / 'benchmark.db').as_posix())
        stack.callback(store.engine.dispose)
        stack.enter_context(patch.object(storage, '_STORE', store))
        for attr, filename in [('PROMPT_FILE', fixture), ('INJECT_FILE', root/'empty.md'),
                               ('MEMORY_MD', root/'empty.md')]:
            stack.enter_context(patch.object(server, attr, filename))
        stack.enter_context(patch.object(server, 'load_config', return_value=cfg))
        stack.enter_context(patch.object(actor_context, 'build', side_effect=lambda *a, **kw: (
            {'stage': len(result['turns']) + 1, 'actor': 'benchmark'}, [], [],
        )))
        stack.enter_context(patch.dict(os.environ, {'RUN_MAINTENANCE_WORKER': 'false'}))
        store.ensure('benchmark', 'default', {'recent_log': [], 'memory_policy': 2})
        client = stack.enter_context(httpx.Client(timeout=60, trust_env=False))
        previous = []
        with role_actions.window('benchmark', 'default', 'wan') as channel:
            for index, (question, scripted_reply) in enumerate(transcript, 1):
                chat_history.append(str(index), 0, 'user', question)
                memory_autosave.flush('benchmark', channel, force=True)
                history = chat_history.messages()
                assembly_start = time.perf_counter()
                _, payload = server.actor_payload('wan', history, False)
                assembly = round((time.perf_counter() - assembly_start) * 1000)
                if args.layout == 'tail-first':
                    systems = [m for m in payload[2:] if m['role'] == 'system']
                    dialogue = [m for m in payload[2:] if m['role'] != 'system']
                    payload = payload[:2] + dialogue[:-1] + systems + dialogue[-1:]
                matches = 0
                for older, newer in zip(previous, payload):
                    if older != newer:
                        break
                    matches += 1
                previous = payload
                start = time.perf_counter()
                response = client.post(
                    args.url.rstrip('/') + '/chat/completions',
                    headers={'Authorization': 'Bearer ' + key},
                    json={'model': args.model, 'messages': payload, 'temperature': 0, 'max_tokens': args.max_output},
                )
                if not response.is_success:
                    raise RuntimeError(f'Model request failed with HTTP {response.status_code} at turn {index}')
                result_body = response.json()
                usage = result_body.get('usage') or {}
                details = usage.get('prompt_tokens_details') or {}
                choice = result_body['choices'][0]
                reply = choice['message'].get('content') or ''
                record = {'turn': index, 'matching_prefix_messages': matches,
                          'input_tokens': usage.get('prompt_tokens'), 'output_tokens': usage.get('completion_tokens'),
                          'cached_input_tokens': details.get('cached_tokens', usage.get('prompt_cache_hit_tokens')),
                          'cache_write_tokens': details.get('cache_write_tokens'),
                          'reply_ms': round((time.perf_counter() - start) * 1000), 'assembly_ms': assembly}
                if index >= len(transcript) - 1:
                    record['finish_reason'] = choice.get('finish_reason')
                    record['evidence_present'] = any(
                        ('植物园团建已经取消' if index == len(transcript) - 1 else 'Q7M9') in m['content']
                        for m in payload[:-1]
                    )
                    record['answer'] = reply
                    record['passed'] = (
                        '取消' in reply and not any(word in reply for word in ('没有取消', '没收到取消', '不确定'))
                    ) if index == len(transcript) - 1 else 'Q7M9' in reply.upper()
                result['turns'].append(record)
                chat_history.append(str(index), 1, 'assistant', scripted_reply, '小婉')
                print(f'{args.layout} turn {index}: {record["input_tokens"]} input, {record["cached_input_tokens"]} cached', flush=True)
            model_usage.flush(force=True)
            report = model_usage.report('benchmark')
            result['summary_metrics'] = report['categories'].get('context_summary', {})
            for run in report['runs']:
                if 'context_summary' in run['categories']:
                    stats = run['categories']['context_summary']
                    result['summary_calls'].append({'input_tokens': stats['input_tokens'],
                                                    'output_tokens': stats['output_tokens'],
                                                    'cached_input_tokens': run['cached_tokens']})
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
