import sys
from typing import Optional
from functools import lru_cache
from pathlib import Path
'''
Production anti-patterns: 
Config scripts should rarely be asyncronous we need a linear script (syncronous), proper checks(i used pydantic v2 fuck if-else checks)
Eventually when i need to deploy this i will use a async 
'''
from pydantic import(
    ValidationError, Field, SecretStr,
    model_validator, field_validator
)
from pydantic_settings import BaseSettings, SettingsConfigDict
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(PROJECT_ROOT))
class Settings(BaseSettings):
    PROJECT_NAME: str = Field("Moodify", description = "Name of the application")
    ENV: str = Field(..., pattern = "^(dev|test|prod)")

    #DB path
    QDRANT_HOST: str = Field("localhost")
    QDRANT_PORT: int = Field(6333)
    QDRANT_API_KEY: Optional[SecretStr] = Field(None)
    COLLECTION_NAME: str = Field(..., min_lengths=1)
    QDRANT_URL : str = Field(..., description="db url")



    VECTOR_SIZE: int = Field(768, gt=0)

    #LLM (query understanding)
    OPENAI_API_KEY: Optional[SecretStr] = Field(None)
    OPENAI_MODEL: str = Field("gpt-6.1-sol", description="mid-tier model used to parse user queries")

    #data
    DATA_DIR: Path = Field(default=PROJECT_ROOT / "data" / "fma_metadata")
    AUDIO_DIR: Path = Field(default=PROJECT_ROOT / "data" / "fma_small")

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding = "utf-8",
        case_sensitive=True,
        extra="ignore"
    )
    @field_validator("DATA_DIR", "AUDIO_DIR", mode="before")
    @classmethod
    def create_directories(cls, v: Path) -> Path:
        path = Path(v)
        path.mkdir(parents=True, exist_ok=True)
        return path
    @model_validator(mode="after")
    def check_prod_security(self) -> 'Settings':
        if self.ENV == 'prod' and not self.QDRANT_API_KEY:
            raise ValueError("api key required for production, update env file or anything related to the api key please dont hardcode it gang")
        return self
try:
    settings = Settings()
except ValidationError as e:
    print("config error\n")
    print(e)
    sys.exit(1)
@lru_cache()
def get_settings():
    return Settings()        