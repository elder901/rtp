import os
from decimal import Decimal as D

import pytest

from app.calculadora.client import AliquotaEfetiva, CalculadoraRTC, Chave
from app.engine.cenarios import Empresa, analisar
from app.engine.premissas import Premissas
from app.ingest.arquivos import ler_conteudos
from app.relatorios import negociacao
from tests.conftest import CNPJ_EMPRESA, FX, CalculadoraFake

REFRI = "22021000"


class CalculadoraComIS(CalculadoraFake):
    """Imita a calculadora para o IS: refrigerante 10% ad valorem; cigarro 13% + R$ 2,13 por VN."""

    def imposto_seletivo(self, ncm, data="2027-01-15"):
        if ncm == REFRI:
            return {"tributadoPeloImpostoSeletivo": True, "aliquotaAdValorem": 10.0, "unidade": None}
        if ncm == "24022000":
            return {"tributadoPeloImpostoSeletivo": True, "aliquotaAdValorem": 13.0, "aliquotaAdRem": 2.13, "unidade": "VN"}
        return None

    def aliquotas(self, chaves, nominais_por_ano, uf, municipio):
        out = super().aliquotas(chaves, nominais_por_ano, uf, municipio)
        for c, a in out.items():
            if c.is_cst:
                nominal, ad_rem = (D("13"), D("2.13")) if c.ncm == "24022000" else (D("10"), D("0"))
                cobrado = c.is_cst == "000"
                a.is_nominal_pct, a.is_ad_rem_nominal, a.is_unidade = nominal, ad_rem, "VN" if ad_rem else "UN"
                a.is_pct, a.is_ad_rem = (nominal, ad_rem) if cobrado else (D(0), D(0))
        return out


def _nota(origem: str, chave: str, cfop: str, ncm: str = REFRI, unidade: str = "UN") -> bytes:
    xml = (FX / "xml" / origem).read_text(encoding="utf-8")
    codigo = "NB-01" if "entrada" in origem else "V-NB01"
    return (xml.replace(xml[xml.index('Id="NFe') + 7: xml.index('Id="NFe') + 51], chave)
               .replace("<NCM>84713012</NCM><CFOP>5102</CFOP>", f"<NCM>{ncm}</NCM><CFOP>{cfop}</CFOP>")
               .replace("<NCM>84713012</NCM><CFOP>6102</CFOP>", f"<NCM>{ncm}</NCM><CFOP>{cfop}</CFOP>")
               .replace(f"<cProd>{codigo}</cProd>", f"<cProd>{codigo}-{cfop}</cProd>")
               .replace("<uCom>UN</uCom><qCom>1.0000</qCom><vProd>1000.00</vProd>",
                        f"<uCom>{unidade}</uCom><qCom>1.0000</qCom><vProd>1000.00</vProd>")).encode()


def _analise(calc, *notas):
    docs = ler_conteudos([(f"n{i}.xml", n) for i, n in enumerate(notas)]).documentos
    return analisar(Empresa(CNPJ_EMPRESA, "LOJA", "real", "SP", 3550308), docs, Premissas(anos=[2033]), calc)


def _itens(a, ncm=REFRI):
    return {(r.direcao, r.item.cfop): r for r in a.itens if r.item.ncm == ncm}


