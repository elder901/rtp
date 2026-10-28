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
from dataclasses import dataclass, field, replace
from decimal import Decimal
from functools import cached_property

from app.engine.cenarios import Analise, ResultadoItem, taxa_por_dentro
from app.engine.premissas import TRANSICAO

ZERO = Decimal("0")
DUAS = Decimal("0.01")
QUATRO = Decimal("0.0001")
# Tributo pago antes na cadeia e que não aparece destacado na nota do fornecedor: PIS/COFINS monofásico ou por
# substituição na revenda (CST 04, 05) e ICMS-ST retido anteriormente (CST 60, CSOSN 500). Quando ele sai, o custo do
# fornecedor também cai, e a meta calculada só com o que está na nota fica abaixo do possível.
PIS_PAGO_ANTES = {"04", "05"}
ICMS_PAGO_ANTES = {"60", "500"}
# Margem de hoje (% do valor líquido de venda) que ainda se explica por preço. Fora disso a unidade vendida não é a
# comprada, ainda que o nome coincida (ex.: o fardo de 18 latas vendido como "UN" com o EAN da lata).
MARGEM_PLAUSIVEL = (Decimal("-50"), Decimal("70"))


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
    quantidade_comercial: Decimal = ZERO      # qCom: caixas, fardos... (quantidade está na unidade do EAN tributável)
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
    # composição do preço de nota de hoje: o que o fornecedor destaca na nota
    icms: Decimal = ZERO                      # ICMS próprio + FCP
    icms_st: Decimal = ZERO                   # ICMS-ST + FCP-ST
    ipi: Decimal = ZERO
    pis_cofins: Decimal = ZERO
    cbs_ibs_ano: Decimal = ZERO               # CBS + IBS na compra no ano analisado (crédito integral)
    # confiabilidade (em preço de nota de hoje)
    valor_presumido: Decimal = ZERO           # classificação presumida pelo NCM ou tributação integral por falta dela
    valor_imposto_anterior: Decimal = ZERO    # com tributo pago antes na cadeia (PIS_PAGO_ANTES, ICMS_PAGO_ANTES)
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
        c.quantidade_comercial += i.quantidade
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
        c.icms += i.v_icms + i.v_fcp
        c.icms_st += i.v_icms_st + i.v_fcp_st
        c.ipi += i.v_ipi
        c.pis_cofins += i.v_pis + i.v_cofins
        c.cbs_ibs_ano += v.cbs + v.ibs
        if r.classificacao.origem in ("anexo", "padrao"):
            c.valor_presumido += i.valor_operacao
        if i.pis_cst in PIS_PAGO_ANTES or i.icms_cst in ICMS_PAGO_ANTES:
            c.valor_imposto_anterior += i.valor_operacao
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
    icms: Decimal = ZERO
    icms_st: Decimal = ZERO
    ipi: Decimal = ZERO
    pis_cofins: Decimal = ZERO
    cbs_ibs_ano: Decimal = ZERO
    imposto_seletivo_ano: Decimal = ZERO
    creditos_atual: Decimal = ZERO
    creditos_ano: Decimal = ZERO
    valor_presumido: Decimal = ZERO
    valor_imposto_anterior: Decimal = ZERO
    cnpjs: int = 1                    # agrupado pela raiz: quantos CNPJs (filiais) do fornecedor faturaram

    @property
    def variacao_custo(self) -> Decimal:
        """A preços de hoje."""
        return self.custo_precos_hoje - self.custo_atual

    def sai_do_preco(self, ano: int) -> Decimal:
        """Tributo destacado hoje na nota que deixa de existir no ano (PIS/COFINS e IPI em 2027; o ICMS aos poucos
        de 2029 a 2033) — o argumento da negociação."""
        t = TRANSICAO[ano]
        return (self.pis_cofins * (1 - t.fator_pis_cofins) + self.ipi * (1 - t.fator_ipi)
                + (self.icms + self.icms_st) * (1 - t.fator_icms_iss))

    def parte(self, valor: Decimal) -> Decimal | None:
        """`valor` como % do preço de nota de hoje."""
        return (valor / self.preco_nota_atual * 100).quantize(DUAS) if self.preco_nota_atual else None

    @property
    def presumido_pct(self) -> Decimal | None:
        return self.parte(self.valor_presumido)

    @property
    def imposto_anterior_pct(self) -> Decimal | None:
        return self.parte(self.valor_imposto_anterior)

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


