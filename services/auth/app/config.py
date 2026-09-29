from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    redis_url: str = "redis://redis:6379/0"
    token_ttl_seconds: int = 3600
    # Comma-separated "username:password" pairs. Fake auth by design (Stage 0 non-goal).
    demo_users: str = "alice:alice,bob:bob"

    @property
    def users(self) -> dict[str, str]:
        pairs = (entry.split(":", 1) for entry in self.demo_users.split(",") if ":" in entry)
        return {name.strip(): password.strip() for name, password in pairs}


settings = Settings()
