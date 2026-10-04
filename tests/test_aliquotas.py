from decimal import Decimal as D

import httpx
import respx

from app.calculadora.client import CalculadoraRTC, Chave
from app.engine.cenarios import Empresa, analisar
from app.engine.premissas import Premissas
from app.ingest.arquivos import ler_caminho
from app.relatorios import agregacao
from tests.conftest import CNPJ_EMPRESA, FX, CalculadoraFake

URL = "http://calc.test/api"


def _resposta(vcbs):
    return httpx.Response(200, json={"objetos": [{"nObj": 1, "tribCalc": {"IBSCBS": {"gIBSCBS": {
        "gCBS": {"vCBS": vcbs}, "gIBSUF": {"vIBSUF": "0"}, "gIBSMun": {"vIBSMun": "0"}}}}}]})


@respx.mock
def test_cache_considera_as_aliquotas_informadas():
    """Mesma NCM/classificação/ano com alíquotas diferentes (outra empresa ou outro município) não reaproveita."""
    rota = respx.post(f"{URL}/calculadora/regime-geral").mock(side_effect=[_resposta("88.00"), _resposta("92.00")])
    calc = CalculadoraRTC(URL)
    chave = Chave("84713012", "000", "000001", 2033)
    a = calc.aliquotas({chave}, {2033: {"cbs": 8.8, "ibsEstadual": 0.0, "ibsMunicipal": 0.0}}, "SP", 1)
    b = calc.aliquotas({chave}, {2033: {"cbs": 9.2, "ibsEstadual": 0.0, "ibsMunicipal": 0.0}}, "SP", 1)
    assert (a[chave].cbs, b[chave].cbs) == (D("8.8"), D("9.2"))
    calc.aliquotas({chave}, {2033: {"cbs": 8.8, "ibsEstadual": 0.0, "ibsMunicipal": 0.0}}, "SP", 1)
    assert rota.call_count == 2


@respx.mock
def test_aliquota_oficial():
    base = f"{URL}/calculadora/dados-abertos"
    respx.get(f"{base}/aliquota-uniao", params={"data": "2026-06-01"}).mock(
        return_value=httpx.Response(200, json={"aliquotaReferencia": 0.9}))
    respx.get(f"{base}/aliquota-uniao", params={"data": "2027-01-15"}).mock(
        return_value=httpx.Response(404, json={"title": "Alíquota não encontrada"}))
    respx.get(f"{base}/aliquota-uf", params={"codigoUf": "31", "data": "2026-06-01"}).mock(
        return_value=httpx.Response(200, json={"aliquotaReferencia": 0.1, "aliquotaPropria": 0.12}))
    calc = CalculadoraRTC(URL)
    assert calc.aliquota_oficial("uniao", "2026-06-01") == D("0.9")
    assert calc.aliquota_oficial("uniao", "2027-01-15") is None              # ainda não publicada
    assert calc.aliquota_oficial("uf", "2026-06-01", 31) == D("0.12")         # alíquota própria do estado


class CalculadoraComCbsOficial2027(CalculadoraFake):
    def aliquota_oficial(self, esfera, data, codigo=None):
        return D("9.0") if esfera == "uniao" and data.startswith("2027") else None


def _analise(calc):
    docs = ler_caminho(FX / "xml").documentos
    return analisar(Empresa(CNPJ_EMPRESA, "LOJA", "real", "SP", 3550308), docs, Premissas(anos=[2027, 2033]), calc)


def test_oficial_tem_precedencia_e_premissa_cobre_o_que_falta():
    a = _analise(CalculadoraComCbsOficial2027())
    u27, u33 = a.aliquotas_usadas[2027], a.aliquotas_usadas[2033]
    assert u27["aliquotas"]["cbs"] == 9.0 and u27["origens"]["cbs"] == "oficial"
    assert u27["origens"]["ibsEstadual"] == "premissa"                        # IBS 2027 ainda sem publicação
    assert u33["aliquotas"]["cbs"] == 8.8 and u33["origens"]["cbs"] == "premissa"
    nb = next(r for r in a.itens if r.item.codigo == "NB-01")
    assert nb.anos[2027].cbs == (D("744.15") * D("0.09")).quantize(D("0.01"))
    assert any("ainda não publicadas" in t for t in a.alertas)
    premissas = {l["premissa"]: l["valor"] for l in agregacao.premissas(a)}
    assert "9.00 (oficial)" in premissas["Alíquotas nominais 2027 (CBS / IBS UF / IBS mun.)"]


def test_calculadora_fora_do_ar_mantem_premissa():
    class Fora(CalculadoraFake):
        def aliquota_oficial(self, esfera, data, codigo=None):
            raise RuntimeError("timeout")

    a = _analise(Fora())
    assert a.aliquotas_usadas[2027]["origens"]["cbs"] == "premissa (calculadora indisponível)"
    assert a.aliquotas_usadas[2027]["aliquotas"]["cbs"] == 8.7               # referência 8,8 - 0,1 p.p.
