"""Banco de dados (SQLite por padrão; PostgreSQL via RTP_DATABASE_URL)."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


def _engine(url: str):
    kwargs = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        caminho = url.removeprefix("sqlite:///")
        if caminho and caminho != ":memory:":
            Path(caminho).parent.mkdir(parents=True, exist_ok=True)
    return create_engine(url, **kwargs)


engine = _engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def configurar(url: str):
    """Troca o banco (usado nos testes)."""
    global engine
    engine = _engine(url)
    SessionLocal.configure(bind=engine)
    criar_tabelas()


def criar_tabelas():
    from app import models  # noqa: F401 — registra os modelos

    Base.metadata.create_all(engine)
    # create_all só cria os índices das tabelas que ele mesmo cria: os novos, em tabelas que já existiam, vêm aqui.
    for tabela in Base.metadata.sorted_tables:
        for indice in tabela.indexes:
            indice.create(engine, checkfirst=True)


@contextmanager
def sessao() -> Session:
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
