"""Manutenção do banco: cópia de segurança, cópia entre bancos e exclusão de estabelecimento.

No SQLite o backup usa a API de cópia do próprio SQLite: sai consistente mesmo com o servidor ligado e gravando. No
PostgreSQL (Supabase) o serviço já guarda os backups dele; o daqui é uma cópia independente num arquivo SQLite, feita
tabela a tabela (copiar_banco), que também serve para levar o banco de um lado para o outro.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from sqlalchemy import String, create_engine, delete, func, insert, select, text
from sqlalchemy.orm import Session

from app import models
from app.config import settings
from app.db import Base, url_sqlalchemy

LOTE = 1000


class ErroCopia(RuntimeError):
    pass


def arquivo_do_banco() -> Path | None:
    """O arquivo do banco SQLite em uso; None quando o banco é PostgreSQL."""
    url = settings.database_url
    return Path(url.removeprefix("sqlite:///")) if url.startswith("sqlite:///") else None


def fazer_backup(destino: Path, manter: int = 7) -> Path:
    """Copia o banco para `destino` (rtp-AAAA-MM-DD-HHMMSS.db) e apaga as cópias deste comando além das `manter`
    mais recentes."""
    origem = arquivo_do_banco()
    destino.mkdir(parents=True, exist_ok=True)
    arquivo = destino / f"rtp-{datetime.now():%Y-%m-%d-%H%M%S}.db"
    parcial = arquivo.with_suffix(".parcial")
    parcial.unlink(missing_ok=True)
    if origem is None:
        copiar_banco(settings.database_url, f"sqlite:///{parcial.as_posix()}")
    else:
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


def textos_longos_demais(url: str) -> list[str]:
    """Colunas com texto maior que o tamanho declarado: o SQLite aceita, o PostgreSQL recusa a cópia inteira."""
    engine, achados = create_engine(url_sqlalchemy(url)), []
    with engine.connect() as c:
        for tabela in Base.metadata.sorted_tables:
            for col in tabela.columns:
                if isinstance(col.type, String) and col.type.length:
                    n = c.scalar(select(func.count()).where(func.length(col) > col.type.length))
                    if n:
                        achados.append(f"{tabela.name}.{col.name}: {n} linha(s) acima de {col.type.length} caracteres")
    engine.dispose()
    return achados


def copiar_banco(origem: str, destino: str, progresso: Callable[[str], None] = lambda _: None) -> dict[str, int]:
    """Copia todas as tabelas do banco `origem` para o `destino` (endereços SQLAlchemy ou postgresql://), que precisa
    estar vazio: cria as tabelas, copia em lotes na ordem das chaves estrangeiras, acerta as sequências de id no
    PostgreSQL e confere a contagem de cada tabela. Devolve as linhas por tabela."""
    if problemas := textos_longos_demais(origem):
        raise ErroCopia("Corrija antes de copiar: " + "; ".join(problemas))
    de, para = create_engine(url_sqlalchemy(origem)), create_engine(url_sqlalchemy(destino))
    try:
        Base.metadata.create_all(para)
        with para.connect() as c:
            cheias = [t.name for t in Base.metadata.sorted_tables if c.scalar(select(func.count()).select_from(t))]
        if cheias:
            raise ErroCopia(f"O banco de destino já tem dados ({', '.join(cheias)}); a cópia é só para banco vazio.")
        linhas = {}
        for tabela in Base.metadata.sorted_tables:
            n = 0
            with de.connect() as leitura, para.begin() as escrita:
                resultado = leitura.execution_options(stream_results=True, yield_per=LOTE).execute(select(tabela))
                for lote in resultado.partitions(LOTE):
                    escrita.execute(insert(tabela), [dict(r._mapping) for r in lote])
                    n += len(lote)
                if para.dialect.name == "postgresql" and "id" in tabela.c and tabela.c.id.autoincrement:
                    escrita.execute(text(f"SELECT setval(pg_get_serial_sequence('{tabela.name}', 'id'), "
                                         f"coalesce(max(id), 1), max(id) IS NOT NULL) FROM {tabela.name}"))
            with para.connect() as c:
                copiadas = c.scalar(select(func.count()).select_from(tabela))
            if copiadas != n:
                raise ErroCopia(f"{tabela.name}: {n} linha(s) lidas e {copiadas} no destino.")
            linhas[tabela.name] = n
            progresso(f"{tabela.name}: {n} linha(s)")
        return linhas
    finally:
        de.dispose()
        para.dispose()


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