def fornecedores(lista: list[Compra], por_raiz: bool = False) -> list[ResumoFornecedor]:
    """Resumo por fornecedor. `por_raiz`: junta os CNPJs do mesmo fornecedor (filiais que faturam), que é com quem se
    negocia; quem fatura por um CNPJ só continua identificado pelo CNPJ completo."""
    grupos: dict[str, list[Compra]] = defaultdict(list)
    for c in lista:
        grupos[c.fornecedor[:8] if por_raiz else c.fornecedor].append(c)
    saida = []
    for chave, cs in grupos.items():
        c0 = max(cs, key=lambda c: c.preco_nota_atual)   # nome, regime e UF do CNPJ que mais faturou
        cnpjs = {c.fornecedor for c in cs}
        cnpj = chave if len(cnpjs) > 1 else c0.fornecedor

        def soma(nome):
            return sum((getattr(c, nome) for c in cs), ZERO)

        saida.append(ResumoFornecedor(
            cnpj, c0.fornecedor_nome, c0.regime, c0.uf, len({(c.chave, c.unidade) for c in cs}),
            len(set().union(*(c.notas for c in cs))),
            soma("valor_liquido"), soma("preco_nota_atual"), soma("preco_nota_equilibrio"), soma("custo_atual"),
            soma("custo_ano"), soma("custo_precos_hoje"),
            sum((c.economia_trocando or ZERO for c in cs), ZERO), sum(1 for c in cs if c.economia_trocando),
            *(soma(n) for n in ("icms", "icms_st", "ipi", "pis_cofins", "cbs_ibs_ano", "imposto_seletivo_ano",
                                "creditos_atual", "creditos_ano", "valor_presumido", "valor_imposto_anterior")),
            cnpjs=len(cnpjs)))
    return sorted(saida, key=lambda f: -f.variacao_custo)


def meses_de_compra(a: Analise) -> int:
    """Meses com compras na análise — base para levar o período a valores por ano."""
    return len({r.documento.emissao.strftime("%Y-%m") for r in a.itens
                if r.direcao == "entrada" and r.documento.emissao}) or 1


@dataclass
class MetaAno:
    ano: int
    preco_nota_equilibrio_pct: Decimal | None   # negativo: desconto a pedir
    variacao_custo_por_ano: Decimal             # R$ por ano se o fornecedor mantiver os preços
    sai_do_preco_pct: Decimal | None            # tributo destacado hoje que deixa de existir no ano


def do_fornecedor(cnpj_ou_raiz: str, fornecedor: str) -> bool:
    """O CNPJ `fornecedor` é o pedido — CNPJ completo ou a raiz (8 dígitos) de um fornecedor com filiais."""
    return fornecedor.startswith(cnpj_ou_raiz) if len(cnpj_ou_raiz) == 8 else fornecedor == cnpj_ou_raiz


def metas_por_ano(a: Analise, cnpj: str, fator_ano: Decimal) -> list[MetaAno]:
    """A meta de preço de nota do fornecedor em cada ano da transição (só as compras dele). `cnpj`: CNPJ ou raiz."""
    so_dele = replace(a, itens=[r for r in a.itens
                                if r.direcao == "entrada" and do_fornecedor(cnpj, r.contraparte.cnpj)])
    saida = []
    for ano in a.premissas.anos:
        resumo = fornecedores(compras(so_dele, ano), por_raiz=True)
        if resumo:
            f = resumo[0]
            saida.append(MetaAno(ano, f.variacao_preco_nota_equilibrio_pct,
                                 (f.variacao_custo * fator_ano).quantize(DUAS), f.parte(f.sai_do_preco(ano))))
    return saida


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

    @cached_property
    def fator_unidade(self) -> Decimal | None:
        """Unidades de compra em cada unidade vendida: 1; ou a embalagem da nota de compra (qTrib / qCom) quando só
        ela explica o preço (a loja vende o fardo com o EAN da unidade); None quando nenhuma dá margem plausível."""
        cs = self._compras_mesma_unidade()
        qtd, qcom = sum((c.quantidade for c in cs), ZERO), sum((c.quantidade_comercial for c in cs), ZERO)
        if not qtd or not self.quantidade_venda or not self.vendas_liquidas:
            return Decimal(1)
        vl = self.vendas_liquidas / self.quantidade_venda
        custo = sum((c.custo_atual for c in cs), ZERO) / qtd
        candidatos = [Decimal(1)]
        if qcom and qtd / qcom >= Decimal("1.5"):
            candidatos.append((qtd / qcom).quantize(Decimal(1)))
        for f in candidatos:
            if MARGEM_PLAUSIVEL[0] <= (vl - custo * f) / vl * 100 <= MARGEM_PLAUSIVEL[1]:
                return f
        return None

    @property
    def unidades_incompativeis(self) -> bool:
        return self.fator_unidade is None

    def _custo_unit(self, nome: str) -> Decimal | None:
        cs = self._compras_mesma_unidade()
        qtd = sum((c.quantidade for c in cs), ZERO)
        if not qtd or self.fator_unidade is None:
            return None
        return _unit(sum((getattr(c, nome) for c in cs), ZERO) * self.fator_unidade, qtd)

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
