from fastapi import APIRouter,BackgroundTasks,HTTPException,Response
from pydantic import BaseModel,Field
from backend.app.core.context import chat_world
from backend.app.infrastructure import storage
from backend.app.characters import image_generation as images

router=APIRouter()


class ImageSettingsIn(BaseModel):
    enabled:bool=False
    inherit_connection:bool=True
    model:str=Field(default='gpt-image-2.5',max_length=200)
    base_url:str=Field(default='',max_length=2000)
    api_key:str=Field(default='',max_length=4000)


@router.get('/api/image-settings')
def image_settings():return images.public_settings(storage.scope.get()[0])


@router.put('/api/image-settings')
def save_image_settings(body:ImageSettingsIn):
    try:return images.save_settings(storage.scope.get()[0],{k:v.strip() if isinstance(v,str) else v for k,v in body.model_dump().items()})
    except ValueError as e:raise HTTPException(422,str(e))


class GenerateIn(BaseModel):
    prompt:str=Field(min_length=1,max_length=4000)
    size:str='1024x1024'
    actor:str=''
    request_id:str=Field(min_length=1,max_length=64)


@router.post('/api/images',status_code=202)
def generate(body:GenerateIn,background:BackgroundTasks):
    world=chat_world.get()
    try:
        result=images.enqueue(storage.scope.get()[0],world['id'],world['channel'],
            world['channel'] if world['channel']!='group' else body.actor,body.prompt,body.size,body.request_id)
        if result['status']=='pending':background.add_task(images.process,result['id'])
        return result
    except ValueError as e:raise HTTPException(422,str(e))
    except PermissionError:raise HTTPException(404,'图片任务不存在')


@router.get('/api/images')
def image_tasks():
    world=chat_world.get()
    return images.recent(storage.scope.get()[0],world['id'],world['channel'])


@router.get('/api/images/{identity}/content')
def image_content(identity:str):
    try:
        data,mime=images.content(storage.scope.get()[0],identity)
        return Response(data,media_type=mime,headers={'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'})
    except PermissionError:raise HTTPException(404,'图片不存在')


@router.get('/api/images/{identity}')
def image_status(identity:str):
    world=chat_world.get()
    try:return images.get(storage.scope.get()[0],world['id'],world['channel'],identity)
    except PermissionError:raise HTTPException(404,'图片任务不存在')


@router.post('/api/images/{identity}/dismiss')
def dismiss_image(identity:str):
    world=chat_world.get()
    try:
        images.dismiss(storage.scope.get()[0],world['id'],world['channel'],identity)
        return {'ok':True}
    except PermissionError:raise HTTPException(404,'图片任务不存在')
    except ValueError as error:raise HTTPException(409,str(error))


@router.get('/api/voices/{identity}')
def voice_status(identity:str):
    from backend.app.characters import voice_generation
    world=chat_world.get()
    try:return voice_generation.get(storage.scope.get()[0],world['id'],world['channel'],identity)
    except PermissionError:raise HTTPException(404,'语音不存在')


@router.get('/api/voices/{identity}/content')
def voice_content(identity:str):
    from backend.app.characters import voice_generation
    try:
        data,mime=voice_generation.content(storage.scope.get()[0],identity)
        return Response(data,media_type=mime,headers={'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'})
    except PermissionError:raise HTTPException(404,'语音不可用')
