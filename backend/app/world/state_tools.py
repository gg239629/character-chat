"""Background, evidence-bound state tool; the server owns scope and timestamps."""
import json

EVENT_SCHEMA={
    'type':'object',
    'properties':{
        'source_id':{'type':'string'},
        'actor':{'type':'string','enum':['wan','yan','liu','user']},
        'evidence':{'type':'string'},
        'status':{'type':'string'},
        'position':{
            'type':'object',
            'properties':{
                'place':{'type':['string','null']},
                'relative_to':{'type':['string','null']},
                'relation':{'type':['string','null']}
            }
        }
    },
    'required':['source_id','actor','evidence'],
    'additionalProperties':False
}
TOOL={
    'type':'function',
    'function':{
        'name':'update_character_state',
        'description':'从已发送原文提取实际发生的动作并更新状态；无变化时events为空。不得把计划、台词自述或谎言当事实。',
        'parameters':{
            'type':'object',
            'properties':{'events':{'type':'array','items':EVENT_SCHEMA}},
            'required':['events'],
            'additionalProperties':False
        }
    }
}


def decode(message):
    calls=getattr(message,'tool_calls',None) or []
    if calls:
        events=[]
        for call in calls:
            if call.function.name!='update_character_state':raise ValueError('Unexpected state tool')
            result=json.loads(call.function.arguments)
            if not isinstance(result,dict) or not isinstance(result.get('events'),list):raise ValueError('Invalid state tool arguments')
            events.extend(result['events'])
        return {'events':events}
    # Older compatible gateways may put structured arguments in content.
    raw=message.content or ''
    result=json.loads(raw[raw.find('{'):raw.rfind('}')+1])
    if not isinstance(result,dict) or not isinstance(result.get('events'),list):raise ValueError('Missing state result')
    return result
