"""Configuração via variáveis de ambiente (arquivo .env opcional)."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="RTP_", extra="ignore")

    # Calculadora RTC. Em produção use a instância local (Docker/JAR) para que nenhum dado saia do ambiente:
    #   http://localhost:8080/api
    # A instância pública da Receita serve para desenvolvimento/validação.
    calculadora_url: str = "https://consumo.tributos.gov.br/servico/calcular-tributos-consumo/api"
    calculadora_timeout: float = 60.0

    database_url: str = "sqlite:///./rtp.db"


settings = Settings()
