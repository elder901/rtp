import base64
import gzip
from datetime import datetime, timedelta

import httpx
import pytest
import respx
from lxml import etree
from sqlalchemy import select

from app import db, models, servicos
from app.dfe.assinatura import DS, verificar
from app.dfe.certificado import CertificadoA1, CertificadoInvalido
from app.dfe.sefaz import URLS, ClienteSefaz, DocDistribuido, RetornoDistribuicao, RetornoEvento
from app.dfe.sincronizar import sincronizar
from tests.conftest import CNPJ_EMPRESA, FX, gerar_pfx

NFE = "http://www.portalfiscal.inf.br/nfe"
CHAVE_NORMAL = "35261044555666000177550010000012341000012345"
CHAVE_RESUMO = "35261055666777000188550010000009991000099999"

RES_NFE = f"""<resNFe xmlns="{NFE}" versao="1.01"><chNFe>{CHAVE_RESUMO}</chNFe><CNPJ>55666777000188</CNPJ>
<xNome>FORNECEDOR SO RESUMO SA</xNome><IE>1</IE><dhEmi>2026-03-15T10:00:00-03:00</dhEmi><tpNF>1</tpNF>
<vNF>300.00</vNF><digVal>x</digVal><dhRecbto>2026-03-15T10:00:01-03:00</dhRecbto><nProt>1</nProt><cSitNFe>1</cSitNFe></resNFe>""".encode()
CANCELAMENTO = f"""<procEventoNFe xmlns="{NFE}" versao="1.00"><evento versao="1.00"><infEvento Id="x"><cOrgao>35</cOrgao>
<tpAmb>1</tpAmb><CNPJ>44555666000177</CNPJ><chNFe>{CHAVE_NORMAL}</chNFe><dhEvento>2026-03-11T10:00:00-03:00</dhEvento>
<tpEvento>110111</tpEvento></infEvento></evento></procEventoNFe>""".encode()


def _proc(nome: str) -> bytes:
    return (FX / "xml" / nome).read_bytes()


# --- Certificado e assinatura ----------------------------------------------------------------------------------

def test_certificado_a1():
    cert = CertificadoA1.carregar(gerar_pfx(), "segredo")
    assert cert.cnpj == CNPJ_EMPRESA
    assert not cert.vencido
    assert cert.contexto_ssl() is not None
    with pytest.raises(CertificadoInvalido):
        CertificadoA1.carregar(gerar_pfx(), "senha-errada")


def test_evento_ciencia_assinado():
    cert = CertificadoA1.carregar(gerar_pfx(), "segredo")
    xml = ClienteSefaz(cert, client=httpx.Client()).evento_ciencia(CNPJ_EMPRESA, CHAVE_NORMAL)
    raiz = etree.fromstring(xml)
    inf = raiz.find(f".//{{{NFE}}}infEvento")
    assert inf.get("Id") == f"ID210210{CHAVE_NORMAL}01"
    assert inf.findtext(f"{{{NFE}}}tpEvento") == "210210"
    sig = raiz.find(f".//{{{DS}}}Signature")
    assert sig.getparent().tag == f"{{{NFE}}}evento"
    assert verificar(inf, sig)
    inf.find(f"{{{NFE}}}chNFe").text = "0" * 44   # adulteração invalida a assinatura
    assert not verificar(inf, sig)


# --- Cliente SOAP (HTTP simulado) -------------------------------------------------------------------------------

def _resposta_distribuicao(docs: list[tuple[str, str, bytes]], cstat="138", ult="000000000000002", maxi="000000000000002"):
    lote = "".join(f'<docZip NSU="{nsu}" schema="{schema}">{base64.b64encode(gzip.compress(x)).decode()}</docZip>'
                   for nsu, schema, x in docs)
    return f"""<?xml version="1.0" encoding="utf-8"?><soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope">
<soap:Body><nfeDistDFeInteresseResponse xmlns="http://www.portalfiscal.inf.br/nfe/wsdl/NFeDistribuicaoDFe">
<nfeDistDFeInteresseResult><retDistDFeInt xmlns="{NFE}" versao="1.01"><tpAmb>1</tpAmb><verAplic>1</verAplic>
<cStat>{cstat}</cStat><xMotivo>Documento(s) localizado(s)</xMotivo><dhResp>2026-10-04T10:00:00-03:00</dhResp>
<ultNSU>{ult}</ultNSU><maxNSU>{maxi}</maxNSU><loteDistDFeInt>{lote}</loteDistDFeInt></retDistDFeInt>
</nfeDistDFeInteresseResult></nfeDistDFeInteresseResponse></soap:Body></soap:Envelope>"""


@respx.mock
def test_distribuicao_soap():
    rota = respx.post(URLS["distribuicao"][1]).mock(return_value=httpx.Response(200, text=_resposta_distribuicao(
        [("000000000000001", "procNFe_v4.00.xsd", _proc("entrada_fornecedor_normal.xml")),
         ("000000000000002", "resNFe_v1.01.xsd", RES_NFE)])))
    cert = CertificadoA1.carregar(gerar_pfx(), "segredo")
    ret = ClienteSefaz(cert, client=httpx.Client()).distribuicao(CNPJ_EMPRESA, "SP", ult_nsu="0")
    enviado = rota.calls[0].request
    assert b"<ultNSU>000000000000000</ultNSU>" in enviado.content and b"<cUFAutor>35</cUFAutor>" in enviado.content
    assert "nfeDistDFeInteresse" in enviado.headers["content-type"]
    assert (ret.cstat, ret.ult_nsu, ret.max_nsu) == ("138", "000000000000002", "000000000000002")
    assert [d.tipo for d in ret.documentos] == ["procNFe", "resNFe"]


