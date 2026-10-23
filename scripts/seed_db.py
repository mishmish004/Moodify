import asyncio
from qdrant_client import AsyncQdrantClient
from src.core.config import settings
from src.services.ingestion import IngestionService

async def main():
    # 1. Instantiate the Infrastructure (Real DB)
    real_client = AsyncQdrantClient(
        host=settings.QDRANT_HOST,
        port=6333,
        api_key=settings.QDRANT_API_KEY.get_secret_value() if settings.QDRANT_API_KEY else None
    )

    # 2. Inject into Service
    service = IngestionService(db_client=real_client)

    # 3. Run
    try:
        await service.run()
    finally:
        await real_client.close()

if __name__ == "__main__":
    asyncio.run(main())