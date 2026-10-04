"""Visões para negociação com fornecedores e formação de preço, por produto (EAN).

Custo efetivo de compra = preço pago - créditos aproveitáveis. Para cada ano da transição há dois cenários de compra:
- **a preços de hoje**: o fornecedor mantém o preço de nota. Ex.: o fim do PIS/COFINS em 2027 vira margem dele e o
  seu custo sobe na medida do crédito de 9,25% perdido;
- **com repasse**: o fornecedor mantém o próprio valor líquido e o preço de nota cai na medida dos tributos que saíram.

Preço de nota de equilíbrio: o preço de nota que, no ano, mantém o seu custo efetivo de hoje — é o número a levar à
negociação. Como tributos e créditos são proporcionais ao valor líquido, o custo no ano é c x valor líquido e o preço
de nota é valor líquido / (1 - parte embutida do preço naquele ano).

Na venda, com o EAN ligando compra e venda: preço ao consumidor, custo e margem unitários hoje e no ano, mantendo a
gôndola ou mantendo a margem, com o fornecedor mantendo o preço ou repassando.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from app.engine.cenarios import Analise, ResultadoItem, taxa_por_dentro

ZERO = Decimal("0")
DUAS = Decimal("0.01")
QUATRO = Decimal("0.0001")


def _pct(novo: Decimal | None, antigo: Decimal | None) -> Decimal | None:
    return ((novo / antigo - 1) * 100).quantize(DUAS) if novo is not None and antigo else None


def _unit(total: Decimal, qtd: Decimal) -> Decimal | None:
    return (total / qtd).quantize(QUATRO) if qtd else None


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
    # ano analisado
    creditos_ano: Decimal = ZERO
    custo_ano: Decimal = ZERO                 # com repasse (fornecedor mantém o próprio valor líquido)
    custo_precos_hoje: Decimal = ZERO         # fornecedor mantém o preço de nota
    preco_nota_atual: Decimal = ZERO
    preco_nota_repasse: Decimal = ZERO
    preco_nota_equilibrio: Decimal = ZERO     # preço de nota que mantém o seu custo de hoje
    imposto_seletivo_ano: Decimal = ZERO      # IS cobrado na compra ou embutido no preço do revendedor (sem crédito)
    tratamento_is: str = ""
    notas: set[str] = field(default_factory=set)
    melhor: "Compra | None" = None   # menor custo no ano, a preços de hoje, entre fornecedores do mesmo produto e unidade

    @property
    def custo_unit_atual(self) -> Decimal | None:
        return _unit(self.custo_atual, self.quantidade)

    @property
    def custo_unit_ano(self) -> Decimal | None:
        return _unit(self.custo_ano, self.quantidade)

    @property
    def custo_unit_precos_hoje(self) -> Decimal | None:
        return _unit(self.custo_precos_hoje, self.quantidade)

    @property
    def preco_unit(self) -> Decimal | None:
        return _unit(self.preco_pago, self.quantidade)

    @property
    def preco_nota_unit(self) -> Decimal | None:
        return _unit(self.preco_nota_atual, self.quantidade)

    @property
    def preco_nota_equilibrio_unit(self) -> Decimal | None:
        return _unit(self.preco_nota_equilibrio, self.quantidade)

    @property
    def variacao_preco_nota_equilibrio_pct(self) -> Decimal | None:
        """Quanto o preço de nota pode variar para o seu custo no ano ficar igual ao de hoje (negativo = desconto)."""
        return _pct(self.preco_nota_equilibrio, self.preco_nota_atual)

    @property
    def variacao_custo_precos_hoje_pct(self) -> Decimal | None:
        return _pct(self.custo_precos_hoje, self.custo_atual)

    @property
    def variacao_custo_pct(self) -> Decimal | None:
        """Com repasse."""
        return _pct(self.custo_ano, self.custo_atual)

    @property
    def variacao_custo(self) -> Decimal:
        return self.custo_precos_hoje - self.custo_atual

    @property
    def preco_equilibrio_pct(self) -> Decimal | None:
        """Variação do valor líquido do fornecedor que mantém o seu custo (referência técnica; ver preço de nota)."""
        return _pct(self.custo_atual, self.custo_ano) if self.custo_ano else None

    @property
    def economia_trocando(self) -> Decimal | None:
        """Quanto o mesmo volume custaria a menos no ano, a preços de hoje, comprando do melhor fornecedor."""
        if not self.melhor or self.melhor is self or self.melhor.custo_unit_precos_hoje is None:
            return None
        return ((self.custo_unit_precos_hoje - self.melhor.custo_unit_precos_hoje) * self.quantidade).quantize(DUAS)


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
        v, vl = r.anos[ano], i.valor_liquido
        custo_hoje, custo_ano = r.atual.custo_ou_receita(vl), v.custo_ou_receita(vl)
        c.quantidade += qtd
        c.valor_liquido += vl
        c.preco_pago += r.atual.preco(vl)
        c.creditos_atual += r.atual.creditos
        c.custo_atual += custo_hoje
        c.creditos_ano += v.creditos
        c.custo_ano += custo_ano
        c.imposto_seletivo_ano += v.imposto_seletivo
        c.tratamento_is = c.tratamento_is or r.imposto_seletivo
        c.notas.add(r.documento.chave)
        c.preco_nota_atual += i.valor_operacao
        if vl > 0 and i.valor_operacao:
            fator = custo_ano / vl                     # custo no ano por R$ de valor líquido
            embutido = 1 - taxa_por_dentro(i, ano)     # parte do preço de nota que não é tributo embutido
            c.preco_nota_repasse += vl / embutido
            c.custo_precos_hoje += fator * i.valor_operacao * embutido
            c.preco_nota_equilibrio += (custo_hoje / fator / embutido) if fator else i.valor_operacao
        else:
            c.preco_nota_repasse += i.valor_operacao
            c.custo_precos_hoje += custo_ano
            c.preco_nota_equilibrio += i.valor_operacao
    for c in grupos.values():
        for nome in ("custo_precos_hoje", "preco_nota_repasse", "preco_nota_equilibrio"):
            setattr(c, nome, getattr(c, nome).quantize(DUAS))

    # melhor fornecedor de cada produto (mesmo EAN/código e mesma unidade) no ano, a preços de hoje
    por_produto: dict[tuple, list[Compra]] = defaultdict(list)
    for c in grupos.values():
        por_produto[(c.chave, c.unidade)].append(c)
    for lista in por_produto.values():
        validos = [c for c in lista if c.custo_unit_precos_hoje is not None]
        melhor = min(validos, key=lambda c: c.custo_unit_precos_hoje) if validos else None
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
    preco_nota_atual: Decimal
    preco_nota_equilibrio: Decimal
    custo_atual: Decimal
    custo_ano: Decimal
    custo_precos_hoje: Decimal
    economia_trocando: Decimal
    produtos_com_alternativa: int

    @property
    def variacao_custo(self) -> Decimal:
        """A preços de hoje."""
        return self.custo_precos_hoje - self.custo_atual

    @property
    def variacao_custo_pct(self) -> Decimal | None:
        return _pct(self.custo_precos_hoje, self.custo_atual)

    @property
    def variacao_custo_repasse_pct(self) -> Decimal | None:
        return _pct(self.custo_ano, self.custo_atual)

    @property
    def variacao_preco_nota_equilibrio_pct(self) -> Decimal | None:
        return _pct(self.preco_nota_equilibrio, self.preco_nota_atual)

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

        def soma(nome):
            return sum((getattr(c, nome) for c in cs), ZERO)

        saida.append(ResumoFornecedor(
            cnpj, c0.fornecedor_nome, c0.regime, c0.uf, len(cs), len(set().union(*(c.notas for c in cs))),
            soma("valor_liquido"), soma("preco_nota_atual"), soma("preco_nota_equilibrio"), soma("custo_atual"),
            soma("custo_ano"), soma("custo_precos_hoje"),
            sum((c.economia_trocando or ZERO for c in cs), ZERO), sum(1 for c in cs if c.economia_trocando)))
    return sorted(saida, key=lambda f: -f.variacao_custo)


@dataclass
class CenarioPreco:
    nome: str
    preco: Decimal | None       # preço unitário ao consumidor
    custo: Decimal | None       # custo efetivo unitário de compra
    margem: Decimal | None      # valor líquido unitário de venda - custo
    margem_pct: Decimal | None  # margem / valor líquido de venda


@dataclass
class ResumoProduto:
    chave: str
    gtin: str
    codigos: list[str]
    descricao: str
    ncm: str
    cclasstrib: str
    origem_classificacao: str
    unidade_venda: str = ""
    quantidade_venda: Decimal = ZERO
    vendas_liquidas: Decimal = ZERO
    receita_consumidor: Decimal = ZERO
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
        return sum((c.custo_precos_hoje for c in self.compras), ZERO)

    @property
    def variacao_custo_pct(self) -> Decimal | None:
        """A preços de hoje."""
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

    # --- preço de venda e margem por unidade (EAN liga a venda às compras na mesma unidade) ----------------------
    def _compras_mesma_unidade(self) -> list[Compra]:
        return [c for c in self.compras if c.unidade == self.unidade_venda and c.quantidade]

    def _custo_unit(self, nome: str) -> Decimal | None:
        cs = self._compras_mesma_unidade()
        qtd = sum((c.quantidade for c in cs), ZERO)
        return _unit(sum((getattr(c, nome) for c in cs), ZERO), qtd) if qtd else None

    @property
    def preco_venda_unit(self) -> Decimal | None:
        return _unit(self.receita_consumidor, self.quantidade_venda)

    @property
    def margem_unit_atual(self) -> Decimal | None:
        custo = self._custo_unit("custo_atual")
        vl = _unit(self.vendas_liquidas, self.quantidade_venda)
        return (vl - custo).quantize(DUAS) if custo is not None and vl is not None else None

    def cenarios_preco(self, ano: int) -> list[CenarioPreco]:
        """Preço ao consumidor, custo e margem unitários hoje e no ano em quatro combinações de decisão."""
        if not self.quantidade_venda or not self.vendas_liquidas:
            return []
        vl_hoje = self.vendas_liquidas / self.quantidade_venda
        p_hoje = self.receita_consumidor / self.quantidade_venda
        carga = self.tributos_venda_ano / self.vendas_liquidas          # tributos/valor líquido no ano
        c_hoje = self._custo_unit("custo_atual")
        c_precos, c_repasse = self._custo_unit("custo_precos_hoje"), self._custo_unit("custo_ano")

        def cenario(nome, preco, custo, vl=None):
            vl = vl if vl is not None else preco / (1 + carga)
            margem = (vl - custo) if custo is not None else None
            return CenarioPreco(nome, preco.quantize(DUAS), custo.quantize(DUAS) if custo is not None else None,
                                margem.quantize(DUAS) if margem is not None else None,
                                (margem / vl * 100).quantize(DUAS) if margem is not None and vl else None)

        saida = [cenario("Hoje", p_hoje, c_hoje, vl_hoje)]
        if c_hoje is None:  # sem compra na mesma unidade: só o efeito na venda
            saida.append(cenario("Mantendo o preço de gôndola", p_hoje, None))
            saida.append(cenario("Mantendo a receita líquida", vl_hoje * (1 + carga), None))
            return saida
        m_hoje = vl_hoje - c_hoje
        saida += [
            cenario("Gôndola mantida · fornecedor mantém o preço", p_hoje, c_precos),
            cenario("Gôndola mantida · fornecedor repassa", p_hoje, c_repasse),
            cenario("Margem mantida · fornecedor mantém o preço", (c_precos + m_hoje) * (1 + carga), c_precos),
            cenario("Margem mantida · fornecedor repassa", (c_repasse + m_hoje) * (1 + carga), c_repasse),
        ]
        return saida

    def margem_ano_precos_hoje(self, ano: int) -> CenarioPreco | None:
        """Se ninguém mudar preço: gôndola e fornecedor mantêm os preços de hoje."""
        cs = self.cenarios_preco(ano)
        return cs[1] if len(cs) > 1 and cs[1].nome.startswith("Gôndola mantida · fornecedor mantém") else None


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

    for r in a.itens:  # vendas primeiro: descrição, unidade e classificação da loja têm precedência
        if r.direcao == "saida":
            p = resumo(r)
            i = r.item
            unidade = (i.unidade_gtin or i.unidade).upper()
            p.unidade_venda = p.unidade_venda or unidade
            if unidade == p.unidade_venda:
                p.quantidade_venda += i.quantidade_gtin if i.gtin and i.quantidade_gtin else i.quantidade
                p.vendas_liquidas += i.valor_liquido
                p.receita_consumidor += r.atual.preco(i.valor_liquido)
                p.tributos_venda_atual += r.atual.tributos
                p.tributos_venda_ano += r.anos[ano].tributos
    for r in a.itens:
        if r.direcao == "entrada":
            resumo(r)
    for c in lista:
        saida[c.chave].compras.append(c)
    return sorted(saida.values(), key=lambda p: -(p.vendas_liquidas + p.custo_atual))
