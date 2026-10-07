"""Consolidação das NFC-e para a análise.

Uma loja emite dezenas de milhares de cupons por mês, todos para o consumidor final. Para a simulação o que importa é,
por produto e tratamento fiscal, a soma dos valores: os cálculos do motor são proporcionais ao valor do item. Então os
itens das NFC-e de cada mês viram um documento por mês, com um item por produto, NCM, CFOP, CST de ICMS e de PIS/COFINS,
classificação IBSCBS e unidade.

Dois usos: na leitura para análise (cupons gravados um a um, ex.: importação de JSON) e na importação direta do banco do
PDV, em que só o consolidado de cada mês é gravado (models.ConsolidadoNFCe, serializado com para_json/de_json).
"""
from __future__ import annotations

import gzip
import json
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime
from decimal import Decimal

from app.parser.nfe import Documento, IBSCBSDestacado, Item, Participante

MODELO_NFCE = "65"
CAMPOS_SOMADOS = ("quantidade", "quantidade_gtin", "v_prod", "v_frete", "v_seg", "v_outro", "v_desc", "v_icms",
                  "v_fcp", "v_icms_st", "v_fcp_st", "v_cred_icms_sn", "v_icms_deson", "v_ipi", "v_pis", "v_cofins",
                  "v_issqn")


def _chave_item(it: Item) -> tuple:
    classe = (it.ibscbs.cst, it.ibscbs.cclasstrib) if it.ibscbs else None
    return (it.chave, it.codigo, it.ncm, it.cfop, it.icms_cst, it.pis_cst, it.unidade.upper(),
            it.unidade_gtin.upper(), classe)


def _somar(acumulado: Item, it: Item):
    for campo in CAMPOS_SOMADOS:
        setattr(acumulado, campo, getattr(acumulado, campo) + getattr(it, campo))
    if it.ibscbs:
        acumulado.ibscbs.vbc += it.ibscbs.vbc
        acumulado.ibscbs.vibs += it.ibscbs.vibs
        acumulado.ibscbs.vcbs += it.ibscbs.vcbs


@dataclass
class MesNFCe:
    emitente: Participante
    cupons: int = 0
    itens: dict[tuple, Item] = field(default_factory=dict)

    @property
    def valor(self) -> Decimal:
        return sum((i.valor_operacao for i in self.itens.values()), Decimal(0))

    def adicionar_item(self, it: Item):
        k = _chave_item(it)
        acumulado = self.itens.get(k)
        if acumulado is None:
            self.itens[k] = replace(it, ibscbs=replace(it.ibscbs) if it.ibscbs else None)
        else:
            _somar(acumulado, it)


class ConsolidadorNFCe:
    def __init__(self):
        self.meses: dict[tuple[str, int, int], MesNFCe] = {}

    def adicionar(self, doc: Documento):
        mes = self.meses.setdefault((doc.emitente.cnpj, doc.emissao.year, doc.emissao.month), MesNFCe(doc.emitente))
        mes.cupons += 1
        for it in doc.itens:
            mes.adicionar_item(it)

    def mesclar(self, outro: ConsolidadorNFCe):
        """Junta um consolidador parcial (ex.: de outro processo) neste."""
        for k, m in outro.meses.items():
            meu = self.meses.setdefault(k, MesNFCe(m.emitente))
            meu.cupons += m.cupons
            for it in m.itens.values():
                meu.adicionar_item(it)

    def documentos(self) -> list[Documento]:
        return [documento_do_mes(cnpj, ano, mes_, m) for (cnpj, ano, mes_), m in sorted(self.meses.items())]


def documento_do_mes(cnpj: str, ano: int, mes: int, m: MesNFCe) -> Documento:
    return Documento(chave=f"NFCE-{cnpj}-{ano}{mes:02d}", modelo=MODELO_NFCE, numero=f"{m.cupons} cupons", serie="",
                     emissao=datetime(ano, mes, 1), tp_nf="1", emitente=m.emitente, destinatario=None,
                     itens=list(m.itens.values()), arquivo=f"NFC-e {mes:02d}/{ano} consolidadas ({m.cupons} cupons)")


def para_json(m: MesNFCe) -> bytes:
    dados = {"emitente": asdict(m.emitente), "cupons": m.cupons, "itens": [asdict(i) for i in m.itens.values()]}
    return gzip.compress(json.dumps(dados, default=str, separators=(",", ":")).encode())


def _de_dict(cls, d: dict):
    valores = {}
    for f in fields(cls):
        if f.name not in d:
            continue
        v = d[f.name]
        if f.type == "Decimal" and v is not None:
            v = Decimal(v)
        elif f.name == "ibscbs" and v is not None:
            v = _de_dict(IBSCBSDestacado, v)
        valores[f.name] = v
    return cls(**valores)


def de_json(dados: bytes) -> MesNFCe:
    d = json.loads(gzip.decompress(dados))
    m = MesNFCe(Participante(**d["emitente"]), d["cupons"])
    for i in d["itens"]:
        m.adicionar_item(_de_dict(Item, i))
    return m
