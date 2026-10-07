"""Revisão da classificação tributária (CST/cClassTrib) dos produtos — principal fonte de erro da simulação.

Cada linha é um produto, identificado pelo EAN (ou pelo código quando não há EAN global), reunindo compras e
vendas. Ordem usada pelo motor, a mesma classificação na compra e na venda do produto: ajuste manual (EAN → código →
NCM) → o que os fornecedores declaram para o produto (o de maior valor quando divergem) → grupo IBSCBS do próprio XML →
NCM em anexo de redução da LC 214 (conferido na calculadora) → padrão 000/000001.
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
    chave: str                 # ean:... | cod:<raiz do emitente>:<código>
    gtin: str
    descricao: str
    codigos: list[str]
    ncms: list[str]
    valor_venda: Decimal = ZERO
    valor_compra: Decimal = ZERO
    cst: str = ""
    cclasstrib: str = ""
    origem: str = "padrao"     # manual | fornecedor | xml | anexo | padrao
    chave_manual: str = ""     # chave do ajuste manual aplicado (ean:/cod:/ncm: ou formato antigo)
    sugestao: tuple[str, str] | None = None
    sugestao_fonte: str = ""
    sugestao_por_ean: bool = False
    emitente_raiz: str = ""    # do cadastro de quem é o código principal (a loja, quando há venda)

    @property
    def codigo(self) -> str:
        return self.codigos[0]

    @property
    def codigo_escopado(self) -> str:
        """Código com a raiz do emitente, usado no ajuste "por código"."""
        return f"{self.emitente_raiz}:{self.codigo}" if self.emitente_raiz else self.codigo

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


def produtos(documentos: list[Documento], cnpj: str, ajustes,
             anexos: dict[str, tuple[str, str]] | None = None,
             xml_incompativeis: set[tuple[str, str]] | None = None,
             consenso: dict[str, tuple[str, str]] | None = None) -> list[ProdutoRevisao]:
    """`anexos`: NCM -> (CST, cClassTrib) dos anexos de redução (cenarios.anexos_por_ncm); `xml_incompativeis`:
    pares (cClassTrib, NCM) do XML recusados pela calculadora (cenarios.xml_incompativeis) — como o motor usa."""
    ajustes = Classificacoes.de(ajustes)
    anexos, xml_incompativeis, consenso = anexos or {}, xml_incompativeis or set(), consenso or {}
    valido = lambda i: i.ibscbs and i.ibscbs.cclasstrib and (i.ibscbs.cclasstrib, i.ncm) not in xml_incompativeis
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
            if direcao == "entrada" and valido(it):
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
            valor_compra=sum((i.valor_operacao for i in g.itens_entrada), ZERO), emitente_raiz=principal.emitente_raiz)
        achado = next((a for a in (ajustes.buscar(i) for i in itens) if a), None)
        # "Em uso" reflete a venda quando há venda (é o que o cálculo usa no débito); senão, a compra.
        base = g.itens_saida or g.itens_entrada
        com_xml = next((i for i in base if valido(i)), None)
        if achado:
            (p.cst, p.cclasstrib), p.chave_manual = achado
            p.origem = "manual"
        elif chave in consenso:
            (p.cst, p.cclasstrib), p.origem = consenso[chave], "fornecedor"
        elif com_xml:
            p.cst, p.cclasstrib, p.origem = com_xml.ibscbs.cst, com_xml.ibscbs.cclasstrib, "xml"
        elif p.ncm in anexos:
            (p.cst, p.cclasstrib), p.origem = anexos[p.ncm], "anexo"
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
            if p.sugestao == (p.cst, p.cclasstrib) and p.origem in ("fornecedor", "xml", "anexo"):
                p.sugestao, p.sugestao_fonte, p.sugestao_por_ean = None, "", False  # nada a sugerir
        saida.append(p)

    ordem = {"padrao": 0, "anexo": 1, "xml": 2, "fornecedor": 3, "manual": 4}
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


# Redução de CBS/IBS de cada cClassTrib dos anexos (para estimar o impacto de um conflito); integral = 0.
REDUCAO_PCT = {"000001": Decimal(0), "200003": Decimal(100), "200014": Decimal(100), "200034": Decimal(60),
               "200035": Decimal(60)}


@dataclass
class Conflito:
    chave: str
    gtin: str
    descricao: str
    ncm: str
    tipo: str                                  # "fornecedor x anexo" | "fornecedores divergem"
    em_uso: tuple[str, str]                     # o que o motor usa (o de maior valor entre os fornecedores)
    alternativa: tuple[str, str]                # o anexo do NCM ou a 2ª declaração dos fornecedores
    participacao_pct: Decimal                   # quanto do valor comprado declara a classificação em uso
    valor_compra: Decimal
    valor_venda: Decimal                        # valor líquido vendido no período
    impacto_vendas: Decimal | None              # CBS/IBS a mais nas vendas com a classificação em uso (2033)


def conflitos(documentos: list[Documento], cnpj: str, ajustes, anexos: dict[str, tuple[str, str]],
              declaracoes: dict[str, dict[tuple, Decimal]], aliquota_total_pct: Decimal = Decimal("26.5"),
              participacao_minima_pct: Decimal = Decimal(20)) -> list[Conflito]:
    """Produtos em que a classificação usada (a dos fornecedores) diverge do anexo do NCM, ou em que os fornecedores
    divergem entre si (2ª declaração com ao menos `participacao_minima_pct` do valor). Ordenados pelo impacto estimado
    nas vendas — é a lista para o tributário revisar."""
    ajustes = Classificacoes.de(ajustes)
    venda, compra, info = defaultdict(Decimal), defaultdict(Decimal), {}
    for doc in documentos:
        direcao = doc.direcao_para(cnpj)
        if direcao is None:
            continue
        for it in doc.itens:
            if it.cfop[-3:] not in CFOP_ONEROSOS or ajustes.buscar(it):
                continue
            if direcao == "saida":
                venda[it.chave] += it.valor_liquido
                info[it.chave] = (it.gtin, it.descricao, it.ncm)      # a venda (cadastro da loja) tem precedência
            else:
                compra[it.chave] += it.valor_operacao
                info.setdefault(it.chave, (it.gtin, it.descricao, it.ncm))
    saida = []
    for chave, decl in declaracoes.items():
        if chave not in info or not decl:
            continue
        gtin, descricao, ncm = info[chave]
        total = sum(decl.values(), Decimal(0)) or Decimal(1)
        ordenadas = sorted(decl.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
        em_uso, valor_em_uso = ordenadas[0]
        participacao = (valor_em_uso / total * 100).quantize(Decimal("0.1"))
        alternativas = []
        if ncm in anexos and anexos[ncm] != em_uso:
            alternativas.append(("fornecedor x anexo", anexos[ncm]))
        if len(ordenadas) > 1 and ordenadas[1][1] / total * 100 >= participacao_minima_pct:
            alternativas.append(("fornecedores divergem", ordenadas[1][0]))
        for tipo, alternativa in alternativas[:1]:
            r_uso, r_alt = REDUCAO_PCT.get(em_uso[1]), REDUCAO_PCT.get(alternativa[1])
            impacto = None
            if r_uso is not None and r_alt is not None:
                impacto = (venda[chave] * (r_alt - r_uso) / 100 * aliquota_total_pct / 100).quantize(Decimal("0.01"))
            saida.append(Conflito(chave, gtin, descricao, ncm, tipo, em_uso, alternativa, participacao,
                                  compra[chave], venda[chave], impacto))
    return sorted(saida, key=lambda c: (-(abs(c.impacto_vendas) if c.impacto_vendas is not None else Decimal(0)),
                                        -(c.valor_venda + c.valor_compra)))
