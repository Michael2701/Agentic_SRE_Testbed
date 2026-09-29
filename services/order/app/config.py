from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql://app:app@postgres:5432/orders"
    db_pool_min_size: int = 1
    db_pool_max_size: int = 10
    payment_url: str = "http://payment:8000"
    http_timeout_seconds: float = 5.0


settings = Settings()
