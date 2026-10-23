import sys
import os
import numpy as np
import pandas as pd
from pathlib import Path
from qdrant_client.models import PointStruct, VectorParams, Distance
import asyncio

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(PROJECT_ROOT))

from src.core.new_config import settings
from src.db.client import QdrantConnector
from src.core.logger import MoodifyLogger

logger = MoodifyLogger.get_logger("IngestionService")

def load_fma_metadata() -> pd.DataFrame:
    csv_path = settings.DATA_DIR / 'tracks.csv'
    logger.info("reading from: {csv_path}")
    tracks = pd.read_csv(csv_path, index_col = 0, header = [0, 1])
    small_subset = tracks[tracks[('set', 'subset')] == 'small']
    return small_subset
def generate_mock_vector(size: int) -> list[float]:
    #This vector o/p will be replaced by our model's o/p
    vec = np.random.rand(size)
    #normalizing for cosine similarity
    vec = vec / np.linalg.norm(vec)
    return vec.tolist()
async def run_async_ingestion():
    client = QdrantConnector.get_client()
    try:
        existing_collections = await client.get_collections()
        exists = any(c.name == settings.COLLECTION_NAME for c in existing_collections.collections)

        if exists:
            logger.warning(f"Collection '{settings.COLLECTION_NAME}' exists, recreating")
            await client.delete_collection(settings.COLLECTION_NAME)

        await client.create_collection(
            collection_name = settings.COLLECTION_NAME,
            vectors_config = VectorParams(
                size=settings.VECTOR_SIZE,
                distance=Distance.COSINE
            )
        )

        logger.info(f"Collection '{settings.COLLECTION_NAME}' created")

        df = load_fma_metadata()
        points_buff=[]
        BATCH_SIZE = 100
        logger.info("Starting ingestion..")

        for track_id, row in df.iterrows():
            track_id_int = int(track_id)

            try:
                title = row[('track', 'title')]
                artist = row[('artist', 'name')]
                genre = row[('track', 'genre_top')]

                if pd.isna(title) or pd.isna(artist):
                    continue
            except KeyError:
                continue
            point = PointStruct(
                    id = track_id_int,
                    vector=generate_mock_vector(settings.VECTOR_SIZE),
                    payload={
                        "title": title,
                        "artist": artist,
                        "genre": genre if not pd.isna(genre) else "Unknown",
                        "file_path": f"{track_id_int:06d}.mp3",
                        "year": int(row[('track', 'date_created')][:4]) if not pd.isna(row[('track', 'date_created')]) else 0
                    }
                )
            points_buff.append(point)
            if len(points_buff) >= BATCH_SIZE:
                await client.upsert(
                    collection_name=settings.COLLECTION_NAME,
                    points=points_buff
                )
                print("ingested batch (end id : '{track_id_int}')")
                points_buff=[]

        if points_buff:
            await client.upsert(
                collection_name=settings.COLLECTION_NAME,
                points=points_buff
            )
        logger.info(f"\n Ingestion completed {len(df)} tracks have been processed")
    except Exception as e:
        logger.critical(f"ingestion failed: '{e}'")
        sys.exit(1)
    finally:
        await QdrantConnector.close()
if __name__ == "__main__":
    asyncio.run(run_async_ingestion())
    