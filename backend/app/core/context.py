"""Trusted execution context shared by HTTP, background jobs and MCP."""
from contextvars import ContextVar

chat_world=ContextVar('chat_world',default=None)
scene_revision=ContextVar('scene_revision',default=None)

scene_batch=ContextVar('scene_batch',default=False)

model_run=ContextVar('model_run',default='')
