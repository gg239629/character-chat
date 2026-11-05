"""One post-response semantic extraction pass, with deterministic fast paths."""
import hashlib
import json
import time
from backend.app.core import services
from backend.app.core.context import chat_world,scene_revision,scene_batch
from backend.app.infrastructure import storage,model_usage
from backend.app.world import state_events,world_time,scene_labels,state_tools
from backend.app.characters import actors


def update_scene(messages,force=False,cfg_override=None):
    from backend.app.characters.role_routing import recent_rounds
    world=chat_world.get();user,cid=storage.scope.get()
    identity=world['id'] if world else cid
    channel=world['channel'] if world else 'group'
    data=storage.get_store().read(user,identity)
    revision=scene_revision.get()
    # Revision checks are per character during projection, so maintenance from
    # an older private window can still preserve its own historical evidence.
    if revision is not None and revision < data.get('memory_reset_at',0):return services.world().displayed_scene()
    batch=messages if scene_batch.get() else recent_rounds(messages,1)
    records=[]
    for i,m in enumerate(batch):
        stamp=m.get('created_at',revision if revision is not None else time.time())
        source=m.get('id') or hashlib.sha256(json.dumps([identity,channel,revision,i,m.get('role'),m.get('content')],ensure_ascii=False).encode()).hexdigest()
        speaker=m.get('speaker') or next((n for n in actors.ROLES.values() if m.get('content','').startswith((n+'：',n+':'))),'')
        records.append({**m,'id':source,'speaker':speaker,'created_at':stamp})
    for record in records:state_events.capture(user,identity,channel,record)
    old=world_time.clock(data);candidate=world_time.advance_candidate(messages,old)
    if old!=candidate:state_events.advance_story(user,identity,candidate,[],revision)
    eligible=state_events.candidates(records,channel)
    if not eligible and not force:return services.world().displayed_scene()
    # Keep a bounded recent sample per author. Raw evidence was captured above;
    # semantic output should refine current state rather than summarize a long chat.
    counts={};selected=[]
    for record in reversed(eligible):
        author=record.get('speaker') or record.get('role')
        if counts.get(author,0)>=3:continue
        counts[author]=counts.get(author,0)+1;selected.append(record)
    eligible=list(reversed(selected))

    cfg=model_usage.config('scene',cfg_override or {})
    if services.models().check_cfg(cfg):return None
    payload=[{'role':'system','content':state_events.EXTRACTION_SYSTEM.replace('只输出JSON：','工具参数结构：')+'\n通过 update_character_state 工具提交结果，不要只在正文描述更新。'},
        {'role':'user','content':json.dumps({'channel':channel,'current':services.world().displayed_scene(),
            'messages':[{'source_id':r['id'],**{k:r[k] for k in ('role','speaker','content')}} for r in eligible]},ensure_ascii=False)}]
    try:
        from openai import OpenAI
        from backend.app.infrastructure.model_transport import client_options
        with model_usage.Measure('scene',cfg,payload,output_limit=3000) as metric:
            with OpenAI(api_key=cfg['api_key'],base_url=cfg.get('base_url') or None,timeout=60,max_retries=0,**client_options(cfg.get('base_url'))) as client:
                response=client.chat.completions.create(model=cfg['model'],messages=payload,temperature=.1,max_tokens=3000,
                    tools=[state_tools.TOOL],tool_choice={'type':'function','function':{'name':'update_character_state'}})
                metric.observe(response)
                raw=response.choices[0].message.content or '';metric.output(raw)
                if getattr(response.choices[0],'finish_reason',None)=='length':raise ValueError('State tool output truncated')
        answer=state_tools.decode(response.choices[0].message)
        if not isinstance(answer,dict) or not isinstance(answer.get('events'),list):return None
        state_events.apply_model(user,identity,channel,eligible,answer)
        return services.world().displayed_scene()
    except Exception:
        # The worker records a content-free diagnostic and retries failures.
        raise
