"""Caso do papel higiênico (Anexo VIII, redução de 60%) comprado da indústria a R$ 20,00 e vendido a R$ 28,90 em MG:
o crédito de PIS/COFINS (9,25%) dá lugar ao de CBS/IBS reduzido; o custo só se mantém se o preço de nota cair."""
from decimal import Decimal as D

import pytest

from app.engine.cenarios import Empresa, analisar
from app.engine.premissas import Premissas
from app.ingest.arquivos import ler_caminho
from app.relatorios import negociacao
from tests.conftest import FX, CalculadoraFake

EAN = "7899000000010"


@pytest.fixture
def analise():
    docs = ler_caminho(FX / "papel").documentos
    return analisar(Empresa("12345678000195", "EXEMPLO", "real", "MG", 3103504), docs,
                    Premissas(anos=[2027, 2029, 2033]), CalculadoraFake(), {f"ean:{EAN}": ("200", "200035")})


@pytest.mark.parametrize("ano, equilibrio, var_eq, custo_precos_hoje, var_custo", [
    (2027, "18.15", "-9.25", "16.40", "10.19"),     # crédito 9,25% -> 3,52%: sem repasse o custo sobe 10,2%
    (2029, "17.76", "-11.20", "16.76", "12.61"),
    (2033, "14.88", "-25.58", "20.00", "34.38"),
])
def test_preco_de_nota_de_equilibrio(analise, ano, equilibrio, var_eq, custo_precos_hoje, var_custo):
    c = negociacao.compras(analise, ano)[0]
    assert c.preco_nota_unit == D("20.0000") and c.custo_unit_atual == D("14.8830")
    assert c.preco_nota_equilibrio_unit.quantize(D("0.01")) == D(equilibrio)
    assert c.variacao_preco_nota_equilibrio_pct == D(var_eq)
    assert c.custo_unit_precos_hoje.quantize(D("0.01")) == D(custo_precos_hoje)
    assert c.variacao_custo_precos_hoje_pct == D(var_custo)
    assert c.custo_unit_ano.quantize(D("0.01")) == D("14.88")          # com repasse, custo mantido


def test_icms_recalculado_sobre_o_preco_de_nota_de_2027(analise):
    compra = next(r for r in analise.itens if r.direcao == "entrada")
    v = compra.anos[2027]
    # Mesmo valor líquido (148,83): nota de 148,83 / 0,82 = 181,50 -> ICMS 18% = 32,67 (era 36,00 na nota de 200,00)
    assert v.tributos - v.cbs - v.ibs == D("32.67")


def test_cenarios_de_preco_e_margem(analise):
    p = negociacao.produtos(analise, negociacao.compras(analise, 2033), 2033)[0]
    assert (p.unidade_venda, p.preco_venda_unit) == ("PCT", D("28.9000"))
    hoje, gondola_mantem, gondola_repassa, margem_mantem, margem_repassa = p.cenarios_preco(2033)
    assert (hoje.preco, hoje.custo, hoje.margem) == (D("28.90"), D("14.88"), D("6.62"))
    assert (gondola_mantem.custo, gondola_mantem.margem) == (D("20.00"), D("6.13"))   # fornecedor fica com o ganho
    assert gondola_repassa.margem == D("11.25")
    assert margem_mantem.margem == D("6.62") and margem_repassa.preco == D("23.79")
    assert p.margem_ano_precos_hoje(2033) is not None
    assert p.margem_ano_precos_hoje(2033).margem == D("6.13")

    p27 = negociacao.produtos(analise, negociacao.compras(analise, 2027), 2027)[0]
    # 2027, ninguém muda preço: a perda de crédito na compra é compensada pelo fim do PIS/COFINS na venda
    assert abs(p27.cenarios_preco(2027)[1].margem - D("6.62")) <= D("0.01")
