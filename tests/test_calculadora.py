import os
from decimal import Decimal as D

import httpx
import pytest
import respx

from app.calculadora.client import CalculadoraIndisponivel, CalculadoraRTC, Chave

URL = "http://calc.test/api"
NOMINAIS = {2033: {"cbs": 8.8, "ibsEstadual": 8.85, "ibsMunicipal": 8.85}}


def _obj(n, vcbs, vuf, vmun, red=None):
    g = {"vCBS": vcbs}
    if red:
        g["gRed"] = {"pRedAliq": red}
    return {"nObj": n, "tribCalc": {"IBSCBS": {"gIBSCBS": {"gCBS": g, "gIBSUF": {"vIBSUF": vuf}, "gIBSMun": {"vIBSMun": vmun}}}}}


@respx.mock
def test_converte_resposta_em_aliquota_efetiva():
    rota = respx.post(f"{URL}/calculadora/regime-geral").mock(return_value=httpx.Response(200, json={
        # o lote é ordenado por NCM: 30049099 vem antes de 84713012
        "objetos": [_obj(1, "35.20", "35.40", "35.40", "60.00"), _obj(2, "88.00", "88.50", "88.50")]}))
    calc = CalculadoraRTC(URL)
    a, b = Chave("84713012", "000", "000001", 2033), Chave("30049099", "200", "200010", 2033)
    res = calc.aliquotas({a, b}, NOMINAIS, "SP", 3550308)
    assert res[a].total == D("26.5")
    assert res[b].cbs == D("3.52") and res[b].reducao_pct == D("60.00")
    calc.aliquotas({a, b}, NOMINAIS, "SP", 3550308)  # cache
    assert rota.call_count == 1


@respx.mock
def test_lote_com_erro_isola_item_invalido():
    def responder(request):
        itens = __import__("json").loads(request.content)["itens"]
        if any(i["cClassTrib"] == "999999" for i in itens):
            return httpx.Response(422, json={"detail": "cClassTrib inexistente"})
        return httpx.Response(200, json={"objetos": [_obj(1, "88.00", "88.50", "88.50")]})

    respx.post(f"{URL}/calculadora/regime-geral").mock(side_effect=responder)
    ok, ruim = Chave("84713012", "000", "000001", 2033), Chave("84713012", "000", "999999", 2033)
    res = CalculadoraRTC(URL).aliquotas({ok, ruim}, NOMINAIS, "SP", 3550308)
    assert res[ok].total == D("26.5") and not res[ok].erro
    assert "cClassTrib inexistente" in res[ruim].erro


@pytest.mark.skipif(not os.getenv("RTP_TESTE_ONLINE"), reason="defina RTP_TESTE_ONLINE=1 para chamar a calculadora real")
def test_contrato_calculadora_real():
    calc = CalculadoraRTC()
    integral, cesta = Chave("84713012", "000", "000001", 2033), Chave("10063021", "200", "200003", 2033)
    res = calc.aliquotas({integral, cesta}, NOMINAIS, "SP", 3550308)
    assert res[integral].total == D("26.5")
    assert res[cesta].total == 0 and res[cesta].reducao_pct == 100


@respx.mock
def test_calculadora_fora_do_ar_interrompe_sem_dividir_lote():
    rota = respx.post(f"{URL}/calculadora/regime-geral").mock(side_effect=httpx.ConnectError("recusada"))
    calc = CalculadoraRTC(URL)
    chaves = {Chave(f"8471{n:04d}", "000", "000001", 2033) for n in range(40)}
    with pytest.raises(CalculadoraIndisponivel):
        calc.aliquotas(chaves, NOMINAIS, "SP", 3550308)
    assert rota.call_count == 1                       # não tenta item a item nem divide o lote
    assert not calc._cache                            # nada guardado como "recusado"


def test_analise_nao_roda_sem_calculadora():
    from pathlib import Path

    from app.engine.cenarios import Empresa, analisar
    from app.engine.premissas import Premissas
    from app.ingest.arquivos import ler_caminho
    from tests.conftest import CalculadoraFake

    class Fora(CalculadoraFake):
        def versao(self):
            raise CalculadoraIndisponivel("calculadora indisponível em fake")

    docs = ler_caminho(Path(__file__).parent / "fixtures" / "xml").documentos
    with pytest.raises(CalculadoraIndisponivel):
        analisar(Empresa("11222333000181", "LOJA", "real", "SP", 3550308), docs, Premissas(anos=[2033]), Fora())
