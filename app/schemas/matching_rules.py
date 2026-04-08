from pydantic import BaseModel, Field


class KeywordUpdateBody(BaseModel):
    keyword: str = Field(min_length=1, max_length=100)
