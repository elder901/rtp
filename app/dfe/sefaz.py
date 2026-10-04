"""Web services do Ambiente Nacional da NF-e: distribuição de DF-e (NT 2014.002) e manifestação do destinatário.

A distribuição não exige XML assinado, apenas TLS mútuo com o certificado A1. A manifestação (evento 210210,
Ciência da Operação) é assinada.
"""
from __future__ import annotations

import base64
import gzip
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx
from lxml import etree

from app.config import settings
from app.dfe.assinatura import assinar
from app.dfe.certificado import CertificadoA1

NFE = "http://www.portalfiscal.inf.br/nfe"
SOAP12 = "http://www.w3.org/2003/05/soap-envelope"

URLS = {
    "distribuicao": {1: "https://www1.nfe.fazenda.gov.br/NFeDistribuicaoDFe/NFeDistribuicaoDFe.asmx",
                     2: "https://hom1.nfe.fazenda.gov.br/NFeDistribuicaoDFe/NFeDistribuicaoDFe.asmx"},
    "evento": {1: "https://www.nfe.fazenda.gov.br/NFeRecepcaoEvento4/NFeRecepcaoEvento4.asmx",
               2: "https://hom1.nfe.fazenda.gov.br/NFeRecepcaoEvento4/NFeRecepcaoEvento4.asmx"},
}
WSDL_DIST = "http://www.portalfiscal.inf.br/nfe/wsdl/NFeDistribuicaoDFe"
WSDL_EVENTO = "http://www.portalfiscal.inf.br/nfe/wsdl/NFeRecepcaoEvento4"

CODIGO_UF = {"RO": 11, "AC": 12, "AM": 13, "RR": 14, "PA": 15, "AP": 16, "TO": 17, "MA": 21, "PI": 22, "CE": 23,
             "RN": 24, "PB": 25, "PE": 26, "AL": 27, "SE": 28, "BA": 29, "MG": 31, "ES": 32, "RJ": 33, "SP": 35,
             "PR": 41, "SC": 42, "RS": 43, "MS": 50, "MT": 51, "GO": 52, "DF": 53}

# cStat da distribuição
DOCUMENTOS_LOCALIZADOS = "138"
NENHUM_DOCUMENTO = "137"
CONSUMO_INDEVIDO = "656"
# cStat de evento aceito: 135 vinculado, 136 não vinculado, 573 duplicidade (já manifestado)
EVENTO_OK = {"135", "136", "573"}

FUSO_BRASILIA = timezone(timedelta(hours=-3))


class ErroSefaz(RuntimeError):
    pass


@dataclass
class DocDistribuido:
    nsu: str
    schema: str   # ex.: procNFe_v4.00.xsd, resNFe_v1.01.xsd, procEventoNFe_v1.00.xsd, resEvento_v1.01.xsd
    xml: bytes

    @property
    def tipo(self) -> str:
        return self.schema.split("_")[0]


@dataclass
class RetornoDistribuicao:
    cstat: str
    motivo: str
    ult_nsu: str
    max_nsu: str
    documentos: list[DocDistribuido] = field(default_factory=list)


@dataclass
class RetornoEvento:
    cstat: str
    motivo: str

    @property
    def aceito(self) -> bool:
        return self.cstat in EVENTO_OK


def _envelope(corpo) -> bytes:
    env = etree.Element(f"{{{SOAP12}}}Envelope", nsmap={"soap12": SOAP12})
    etree.SubElement(env, f"{{{SOAP12}}}Body").append(corpo)
    return etree.tostring(env, xml_declaration=True, encoding="utf-8")


def _sub(pai, tag: str, texto: str | None = None, **attrs):
    el = etree.SubElement(pai, f"{{{NFE}}}{tag}", **attrs)
    if texto is not None:
        el.text = texto
    return el


def _localizar(xml: bytes, tag: str):
    raiz = etree.fromstring(xml, parser=etree.XMLParser(resolve_entities=False, no_network=True))
    el = next((e for e in raiz.iter() if isinstance(e.tag, str) and etree.QName(e).localname == tag), None)
    if el is None:
        falha = next((e for e in raiz.iter() if isinstance(e.tag, str) and etree.QName(e).localname == "Text"), None)
        raise ErroSefaz(f"Resposta sem {tag}: {falha.text if falha is not None else xml[:300]!r}")
    return el


def _texto(el, tag: str) -> str:
    achado = el.find(f"{{{NFE}}}{tag}")
    return achado.text.strip() if achado is not None and achado.text else ""


