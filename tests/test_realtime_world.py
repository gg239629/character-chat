import unittest
from datetime import datetime,timezone
from backend.app.world import realtime_world
from backend.app.world import world_time


class RealtimeTests(unittest.TestCase):
    def test_fresh_start_uses_actual_hour_not_fixed_morning(self):
        now=datetime(2026,11,2,12,15,tzinfo=timezone.utc).timestamp()
        data=realtime_world.fresh_scene(now)
        self.assertEqual(data['scene_state']['时间'],'2026-11-02 20:15')
        self.assertEqual(data['story_clock']['minute'],1215)
        self.assertEqual(data['role_states']['wan']['status'],'合租屋·自由活动')
        self.assertNotIn('刚起床',str(data))

    def test_real_time_and_bounded_activity(self):
        now=datetime(2026,11,2,1,0,tzinfo=timezone.utc).timestamp()
        data={'scene_state':{'小婉':'厨房·做饭','小研':'书房·看书'}}
        self.assertTrue(realtime_world.advance(data,now))
        self.assertEqual(data['scene_state']['时间'],'2026-11-02 09:00')
        self.assertFalse(realtime_world.advance(data,now+1))
        realtime_world.advance(data,now+7200)
        self.assertEqual(data['role_states']['wan']['status'],'厨房·暂无新动态')
        self.assertNotIn('recent_log',data)
        self.assertFalse(world_time.apply(data,{'day':100,'minute':0},[]))

    def test_new_observation_replaces_expired_action_and_midnight(self):
        now=datetime(2026,11,2,15,30,tzinfo=timezone.utc).timestamp()
        data={'scene_state':{'小婉':'客厅·休息'}}
        realtime_world.advance(data,now)
        data['role_states']['wan']['status']='卧室·睡觉'
        realtime_world.advance(data,now+3600)
        self.assertEqual(data['story_clock']['day'],2)
        self.assertEqual(data['role_states']['wan']['timed_action']['started_at'],now+3600)
        self.assertEqual(world_time.display(world_time.clock(data)),'2026-11-03 00:30')
