"""Visões para negociação com fornecedores: custo efetivo de compra (preço pago - créditos) hoje e em um ano da
transição, por fornecedor e por produto, e comparação entre fornecedores do mesmo produto (mesmo EAN).

Preço de equilíbrio: tributos e créditos são proporcionais ao preço líquido (sem tributos), então o custo efetivo é
linear nele. A variação de preço líquido que mantém o custo de hoje é custo_atual / custo_ano - 1:
negativa = o fornecedor precisa reduzir o preço; positiva = há espaço para aceitar aumento.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from app.engine.cenarios import Analise, ResultadoItem

ZERO = Decimal("0")
DUAS = Decimal("0.01")
QUATRO = Decimal("0.0001")


def _pct(novo: Decimal, antigo: Decimal) -> Decimal | None:
    return ((novo / antigo - 1) * 100).quantize(DUAS) if antigo else None


@dataclass
class Compra:
    """Compras de um produto a um fornecedor, somadas no período."""
    fornecedor: str
    fornecedor_nome: str
    regime: str
    uf: str
    chave: str
    gtin: str
    codigos: list[str]
    descricao: str
    ncm: str
    cclasstrib: str
    unidade: str
    quantidade: Decimal = ZERO
    valor_liquido: Decimal = ZERO
    preco_pago: Decimal = ZERO
    creditos_atual: Decimal = ZERO
    custo_atual: Decimal = ZERO
    creditos_ano: Decimal = ZERO
    custo_ano: Decimal = ZERO
    notas: set[str] = field(default_factory=set)
    melhor: "Compra | None" = None   # menor custo no ano entre os fornecedores do mesmo produto e unidade

    @property
    def custo_unit_atual(self) -> Decimal | None:
        return (self.custo_atual / self.quantidade).quantize(QUATRO) if self.quantidade else None

    @property
    def custo_unit_ano(self) -> Decimal | None:
        return (self.custo_ano / self.quantidade).quantize(QUATRO) if self.quantidade else None

    @property
    def preco_unit(self) -> Decimal | None:
        return (self.preco_pago / self.quantidade).quantize(QUATRO) if self.quantidade else None

    @property
    def variacao_custo_pct(self) -> Decimal | None:
        return _pct(self.custo_ano, self.custo_atual)

    @property
    def variacao_custo(self) -> Decimal:
        return self.custo_ano - self.custo_atual

    @property
    def preco_equilibrio_pct(self) -> Decimal | None:
        """Variação do preço líquido que mantém o custo efetivo de hoje no ano analisado."""
        return _pct(self.custo_atual, self.custo_ano) if self.custo_ano else None

    @property
    def economia_trocando(self) -> Decimal | None:
        """Quanto o mesmo volume custaria a menos no ano comprando do melhor fornecedor do mesmo produto."""
        if not self.melhor or self.melhor is self or self.melhor.custo_unit_ano is None:
            return None
        return ((self.custo_unit_ano - self.melhor.custo_unit_ano) * self.quantidade).quantize(DUAS)


def compras(a: Analise, ano: int) -> list[Compra]:
    grupos: dict[tuple, Compra] = {}
    for r in a.itens:
        if r.direcao != "entrada":
            continue
        i = r.item
        unidade = (i.unidade_gtin or i.unidade).upper()
        qtd = i.quantidade_gtin if i.gtin and i.quantidade_gtin else i.quantidade
        k = (r.contraparte.cnpj, i.chave, unidade)
        c = grupos.get(k)
        if c is None:
            c = grupos[k] = Compra(r.contraparte.cnpj, r.contraparte.nome, r.contraparte.regime, r.contraparte.uf,
                                   i.chave, i.gtin, [], i.descricao, i.ncm, r.classificacao.cclasstrib, unidade)
        if i.codigo not in c.codigos:
            c.codigos.append(i.codigo)
        v = r.anos[ano]
        c.quantidade += qtd
        c.valor_liquido += i.valor_liquido
        c.preco_pago += r.atual.preco(i.valor_liquido)
        c.creditos_atual += r.atual.creditos
        c.custo_atual += r.atual.custo_ou_receita(i.valor_liquido)
        c.creditos_ano += v.creditos
        c.custo_ano += v.custo_ou_receita(i.valor_liquido)
        c.notas.add(r.documento.chave)

    # melhor fornecedor de cada produto (mesmo EAN/código e mesma unidade) no ano
    por_produto: dict[tuple, list[Compra]] = defaultdict(list)
    for c in grupos.values():
        por_produto[(c.chave, c.unidade)].append(c)
    for lista in por_produto.values():
        validos = [c for c in lista if c.custo_unit_ano is not None]
        melhor = min(validos, key=lambda c: c.custo_unit_ano) if validos else None
        for c in lista:
            c.melhor = melhor if len(lista) > 1 else None
    return list(grupos.values())


@dataclass
class ResumoFornecedor:
    cnpj: str
    nome: str
    regime: str
    uf: str
    produtos: int
    notas: int
    valor_liquido: Decimal
    custo_atual: Decimal
    custo_ano: Decimal
    economia_trocando: Decimal
    produtos_com_alternativa: int

    @property
    def variacao_custo(self) -> Decimal:
        return self.custo_ano - self.custo_atual

    @property
    def variacao_custo_pct(self) -> Decimal | None:
        return _pct(self.custo_ano, self.custo_atual)

    @property
    def preco_equilibrio_pct(self) -> Decimal | None:
        return _pct(self.custo_atual, self.custo_ano) if self.custo_ano else None


def fornecedores(lista: list[Compra]) -> list[ResumoFornecedor]:
    grupos: dict[str, list[Compra]] = defaultdict(list)
    for c in lista:
        grupos[c.fornecedor].append(c)
    saida = []
    for cnpj, cs in grupos.items():
        c0 = cs[0]
        saida.append(ResumoFornecedor(
            cnpj, c0.fornecedor_nome, c0.regime, c0.uf, len(cs), len(set().union(*(c.notas for c in cs))),
            sum((c.valor_liquido for c in cs), ZERO), sum((c.custo_atual for c in cs), ZERO),
            sum((c.custo_ano for c in cs), ZERO),
            sum((c.economia_trocando or ZERO for c in cs), ZERO),
            sum(1 for c in cs if c.economia_trocando)))
    return sorted(saida, key=lambda f: -f.variacao_custo)


@dataclass
class ResumoProduto:
    chave: str
    gtin: str
    codigos: list[str]
    descricao: str
    ncm: str
    cclasstrib: str
    origem_classificacao: str
    vendas_liquidas: Decimal = ZERO
    tributos_venda_atual: Decimal = ZERO
    tributos_venda_ano: Decimal = ZERO
    compras: list[Compra] = field(default_factory=list)

    @property
    def fornecedores(self) -> int:
        return len({c.fornecedor for c in self.compras})

    @property
    def custo_atual(self) -> Decimal:
        return sum((c.custo_atual for c in self.compras), ZERO)

    @property
    def custo_ano(self) -> Decimal:
        return sum((c.custo_ano for c in self.compras), ZERO)

    @property
    def variacao_custo_pct(self) -> Decimal | None:
        return _pct(self.custo_ano, self.custo_atual)

    @property
    def carga_venda_atual_pct(self) -> Decimal | None:
        return (self.tributos_venda_atual / self.vendas_liquidas * 100).quantize(DUAS) if self.vendas_liquidas else None

    @property
    def carga_venda_ano_pct(self) -> Decimal | None:
        return (self.tributos_venda_ano / self.vendas_liquidas * 100).quantize(DUAS) if self.vendas_liquidas else None

    @property
    def variacao_preco_venda_pct(self) -> Decimal | None:
        """Variação do preço ao cliente para manter a receita líquida."""
        if not self.vendas_liquidas:
            return None
        return _pct(self.vendas_liquidas + self.tributos_venda_ano, self.vendas_liquidas + self.tributos_venda_atual)

    @property
    def economia_possivel(self) -> Decimal:
        return sum((c.economia_trocando or ZERO for c in self.compras), ZERO)


def produtos(a: Analise, lista: list[Compra], ano: int) -> list[ResumoProduto]:
    saida: dict[str, ResumoProduto] = {}

    def resumo(r: ResultadoItem) -> ResumoProduto:
        i = r.item
        if i.chave not in saida:
            saida[i.chave] = ResumoProduto(i.chave, i.gtin, [], i.descricao, i.ncm, r.classificacao.cclasstrib,
                                          r.classificacao.origem)
        p = saida[i.chave]
        if i.codigo not in p.codigos:
            p.codigos.append(i.codigo)
        return p

    for r in a.itens:  # vendas primeiro: descrição e classificação da loja têm precedência
        if r.direcao == "saida":
            p = resumo(r)
            p.vendas_liquidas += r.item.valor_liquido
            p.tributos_venda_atual += r.atual.tributos
            p.tributos_venda_ano += r.anos[ano].tributos
    for r in a.itens:
        if r.direcao == "entrada":
            resumo(r)
    for c in lista:
        saida[c.chave].compras.append(c)
    return sorted(saida.values(), key=lambda p: -(p.vendas_liquidas + p.custo_atual))