class ClienteSefaz:
    def __init__(self, certificado: CertificadoA1, ambiente: int = 1, client: httpx.Client | None = None):
        self.cert = certificado
        self.ambiente = ambiente
        self.http = client or httpx.Client(verify=certificado.contexto_ssl(), timeout=settings.dfe_timeout)

    def _post(self, servico: str, acao: str, corpo) -> bytes:
        try:
            r = self.http.post(URLS[servico][self.ambiente], content=_envelope(corpo), headers={
                "Content-Type": f'application/soap+xml; charset=utf-8; action="{acao}"'})
        except httpx.HTTPError as e:
            raise ErroSefaz(f"Falha de comunicação com a SEFAZ: {e}") from e
        if r.status_code != 200:
            raise ErroSefaz(f"SEFAZ respondeu HTTP {r.status_code}: {r.text[:300]}")
        return r.content

    # --- Distribuição DF-e ---------------------------------------------------------------------------------------
    def distribuicao(self, cnpj: str, uf: str, ult_nsu: str | None = None, chave: str | None = None) -> RetornoDistribuicao:
        corpo = etree.Element(f"{{{WSDL_DIST}}}nfeDistDFeInteresse", nsmap={None: WSDL_DIST})
        msg = etree.SubElement(corpo, f"{{{WSDL_DIST}}}nfeDadosMsg")
        dist = etree.SubElement(msg, f"{{{NFE}}}distDFeInt", nsmap={None: NFE}, versao="1.01")
        _sub(dist, "tpAmb", str(self.ambiente))
        _sub(dist, "cUFAutor", str(CODIGO_UF[uf.upper()]))
        _sub(dist, "CNPJ", cnpj)
        if chave:
            _sub(_sub(dist, "consChNFe"), "chNFe", chave)
        else:
            _sub(_sub(dist, "distNSU"), "ultNSU", (ult_nsu or "0").zfill(15))

        ret = _localizar(self._post("distribuicao", f"{WSDL_DIST}/nfeDistDFeInteresse", corpo), "retDistDFeInt")
        docs = []
        lote = ret.find(f"{{{NFE}}}loteDistDFeInt")
        if lote is not None:
            for dz in lote.findall(f"{{{NFE}}}docZip"):
                docs.append(DocDistribuido(dz.get("NSU", ""), dz.get("schema", ""),
                                           gzip.decompress(base64.b64decode(dz.text))))
        return RetornoDistribuicao(_texto(ret, "cStat"), _texto(ret, "xMotivo"),
                                   _texto(ret, "ultNSU") or "0", _texto(ret, "maxNSU") or "0", docs)

    # --- Manifestação: Ciência da Operação -----------------------------------------------------------------------
    def evento_ciencia(self, cnpj: str, chave: str, agora: datetime | None = None) -> bytes:
        agora = (agora or datetime.now(FUSO_BRASILIA)).astimezone(FUSO_BRASILIA).replace(microsecond=0)
        env = etree.Element(f"{{{NFE}}}envEvento", nsmap={None: NFE}, versao="1.00")
        _sub(env, "idLote", agora.strftime("%y%m%d%H%M%S"))
        evento = _sub(env, "evento", versao="1.00")
        inf = _sub(evento, "infEvento", Id=f"ID210210{chave}01")
        _sub(inf, "cOrgao", "91")
        _sub(inf, "tpAmb", str(self.ambiente))
        _sub(inf, "CNPJ", cnpj)
        _sub(inf, "chNFe", chave)
        _sub(inf, "dhEvento", agora.isoformat())
        _sub(inf, "tpEvento", "210210")
        _sub(inf, "nSeqEvento", "1")
        _sub(inf, "verEvento", "1.00")
        det = _sub(inf, "detEvento", versao="1.00")
        _sub(det, "descEvento", "Ciencia da Operacao")
        assinar(evento, inf, self.cert)
        return etree.tostring(env)

    def manifestar_ciencia(self, cnpj: str, chave: str) -> RetornoEvento:
        corpo = etree.Element(f"{{{WSDL_EVENTO}}}nfeDadosMsg", nsmap={None: WSDL_EVENTO})
        corpo.append(etree.fromstring(self.evento_ciencia(cnpj, chave)))
        ret = _localizar(self._post("evento", f"{WSDL_EVENTO}/nfeRecepcaoEvento", corpo), "retEnvEvento")
        inf = ret.find(f"{{{NFE}}}retEvento/{{{NFE}}}infEvento")
        if inf is None:  # lote rejeitado antes de processar o evento
            return RetornoEvento(_texto(ret, "cStat"), _texto(ret, "xMotivo"))
        return RetornoEvento(_texto(inf, "cStat"), _texto(inf, "xMotivo"))
