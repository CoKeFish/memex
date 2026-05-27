from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MEMEX_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str
    auth_enforced: bool = False
    api_token: str = ""
    log_level: str = "INFO"


settings = Settings()  # type: ignore[call-arg]
