from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_path: str = "/data/experiments.db"
    scenarios_dir: str = "/scenarios"
    fault_injector_url: str = "http://fault-injector:8000"
    base_url: str = "http://nginx"  # traffic goes through the public entry point, like real clients
    prometheus_url: str = "http://prometheus:9090"


settings = Settings()
