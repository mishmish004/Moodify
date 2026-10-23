import asyncio
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import VectorParams, Distance
from src.core import settings

async def init_db():
    print(f"--- Initializing Qdrant Collection: {settings.COLLECTION_NAME} ---")
    
    client = AsyncQdrantClient(
        host=settings.QDRANT_HOST,
        port=6333,
        api_key=settings.QDRANT_API_KEY.get_secret_value() if settings.QDRANT_API_KEY else None,
    )

    # 1. Universal "Check Exists" Logic
    # We fetch all collections and check if ours is in the list
    existing_collections = await client.get_collections()
    exists = any(c.name == settings.COLLECTION_NAME for c in existing_collections.collections)

    if exists:
        print(f"⚠️ Collection '{settings.COLLECTION_NAME}' already exists.")
    else:
        print(f"🔨 Creating bucket for {settings.VECTOR_SIZE}-dim vectors...")
        await client.create_collection(
            collection_name=settings.COLLECTION_NAME,
            vectors_config=VectorParams(size=settings.VECTOR_SIZE, distance=Distance.COSINE)
        )
        print("✅ Success! Database ready.")

    await client.close()

if __name__ == "__main__":
    asyncio.run(init_db())