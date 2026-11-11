from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration settings for Actual MCP server."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Actual Budget Server Settings
    actual_url: str = "http://localhost:5006"
    actual_password: str = ""
    actual_file: str = ""
    actual_encryption_password: str | None = None
    actual_data_dir: Path = Path("/data")
    actual_verify_ssl: bool = True

    # Optional Cloudflare Access Service Token
    cf_access_client_id: str | None = None
    cf_access_client_secret: str | None = None

    # MCP Server Settings
    mcp_api_key: str | None = None
    host: str = "0.0.0.0"
    port: int = 8000
    transport: str = "http"

    @property
    def is_configured(self) -> bool:
        """Returns True if the required Actual Budget credentials are set."""
        return bool(self.actual_password)

    @property
    def extra_headers(self) -> dict[str, str] | None:
        """Constructs headers for Cloudflare Access if service token credentials are provided."""
        if self.cf_access_client_id and self.cf_access_client_secret:
            return {
                "CF-Access-Client-Id": self.cf_access_client_id,
                "CF-Access-Client-Secret": self.cf_access_client_secret,
            }
        return None


settings = Settings()
