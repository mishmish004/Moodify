# run_search.py
import asyncio
import numpy as np
from src.services.searcher import get_searcher
from src.core.new_config import settings

async def main():
    searcher = get_searcher()
    mock_vec = np.random.rand(settings.VECTOR_SIZE).astype(np.float32)
    
    print(f"--- [PRODUCTION EXECUTION] Target: {settings.COLLECTION_NAME} ---")

    # 1. Raw
    print(f"\n[1] Raw Search...")
    raw_hits = await searcher.execute_search(mock_vec.tolist())
    print(f"   Found {len(raw_hits)} hits.")

    # 2. Result Pattern
    print(f"\n[2] Result Pattern...")
    result = await searcher.perform_vector_search(mock_vec)
    if result.is_ok():
        print(f"   ✅ Success: {len(result.unwrap())} hits")
    else:
        print(f"   ❌ Error: {result.unwrap_err()}")

    # 3. Validated
    print(f"\n[3] Validated Models...")
    try:
        songs = await searcher.search_and_validate(mock_vec.tolist())
        for s in songs[:2]:
            print(f"   🎵 {s.title} - {s.artist} (BPM: {s.bpm})")
    except Exception as e:
        print(f"   ⚠️ Validation Failed: {e}")

    await searcher.client.close()

if __name__ == "__main__":
    asyncio.run(main())