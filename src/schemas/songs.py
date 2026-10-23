from pydantic import BaseModel, Field

class SongVibe(BaseModel):
    title: str
    artist: str
    genre: str
    bpm: int = Field(default=120, gt=0)