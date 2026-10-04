import json
from decimal import Decimal as D

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import db, models, servicos
from app.api import main
from app.apuracao import conciliacao
from app.apuracao import servico as apuracao
from app.apuracao.cliente import ClienteApuracao, ErroApuracao, Situacao
from app.config import settings
from tests.conftest import CNPJ_EMPRESA, FX, CalculadoraFake

PRR = settings.apuracao_url_prr
CHAVE_ENTRADA = "35261044555666000177550010000012341000012345"   # CBS destacada 6,70
CHAVE_SAIDA = "35261011222333000181550010000000101000000101"     # sem grupo IBSCBS
CHAVE_FORA = "35261099999999000199550010000000011000000011"


def arquivo(tipo: str, itens: list[tuple[str, float, int]], pa="03/2026", ni="11222333"):
    lista = [{"origem": origem, "documento": 55, "chave": chave, "emissao": "2026-03-10T13:00:00Z",
              "registro": "2026-03-11T10:00:00.123456789Z", "atualizacao": "2026-03-11T10:00:00Z",
              "cbs": {"apurado": valor, "saldoDevedor": valor} if tipo == "debitos" else {"apurado": valor}}
             for chave, valor, origem in itens]
    return {"tiqueteSolicitacao": "t-1", "ni": ni, "geradoEm": "2026-03-12T00:00:00Z",
            "apuracao": [{"pa": pa, tipo: lista}]}


@pytest.fixture
def empresa(banco):
    with db.sessao() as s:
        e = servicos.criar_empresa(s, CNPJ_EMPRESA, "LOJA TESTE", "real", "SP", 3550308)
        servicos.importar_arquivos(s, e, [(p.name, p.read_bytes()) for p in (FX / "xml").glob("*.xml")])
        return e.id


# --- Cliente HTTP ---------------------------------------------------------------------------------------------

@respx.mock
def test_cliente_oauth_solicitacao_situacao_download():
    token = respx.post(settings.apuracao_token_url).mock(
        return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 3600}))
    abre = respx.post(f"{PRR}/debitos/11222333").mock(
        return_value=httpx.Response(201, json={"tiqueteSolicitacao": "abc.123", "tEASegundos": "120"}))
    respx.get(f"{PRR}/situacao/abc.123").mock(return_value=httpx.Response(200, json={
        "estado": "CONCLUIDA", "urlAssinada": "https://storage.exemplo/arq.json?X-Amz-Signature=x",
        "urlAssinadaExpiraEm": "2026-03-14T00:00:00Z"}))
    download = respx.get("https://storage.exemplo/arq.json").mock(return_value=httpx.Response(200, json={"ok": 1}))

    c = ClienteApuracao("id", "segredo", "prr")
    assert c.solicitar("debitos", CNPJ_EMPRESA, "https://rtp.exemplo/webhooks/apuracao-cbs/x") == ("abc.123", 120)
    pedido_token = token.calls[0].request
    assert pedido_token.headers["authorization"].startswith("Basic ")
    assert b"grant_type=client_credentials" in pedido_token.content
    assert abre.calls[0].request.headers["authorization"] == "Bearer tok"
    assert json.loads(abre.calls[0].request.content) == {"urlRetorno": "https://rtp.exemplo/webhooks/apuracao-cbs/x"}

    sit = c.situacao("abc.123")
    assert sit.estado == "CONCLUIDA"
    assert c.baixar(sit.url_assinada) == {"ok": 1}
    assert "authorization" not in download.calls[0].request.headers   # URL pré-assinada não leva token
    assert token.call_count == 1                                       # token reaproveitado


@respx.mock
def test_cliente_erros():
    respx.post(settings.apuracao_token_url).mock(return_value=httpx.Response(401, json={"error": "invalid_client"}))
    with pytest.raises(ErroApuracao, match="credenciais"):
        ClienteApuracao("id", "errado").solicitar("debitos", CNPJ_EMPRESA, "https://x")
    with pytest.raises(ErroApuracao, match="HTTPS"):
        ClienteApuracao("id", "s").baixar("http://inseguro/arquivo.json")


# --- Serviço: solicitação, limite, webhook, importação ------------------------------------------------------------

class ClienteFake:
    def __init__(self, arquivo_retorno=None):
        self.arquivo = arquivo_retorno
        self.urls = []

    def solicitar(self, tipo, cnpj, url_retorno):
        self.urls.append(url_retorno)
        return f"tiq-{len(self.urls)}", 60

    def situacao(self, tiquete):
        return Situacao("CONCLUIDA", "https://storage.exemplo/a.json")

    def baixar(self, url):
        return self.arquivo


def test_solicitacao_exige_webhook_publico_e_respeita_limite(empresa, monkeypatch):
    with db.sessao() as s:
        e = s.get(models.Empresa, empresa)
        with pytest.raises(ErroApuracao, match="webhook HTTPS"):
            apuracao.solicitar(s, e, "debitos", ClienteFake())
        monkeypatch.setattr(settings, "url_publica", "https://rtp.exemplo")
        cliente = ClienteFake()
        for _ in range(4):
            assert apuracao.solicitar(s, e, "debitos", cliente).estado == "PENDENTE"
        assert cliente.urls[0].startswith("https://rtp.exemplo/webhooks/apuracao-cbs/")
        with pytest.raises(ErroApuracao, match="Limite"):
            apuracao.solicitar(s, e, "debitos", cliente)
        assert apuracao.solicitar(s, e, "creditos", cliente).estado == "PENDENTE"  # limite é por tipo


