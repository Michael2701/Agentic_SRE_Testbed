from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    auth_url: str = "http://auth:8000"
    order_url: str = "http://order:8000"
    http_timeout_seconds: float = 5.0


settings = Settings()
