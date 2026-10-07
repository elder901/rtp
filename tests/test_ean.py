from decimal import Decimal as D
from pathlib import Path

from fastapi.testclient import TestClient

from app import classificacao, db, servicos
from app.api import main
from app.engine.cenarios import Classificacoes, Empresa, analisar
from app.engine.premissas import Premissas
from app.ingest.arquivos import ler_caminho
from app.parser.nfe import ler_nfe
from app.produto import gtin_global
from tests.conftest import CNPJ_EMPRESA, FX, CalculadoraFake

ARROZ, NOTEBOOK = "7890000000024", "7890000000017"


def test_gtin_global():
    assert gtin_global("7891000100103") == "7891000100103"
    assert gtin_global("7891000100104") == ""          # dígito verificador errado
    assert gtin_global("2001234000006") == ""          # balança/uso interno (prefixo 2)
    assert gtin_global("SEM GTIN") == ""


def test_prefere_ean_da_unidade_tributavel():
    xml = (FX / "xml" / "entrada_fornecedor_normal.xml").read_text(encoding="utf-8").replace(
        "<cProd>NB-01</cProd><cEAN>7890000000017</cEAN>",
        "<cProd>NB-01</cProd><cEAN>17891000100100</cEAN>").replace(
        "<vProd>1000.00</vProd></prod>",
        "<vProd>1000.00</vProd><cEANTrib>7891000100103</cEANTrib><uTrib>UN</uTrib><qTrib>12.0000</qTrib></prod>", 1)
    item = ler_nfe(xml.encode()).itens[0]
    assert (item.gtin, item.unidade_gtin, item.quantidade_gtin) == ("7891000100103", "UN", D("12"))
    assert item.chave == "ean:7891000100103"


def test_revisao_une_compra_e_venda_pelo_ean_e_sugere_pelo_fornecedor():
    docs = ler_caminho(FX / "xml").documentos
    produtos = {p.chave: p for p in classificacao.produtos(docs, CNPJ_EMPRESA, {})}
    arroz = produtos[f"ean:{ARROZ}"]
    assert arroz.codigos == ["V-ARZ5", "ARZ-5"] and arroz.lados == "venda e compra"
    assert arroz.sugestao == ("200", "200003") and arroz.sugestao_por_ean   # cClassTrib do fornecedor, mesmo EAN
    assert arroz.origem == "padrao"                                          # a venda não traz cClassTrib
    assert "cod:11222333:BRINDE" not in produtos                             # bonificação fica fora (CFOP)
    assert "cod:11222333:TOM-KG" in produtos                                 # sem GTIN: pelo código


def test_ajuste_por_ean_vale_para_compra_e_venda():
    docs = ler_caminho(FX / "xml").documentos
    a = analisar(Empresa(CNPJ_EMPRESA, "LOJA", "real", "SP", 3550308), docs, Premissas(anos=[2033]),
                 CalculadoraFake(), {f"ean:{ARROZ}": ("200", "200003")})
    arrozes = [r for r in a.itens if r.item.gtin == ARROZ]
    assert {r.direcao for r in arrozes} == {"entrada", "saida"}
    assert all(r.classificacao.origem == "manual" and r.classificacao.cclasstrib == "200003" for r in arrozes)


def test_ajustes_antigos_sem_prefixo_continuam_valendo():
    c = Classificacoes.de({"V-ARZ5": ("200", "200003"), "84713012": ("000", "000001")})
    assert c.por_codigo["V-ARZ5"] == ("200", "200003") and c.por_ncm["84713012"] == ("000", "000001")


