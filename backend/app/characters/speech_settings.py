"""Account-scoped speech connection settings; voices belong to each character."""
from urllib.parse import urlsplit
import base64
from sqlalchemy.orm import Session
from backend.app.characters.model_defaults import Defaults
from backend.app.infrastructure import storage,secrets_store


def read(user):
    with Session(storage.get_store().engine) as db:row=db.get(Defaults,user)
    cfg=secrets_store.decode_settings((row.settings if row else {}).get('speech',{}))
    # Connections saved before the switch existed have no enabled flag.
    # Preserve their configured service; an explicit false always stays false.
    if 'enabled' not in cfg:cfg['enabled']=bool(cfg.get('model'))
    return cfg


def public(user):
    cfg={'enabled':False,'provider':'auto','model':'','base_url':'','inherit_connection':True,**read(user)}
    return {k:v for k,v in cfg.items() if k!='api_key'}|{'has_key':bool(cfg.get('api_key'))}


def save(user,body):
    cfg={**read(user),**{k:v for k,v in body.items() if k!='api_key'}}
    if body.get('api_key'):cfg['api_key']=body['api_key']
    if cfg.get('base_url'):
        url=urlsplit(cfg['base_url'])
        if url.scheme not in ('http','https') or not url.hostname or url.username or url.password:
            raise ValueError('语音接口地址须为 http 或 https 地址，不应包含用户名或密码')
    with Session(storage.get_store().engine) as db:
        row=db.get(Defaults,user)
        if not row:row=Defaults(user_id=user,settings={});db.add(row)
        row.settings={**row.settings,'speech':secrets_store.encode_settings(cfg)}
        db.commit()
    return public(user)


def connection(user,body=None):
    from backend.app.characters import model_defaults
    from backend.app.core import services
    cfg={**read(user),**{k:v for k,v in (body or {}).items() if k!='api_key'}}
    if (body or {}).get('api_key'):cfg['api_key']=body['api_key']
    if cfg.get('inherit_connection',True):
        text=model_defaults.read(user) or services.models().load_config()
        cfg.update(base_url=text.get('base_url',''),api_key=text.get('api_key',''))
    if not cfg.get('model') or not cfg.get('api_key'):raise ValueError('请填写语音模型和可用密钥，或先配置文字模型连接')
    if cfg.get('base_url'):
        url=urlsplit(cfg['base_url'])
        if url.scheme not in ('http','https') or not url.hostname or url.username or url.password:raise ValueError('语音接口地址无效')
    return cfg


async def synthesize(cfg,voice,text,style=''):
    from openai import AsyncOpenAI
    from backend.app.infrastructure.model_transport import client_options
    from backend.app.infrastructure import model_usage
    if not voice:raise ValueError('请先为该角色填写基础音色')
    async with AsyncOpenAI(api_key=cfg['api_key'],base_url=cfg.get('base_url') or None,
            timeout=60,max_retries=0,**client_options(cfg.get('base_url'),asynchronous=True)) as client:
        provider=cfg.get('provider','auto')
        mimo=provider=='mimo' or (provider=='auto' and (cfg['model'].lower().startswith('mimo-') or urlsplit(cfg.get('base_url','')).hostname=='api.xiaomimimo.com'))
        messages=[{'role':'user','content':style},{'role':'assistant','content':text}]
        with model_usage.Measure('speech_generation',cfg,messages) as metric:
            if mimo:
                if 'voiceclone' in cfg['model'].lower():raise ValueError('MiMo 音色复刻需要音频样本，当前暂不支持')
                design='voicedesign' in cfg['model'].lower()
                guidance=(voice+'。') if design else '保持指定的基础音色。'
                guidance+=(style or '用自然、平静、亲切的语气说话。')+'只调整情绪、语速和停顿，保持基础音色的声音身份。'
                audio={'format':'wav'}
                if not design:audio['voice']=voice
                response=await client.chat.completions.create(model=cfg['model'],
                    messages=[{'role':'user','content':guidance},{'role':'assistant','content':text}],audio=audio)
                metric.observe(response)
                result=response.choices[0].message.audio
                encoded=result.get('data') if isinstance(result,dict) else getattr(result,'data',None)
                if not isinstance(encoded,str) or len(encoded)>28*1024*1024:raise ValueError('MiMo 未返回有效的音频数据')
                data=base64.b64decode(encoded,validate=True)
                if len(data)<12 or data[:4]!=b'RIFF' or data[8:12]!=b'WAVE':raise ValueError('MiMo 返回的音频不是有效 WAV 文件')
                return data,'audio/wav'
            options={'model':cfg['model'],'voice':voice,'input':text,'response_format':'mp3'}
            if style:options['instructions']=style
            response=await client.audio.speech.create(**options)
            data=await response.aread();mime=response.headers.get('content-type','').lower()
            if not data or len(data)>20*1024*1024 or ('audio/' not in mime and 'application/octet-stream' not in mime):raise ValueError('语音接口未返回有效音频')
            return data,'audio/mpeg'


async def test(user,body,world,actor):
    from backend.app.characters import actors
    cfg=connection(user,body)
    with actors.role_scope(user,world,actor):voice=storage.read_document('base_voice','').strip()
    if not voice:
        from openai import AsyncOpenAI
        from backend.app.infrastructure.model_transport import client_options
        async with AsyncOpenAI(api_key=cfg['api_key'],base_url=cfg.get('base_url') or None,timeout=45,max_retries=0,
                **client_options(cfg.get('base_url'),asynchronous=True)) as client:
            models=await client.models.list()
            if not any(getattr(model,'id',None)==cfg['model'] for model in models.data):raise ValueError('接口可以访问，但模型列表中未找到填写的语音模型；请核对名称或配置角色音色后测试实际合成')
            return {'ok':True,'level':'model','message':'连接正常，模型可访问；尚未验证语音合成，请先为角色填写基础音色。'}
    await synthesize(cfg,voice,'你好，我在这里。')
    return {'ok':True,'level':'speech','message':'连接正常，已使用该角色的基础音色成功合成测试语音。'}
