from decimal import Decimal as D

from app.engine.cenarios import Empresa, analisar
from app.engine.premissas import Premissas
from app.ingest.consolidacao import ConsolidadorNFCe
from app.parser.nfe import ler_nfe
from tests.conftest import CNPJ_EMPRESA, FX, CalculadoraFake


def _cupons(n: int):
    """A nota de venda da loja transformada em n NFC-e (modelo 65) do mesmo mês, com chaves diferentes."""
    xml = (FX / "xml" / "saida_venda.xml").read_text(encoding="utf-8").replace("<mod>55</mod>", "<mod>65</mod>")
    return [ler_nfe(xml.replace("NFe3526", f"NFe35{26 + i:02d}"[:9], 1).encode()) for i in range(n)]


def test_consolida_cupons_do_mes_sem_mudar_o_resultado():
    cupons = _cupons(3)
    assert {d.modelo for d in cupons} == {"65"}
    c = ConsolidadorNFCe()
    for d in cupons:
        c.adicionar(d)
    [mes] = c.documentos()
    assert mes.modelo == "65" and mes.numero == "3 cupons" and len(mes.itens) == len(cupons[0].itens)
    assert sum(i.v_prod for i in mes.itens) == 3 * sum(i.v_prod for i in cupons[0].itens)

    empresa, p = Empresa(CNPJ_EMPRESA, "LOJA", "real", "SP", 3550308), Premissas(anos=[2027, 2033])
    soma = lambda a, ano: sum((r.anos[ano].tributos for r in a.itens), D(0))
    separado, junto = analisar(empresa, cupons, p, CalculadoraFake()), analisar(empresa, [mes], p, CalculadoraFake())
    for ano in (2027, 2033):
        assert abs(soma(separado, ano) - soma(junto, ano)) <= D("0.05")   # só arredondamento por item
    assert sum(r.atual.tributos for r in separado.itens) == sum(r.atual.tributos for r in junto.itens)
