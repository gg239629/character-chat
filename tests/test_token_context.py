import unittest

from backend.app.characters import context_budget


def turn(user, reply):
    return [{'role': 'user', 'content': user}, {'role': 'assistant', 'content': reply}]


class TokenContextTests(unittest.TestCase):
    def test_default_window_reserves_output_and_obeys_configured_model_limit(self):
        import os
        from unittest.mock import patch
        from backend.app.memory import prompt_history

        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(prompt_history.input_limit({'max_tokens': 1500}), 24576)
        with patch.dict(os.environ, {'CHAT_MODEL_CONTEXT_TOKENS': '16000'}, clear=True):
            self.assertEqual(prompt_history.input_limit({'max_tokens': 1500}), 14244)

    def test_history_can_grow_to_ninety_percent_before_compression(self):
        history = turn('a' * 36, 'b' * 6) + turn('c' * 25, 'd' * 4)
        self.assertEqual(context_budget.rotation_index(history, len, 100), 0)

    def test_exact_booking_reference_triggers_historical_recall(self):
        self.assertTrue(context_budget.needs_recall('请复核植物园原预约编号。只回复编号。'))

    def test_adding_a_turn_keeps_history_before_changing_state(self):
        prefix = [{'role': 'system', 'content': 'Rules and character'}]
        first_turn = turn('Old question', 'Old reply')
        next_turn = turn('Next question', 'Next reply')
        first = context_budget.token_payload(
            prefix, first_turn, [{'role': 'system', 'content': 'State 1'}],
            {'role': 'user', 'content': 'Question 1'}, len, 200,
        )
        second = context_budget.token_payload(
            prefix, first_turn + next_turn, [{'role': 'system', 'content': 'State 2'}],
            {'role': 'user', 'content': 'Question 2'}, len, 200,
        )
        self.assertEqual(first[:3], prefix + first_turn)
        self.assertEqual(second[:3], first[:3])
        self.assertEqual(second[-1]['content'], 'Question 2')

    def test_rotation_uses_message_size_and_keeps_latest_complete_turn(self):
        long = turn('a' * 55, 'b' * 20)
        short = turn('recent', 'reply')
        self.assertEqual(context_budget.rotation_index(long + short, len, 90), 2)
        self.assertEqual(context_budget.rotation_index(short * 4, len, 200), 0)

    def test_oversized_current_input_is_rejected_before_model_call(self):
        with self.assertRaises(ValueError):
            context_budget.token_payload([], [], [], {'role': 'user', 'content': 'x' * 101}, len, 100)

    def test_summary_budget_keeps_multiple_small_frozen_batches(self):
        from backend.app.memory import prompt_history

        summaries = ['old cancellation', 'recent logistics', 'new checklist']
        self.assertEqual(prompt_history.retain_summaries(summaries, len), summaries)
        large = ['a' * 370, 'b' * 370, 'c' * 370]
        self.assertEqual(prompt_history.retain_summaries(large, len), large[1:])
