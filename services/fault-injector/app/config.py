from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_path: str = "/data/faults.db"
    docker_socket: str = "/var/run/docker.sock"
    compose_project: str = "sre-testbed"
    admin_database_url: str = "postgresql://app:app@postgres:5432/orders"
    reporting_database_url: str = "postgresql://reporting:reporting@postgres:5432/orders"
    redis_url: str = "redis://redis:6379/0"
    reconcile_interval_seconds: float = 2.0
    healthy_timeout_seconds: float = 60.0


settings = Settings()
