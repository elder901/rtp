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
from app.parser.nfe import Documento, Item, Participante

ZERO = Decimal("0")
PIS_COFINS_NAO_CUMULATIVO = Decimal("0.0925")
CLASSIFICACAO_PADRAO = ("000", "000001")  # tributação integral

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


@dataclass
class Analise:
    empresa: Empresa
    premissas: Premissas
    itens: list[ResultadoItem]
    alertas: list[str]
    ignorados: int


def _classificar(item: Item, overrides: dict[str, tuple[str, str]]) -> Classificacao:
    for chave in (item.codigo, item.ncm):
        if chave in overrides:
            cst, cct = overrides[chave]
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
    base_pc = i.valor_operacao + (ZERO if p.industria else i.v_ipi) - i.v_icms
    return icms + ipi + (base_pc * PIS_COFINS_NAO_CUMULATIVO).quantize(Decimal("0.01"))


def _tributos_reforma(i: Item, ano: int, aliq: AliquotaEfetiva) -> tuple[Decimal, Decimal, Decimal]:
    t = TRANSICAO[ano]
    vl = i.valor_liquido
    cbs = (vl * aliq.cbs / 100).quantize(Decimal("0.01"))
    ibs = (vl * (aliq.ibs_uf + aliq.ibs_mun) / 100).quantize(Decimal("0.01"))
    residuais = (_icms_total(i) + i.v_issqn) * t.fator_icms_iss + i.v_ipi * t.fator_ipi \
        + (i.v_pis + i.v_cofins) * t.fator_pis_cofins
    return residuais + cbs + ibs, cbs, ibs


def analisar(empresa: Empresa, documentos: list[Documento], premissas: Premissas,
             calculadora: CalculadoraRTC, overrides: dict[str, tuple[str, str]] | None = None) -> Analise:
    overrides = overrides or {}
    alertas: list[str] = []
    ignorados = 0
    pendentes: list[tuple[str, Documento, Item, Participante, Classificacao]] = []

    for doc in documentos:
        direcao = doc.direcao_para(empresa.cnpj)
        if direcao is None:
            alertas.append(f"NF {doc.numero} ({doc.arquivo}): CNPJ {empresa.cnpj} não é emitente nem destinatário — ignorada.")
            continue
        contraparte = doc.emitente if direcao == "entrada" else (doc.destinatario or Participante("", "Consumidor final", "", ""))
        for item in doc.itens:
            if item.cfop[-3:] not in CFOP_ONEROSOS:
                ignorados += 1
                continue
            pendentes.append((direcao, doc, item, contraparte, _classificar(item, overrides)))

    chaves = {Chave(it.ncm, c.cst, c.cclasstrib, ano) for _, _, it, _, c in pendentes for ano in premissas.anos}
    nominais = {ano: premissas.aliquotas_nominais(ano) for ano in premissas.anos}
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
            aliq = aliquotas[Chave(item.ncm, classif.cst, classif.cclasstrib, ano)]
            r.aliquotas[ano] = aliq
            t = TRANSICAO[ano]
            if direcao == "saida" and empresa.regime == "simples":
                # Permanecendo no Simples, ICMS/PIS/COFINS dentro do DAS dão lugar a IBS/CBS dentro do DAS.
                r.anos[ano] = ValoresCenario(r.atual.tributos)
                continue
            if direcao == "entrada" and not fornecedor_regular:
                # Fornecedor do Simples/MEI continua recolhendo pelo DAS: o preço não muda,
                # mas o crédito do adquirente passa a ser só a parcela de CBS/IBS do DAS.
                tributos = item.tributos_por_dentro + (item.v_icms_st + item.v_fcp_st) * t.fator_icms_iss
                cred = ZERO
                if empresa.regime != "simples":
                    pct = premissas.credito_fornecedor_simples_pct if contraparte.regime == "simples" \
                        else premissas.credito_fornecedor_mei_pct
                    cred = (item.valor_liquido * pct / 100).quantize(Decimal("0.01")) \
                        + item.v_cred_icms_sn * t.fator_icms_iss
                r.anos[ano] = ValoresCenario(tributos, cred)
                continue

            tributos, cbs, ibs = _tributos_reforma(item, ano, aliq)
            cred = ZERO
            if direcao == "entrada" and empresa.regime != "simples":
                # Crédito amplo de CBS/IBS + ICMS residual da transição.
                cred = cbs + ibs + (item.v_icms + item.v_fcp) * t.fator_icms_iss
            r.anos[ano] = ValoresCenario(tributos, cred, cbs, ibs)
        resultados.append(r)

    _alertas_gerais(resultados, empresa, alertas, ignorados)
    return Analise(empresa, premissas, resultados, alertas, ignorados)


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
    if ignorados:
        alertas.append(f"{ignorados} item(ns) com CFOP não oneroso (devolução, remessa, bonificação etc.) foram "
                       f"desconsiderados.")
    if empresa.regime == "simples":
        alertas.append("Empresa do Simples: saídas estimadas pela alíquota efetiva do DAS informada nas premissas. "
                       "A simulação de opção pelo regime regular de CBS/IBS fica na próxima fase.")
