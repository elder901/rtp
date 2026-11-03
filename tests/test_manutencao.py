import sqlite3
import time

from sqlalchemy import func, select

from app import auth, db, models, servicos
from app import manutencao
from app.manutencao import ErroCopia, copiar_banco, excluir_estabelecimento, fazer_backup
from tests.conftest import FX, SENHA_TESTE


def test_backup_consistente_e_guarda_so_as_ultimas(banco, tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "database_url", str(db.engine.url))
    with db.sessao() as s:
        servicos.criar_empresa(s, "11222333000181", "LOJA", "real", "SP", 3550308)
    copias = []
    for _ in range(3):
        copias.append(fazer_backup(tmp_path / "bk", manter=2))
        time.sleep(1.1)                                  # o nome leva os segundos
    restantes = sorted((tmp_path / "bk").glob("rtp-*.db"))
    assert restantes == copias[1:]
    con = sqlite3.connect(restantes[-1])
    assert con.execute("select nome from empresa").fetchall() == [("LOJA",)]
    con.close()


def test_excluir_estabelecimento_apaga_os_dados_dele(banco):
    xmls = [(p.name, p.read_bytes()) for p in sorted((FX / "xml").glob("*.xml"))]
    with db.sessao() as s:
        matriz = servicos.criar_empresa(s, "11222333000181", "LOJA", "real", "SP", 3550308)
        filial = servicos.criar_empresa(s, "11222333000262", "LOJA 2", "real", "SP", 3550308)
        servicos.importar_arquivos(s, matriz, xmls)
        auth.criar_usuario(s, "Comprador", "c@teste.com.br", SENHA_TESTE, "consulta", ["11222333"])
        matriz_id, filial_id = matriz.id, filial.id
    with db.sessao() as s:
        apagadas = excluir_estabelecimento(s, s.get(models.Empresa, matriz_id))
        assert apagadas["documento"] == 4 and apagadas["empresa"] == 1 and "acesso_empresa" not in apagadas
    with db.sessao() as s:
        assert s.scalar(select(func.count()).select_from(models.DocumentoFiscal)) == 0
        assert s.scalar(select(func.count()).select_from(models.AcessoEmpresa)) == 1   # a filial continua
        excluir_estabelecimento(s, s.get(models.Empresa, filial_id))
    with db.sessao() as s:
        assert s.scalar(select(func.count()).select_from(models.AcessoEmpresa)) == 0   # último da empresa


def _banco_com_dados(tmp_path):
    xmls = [(p.name, p.read_bytes()) for p in sorted((FX / "xml").glob("*.xml"))]
    with db.sessao() as s:
        e = servicos.criar_empresa(s, "11222333000181", "LOJA", "real", "SP", 3550308)
        servicos.importar_arquivos(s, e, xmls)
        auth.criar_usuario(s, "Comprador", "c@teste.com.br", SENHA_TESTE, "consulta", ["11222333"])
    return str(db.engine.url)


def test_copia_o_banco_inteiro_para_outro_vazio_e_confere(banco, tmp_path):
    import pytest

    origem = _banco_com_dados(tmp_path)
    destino = f"sqlite:///{(tmp_path / 'novo.db').as_posix()}"
    linhas = copiar_banco(origem, destino)
    assert linhas["documento"] == 4 and linhas["empresa"] == 1 and linhas["usuario"] == 1
    con = sqlite3.connect(tmp_path / "novo.db")
    assert con.execute("select nome from empresa").fetchall() == [("LOJA",)]
    assert con.execute("select count(*) from documento where xml_gz is not null").fetchone()[0] == 4
    con.close()
    with pytest.raises(ErroCopia, match="já tem dados"):
        copiar_banco(origem, destino)                               # nunca mistura com dados que já estão lá


def test_texto_maior_que_a_coluna_impede_a_copia(banco, tmp_path):
    import pytest

    origem = _banco_com_dados(tmp_path)
    with db.engine.begin() as c:                                    # o SQLite aceita; o PostgreSQL recusaria
        c.exec_driver_sql("update empresa set uf = 'SPX'")
    with pytest.raises(ErroCopia, match="empresa.uf: 1 linha"):
        copiar_banco(origem, f"sqlite:///{(tmp_path / 'novo.db').as_posix()}")


def test_backup_do_banco_que_nao_e_arquivo_sai_num_sqlite(banco, tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "database_url", _banco_com_dados(tmp_path))
    monkeypatch.setattr(manutencao, "arquivo_do_banco", lambda: None)   # como no PostgreSQL
    arquivo = fazer_backup(tmp_path / "bk")
    con = sqlite3.connect(arquivo)
    assert con.execute("select count(*) from documento").fetchone()[0] == 4
    con.close()


def test_url_do_supabase_usa_o_psycopg():
    assert db.url_sqlalchemy("postgresql://u:s@h:5432/postgres") == "postgresql+psycopg://u:s@h:5432/postgres"
    assert db.url_sqlalchemy("postgres://u:s@h/p") == "postgresql+psycopg://u:s@h/p"
    assert db.url_sqlalchemy("sqlite:///x.db") == "sqlite:///x.db"
