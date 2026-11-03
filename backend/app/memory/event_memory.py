"""Compatibility facade for durable, evidence-backed world events."""
from backend.app.world import state_events


def capture(user,world,channel,record,audience=None):
    state_events.capture(user,world,channel,record,audience)


def recall(user,world,actor,query):
    state_events.adopt_legacy(user,world)
    events=state_events.history(user,world,actor,query)
    return [{'kind':'observed_action' if e['basis']=='亲历' else 'reported_action',
        'basis':e['basis'],'reported_by':state_events.actors.ROLES.get(e['actor'],e['actor']),
        'text':e['text'],'time':e['created_at'],
        'location':(e.get('position') or {}).get('place','未知'),'source_id':e['source_id']} for e in events]
