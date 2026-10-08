from pydantic import BaseModel
from typing import Optional, Any, Dict

class ProxyRequest(BaseModel):
    prompt: str

class ProxyResponse(BaseModel):
    reply: Optional[str]
    raw: Optional[Dict[str, Any]]

class TelegramMessage(BaseModel):
    message_id: int
    chat: Dict[str, Any]
    text: Optional[str]

class TelegramUpdate(BaseModel):
    update_id: int
    message: Optional[TelegramMessage]

class VKCallback(BaseModel):
    type: str
    object: Dict[str, Any]
    group_id: Optional[int] = None
    secret: Optional[str] = None
    event_id: Optional[str] = None
