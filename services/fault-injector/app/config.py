from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_path: str = "/data/faults.db"
    docker_socket: str = "/var/run/docker.sock"
    compose_project: str = "sre-testbed"
    payment_url: str = "http://payment:8000"
    reconcile_interval_seconds: float = 2.0
    healthy_timeout_seconds: float = 60.0


settings = Settings()
