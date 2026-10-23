from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.core.config import get_settings
from src.db import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Settings are validated here, so a bad .env stops startup with the field name
    init_db(get_settings().DB_PATH)
    yield


app = FastAPI(title="Moodify", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
