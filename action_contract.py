"""Channel-independent delegated send contract and receipt wording."""


def destination(task):
    if not isinstance(task,dict):return None
    return {'contact_user':'user','contact_group':'group'}.get(task.get('type'))


def result_text(name,target,status):
    place='群聊' if target=='group' else '与你的私聊'
    return {
        'sent':name+'已经实际发送到'+place+'。',
        'pending':name+'决定稍后发送到'+place+'，目前尚未发出。',
        'sending':name+'正在处理发送到'+place+'的请求，尚未确认送达。',
        'refuse':name+'明确拒绝了这次发送到'+place+'的请求。',
        'none':name+'暂时没有执行这次发送到'+place+'的请求。',
        'failed':name+'没有成功发送到'+place+'。',
    }.get(status,'请求已转达'+name+'，但还没有她发送到'+place+'的记录，不能认定她拒绝了。')
