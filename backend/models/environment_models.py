from pydantic import BaseModel


class CreateEnvironmentRequest(BaseModel):
    name: str


class SetEnvironmentRequest(BaseModel):
    environment: str
