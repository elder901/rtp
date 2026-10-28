import sqlite3
import time

from sqlalchemy import func, select

from app import auth, db, models, servicos
from app.manutencao import excluir_estabelecimento, fazer_backup
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