def test_aplicar_sugestoes_por_ean_e_substituir_ajuste_antigo(banco, monkeypatch):
    monkeypatch.setattr(main, "calculadora", CalculadoraFake())
    with db.sessao() as s:
        e = servicos.criar_empresa(s, CNPJ_EMPRESA, "LOJA", "real", "SP", 3550308)
        servicos.importar_arquivos(s, e, [(p.name, p.read_bytes()) for p in (FX / "xml").glob("*.xml")])
        servicos.salvar_classificacao(s, e, "V-NB01", "000", "000001")          # formato antigo
        id_ = e.id
    c = TestClient(main.app)
    r = c.post(f"/empresas/{id_}/classificacao/sugestoes-ean")
    assert "sugestão(ões) por EAN aplicada(s)" in r.text
    with db.sessao() as s:
        e = s.get(servicos.models.Empresa, id_)
        ajustes = servicos.classificacoes(s, e)
        assert ajustes[f"ean:{ARROZ}"] == ("200", "200003")
        # regravar por código substitui a chave antiga sem prefixo
        servicos.salvar_classificacao(s, e, "cod:V-NB01", "000", "000001")
        s.flush()
        assert "V-NB01" not in servicos.classificacoes(s, e)


def test_revisao_mostra_classificacao_pelo_anexo():
    docs = ler_caminho(FX / "xml").documentos
    produtos = {p.chave: p for p in classificacao.produtos(docs, CNPJ_EMPRESA, {}, {"10063021": ("200", "200003")})}
    arroz = produtos[f"ean:{ARROZ}"]
    assert (arroz.cst, arroz.cclasstrib, arroz.origem) == ("200", "200003", "anexo")
    assert arroz.sugestao is None                     # a sugestão do fornecedor coincide com o que já está em uso


def test_mesmo_codigo_de_fornecedores_diferentes_nao_se_mistura():
    original = (FX / "xml" / "entrada_fornecedor_simples.xml").read_text(encoding="utf-8")
    outro = original.replace("<emit><CNPJ>77888999000155</CNPJ>", "<emit><CNPJ>55444333000122</CNPJ>")
    assert outro != original
    docs = [ler_nfe(original.encode()), ler_nfe(outro.encode())]
    assert [d.itens[0].chave for d in docs] == ["cod:77888999:POTE-10", "cod:55444333:POTE-10"]
    assert len(classificacao.produtos(docs, CNPJ_EMPRESA, {})) == 2

    empresa = Empresa(CNPJ_EMPRESA, "LOJA", "real", "SP", 3550308)
    a = analisar(empresa, docs, Premissas(anos=[2033]), CalculadoraFake(), {"cod:77888999:POTE-10": ("200", "200035")})
    assert [r.classificacao.origem for r in a.itens] == ["manual", "padrao"]     # só o do fornecedor ajustado
    a = analisar(empresa, docs, Premissas(anos=[2033]), CalculadoraFake(), {"cod:POTE-10": ("200", "200035")})
    assert [r.classificacao.origem for r in a.itens] == ["manual", "manual"]     # ajuste antigo vale para todos


def test_conflito_entre_o_que_o_fornecedor_declara_e_o_anexo():
    from app.engine.cenarios import declaracoes_fornecedores

    xml = (FX / "xml" / "entrada_fornecedor_normal.xml").read_text(encoding="utf-8")
    integral = xml.replace("<cClassTrib>200003</cClassTrib>", "<cClassTrib>000001</cClassTrib>").replace(
        "<CST>200</CST><cClassTrib>000001", "<CST>000</CST><cClassTrib>000001")
    assert integral != xml
    docs = [ler_nfe(integral.encode()), *[d for d in ler_caminho(FX / "xml").documentos if d.emitente.cnpj == CNPJ_EMPRESA]]
    entradas = [it for d in docs if d.direcao_para(CNPJ_EMPRESA) == "entrada" for it in d.itens]
    declaracoes = declaracoes_fornecedores(entradas, set())
    lista = classificacao.conflitos(docs, CNPJ_EMPRESA, {}, {"10063021": ("200", "200003")}, declaracoes)
    [c] = [c for c in lista if c.gtin == ARROZ]
    assert (c.tipo, c.em_uso[1], c.alternativa[1]) == ("fornecedor x anexo", "000001", "200003")
    assert c.impacto_vendas > 0                         # integral paga mais CBS/IBS na venda do que a cesta básica
