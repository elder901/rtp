"""Manutenção do banco: cópia de segurança e exclusão de estabelecimento.

O backup usa a API de cópia do SQLite: sai consistente mesmo com o servidor ligado e gravando. No PostgreSQL a cópia
é do próprio servidor de banco (pg_dump ou o backup do serviço gerenciado).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app import models
from app.config import settings
from app.db import Base


def arquivo_do_banco() -> Path:
    url = settings.database_url
    if not url.startswith("sqlite:///"):
        raise ValueError("O backup automático é só para SQLite; no PostgreSQL use o backup do servidor de banco.")
    return Path(url.removeprefix("sqlite:///"))


def fazer_backup(destino: Path, manter: int = 7) -> Path:
    """Copia o banco para `destino` (rtp-AAAA-MM-DD-HHMMSS.db) e apaga as cópias deste comando além das `manter`
    mais recentes."""
    origem = arquivo_do_banco()
    destino.mkdir(parents=True, exist_ok=True)
    arquivo = destino / f"rtp-{datetime.now():%Y-%m-%d-%H%M%S}.db"
    parcial = arquivo.with_suffix(".parcial")
    fonte = sqlite3.connect(f"file:{origem.as_posix()}?mode=ro", uri=True)
    copia = sqlite3.connect(parcial)
    try:
        fonte.backup(copia)
    finally:
        copia.close()
        fonte.close()
    parcial.replace(arquivo)                     # só vira rtp-*.db quando a cópia termina
    for antigo in sorted(destino.glob("rtp-*.db"))[:-manter]:
        antigo.unlink()
    return arquivo


def excluir_estabelecimento(s: Session, e: models.Empresa) -> dict[str, int]:
    """Apaga o estabelecimento e tudo o que é dele (notas, SPED, classificação, certificado, apuração). Se era o
    último da empresa (raiz do CNPJ), apaga também os acessos dos usuários a ela. Devolve as linhas apagadas."""
    apagadas = {}
    for tabela in reversed(Base.metadata.sorted_tables):
        if "empresa_id" in tabela.c:
            n = s.execute(delete(tabela).where(tabela.c.empresa_id == e.id)).rowcount
            if n:
                apagadas[tabela.name] = n
    raiz = e.cnpj[:8]
    s.execute(delete(models.Empresa).where(models.Empresa.id == e.id))
    s.expunge(e)
    apagadas["empresa"] = 1
    restantes = s.scalar(select(func.count()).select_from(models.Empresa).where(models.Empresa.cnpj.like(f"{raiz}%")))
    if not restantes:
        n = s.execute(delete(models.AcessoEmpresa).where(models.AcessoEmpresa.raiz == raiz)).rowcount
        if n:
            apagadas["acesso_empresa"] = n
    return apagadas
