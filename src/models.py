from pydantic import BaseModel, Field
from enum import Enum
from typing import Optional


class Algorithm(str, Enum):
    TOKEN_BUCKET = "token_bucket"
    SLIDING_WINDOW = "sliding_window"


class ClientConfig(BaseModel):
    rate: float = Field(..., gt=0, description="Tokens (requests) per second")
    burst_size: int = Field(..., gt=0, description="Max tokens in bucket / max burst")
    algorithm: Algorithm = Algorithm.TOKEN_BUCKET
    window_size: float = Field(default=1.0, gt=0, description="Sliding window size in seconds")


class ClientConfigResponse(ClientConfig):
    client_key: str


class CheckResponse(BaseModel):
    allowed: bool
    client_key: str
    algorithm: str
    tokens_remaining: Optional[float] = None
