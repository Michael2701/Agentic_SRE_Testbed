from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    auth_url: str = "http://auth:8000"
    order_url: str = "http://order:8000"
    # Above order's own budget (its payment call has 5 s), so order answers - e.g. a 502 that records the
    # failed payment - before the gateway gives up on it (a 504 for an order that may still go through).
    http_timeout_seconds: float = 8.0
    ready_timeout_seconds: float = 2.0


settings = Settings()
