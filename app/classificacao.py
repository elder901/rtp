"""Revisão da classificação tributária (CST/cClassTrib) dos produtos — principal fonte de erro da simulação.

Cada linha é um produto, identificado pelo EAN (ou pelo código quando não há EAN global), reunindo compras e
vendas. Ordem usada pelo motor: ajuste manual (EAN → código → NCM) → grupo IBSCBS do XML → padrão 000/000001.
Para produtos sem classificação sugere o cClassTrib informado pelos fornecedores para o mesmo EAN (evidência
forte) ou, na falta, para o mesmo NCM.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from app.calculadora.client import CalculadoraRTC, ErroCalculadora
from app.engine.cenarios import CFOP_ONEROSOS, CLASSIFICACAO_PADRAO, Classificacoes
from app.parser.nfe import Documento

ZERO = Decimal("0")


@dataclass
class ProdutoRevisao:
    chave: str                 # ean:... | cod:...
    gtin: str
    descricao: str
    codigos: list[str]
    ncms: list[str]
    valor_venda: Decimal = ZERO
    valor_compra: Decimal = ZERO
    cst: str = ""
    cclasstrib: str = ""
    origem: str = "padrao"     # manual | xml | padrao
    chave_manual: str = ""     # chave do ajuste manual aplicado (ean:/cod:/ncm: ou formato antigo)
    sugestao: tuple[str, str] | None = None
    sugestao_fonte: str = ""
    sugestao_por_ean: bool = False

    @property
    def codigo(self) -> str:
        return self.codigos[0]

    @property
    def ncm(self) -> str:
        return self.ncms[0]

    @property
    def ncm_divergente(self) -> bool:
        return len(self.ncms) > 1

    @property
    def escopo_ajuste(self) -> str:
        """Por onde o ajuste vale: o já gravado ou, para um novo, o EAN quando houver."""
        prefixo = self.chave_manual.partition(":")[0]
        return {"ean": "ean", "cod": "codigo", "ncm": "ncm"}.get(prefixo, "ean" if self.gtin else "codigo")

    @property
    def lados(self) -> str:
        return " e ".join(l for l, v in (("venda", self.valor_venda), ("compra", self.valor_compra)) if v)


@dataclass
class _Acumulado:
    itens_saida: list = field(default_factory=list)
    itens_entrada: list = field(default_factory=list)


def produtos(documentos: list[Documento], cnpj: str, ajustes) -> list[ProdutoRevisao]:
    ajustes = Classificacoes.de(ajustes)
    grupos: dict[str, _Acumulado] = defaultdict(_Acumulado)
    por_ean_fornecedor: dict[str, Counter] = defaultdict(Counter)
    por_ncm_fornecedor: dict[str, Counter] = defaultdict(Counter)

    for doc in documentos:
        direcao = doc.direcao_para(cnpj)
        if direcao is None:
            continue
        for it in doc.itens:
            if it.cfop[-3:] not in CFOP_ONEROSOS:  # fora da análise (devolução, remessa, bonificação...)
                continue
            if direcao == "entrada" and it.ibscbs and it.ibscbs.cclasstrib:
                classe = (it.ibscbs.cst, it.ibscbs.cclasstrib)
                if it.gtin:
                    por_ean_fornecedor[it.gtin][classe] += 1
                por_ncm_fornecedor[it.ncm][classe] += 1
            g = grupos[it.chave]
            (g.itens_saida if direcao == "saida" else g.itens_entrada).append(it)

    saida = []
    for chave, g in grupos.items():
        itens = g.itens_saida + g.itens_entrada          # a venda (cadastro da loja) tem precedência
        principal = itens[0]
        p = ProdutoRevisao(
            chave=chave, gtin=principal.gtin, descricao=principal.descricao,
            codigos=list(dict.fromkeys(i.codigo for i in itens)), ncms=list(dict.fromkeys(i.ncm for i in itens)),
            valor_venda=sum((i.valor_operacao for i in g.itens_saida), ZERO),
            valor_compra=sum((i.valor_operacao for i in g.itens_entrada), ZERO))
        achado = next((a for a in (ajustes.buscar(i) for i in itens) if a), None)
        # "Em uso" reflete a venda quando há venda (é o que o cálculo usa no débito); senão, a compra.
        base = g.itens_saida or g.itens_entrada
        com_xml = next((i for i in base if i.ibscbs and i.ibscbs.cclasstrib), None)
        if achado:
            (p.cst, p.cclasstrib), p.chave_manual = achado
            p.origem = "manual"
        elif com_xml:
            p.cst, p.cclasstrib, p.origem = com_xml.ibscbs.cst, com_xml.ibscbs.cclasstrib, "xml"
        else:
            p.cst, p.cclasstrib = CLASSIFICACAO_PADRAO

        if p.origem != "manual":
            if p.gtin and por_ean_fornecedor.get(p.gtin):
                (cst, cct), n = por_ean_fornecedor[p.gtin].most_common(1)[0]
                p.sugestao, p.sugestao_por_ean = (cst, cct), True
                p.sugestao_fonte = f"informado pelo fornecedor em {n} item(ns) com o mesmo EAN {p.gtin}"
            elif por_ncm_fornecedor.get(p.ncm):
                (cst, cct), n = por_ncm_fornecedor[p.ncm].most_common(1)[0]
                p.sugestao = (cst, cct)
                p.sugestao_fonte = f"usado por fornecedores em {n} item(ns) com NCM {p.ncm}"
            if p.sugestao == (p.cst, p.cclasstrib) and p.origem == "xml":
                p.sugestao, p.sugestao_fonte, p.sugestao_por_ean = None, "", False  # nada a sugerir
        saida.append(p)

    ordem = {"padrao": 0, "xml": 1, "manual": 2}
    return sorted(saida, key=lambda p: (ordem[p.origem], -(p.valor_venda + p.valor_compra)))


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
