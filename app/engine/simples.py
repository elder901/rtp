"""Empresa do Simples: permanecer no Simples puro x optar por recolher CBS/IBS pelo regime regular (LC 214/2025).

- Simples puro: tudo pelo DAS. Clientes PJ só creditam a parcela de CBS/IBS embutida no DAS.
- Híbrido (CBS/IBS regular): o DAS deixa de incluir a parcela que CBS/IBS substituem; CBS/IBS passam a ser
  apurados com débito nas vendas e crédito amplo nas compras, e o cliente PJ credita o valor cheio.

A parcela do DAS substituída depende do anexo e da faixa (premissas). Durante a transição só a fração do
ICMS/ISS já extinta é substituída pelo IBS.
"""
from __future__ import annotations

from decimal import Decimal

from app.engine.cenarios import Analise
from app.engine.premissas import TRANSICAO

ZERO = Decimal("0")


def _cliente_pj(r) -> bool:
    return len(r.contraparte.cnpj) == 14


def comparar(a_simples: Analise, a_regular: Analise) -> list[dict]:
    """a_simples: análise com regime 'simples'; a_regular: mesma base analisada como regime regular."""
    p = a_simples.premissas
    das_total = sum((r.atual.tributos for r in a_simples.itens if r.direcao == "saida"), ZERO)
    vendas = sum((r.item.valor_liquido for r in a_simples.itens if r.direcao == "saida"), ZERO)
    vendas_pj = [r for r in a_simples.itens if r.direcao == "saida" and _cliente_pj(r)]
    das_pj = sum((r.atual.tributos for r in vendas_pj), ZERO)
    regulares = {(r.documento.chave, r.item.n_item): r for r in a_regular.itens}

    linhas = []
    for ano in p.anos:
        t = TRANSICAO[ano]
        parcela = (p.das_pis_cofins_pct + p.das_icms_iss_pct * (1 - t.fator_icms_iss)) / 100
        debitos = creditos = credito_cliente = ZERO
        for r in a_simples.itens:
            reg = regulares[(r.documento.chave, r.item.n_item)]
            v = reg.anos[ano]
            if r.direcao == "saida":
                debitos += v.cbs + v.ibs
                if _cliente_pj(r):
                    credito_cliente += v.cbs + v.ibs
            elif r.contraparte.regime == "normal":
                creditos += v.cbs + v.ibs
            else:
                pct = {"simples": p.credito_fornecedor_simples_pct, "mei": p.credito_fornecedor_mei_pct,
                       "nao_contribuinte": p.credito_presumido_nao_contribuinte_pct}[r.contraparte.regime]
                creditos += (r.item.valor_liquido * pct / 100).quantize(Decimal("0.01"))
        das_reduzido = (das_total * (1 - parcela)).quantize(Decimal("0.01"))
        hibrido = das_reduzido + debitos - creditos
        linhas.append({
            "ano": ano, "vendas_liquidas": vendas,
            "simples_carga": das_total,
            "simples_credito_clientes_pj": (das_pj * parcela).quantize(Decimal("0.01")),
            "hibrido_das": das_reduzido, "hibrido_debitos": debitos, "hibrido_creditos": creditos,
            "hibrido_carga": hibrido,
            "hibrido_credito_clientes_pj": credito_cliente,
            "diferenca": hibrido - das_total,
            "melhor": "Simples puro" if das_total <= hibrido else "CBS/IBS regular",
        })
    return linhas
