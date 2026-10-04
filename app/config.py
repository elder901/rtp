"""Configuração via variáveis de ambiente (arquivo .env opcional)."""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="RTP_", extra="ignore")

    # Calculadora RTC. Em produção use a instância local (Docker/JAR) para que nenhum dado saia do ambiente:
    #   http://localhost:8080/api
    # A instância pública da Receita serve para desenvolvimento/validação.
    calculadora_url: str = "https://consumo.tributos.gov.br/servico/calcular-tributos-consumo/api"
    calculadora_timeout: float = 60.0

    database_url: str = "sqlite:///./dados/rtp.db"

    # DF-e: liga a sincronização automática dentro do servidor web (senão, use `python -m app.cli dfe`).
    dfe_agendador: bool = False
    dfe_intervalo_minutos: int = 15
    dfe_timeout: float = 60.0
    # Limite de consultas por chave (XML completo após Ciência) em cada sincronização.
    dfe_max_consultas_chave: int = 10

    # Proteção mínima da interface web até a fase 4 (login multi-cliente). Vazio = sem senha.
    usuario: str = ""
    senha: str = ""


settings = Settings()