# --- Sincronização (cliente SEFAZ falso) ------------------------------------------------------------------------

class SefazFake:
    def __init__(self, lotes, por_chave=None):
        self.lotes = list(lotes)
        self.por_chave = por_chave or {}
        self.pedidos, self.ciencias = [], []

    def distribuicao(self, cnpj, uf, ult_nsu=None, chave=None):
        self.pedidos.append(chave or ult_nsu)
        if chave:
            return self.por_chave.get(chave, RetornoDistribuicao("632", "não disponível", "0", "0"))
        return self.lotes.pop(0)

    def manifestar_ciencia(self, cnpj, chave):
        self.ciencias.append(chave)
        return RetornoEvento("135", "Evento registrado")


@pytest.fixture
def empresa(banco):
    with db.sessao() as s:
        e = servicos.criar_empresa(s, CNPJ_EMPRESA, "LOJA TESTE", "real", "SP", 3550308)
        servicos.salvar_certificado(s, e, gerar_pfx(), "segredo")
        return e.id


def test_sincronizacao_completa(empresa):
    sefaz = SefazFake([
        RetornoDistribuicao("138", "ok", "2", "3", [
            DocDistribuido("1", "procNFe_v4.00.xsd", _proc("entrada_fornecedor_normal.xml")),
            DocDistribuido("2", "resNFe_v1.01.xsd", RES_NFE)]),
        RetornoDistribuicao("138", "ok", "3", "3", [DocDistribuido("3", "procEventoNFe_v1.00.xsd", CANCELAMENTO)]),
    ])
    with db.sessao() as s:
        r = sincronizar(s, s.get(models.Empresa, empresa), cliente=sefaz)
    assert (r.completos, r.resumos, r.cancelados) == (1, 1, 1)
    assert sefaz.pedidos == ["0", "2"]
    assert any("Ciência" in m for m in r.mensagens)   # resumo pendente, ciência desligada
    with db.sessao() as s:
        docs = {d.chave: d for d in s.scalars(select(models.DocumentoFiscal))}
        assert docs[CHAVE_NORMAL].situacao == "cancelado"
        assert docs[CHAVE_RESUMO].situacao == "resumo" and docs[CHAVE_RESUMO].participante_nome.startswith("FORNECEDOR")
        ctrl = s.get(models.Empresa, empresa).controle_dfe
        assert ctrl.ult_nsu == "3" and ctrl.proxima_consulta > datetime.now() + timedelta(minutes=59)

        # dentro da janela de 1 hora não consulta de novo
        r2 = sincronizar(s, s.get(models.Empresa, empresa), cliente=SefazFake([]))
        assert not r2.executada


def test_ciencia_e_xml_completo(empresa):
    proc_resumo = _proc("entrada_fornecedor_simples.xml").replace(
        b"35261077888999000155550010000005671000056789", CHAVE_RESUMO.encode())
    sefaz = SefazFake(
        [RetornoDistribuicao("138", "ok", "1", "1", [DocDistribuido("1", "resNFe_v1.01.xsd", RES_NFE)])],
        por_chave={CHAVE_RESUMO: RetornoDistribuicao("138", "ok", "1", "1", [
            DocDistribuido("", "procNFe_v4.00.xsd", proc_resumo)])})
    with db.sessao() as s:
        e = s.get(models.Empresa, empresa)
        e.manifestar_ciencia = True
        r = sincronizar(s, e, cliente=sefaz)
    assert sefaz.ciencias == [CHAVE_RESUMO]
    assert (r.manifestados, r.xml_obtidos) == (1, 1)
    with db.sessao() as s:
        d = s.scalar(select(models.DocumentoFiscal).where(models.DocumentoFiscal.chave == CHAVE_RESUMO))
        assert d.situacao == "completo" and d.manifestado_em and d.xml_gz


def test_consumo_indevido_bloqueia_uma_hora(empresa):
    with db.sessao() as s:
        r = sincronizar(s, s.get(models.Empresa, empresa),
                        cliente=SefazFake([RetornoDistribuicao("656", "Consumo Indevido", "0", "0")]))
        assert any("656" in m for m in r.mensagens)
        assert s.get(models.Empresa, empresa).controle_dfe.proxima_consulta > datetime.now()


def test_certificado_de_outra_raiz_recusado(banco):
    with db.sessao() as s:
        e = servicos.criar_empresa(s, CNPJ_EMPRESA, "LOJA", "real", "SP", 3550308)
        with pytest.raises(CertificadoInvalido):
            servicos.salvar_certificado(s, e, gerar_pfx("99888777000166"), "segredo")
        cert = servicos.salvar_certificado(s, e, gerar_pfx(), "segredo")
        assert e.certificado.pfx_cifrado != gerar_pfx()          # guardado cifrado
        assert servicos.carregar_certificado(e).cnpj == cert.cnpj
