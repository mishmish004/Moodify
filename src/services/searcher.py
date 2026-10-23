import asyncio
import uvloop
import numpy as np
from result import Result, Ok, Err
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue
from pydantic import BaseModel, Field
from src.core import settings

# 1. Schema Definition
class SongVibe(BaseModel):
    title: str
    artist: str
    genre: str
    bpm: int = Field(default=120, gt=0)

asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

class VibeSearch:
    def __init__(self):
        self.settings = settings
        self.client = AsyncQdrantClient(
            host=self.settings.QDRANT_HOST,
            port=6333,
            api_key=self.settings.QDRANT_API_KEY.get_secret_value() if self.settings.QDRANT_API_KEY else None,
            prefer_grpc=False
        )

    # --- Method 1: The Result Pattern (Production Safe) ---
    async def perform_vector_search(self, vector: np.ndarray) -> Result[list, str]:
        try:
            hits = await self.client.search(
                collection_name=self.settings.COLLECTION_NAME,
                query_vector=vector.tolist(),
                limit=10,
                with_payload=True
            )
            if not hits:
                return Err("no matches found")
            return Ok(hits)
        except Exception as e:
            return Err(f"Database Error: {str(e)}")

    # --- Method 2: Raw SDK (Fast / Debugging) ---
    async def execute_search(self, vector: list[float], limit: int = 10):
        return await self.client.search(
            collection_name=self.settings.COLLECTION_NAME,
            query_vector=vector,
            limit=limit
        )

    # --- Method 3: Strict Validation (Data Integrity) ---
    async def search_and_validate(self, vector: list[float]):
        hits = await self.execute_search(vector)
        validated_songs = [
            SongVibe.model_validate(hit.payload)
            for hit in hits
            if hit.payload
        ]
        return validated_songs

    async def close(self):
        await self.client.close()

def get_searcher():
    return VibeSearch()