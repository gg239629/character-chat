import unittest
from role_routing import merge_recent_history

def rounds(start, end):
    return [m for i in range(start,end) for m in [{"role":"user","content":str(i)}, {"role":"assistant","content":"小研：答"+str(i)}]]

class HistoryTests(unittest.TestCase):
    def test_saved_overlap(self):
        self.assertEqual(merge_recent_history(rounds(0,8), rounds(4,12)), rounds(2,12))
    def test_unsaved_browser_wins_window(self):
        self.assertEqual(merge_recent_history(rounds(0,5), rounds(5,20)), rounds(10,20))
    def test_database_supplements(self):
        self.assertEqual(merge_recent_history(rounds(0,10), rounds(10,12)), rounds(2,12))
    def test_current_unanswered_round(self):
        current={"role":"user","content":"new"}
        result=merge_recent_history(rounds(0,10), rounds(8,10)+[current])
        self.assertEqual(result, rounds(1,10)+[current])
    def test_repeated_greetings_preserved(self):
        messages=[{"role":"user","content":"hi"},{"role":"assistant","content":"小研：你好"}]
        self.assertEqual(len(merge_recent_history([], messages*3)),6)
    def test_long_browser_prefix(self):
        self.assertEqual(merge_recent_history(rounds(8,12), rounds(0,15)), rounds(5,15))
