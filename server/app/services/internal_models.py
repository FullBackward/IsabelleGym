from pydantic import BaseModel
from typing import List, Optional, Dict, Any

class SessionInfo(BaseModel):
    session_id: str
    created_at: float
    theories: List[str]
    status: str