def test_importacao_incremental_e_cnpj(empresa):
    with db.sessao() as s:
        e = s.get(models.Empresa, empresa)
        assert apuracao.processar_arquivo(s, e, arquivo("creditos", [(CHAVE_ENTRADA, 5.0, 0)])) == 1
        assert apuracao.processar_arquivo(s, e, arquivo("creditos", [(CHAVE_ENTRADA, 6.7, 0)])) == 1
        regs = list(s.scalars(select(models.RegistroApuracao)))
        assert len(regs) == 1 and regs[0].apurado == D("6.70") and regs[0].pa == "2026-03"
        with pytest.raises(ValueError, match="CNPJ base"):
            apuracao.processar_arquivo(s, e, arquivo("debitos", [], ni="99999999"))


def test_webhook(empresa, monkeypatch):
    monkeypatch.setattr(settings, "url_publica", "https://rtp.exemplo")
    monkeypatch.setattr(main, "calculadora", CalculadoraFake())
    with db.sessao() as s:
        sol = apuracao.solicitar(s, s.get(models.Empresa, empresa), "creditos", ClienteFake())
        token, tiquete = sol.token_webhook, sol.tiquete
    retorno = arquivo("creditos", [(CHAVE_ENTRADA, 6.7, 0)])
    monkeypatch.setattr(apuracao, "cliente_da_empresa", lambda s, e: ClienteFake(retorno))
    c = TestClient(main.app)
    assert c.head(f"/webhooks/apuracao-cbs/{token}").status_code == 200
    assert c.head("/webhooks/apuracao-cbs/inexistente").status_code == 404
    assert c.post(f"/webhooks/apuracao-cbs/{token}", json={"tiqueteSolicitacao": "outro"}).status_code == 404
    r = c.post(f"/webhooks/apuracao-cbs/{token}", json={
        "tiqueteSolicitacao": tiquete, "urlAssinada": "https://storage.exemplo/a.json",
        "urlAssinadaExpiraEm": "2026-03-14T00:00:00Z"})
    assert r.status_code == 200
    with db.sessao() as s:
        sol = s.scalar(select(models.SolicitacaoApuracao))
        assert (sol.estado, sol.registros) == ("CONCLUIDA", 1)
        assert s.scalar(select(func.count()).select_from(models.RegistroApuracao)) == 1
    # webhook funciona mesmo com login básico ligado na interface
    monkeypatch.setattr(main.settings, "usuario", "bix")
    monkeypatch.setattr(main.settings, "senha", "x")
    assert c.head(f"/webhooks/apuracao-cbs/{token}").status_code == 200
    assert c.get("/").status_code == 401


def test_verificar_pendentes(empresa, monkeypatch):
    monkeypatch.setattr(settings, "url_publica", "https://rtp.exemplo")
    with db.sessao() as s:
        e = s.get(models.Empresa, empresa)
        apuracao.solicitar(s, e, "debitos", ClienteFake())
        msgs = apuracao.verificar_pendentes(s, e, ClienteFake(arquivo("debitos", [(CHAVE_SAIDA, 3.0, 0)])))
        assert msgs == ["debitos: CONCLUIDA — 1 registros"]


# --- Conciliação -------------------------------------------------------------------------------------------------

def test_conciliacao(empresa):
    with db.sessao() as s:
        e = s.get(models.Empresa, empresa)
        apuracao.processar_arquivo(s, e, arquivo("creditos", [(CHAVE_ENTRADA, 6.70, 0), (CHAVE_FORA, 9.0, 0)]))
        apuracao.processar_arquivo(s, e, arquivo("debitos", [(CHAVE_SAIDA, 13.5, 0), (CHAVE_SAIDA, -2.0, 20)]))
        conc = conciliacao.conciliar(s, e)
        por_chave = {(l.tipo, l.chave): l for l in conc.linhas}
        assert por_chave[("creditos", CHAVE_ENTRADA)].situacao == "ok"
        assert por_chave[("creditos", CHAVE_FORA)].situacao == "so_receita"
        saida = por_chave[("debitos", CHAVE_SAIDA)]
        assert saida.situacao == "divergente" and saida.diferenca == D("13.5") and saida.ajustes == D("-2.0")
        resumo = {r["tipo"]: r for r in conc.resumo()}
        assert resumo["creditos"]["ok"] == 1 and resumo["creditos"]["so_receita"] == 1

        # nota cancelada na base que a Receita ainda lista
        doc = s.scalar(select(models.DocumentoFiscal).where(models.DocumentoFiscal.chave == CHAVE_ENTRADA))
        doc.situacao = "cancelado"
        s.flush()
        assert {(l.tipo, l.chave): l for l in conciliacao.conciliar(s, e).linhas}[("creditos", CHAVE_ENTRADA)].situacao == "cancelada"


def test_tela_e_importacao_web(empresa, monkeypatch):
    monkeypatch.setattr(main, "calculadora", CalculadoraFake())
    c = TestClient(main.app)
    tela = c.get(f"/empresas/{empresa}/apuracao")
    assert "webhook HTTPS público" in tela.text and "Sem registros da Receita" in tela.text
    corpo = json.dumps(arquivo("creditos", [(CHAVE_ENTRADA, 6.70, 0)])).encode()
    r = c.post(f"/empresas/{empresa}/apuracao/importar", files=[("arquivos", ("cred.json", corpo, "application/json"))])
    assert "1 registro(s) importado(s)" in r.text and "Confere" in r.text
    r = c.post(f"/empresas/{empresa}/apuracao/importar", files=[("arquivos", ("x.json", b"[1,2]", "application/json"))])
    assert "arquivo não reconhecido" in r.text
