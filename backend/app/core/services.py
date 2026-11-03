"""Explicit service capabilities injected at composition time.

Domain modules select a capability, never receive the entire HTTP application.
Resolution stays dynamic so existing integration patches remain effective.
"""
from typing import Protocol,Any,cast

class Models(Protocol):
    def load_config(self)->dict: ...
    def check_cfg(self,config:dict)->str: ...
    def make_client(self,config:dict)->Any: ...

class World(Protocol):
    def shared_world_data(self)->dict: ...
    def displayed_scene(self)->dict: ...
    def update_scene_state(self,messages:list,**options)->Any: ...

class Capability:
    def __init__(self,provider,names):self._provider=provider;self._names=frozenset(names)
    def __getattr__(self,name):
        if name not in self._names:raise AttributeError('Service capability does not expose '+name)
        return getattr(self._provider,name)

_groups={}

def bind(provider):
    global _groups
    _groups={name:Capability(provider,fields) for name,fields in CAPABILITIES.items()}

def _get(name):
    if name not in _groups:raise RuntimeError('Application services are not bound; start backend.app.main:app')
    return _groups[name]

def models()->Models:return cast(Models,_get('models'))
def world()->World:return cast(World,_get('world'))
def prompts():return _get('prompts')
def generation():return _get('generation')
def api():return _get('api')

CAPABILITIES={'models': ['load_config', 'check_cfg', 'make_client', 'make_kwargs', 'friendly_error', '_load_env_file', 'CONFIG_FILE', 'mask_key', 'home_model_draft', 'home_model'], 'prompts': ['character_profile', 'read_text', '_cached_prompt', 'PROMPT_FILE', 'INJECT_FILE', 'MEMORY_MD', 'recent_phrases', 'personal_memory', 'build_prompt_parts'], 'world': ['displayed_scene', 'shared_world_data', 'model_scene', 'story_root_id', 'PRIVATE_NAMES', 'SCENE_FIELDS', 'SCENE_SYSTEM', 'update_scene_state'], 'generation': ['actor_payload', 'chat_stream', 'sse', 'wake_maintenance'], 'api': ['auth_rate_limit', 'conversation_profile', 'get_settings', 'read_story_flag', 'write_story_flag']}
