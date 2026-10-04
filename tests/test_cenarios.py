from decimal import Decimal as D
from pathlib import Path

import pytest

from app.engine.cenarios import Empresa, analisar
from app.engine.premissas import Premissas
from app.ingest.arquivos import ler_caminho
from app.relatorios import agregacao
from app.relatorios.excel import exportar
from tests.conftest import CalculadoraFake

FX = Path(__file__).parent / "fixtures" / "xml"


@pytest.fixture
def analise():
    docs = ler_caminho(FX).documentos
    empresa = Empresa("11222333000181", "LOJA TESTE", "real", "SP", 3550308)
    return analisar(empresa, docs, Premissas(anos=[2027, 2033]), CalculadoraFake(), {"V-ARZ5": ("200", "200003")})


def _item(a, codigo):
    return next(r for r in a.itens if r.item.codigo == codigo)


def test_entrada_fornecedor_normal_lucro_real(analise):
    r = _item(analise, "NB-01")
    assert r.item.valor_liquido == D("744.15")
    assert r.atual.tributos == D("305.85")
    assert r.atual.creditos == D("180.00") + D("80.48")   # ICMS + 9,25% x (1000 + 50 IPI - 180 ICMS)
    v = r.anos[2033]
    assert (v.cbs, v.ibs) == (D("65.49"), D("131.71"))
    assert v.tributos == D("197.20")                     # ICMS, IPI, PIS e COFINS extintos
    assert v.creditos == v.cbs + v.ibs                   # crédito amplo
    assert v.custo_ou_receita(r.item.valor_liquido) == D("744.15")


def test_sem_credito_de_pis_cofins_em_compra_com_aliquota_zero(analise):
    arroz = _item(analise, "ARZ-5")               # PIS/COFINS CST 06 (alíquota zero) no fornecedor
    assert arroz.item.pis_cst == "06"
    assert arroz.atual.creditos == D("35.00")      # só o ICMS


def test_transicao_2027_mantem_icms(analise):
    v = _item(analise, "NB-01").anos[2027]
    assert v.cbs == (D("744.15") * D("8.7") / 100).quantize(D("0.01"))  # CBS - 0,1 p.p.
    # ICMS integral, mas recalculado sobre o preço de nota de 2027: sem PIS/COFINS, o mesmo valor líquido (744,15)
    # corresponde a uma nota de 744,15 / (1 - 0,18) = 907,50 -> ICMS 18% = 163,35. IPI/PIS/COFINS extintos.
    assert v.tributos == D("163.35") + v.cbs + v.ibs
    assert v.creditos == D("163.35") + v.cbs + v.ibs                   # crédito de ICMS acompanha a nota


def test_entrada_fornecedor_simples_perde_credito(analise):
    r = _item(analise, "POTE-10")
    assert r.atual.creditos == D("12.50") + D("46.25")
    assert r.anos[2033].creditos == D("20.00")         # 4% (premissa) sobre 500
    assert r.anos[2033].tributos == D("0")


def test_saida_classificacao_manual_e_cfop_ignorado(analise):
    arroz = _item(analise, "V-ARZ5")
    assert arroz.classificacao.origem == "manual"
    assert arroz.anos[2033].tributos == D("0")
    assert analise.ignorados == 1
    assert not any(r.item.codigo == "BRINDE" for r in analise.itens)
    nb = _item(analise, "V-NB01")
    assert nb.classificacao.origem == "padrao"
    assert nb.anos[2033].tributos == D("105.42") + D("212.03")


def test_agregacoes_e_excel(analise, tmp_path):
    res = agregacao.resumo(analise)
    assert [l["cenario"] for l in res] == ["Atual", "2027", "2033"]
    prod = {l["codigo"]: l for l in agregacao.por_produto(analise)}
    assert prod["V-ARZ5"]["var_preco_2033_pct"] < 0
    forn = {l["cnpj"]: l for l in agregacao.por_fornecedor(analise)}
    assert forn["77888999000155"]["var_custo_2033_pct"] > 0      # fornecedor do Simples encarece
    assert any("sem cClassTrib" in t for t in analise.alertas)
    destino = exportar(analise, tmp_path / "r.xlsx")
    assert destino.stat().st_size > 0


def test_nota_de_entrada_propria_de_produtor_rural(analise):
    tomate = _item(analise, "TOM-KG")              # nota emitida pela própria loja, fornecedor é o destinatário
    assert tomate.direcao == "entrada"
    assert (tomate.contraparte.nome, tomate.contraparte.regime) == ("JOSE PRODUTOR RURAL", "nao_contribuinte")
    assert tomate.atual.creditos == D("0")          # pessoa física: sem crédito de PIS/COFINS; ICMS isento
    assert tomate.anos[2033].creditos == D("0")     # crédito presumido padrão = 0 (premissa)
    assert any("não contribuinte" in t for t in analise.alertas)


def test_credito_presumido_de_produtor_rural_como_premissa():
    docs = ler_caminho(FX).documentos
    p = Premissas(anos=[2033], credito_presumido_nao_contribuinte_pct=D("5"))
    a = analisar(Empresa("11222333000181", "LOJA", "real", "SP", 3550308), docs, p, CalculadoraFake())
    assert _item(a, "TOM-KG").anos[2033].creditos == D("15.00")


def test_versao_da_calculadora_registrada(analise):
    assert analise.versao_calculadora.startswith("app 1.0-teste · base de regras V0000")
    premissas = {l["premissa"]: l["valor"] for l in agregacao.premissas(analise)}
    assert "V0000" in premissas["Calculadora RTC usada"]
