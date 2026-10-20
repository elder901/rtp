import json
import re

from app import db, servicos
from app.exportacao import exportar_tenant, nome_de_pasta
from tests.conftest import CNPJ_EMPRESA, FX


def test_pasta_do_tenant_por_estabelecimento_modelo_mes_e_lado(banco, tmp_path):
    xmls = [(p.name, p.read_bytes()) for p in sorted((FX / "xml").glob("*.xml"))]
    with db.sessao() as s:
        e = servicos.criar_empresa(s, CNPJ_EMPRESA, "LOJA TESTE", "real", "SP", 3550308)
        servicos.importar_arquivos(s, e, xmls)
        filial = servicos.criar_empresa(s, CNPJ_EMPRESA[:8] + "000262", "Loja Teste Filial", "real", "SP", 3550308)
        outra = servicos.criar_empresa(s, "99888777000166", "Outra Empresa", "real", "SP", 3550308)
        pasta = exportar_tenant(s, CNPJ_EMPRESA[:8], tmp_path, progresso=lambda _: None)
        arquivos = sorted(p.relative_to(pasta).as_posix() for p in pasta.rglob("*.xml"))

        assert pasta.name == f"{CNPJ_EMPRESA[:8]}-loja-teste"
        resumo = json.loads((pasta / "tenant.json").read_text(encoding="utf-8"))
        assert [x["cnpj"] for x in resumo["estabelecimentos"]] == [e.cnpj, filial.cnpj]   # matriz primeiro, sem a outra raiz
        assert outra.cnpj not in (pasta / "tenant.json").read_text(encoding="utf-8")
        matriz = resumo["estabelecimentos"][0]
        assert matriz["gravados"] == len(arquivos) == matriz["nfe_entrada"] + matriz["nfe_saida"] + matriz["nfce"]
        assert all(a.startswith(f"{CNPJ_EMPRESA}-loja-teste/") for a in arquivos)
        assert any("/NFe/" in a and "/entrada/" in a for a in arquivos) and any("/saida/" in a for a in arquivos)
        assert all(re.fullmatch(r"\d{44}(-cancelada)?\.xml", a.rsplit("/", 1)[1]) for a in arquivos)
        assert all(re.search(r"/NFC?e/\d{4}-\d{2}/", a) for a in arquivos)

        # Rodar de novo não regrava: só acrescenta o que for novo
        exportar_tenant(s, CNPJ_EMPRESA[:8], tmp_path, progresso=lambda _: None)
        de_novo = json.loads((pasta / "tenant.json").read_text(encoding="utf-8"))["estabelecimentos"][0]
        assert de_novo["gravados"] == 0 and de_novo["ja_existiam"] == len(arquivos)


def test_nome_de_pasta_sem_acento_nem_espaco():
    assert nome_de_pasta("Supermercado Tejotão Lj02 - Melo Viana ") == "supermercado-tejotao-lj02-melo-viana"
