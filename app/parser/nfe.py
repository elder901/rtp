"""Leitura de NF-e / NFC-e (leiaute 4.00, incluindo o grupo IBSCBS da NT 2025.002).

Extrai apenas o necessário para comparar a carga atual com a da reforma.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from lxml import etree

ZERO = Decimal("0")

# CRT do emitente: 1 = Simples Nacional, 2 = Simples (excesso de sublimite), 3 = Regime Normal, 4 = MEI
CRT_SIMPLES = {"1", "2"}
CRT_MEI = {"4"}


class XMLInvalido(ValueError):
    pass


@dataclass
class Participante:
    cnpj: str
    nome: str
    uf: str
    cod_municipio: str
    crt: str = ""
    ind_ie: str = ""  # indIEDest: 9 = não contribuinte

    @property
    def regime(self) -> str:
        if self.crt in CRT_SIMPLES:
            return "simples"
        if self.crt in CRT_MEI:
            return "mei"
        if not self.crt and (len(self.cnpj) == 11 or self.ind_ie == "9"):
            return "nao_contribuinte"  # pessoa física (ex.: produtor rural) ou PJ não contribuinte
        return "normal"


@dataclass
class IBSCBSDestacado:
    """Grupo IBSCBS informado no próprio XML (obrigatório em 2026, valores de teste)."""
    cst: str
    cclasstrib: str
    vbc: Decimal = ZERO
    vibs: Decimal = ZERO
    vcbs: Decimal = ZERO


@dataclass
class Item:
    n_item: int
    codigo: str
    descricao: str
    ncm: str
    cfop: str
    unidade: str
    quantidade: Decimal
    v_prod: Decimal
    v_frete: Decimal = ZERO
    v_seg: Decimal = ZERO
    v_outro: Decimal = ZERO
    v_desc: Decimal = ZERO
    # ICMS
    icms_cst: str = ""          # CST (regime normal) ou CSOSN (Simples)
    v_icms: Decimal = ZERO
    v_fcp: Decimal = ZERO
    v_icms_st: Decimal = ZERO
    v_fcp_st: Decimal = ZERO
    v_cred_icms_sn: Decimal = ZERO  # crédito de ICMS permitido ao adquirente quando o emitente é do Simples
    v_icms_deson: Decimal = ZERO
    # Federais atuais
    v_ipi: Decimal = ZERO
    v_pis: Decimal = ZERO
    v_cofins: Decimal = ZERO
    pis_cst: str = ""
    v_issqn: Decimal = ZERO
    ibscbs: IBSCBSDestacado | None = None
    gtin: str = ""  # cEAN; liga a compra e a venda do mesmo produto mesmo com códigos diferentes

    @property
    def tributos_por_dentro(self) -> Decimal:
        """Tributos atuais embutidos no valor do produto (ICMS próprio, FCP, PIS, COFINS, ISS)."""
        return self.v_icms + self.v_fcp + self.v_pis + self.v_cofins + self.v_issqn

    @property
    def tributos_por_fora(self) -> Decimal:
        """Tributos atuais somados ao valor do produto (IPI, ICMS-ST, FCP-ST)."""
        return self.v_ipi + self.v_icms_st + self.v_fcp_st

    @property
    def valor_operacao(self) -> Decimal:
        return self.v_prod + self.v_frete + self.v_seg + self.v_outro - self.v_desc

    @property
    def valor_liquido(self) -> Decimal:
        """Valor da operação sem nenhum tributo sobre consumo: base comum dos dois cenários."""
        return self.valor_operacao - self.tributos_por_dentro


@dataclass
class Documento:
    chave: str
    modelo: str
    numero: str
    serie: str
    emissao: datetime
    tp_nf: str  # 0 = entrada, 1 = saída (do ponto de vista do emitente)
    emitente: Participante
    destinatario: Participante | None
    itens: list[Item] = field(default_factory=list)
    arquivo: str = ""

    def contraparte_para(self, cnpj_empresa: str) -> Participante:
        """Quem está do outro lado da operação. Na nota de entrada emitida pela própria empresa (compra de produtor
        rural, importação...) o fornecedor é o destinatário."""
        if self.emitente.cnpj == _so_digitos(cnpj_empresa):
            return self.destinatario or Participante("", "Consumidor final", "", "")
        return self.emitente

    def direcao_para(self, cnpj_empresa: str) -> str | None:
        """'saida' se a empresa emitiu, 'entrada' se a empresa recebeu; None se não participa."""
        cnpj_empresa = _so_digitos(cnpj_empresa)
        if self.emitente.cnpj == cnpj_empresa:
            return "entrada" if self.tp_nf == "0" else "saida"
        if self.destinatario and self.destinatario.cnpj == cnpj_empresa:
            return "entrada"
        return None


def _so_digitos(s: str) -> str:
    return "".join(c for c in s if c.isdigit())


def _strip_ns(root: etree._Element) -> etree._Element:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _t(el, path: str, default: str = "") -> str:
    if el is None:
        return default
    found = el.find(path)
    return found.text.strip() if found is not None and found.text else default


def _d(el, path: str) -> Decimal:
    v = _t(el, path)
    return Decimal(v) if v else ZERO


def _participante(el, com_crt: bool) -> Participante | None:
    if el is None:
        return None
    ender = el.find("enderEmit") if el.find("enderEmit") is not None else el.find("enderDest")
    return Participante(
        cnpj=_t(el, "CNPJ") or _t(el, "CPF"),
        nome=_t(el, "xNome"),
        uf=_t(ender, "UF"),
        cod_municipio=_t(ender, "cMun"),
        crt=_t(el, "CRT") if com_crt else "",
        ind_ie=_t(el, "indIEDest"),
    )


def _grupo_unico(pai):
    """Retorna o único filho de um grupo de escolha (ex.: ICMS00, ICMSSN101, PISAliq...)."""
    if pai is None:
        return None
    filhos = [c for c in pai if isinstance(c.tag, str)]
    return filhos[0] if filhos else None


def _gtin(valor: str) -> str:
    return valor if valor.isdigit() and len(valor) in (8, 12, 13, 14) else ""  # descarta "SEM GTIN"


def _item(det) -> Item:
    prod = det.find("prod")
    imp = det.find("imposto")
    item = Item(
        n_item=int(det.get("nItem", "0")),
        codigo=_t(prod, "cProd"),
        descricao=_t(prod, "xProd"),
        ncm=_t(prod, "NCM"),
        cfop=_t(prod, "CFOP"),
        unidade=_t(prod, "uCom"),
        quantidade=_d(prod, "qCom"),
        v_prod=_d(prod, "vProd"),
        v_frete=_d(prod, "vFrete"),
        v_seg=_d(prod, "vSeg"),
        v_outro=_d(prod, "vOutro"),
        v_desc=_d(prod, "vDesc"),
        gtin=_gtin(_t(prod, "cEAN")) or _gtin(_t(prod, "cEANTrib")),
    )
    if imp is None:
        return item

    icms = _grupo_unico(imp.find("ICMS"))
    if icms is not None:
        item.icms_cst = _t(icms, "CST") or _t(icms, "CSOSN")
        item.v_icms = _d(icms, "vICMS")
        item.v_fcp = _d(icms, "vFCP")
        item.v_icms_st = _d(icms, "vICMSST")
        item.v_fcp_st = _d(icms, "vFCPST")
        item.v_cred_icms_sn = _d(icms, "vCredICMSSN")
        item.v_icms_deson = _d(icms, "vICMSDeson")

    ipi_trib = imp.find("IPI/IPITrib")
    item.v_ipi = _d(ipi_trib, "vIPI") if ipi_trib is not None else ZERO
    pis = _grupo_unico(imp.find("PIS"))
    item.v_pis = _d(pis, "vPIS")
    item.pis_cst = _t(pis, "CST")
    item.v_cofins = _d(_grupo_unico(imp.find("COFINS")), "vCOFINS")
    item.v_issqn = _d(imp.find("ISSQN"), "vISSQN")

    ibscbs = imp.find("IBSCBS")
    if ibscbs is not None:
        g = ibscbs.find("gIBSCBS")
        item.ibscbs = IBSCBSDestacado(
            cst=_t(ibscbs, "CST"),
            cclasstrib=_t(ibscbs, "cClassTrib"),
            vbc=_d(g, "vBC"),
            vibs=_d(g, "vIBS"),
            vcbs=_d(g, "gCBS/vCBS"),
        )
    return item


def ler_nfe(conteudo: bytes, arquivo: str = "") -> Documento:
    try:
        root = _strip_ns(etree.fromstring(conteudo, parser=etree.XMLParser(resolve_entities=False, no_network=True)))
    except etree.XMLSyntaxError as e:
        raise XMLInvalido(f"{arquivo}: XML mal formado ({e})") from e

    inf = root.find(".//infNFe")
    if inf is None:
        raise XMLInvalido(f"{arquivo}: não é NF-e/NFC-e (infNFe ausente)")

    ide = inf.find("ide")
    dh = _t(ide, "dhEmi") or _t(ide, "dEmi")
    return Documento(
        chave=inf.get("Id", "").removeprefix("NFe"),
        modelo=_t(ide, "mod"),
        numero=_t(ide, "nNF"),
        serie=_t(ide, "serie"),
        emissao=datetime.fromisoformat(dh) if dh else datetime.min,
        tp_nf=_t(ide, "tpNF"),
        emitente=_participante(inf.find("emit"), com_crt=True),
        destinatario=_participante(inf.find("dest"), com_crt=False),
        itens=[_item(det) for det in inf.findall("det")],
        arquivo=arquivo,
    )


def ler_arquivo(caminho: Path) -> Documento:
    return ler_nfe(caminho.read_bytes(), arquivo=caminho.name)
