# run_benchmarks.py
import numpy as np
import asyncio
import uvloop
from src.services.searcher import get_searcher
from src.core.config import settings
from utils.physics_lab import benchmark_physics

# Physics: Enable uvloop
asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

@benchmark_physics
async def test_raw_sdk_search(searcher, vec: list):
    return await searcher.execute_search(vec)

@benchmark_physics
async def test_validated_search(searcher, vec: list):
    return await searcher.search_and_validate(vec)

@benchmark_physics
async def test_result_pattern(searcher, vec: np.ndarray):
    return await searcher.perform_vector_search(vec)

async def run_comparison():
    # Factory returns instance now
    searcher = get_searcher()
    mock_vec = np.random.rand(settings.VECTOR_SIZE).astype(np.float32)
    
    print(f"🚀 Starting Moodify Physics Audit [Collection: {settings.COLLECTION_NAME}]")
    
    print("\n--- Test 1: Raw SDK ---")
    await test_raw_sdk_search(searcher, mock_vec.tolist())
    
    print("\n--- Test 2: Pydantic Validation ---")
    await test_validated_search(searcher, mock_vec.tolist())
    
    print("\n--- Test 3: Result Pattern ---")
    await test_result_pattern(searcher, mock_vec)

    await searcher.client.close()

if __name__ == "__main__":
    asyncio.run(run_comparison())