def test_refrigerante_fabricante_revendedor_e_venda():
    a = _analise(CalculadoraComIS(),
                 _nota("entrada_fornecedor_normal.xml", "3" * 44, "5401"),   # engarrafadora: produção própria
                 _nota("entrada_fornecedor_normal.xml", "4" * 44, "5405"),   # distribuidor: revenda com ST
                 _nota("saida_venda.xml", "5" * 44, "6405"))                 # venda do supermercado
    itens = _itens(a)
    fabricante, distribuidor, venda = itens[("entrada", "5401")], itens[("entrada", "5405")], itens[("saida", "6405")]

    v = fabricante.anos[2033]
    assert fabricante.imposto_seletivo == "cobrado (fabricante)"
    assert v.imposto_seletivo == D("74.42")                                 # 10% sobre 744,15
    assert v.cbs == ((D("744.15") + D("74.42")) * D("0.088")).quantize(D("0.01"))  # IS na base da CBS
    assert v.creditos == v.cbs + v.ibs                                       # IS não gera crédito
    assert v.custo_ou_receita(D("744.15")) == D("744.15") + D("74.42")

    assert distribuidor.imposto_seletivo == "embutido (revendedor, estimado)"
    assert distribuidor.anos[2033].imposto_seletivo == D("74.42")            # repasse padrão 100%

    assert venda.imposto_seletivo == "não incide (revenda)"
    assert venda.anos[2033].imposto_seletivo == D("0")
    assert venda.anos[2033].cbs == (D("1197.90") * D("0.088")).quantize(D("0.01"))
    assert any("Imposto Seletivo em 3 item(ns)" in t for t in a.alertas)
    assert any("repasse de 100%" in t for t in a.alertas)

    # nas telas de negociação o custo já inclui o IS (as duas compras são do mesmo fornecedor e do mesmo EAN)
    compra = next(c for c in negociacao.compras(a, 2033) if c.ncm == REFRI)
    assert compra.custo_ano == 2 * D("818.57") and compra.quantidade == 2
    assert compra.imposto_seletivo_ano == 2 * D("74.42")


def test_repasse_do_revendedor_e_premissa():
    p = Premissas(anos=[2033], repasse_is_revendedor_pct=D("50"))
    docs = ler_conteudos([("d.xml", _nota("entrada_fornecedor_normal.xml", "4" * 44, "5405"))]).documentos
    a = analisar(Empresa(CNPJ_EMPRESA, "LOJA", "real", "SP", 3550308), docs, p, CalculadoraComIS())
    assert _itens(a)[("entrada", "5405")].anos[2033].imposto_seletivo == D("37.21")


def test_cigarro_ad_rem_e_unidade_incompativel():
    a = _analise(CalculadoraComIS(),
                 _nota("entrada_fornecedor_normal.xml", "6" * 44, "5101", "24022000", "MC"),
                 _nota("entrada_fornecedor_normal.xml", "7" * 44, "5101", "24022000", "CX"))
    itens = {r.item.unidade: r for r in a.itens if r.item.ncm == "24022000"}
    assert itens["MC"].anos[2033].imposto_seletivo == (D("744.15") * D("0.13") + D("2.13")).quantize(D("0.01"))
    assert itens["CX"].anos[2033].imposto_seletivo == (D("744.15") * D("0.13")).quantize(D("0.01"))
    assert any("parte ad rem do IS" in t and "(CX)" in t for t in a.alertas)


def test_sem_is_nada_muda():
    a = _analise(CalculadoraComIS(), _nota("entrada_fornecedor_normal.xml", "8" * 44, "5102", "84713012"))
    r = a.itens[0]
    assert r.imposto_seletivo == "" and r.anos[2033].imposto_seletivo == D("0")


@pytest.mark.skipif(not os.getenv("RTP_TESTE_ONLINE"), reason="defina RTP_TESTE_ONLINE=1 para chamar a calculadora real")
def test_contrato_is_calculadora_real():
    calc = CalculadoraRTC()
    nominais = {2027: Premissas().aliquotas_nominais(2027)}
    fab, rev = Chave(REFRI, "000", "000001", 2027, "000", "000001"), Chave(REFRI, "000", "000001", 2027, "200", "200007")
    res = calc.aliquotas({fab, rev}, nominais, "SP", 3550308)
    assert res[fab].is_pct == D("10.00") and not res[fab].erro
    assert res[rev].is_pct == 0 and res[rev].is_nominal_pct == D("10.00")
    assert calc.imposto_seletivo(REFRI) and calc.imposto_seletivo("84713012") is None
