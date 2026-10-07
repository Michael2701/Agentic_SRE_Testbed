from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql://order_svc:order_svc@postgres:5432/orders"  # not a superuser (orders.md)
    db_pool_min_size: int = 1
    db_pool_max_size: int = 10
    payment_url: str = "http://payment:8000"
    http_timeout_seconds: float = 5.0
    # Bounds on waiting for a pooled connection / a query, so a blackholed database can't hang requests forever.
    # Above every calibrated slowdown (queueing for a small pool, slow queries, held locks).
    db_timeout_seconds: float = 10.0


settings = Settings()
