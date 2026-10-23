import pandas as pd
import asyncio
from typing import AsyncGenerator, List
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import PointStruct, VectorParams, Distance
from src.core.config import settings
from src.core.logger import MoodifyLogger
from src.schemas.ingestion import RawTrack

logger = MoodifyLogger.get_logger("IngestionService")

class IngestionService:
    def __init__(self, db_client: AsyncQdrantClient):
        """
        DEPENDENCY INJECTION:
        We pass the client in. This allows us to pass a 'MockClient' 
        during unit tests so we don't need a real database running.
        """
        self.client = db_client

    async def _stream_tracks(self, path: str) -> AsyncGenerator[RawTrack, None]:
        """
        GENERATOR PATTERN:
        Netflix scales to petabytes. We never read `pd.read_csv()` 
        into memory fully if we can avoid it. We stream.
        """
        # In a real scenario, we'd use a chunked reader. 
        # For FMA (small), pandas is acceptable IF encapsulated here.
        try:
            df = pd.read_csv(path, index_col=0, header=[0, 1])
            subset = df[df[('set', 'subset')] == 'small']
            
            for track_id, row in subset.iterrows():
                try:
                    # Validate against Schema immediately
                    yield RawTrack(
                        track_id=int(track_id),
                        title=str(row[('track', 'title')]),
                        artist=str(row[('artist', 'name')]),
                        genre=str(row[('track', 'genre_top')]),
                        year=int(row[('track', 'date_created')][:4]) if not pd.isna(row[('track', 'date_created')]) else 0
                    )
                except ValueError:
                    continue # Skip bad rows silently (or log as debug)
                    
        except FileNotFoundError:
            logger.critical(f"Dataset missing at {path}")
            raise

    async def run(self):
        """
        Orchestration Logic
        """
        # 1. Reset State (Idempotency)
        logger.warning("Resetting Collection...")
        await self.client.recreate_collection(
            collection_name=settings.COLLECTION_NAME,
            vectors_config=VectorParams(size=settings.VECTOR_SIZE, distance=Distance.COSINE)
        )

        buffer: List[PointStruct] = []
        BATCH_SIZE = 100
        
        # 2. Stream & Process
        data_path = settings.DATA_DIR / 'raw' / 'tracks.csv'
        
        async for track in self._stream_tracks(str(data_path)):
            # Mock Embedding (Replace with ClapScorer later)
            # vector = self.scorer.embed(track) 
            vector = np.random.rand(settings.VECTOR_SIZE).tolist()

            point = PointStruct(
                id=track.track_id,
                vector=vector,
                payload=track.model_dump() # Pydantic converts to dict
            )
            
            buffer.append(point)

            if len(buffer) >= BATCH_SIZE:
                await self.client.upsert(
                    collection_name=settings.COLLECTION_NAME,
                    points=buffer
                )
                buffer = []

        # Flush remainder
        
        if buffer:
            await self.client.upsert(
                collection_name=settings.COLLECTION_NAME,
                points=buffer
            )
            
        logger.info("Ingestion Pipeline Complete.")