from functools import cached_property

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    redis_url: str = "redis://redis:6379/0"
    token_ttl_seconds: int = 3600
    # Bounds a Redis call (also connecting), so a blackholed Redis can't hang requests forever; above the
    # calibrated stalls (redis_latency pauses up to 2.5 s).
    redis_timeout_seconds: float = 5.0
    # Comma-separated "username:password" pairs. Fake auth by design (Stage 0 non-goal).
    demo_users: str = "alice:alice,bob:bob"

    @cached_property
    def users(self) -> dict[str, str]:
        pairs = (entry.split(":", 1) for entry in self.demo_users.split(",") if ":" in entry)
        return {name.strip(): password.strip() for name, password in pairs}


settings = Settings()
