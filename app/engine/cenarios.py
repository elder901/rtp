"""Cenário atual x cenário da reforma, item a item.

Premissa central: o valor líquido (preço sem nenhum tributo sobre consumo) é mantido. Sobre ele se aplicam
os tributos atuais (como estão no XML) e os da reforma em cada ano da transição. Assim a diferença mostra
quanto o preço precisaria mudar para manter a mesma receita líquida (saídas) ou quanto o custo efetivo de
compra muda depois dos créditos (entradas).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.calculadora.client import AliquotaEfetiva, CalculadoraRTC, Chave
from app.engine.premissas import TRANSICAO, Premissas
from app.localidades import CODIGO_UF
from app.parser.nfe import Documento, Item, Participante

ZERO = Decimal("0")
PIS_COFINS_NAO_CUMULATIVO = Decimal("0.0925")
# CST de PIS do fornecedor em que a aquisição não está sujeita à contribuição e por isso não gera crédito
# (Lei 10.833/2003, art. 3º, § 2º, II): monofásico, ST, alíquota zero, isenção, sem incidência, suspensão.
CST_PIS_SEM_CREDITO = {"04", "05", "06", "07", "08", "09"}
CLASSIFICACAO_PADRAO = ("000", "000001")  # tributação integral

# CFOPs de venda de produção do próprio estabelecimento: quem vende é o fabricante, e é nele que o Imposto
# Seletivo é cobrado (primeiro fornecimento). Nas revendas o IS já foi cobrado antes (cClassTrib IS 200007).
CFOP_PRODUCAO_PROPRIA = {"101", "103", "105", "107", "109", "111", "113", "116", "118", "122", "401", "402"}
# Unidades comerciais equivalentes a 1 unidade do IS para a parte ad rem (ex.: cigarro, VN = maço de 20).
UNIDADES_EQUIVALENTES_IS = {"VN": {"VN", "MC", "MACO", "MAÇO", "UN", "UND", "UNID"}}

# CFOPs onerosos considerados (3 últimos dígitos): vendas, vendas com ST, combustíveis, serviços.
CFOP_ONEROSOS = {f"{n:03d}" for n in [*range(101, 125), *range(401, 406), *range(651, 657), 933]}


@dataclass
class Empresa:
    cnpj: str
    nome: str
    regime: str          # "real" | "presumido" | "simples"
    uf: str
    cod_municipio: int


@dataclass
class Classificacao:
    cst: str
    cclasstrib: str
    origem: str  # "manual" | "xml" | "padrao"


@dataclass
class ValoresCenario:
    tributos: Decimal      # tributos sobre consumo embutidos/somados ao preço
    creditos: Decimal = ZERO  # créditos que a empresa aproveita (só entradas)
    cbs: Decimal = ZERO
    ibs: Decimal = ZERO
    imposto_seletivo: Decimal = ZERO  # já incluído em tributos; nunca gera crédito

    def preco(self, valor_liquido: Decimal) -> Decimal:
        return valor_liquido + self.tributos

    def custo_ou_receita(self, valor_liquido: Decimal) -> Decimal:
        """Entrada: custo efetivo (preço - créditos). Saída: preço ao cliente."""
        return self.preco(valor_liquido) - self.creditos


@dataclass
class ResultadoItem:
    direcao: str
    documento: Documento
    item: Item
    contraparte: Participante
    classificacao: Classificacao
    atual: ValoresCenario
    anos: dict[int, ValoresCenario] = field(default_factory=dict)
    aliquotas: dict[int, AliquotaEfetiva] = field(default_factory=dict)
    imposto_seletivo: str = ""  # "" | cobrado (fabricante) | embutido (revendedor) | não incide (venda)


@dataclass
class Analise:
    empresa: Empresa
    premissas: Premissas
    itens: list[ResultadoItem]
    alertas: list[str]
    ignorados: int
    calculadora: dict = field(default_factory=dict)  # endereço e versão usados (rastreabilidade)
    aliquotas_usadas: dict = field(default_factory=dict)  # ano -> {"aliquotas": {...}, "origens": {...}}

    @property
    def versao_calculadora(self) -> str:
        c = self.calculadora
        if not c:
            return "não informada"
        if c.get("erro"):
            return f"indisponível ({c['erro'][:80]})"
        return f"app {c['versao_app']} · base de regras {c['versao_base']} de {c['data_base']} · {c['url']}"


@dataclass
class Classificacoes:
    """Ajustes manuais de classificação, buscados nesta ordem: EAN, código do produto, NCM.

    Chaves gravadas com prefixo ("ean:", "cod:", "ncm:"). Chaves sem prefixo são do formato antigo
    (código ou NCM) e continuam valendo para os dois.
    """
    por_ean: dict[str, tuple[str, str]] = field(default_factory=dict)
    por_codigo: dict[str, tuple[str, str]] = field(default_factory=dict)
    por_ncm: dict[str, tuple[str, str]] = field(default_factory=dict)

    @classmethod
    def de(cls, ajustes: "dict[str, tuple[str, str]] | Classificacoes | None") -> "Classificacoes":
        if isinstance(ajustes, Classificacoes):
            return ajustes
        c = cls()
        for chave, valor in (ajustes or {}).items():
            tipo, sep, resto = chave.partition(":")
            if sep and tipo == "ean":
                c.por_ean[resto] = valor
            elif sep and tipo == "cod":
                c.por_codigo[resto] = valor
            elif sep and tipo == "ncm":
                c.por_ncm[resto] = valor
            else:
                c.por_codigo.setdefault(chave, valor)
                c.por_ncm.setdefault(chave, valor)
        return c

    def buscar(self, item: Item) -> tuple[tuple[str, str], str] | None:
        """Retorna ((cst, cClassTrib), chave usada) ou None."""
        if item.gtin and item.gtin in self.por_ean:
            return self.por_ean[item.gtin], f"ean:{item.gtin}"
        if item.codigo in self.por_codigo:
            return self.por_codigo[item.codigo], f"cod:{item.codigo}"
        if item.ncm in self.por_ncm:
            return self.por_ncm[item.ncm], f"ncm:{item.ncm}"
        return None


def _classificar(item: Item, ajustes: Classificacoes) -> Classificacao:
    achado = ajustes.buscar(item)
    if achado:
        (cst, cct), _ = achado
        return Classificacao(cst, cct, "manual")
    if item.ibscbs and item.ibscbs.cclasstrib:
        return Classificacao(item.ibscbs.cst, item.ibscbs.cclasstrib, "xml")
    return Classificacao(*CLASSIFICACAO_PADRAO, "padrao")


def _icms_total(i: Item) -> Decimal:
    return i.v_icms + i.v_fcp + i.v_icms_st + i.v_fcp_st


def _creditos_atuais(i: Item, empresa: Empresa, fornecedor: Participante, p: Premissas) -> Decimal:
    if empresa.regime == "simples":
        return ZERO
    icms = i.v_cred_icms_sn if fornecedor.regime in ("simples", "mei") else i.v_icms + i.v_fcp
    ipi = i.v_ipi if p.industria else ZERO
    if empresa.regime == "presumido":
        return icms + ipi
    # Lucro real: PIS/COFINS não cumulativo sobre o custo de aquisição, sem o ICMS (Lei 14.592/2023).
    # Sem crédito em compra de pessoa física (art. 3º, § 3º) ou não sujeita à contribuição (CST 04 a 09).
    if i.pis_cst in CST_PIS_SEM_CREDITO or len(fornecedor.cnpj) != 14:
        return icms + ipi
    base_pc = i.valor_operacao + (ZERO if p.industria else i.v_ipi) - i.v_icms
    return icms + ipi + (base_pc * PIS_COFINS_NAO_CUMULATIVO).quantize(Decimal("0.01"))


def taxa_por_dentro(i: Item, ano: int | None = None) -> Decimal:
    """Parte do preço de nota que é tributo embutido (ICMS, FCP, ISS; e PIS/COFINS hoje). ano=None: hoje."""
    if not i.valor_operacao:
        return ZERO
    if ano is None:
        return i.tributos_por_dentro / i.valor_operacao
    t = TRANSICAO[ano]
    return ((i.v_icms + i.v_fcp + i.v_issqn) * t.fator_icms_iss
            + (i.v_pis + i.v_cofins) * t.fator_pis_cofins) / i.valor_operacao


def escala_preco(i: Item, ano: int) -> Decimal:
    """Preço de nota no ano (mantido o valor líquido) / preço de nota de hoje.

    Ex.: em 2027 o PIS/COFINS sai do preço e só o ICMS continua embutido, então o mesmo valor líquido corresponde a
    um preço de nota menor. ICMS, FCP, ISS, ICMS-ST e IPI são proporcionais ao preço e acompanham essa razão.
    """
    if not i.valor_operacao:
        return Decimal(1)
    return i.valor_liquido / (1 - taxa_por_dentro(i, ano)) / i.valor_operacao


def _residuais(i: Item, ano: int) -> Decimal:
    """Tributos atuais que ainda existem no ano, recalculados sobre o preço de nota daquele ano."""
    t = TRANSICAO[ano]
    proporcionais = (_icms_total(i) + i.v_issqn) * t.fator_icms_iss + i.v_ipi * t.fator_ipi
    return proporcionais * escala_preco(i, ano) + (i.v_pis + i.v_cofins) * t.fator_pis_cofins


def _tributos_reforma(i: Item, ano: int, aliq: AliquotaEfetiva,
                      imposto_seletivo: Decimal = ZERO) -> tuple[Decimal, Decimal, Decimal]:
    """O IS integra a base da CBS/IBS (LC 214/2025): base = valor líquido + IS."""
    base = i.valor_liquido + imposto_seletivo
    cbs = (base * aliq.cbs / 100).quantize(Decimal("0.01"))
    ibs = (base * (aliq.ibs_uf + aliq.ibs_mun) / 100).quantize(Decimal("0.01"))
    residuais = _residuais(i, ano).quantize(Decimal("0.01"))
    return residuais + imposto_seletivo + cbs + ibs, cbs, ibs


def _tratamento_is(item: Item, ncms_is: set[str]) -> tuple[str, str]:
    """CST/cClassTrib do IS da operação: cobrado na venda de produção própria; já cobrado antes nas revendas."""
    if item.ncm not in ncms_is:
        return "", ""
    return ("000", "000001") if item.cfop[-3:] in CFOP_PRODUCAO_PROPRIA else ("200", "200007")


def _quantidade_is(item: Item, unidade_is: str) -> Decimal | None:
    if not unidade_is or item.unidade.upper() in UNIDADES_EQUIVALENTES_IS.get(unidade_is, {unidade_is}):
        return item.quantidade
    return None


def _valor_is(item: Item, aliq: AliquotaEfetiva, direcao: str, p: Premissas,
              avisos: set[str]) -> tuple[Decimal, str]:
    """Valor do IS que pesa nesta operação e o tratamento. Compra de fabricante: IS cobrado na própria nota
    (alíquotas oficiais). Compra de revendedor: IS cobrado antes e embutido no preço — estimado com a alíquota
    oficial sobre o preço do revendedor x repasse (premissa; limite superior com 100%)."""
    if aliq.is_pct or aliq.is_ad_rem:
        pct, ad_rem, fator, tratamento = aliq.is_pct, aliq.is_ad_rem, Decimal(1), "cobrado (fabricante)"
    elif direcao == "entrada" and (aliq.is_nominal_pct or aliq.is_ad_rem_nominal):
        pct, ad_rem = aliq.is_nominal_pct, aliq.is_ad_rem_nominal
        fator, tratamento = p.repasse_is_revendedor_pct / 100, "embutido (revendedor, estimado)"
    elif aliq.is_nominal_pct or aliq.is_ad_rem_nominal:
        return ZERO, "não incide (revenda)"
    else:
        return ZERO, ""
    valor = item.valor_liquido * pct / 100
    if ad_rem:
        qtd = _quantidade_is(item, aliq.is_unidade)
        if qtd is None:
            avisos.add(f"NCM {item.ncm}: parte ad rem do IS (R$ {ad_rem} por {aliq.is_unidade}) não incluída — a "
                       f"unidade da nota ({item.unidade}) não equivale à do IS.")
        else:
            valor += ad_rem * qtd
    return (valor * fator).quantize(Decimal("0.01")), tratamento


def analisar(empresa: Empresa, documentos: list[Documento], premissas: Premissas,
             calculadora: CalculadoraRTC,
             overrides: "dict[str, tuple[str, str]] | Classificacoes | None" = None) -> Analise:
    overrides = Classificacoes.de(overrides)
    alertas: list[str] = []
    ignorados = 0
    pendentes: list[tuple[str, Documento, Item, Participante, Classificacao]] = []

    for doc in documentos:
        direcao = doc.direcao_para(empresa.cnpj)
        if direcao is None:
            alertas.append(f"NF {doc.numero} ({doc.arquivo}): CNPJ {empresa.cnpj} não é emitente nem destinatário — ignorada.")
            continue
        contraparte = doc.contraparte_para(empresa.cnpj)
        for item in doc.itens:
            if item.cfop[-3:] not in CFOP_ONEROSOS:
                ignorados += 1
                continue
            pendentes.append((direcao, doc, item, contraparte, _classificar(item, overrides)))

    ncms_is: set[str] = set()
    if hasattr(calculadora, "imposto_seletivo"):
        for ncm in {it.ncm for _, _, it, _, _ in pendentes}:
            try:
                if calculadora.imposto_seletivo(ncm):
                    ncms_is.add(ncm)
            except Exception:  # noqa: BLE001 — sem a base oficial não dá para saber; avisa
                alertas.append(f"Não foi possível verificar na calculadora se o NCM {ncm} tem Imposto Seletivo.")
    tratamento_is = {id(it): _tratamento_is(it, ncms_is) for _, _, it, _, _ in pendentes}
    chaves = {Chave(it.ncm, c.cst, c.cclasstrib, ano, *tratamento_is[id(it)])
              for _, _, it, _, c in pendentes for ano in premissas.anos}
    avisos_is: set[str] = set()
    nominais, origens = {}, {}
    for ano in premissas.anos:
        nominais[ano], origens[ano] = premissas.aliquotas_do_ano(ano, calculadora, CODIGO_UF.get(empresa.uf),
                                                                 empresa.cod_municipio)
    aliquotas = calculadora.aliquotas(chaves, nominais, empresa.uf, empresa.cod_municipio) if chaves else {}

    resultados = []
    for direcao, doc, item, contraparte, classif in pendentes:
        r = ResultadoItem(direcao, doc, item, contraparte, classif, atual=ValoresCenario(ZERO))
        fornecedor_regular = contraparte.regime == "normal"

        if direcao == "saida" and empresa.regime == "simples":
            das = (item.valor_operacao * premissas.aliquota_das_pct / 100).quantize(Decimal("0.01"))
            r.atual = ValoresCenario(das)
        elif direcao == "saida":
            r.atual = ValoresCenario(item.tributos_por_dentro + item.tributos_por_fora)
        else:
            r.atual = ValoresCenario(item.tributos_por_dentro + item.tributos_por_fora,
                                     _creditos_atuais(item, empresa, contraparte, premissas))

        for ano in premissas.anos:
            aliq = aliquotas[Chave(item.ncm, classif.cst, classif.cclasstrib, ano, *tratamento_is[id(item)])]
            r.aliquotas[ano] = aliq
            t = TRANSICAO[ano]
            valor_is, r.imposto_seletivo = _valor_is(item, aliq, direcao, premissas, avisos_is)
            if direcao == "saida" and empresa.regime == "simples":
                # Permanecendo no Simples, ICMS/PIS/COFINS dentro do DAS dão lugar a IBS/CBS dentro do DAS.
                r.anos[ano] = ValoresCenario(r.atual.tributos)
                continue
            if direcao == "entrada" and not fornecedor_regular:
                # Fornecedor do Simples/MEI continua recolhendo pelo DAS e o não contribuinte (produtor rural
                # pessoa física) não recolhe CBS/IBS: o preço não muda, mas o crédito do adquirente passa a ser só
                # a parcela de CBS/IBS do DAS ou o crédito presumido (premissas).
                tributos = _residuais(item, ano).quantize(Decimal("0.01")) + valor_is
                cred = ZERO
                if empresa.regime != "simples":
                    pct = {"simples": premissas.credito_fornecedor_simples_pct,
                           "mei": premissas.credito_fornecedor_mei_pct,
                           "nao_contribuinte": premissas.credito_presumido_nao_contribuinte_pct}[contraparte.regime]
                    icms_residual = ((item.v_cred_icms_sn if contraparte.regime in ("simples", "mei")
                                      else item.v_icms + item.v_fcp) * t.fator_icms_iss
                                     * escala_preco(item, ano)).quantize(Decimal("0.01"))
                    cred = (item.valor_liquido * pct / 100).quantize(Decimal("0.01")) + icms_residual
                r.anos[ano] = ValoresCenario(tributos, cred, imposto_seletivo=valor_is)
                continue

            tributos, cbs, ibs = _tributos_reforma(item, ano, aliq, valor_is)
            cred = ZERO
            if direcao == "entrada" and empresa.regime != "simples":
                # Crédito amplo de CBS/IBS + ICMS residual da transição.
                cred = cbs + ibs + ((item.v_icms + item.v_fcp) * t.fator_icms_iss
                                    * escala_preco(item, ano)).quantize(Decimal("0.01"))
            r.anos[ano] = ValoresCenario(tributos, cred, cbs, ibs, valor_is)
        resultados.append(r)

    _alertas_gerais(resultados, empresa, alertas, ignorados)
    _alertas_is(resultados, premissas, alertas, avisos_is)
    ident = calculadora.identificacao() if hasattr(calculadora, "identificacao") else {}
    usadas = {ano: {"aliquotas": nominais[ano], "origens": origens[ano]} for ano in premissas.anos}
    if any(o != "oficial" for u in usadas.values() for o in u["origens"].values()):
        alertas.append("Alíquotas de CBS/IBS de 2027 em diante ainda não publicadas na base oficial da calculadora: "
                       "usadas as alíquotas de referência das premissas. Quando a Receita publicar, as oficiais passam "
                       "a ser usadas automaticamente.")
    return Analise(empresa, premissas, resultados, alertas, ignorados, ident, usadas)


def _alertas_is(resultados: list[ResultadoItem], p: Premissas, alertas: list[str], avisos: set[str]):
    com_is = [r for r in resultados if r.imposto_seletivo]
    if not com_is:
        return
    ncms = sorted({r.item.ncm for r in com_is})
    alertas.append(f"Imposto Seletivo em {len(com_is)} item(ns), NCM {', '.join(ncms[:12])}{'...' if len(ncms) > 12 else ''} "
                   f"(alíquotas oficiais da calculadora). Na venda pela empresa o IS não incide; em compras de fabricante "
                   f"entra no custo sem crédito.")
    if any(r.imposto_seletivo.startswith("embutido") for r in com_is):
        alertas.append(f"Compras de revendedor de produtos com IS: o imposto foi cobrado no fabricante e está embutido no "
                       f"preço; estimado com a alíquota sobre o preço do revendedor x repasse de "
                       f"{p.repasse_is_revendedor_pct}% (premissa; 100% é o limite superior).")
    alertas.extend(sorted(avisos))


def _alertas_gerais(resultados: list[ResultadoItem], empresa: Empresa, alertas: list[str], ignorados: int):
    padrao = {(r.item.codigo, r.item.ncm) for r in resultados if r.classificacao.origem == "padrao"}
    if padrao:
        alertas.append(f"{len(padrao)} produto(s) sem cClassTrib no XML nem classificação manual: assumida "
                       f"tributação integral (CST 000 / 000001). Revise na aba Classificação.")
    erros = {(r.item.ncm, r.classificacao.cclasstrib, a.erro) for r in resultados for a in r.aliquotas.values() if a.erro}
    for ncm, cct, erro in sorted(erros):
        alertas.append(f"Calculadora recusou NCM {ncm} / cClassTrib {cct}: {erro} — CBS/IBS desse item ficou zerado.")
    mono = {r.item.ncm for r in resultados if r.classificacao.cclasstrib.startswith("62")}
    if mono:
        alertas.append(f"Itens com monofasia (combustíveis, NCM {', '.join(sorted(mono))}): cálculo ad rem não "
                       f"suportado nesta versão.")
    rurais = {r.contraparte.cnpj for r in resultados
              if r.direcao == "entrada" and r.contraparte.regime == "nao_contribuinte"}
    if rurais:
        alertas.append(f"Compras de {len(rurais)} fornecedor(es) não contribuinte(s) (ex.: produtor rural pessoa "
                       f"física): na reforma o crédito vem do crédito presumido informado nas premissas.")
    if ignorados:
        alertas.append(f"{ignorados} item(ns) com CFOP não oneroso (devolução, remessa, bonificação etc.) foram "
                       f"desconsiderados.")
    if empresa.regime == "simples":
        alertas.append("Empresa do Simples: saídas estimadas pela alíquota efetiva do DAS informada nas premissas. "
                       "A simulação de opção pelo regime regular de CBS/IBS fica na próxima fase.")
