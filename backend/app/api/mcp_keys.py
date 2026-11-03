from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from backend.app.infrastructure import storage
from backend.app.mcp import access

router=APIRouter(prefix='/api/mcp',tags=['MCP'])


class KeyIn(BaseModel):
    label:str=Field(min_length=1,max_length=80)
    scopes:list[str]=Field(default=['read','chat','wan','yan','liu','group'],max_length=6)
    daily_limit:int=Field(default=50,ge=1,le=1000)
    daily_tokens:int=Field(default=0,ge=0,le=10000000)
    days:int=Field(default=30,ge=1,le=365)


@router.get('/keys')
def keys():return access.list_keys(storage.scope.get()[0])


@router.post('/keys')
def create_key(body:KeyIn):
    try:return access.create(storage.scope.get()[0],body.label,body.scopes,body.daily_limit,body.days,body.daily_tokens)
    except ValueError as error:raise HTTPException(422,str(error))


@router.delete('/keys/{identity}')
def revoke_key(identity:str):
    try:access.revoke(storage.scope.get()[0],identity)
    except PermissionError:raise HTTPException(404,'Credential unavailable')
    return {'ok':True}
