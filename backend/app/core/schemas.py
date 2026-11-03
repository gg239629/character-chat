from pydantic import BaseModel,Field
from uuid import uuid4

class ChatIn(BaseModel):
    messages: list[dict[str, str]]
    temperature: float | None = None
    request_id: str = Field(default_factory=lambda: uuid4().hex, min_length=1, max_length=64, pattern=r'^[a-zA-Z0-9_-]+$')
