from pydantic import BaseModel, Field, field_validator
import numpy as np

class RawTrack(BaseModel):
    
    transaction: hex
    trader_address: hex
    

    @field_validator('transaction', 'trader_address')
    @classmethod
    def must_not_be_empty(cls, v: hex) -> str:
        if not v or hex(v) == 'nan':
            raise ValueError("Field cannot be empty or NaN")
        return v