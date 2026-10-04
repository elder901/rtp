"""Revisão da classificação tributária (CST/cClassTrib) dos produtos — principal fonte de erro da simulação.

Ordem usada pelo motor: ajuste manual (código do produto, depois NCM) → grupo IBSCBS do XML → padrão 000/000001.
Para produtos sem classificação, sugere o cClassTrib que os fornecedores usaram para o mesmo NCM.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal

from app.calculadora.client import CalculadoraRTC, ErroCalculadora
from app.engine.cenarios import CFOP_ONEROSOS, CLASSIFICACAO_PADRAO
from app.parser.nfe import Documento


@dataclass
class ProdutoRevisao:
    direcao: str
    codigo: str
    descricao: str
    ncm: str
    valor: Decimal
    cst: str
    cclasstrib: str
    origem: str              # manual | xml | padrao
    chave_manual: str = ""   # chave do ajuste manual aplicado (código ou NCM)
    sugestao: tuple[str, str] | None = None
    sugestao_fonte: str = ""


def produtos(documentos: list[Documento], cnpj: str, overrides: dict[str, tuple[str, str]]) -> list[ProdutoRevisao]:
    vistos: dict[tuple[str, str], ProdutoRevisao] = {}
    por_ncm_fornecedor: dict[str, Counter] = defaultdict(Counter)

    for doc in documentos:
        direcao = doc.direcao_para(cnpj)
        if direcao is None:
            continue
        for it in doc.itens:
            if it.cfop[-3:] not in CFOP_ONEROSOS:  # fora da análise (devolução, remessa, bonificação...)
                continue
            if direcao == "entrada" and it.ibscbs and it.ibscbs.cclasstrib:
                por_ncm_fornecedor[it.ncm][(it.ibscbs.cst, it.ibscbs.cclasstrib)] += 1
            k = (direcao, it.codigo)
            if k in vistos:
                vistos[k].valor += it.valor_operacao
                continue
            if it.codigo in overrides or it.ncm in overrides:
                chave = it.codigo if it.codigo in overrides else it.ncm
                (cst, cct), origem = overrides[chave], "manual"
            elif it.ibscbs and it.ibscbs.cclasstrib:
                (cst, cct), origem, chave = (it.ibscbs.cst, it.ibscbs.cclasstrib), "xml", ""
            else:
                (cst, cct), origem, chave = CLASSIFICACAO_PADRAO, "padrao", ""
            vistos[k] = ProdutoRevisao(direcao, it.codigo, it.descricao, it.ncm, it.valor_operacao, cst, cct, origem, chave)

    for p in vistos.values():
        if p.origem == "padrao" and por_ncm_fornecedor.get(p.ncm):
            (cst, cct), n = por_ncm_fornecedor[p.ncm].most_common(1)[0]
            p.sugestao, p.sugestao_fonte = (cst, cct), f"usado por fornecedores em {n} item(ns) com NCM {p.ncm}"
    ordem = {"padrao": 0, "xml": 1, "manual": 2}
    return sorted(vistos.values(), key=lambda p: (ordem[p.origem], p.direcao != "saida", -p.valor))


def validar(calc: CalculadoraRTC, ncm: str, cst: str, cclasstrib: str, data: str = "2027-01-01") -> str:
    """Retorna "" se a combinação é aceita pela calculadora; senão, o motivo."""
    try:
        situacoes = calc.situacoes_tributarias(data)
        if cst not in situacoes:
            return f"CST {cst} não existe para NF-e"
        if cclasstrib not in situacoes[cst]["classificacoes"]:
            return f"cClassTrib {cclasstrib} não pertence ao CST {cst}"
        if ncm and len(ncm) == 8 and not calc.ncm_aplicavel(cclasstrib, ncm, data):
            return f"cClassTrib {cclasstrib} não se aplica ao NCM {ncm}"
    except ErroCalculadora as e:
        return f"não foi possível validar: {e}"
    return ""
