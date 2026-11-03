"""Venda do PDV pelo ERP do Flex, consolidada por mês, para a loja cujo PDV não grava o XML das NFC-e.

A loja 003 do Tejotão emite e autoriza as NFC-e normalmente (a capa delas está em wrpdv.movnfce), mas o XML de cada
cupom nunca foi gravado em xmlpdv_MMAA. O ERP guarda a mesma venda por produto e dia em movprodd_part (documento
EVP, status N). Conferido em 2026: na loja 002, que grava os XMLs, essa venda bate ao centavo, mês a mês, com a soma
das NFC-e consolidadas; na 003, com o total das NFC-e autorizadas em movnfce.

Do ERP vêm os valores (venda, desconto, quantidade, ICMS, FCP, PIS, COFINS), o CST de ICMS, NCM, EAN e unidade do
cadastro. O que o ERP não guarda por item (CST de PIS/COFINS, descrição e unidade como saem na NFC-e) vem do mesmo
produto nas NFC-e da própria loja, nos meses que têm XML (ex.: o backup de ~40 dias do PDV), ou, sem elas, nas das
outras lojas da empresa; sem nenhuma referência, das alíquotas e do cadastro. Conferido em 21-30/09/2026, com os XMLs
da 003: mesmo total ao centavo e o mesmo tratamento (NCM, CST de ICMS e de PIS/COFINS, CFOP) em 98,5% do valor.
"""
from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path

from app.ingest.consolidacao import MesNFCe
from app.parser.nfe import Item, Participante
from app.produto import gtin_global

ZERO = Decimal("0")

# Uma consulta por faixa de meses (database erp). Agrupada por mês, produto e CST de ICMS: ~8 mil linhas por mês.
CONSULTA = """SELECT to_char(m.mprd_datamvto, 'YYYY-MM') AS mes, m.mprd_prod_codigo AS cod, m.mprd_cst AS cst,
 max(coalesce(nullif(p.prod_descrpdvs, ''), p.prod_descricao)) AS descricao, max(p.prod_codbarras) AS ean,
 max(p.prod_emb) AS emb, max(p.prod_codigoncm) AS ncm, round(sum(m.mprd_valor), 2) AS valor,
 round(sum(m.mprd_descontovalor), 2) AS desconto, round(sum(m.mprd_qtde), 4) AS qtde,
 round(sum(m.mprd_icmsvalor), 2) AS icms, round(sum(m.mprd_valorfcp), 2) AS fcp, round(sum(m.mprd_valorpis), 2) AS pis,
 round(sum(m.mprd_valorcofins), 2) AS cofins, max(m.mprd_aliqpis) AS aliqpis
FROM movprodd_part m JOIN produtos p ON p.prod_codigo = m.mprd_prod_codigo
WHERE m.mprd_unid_codigo = '{unidade}' AND m.mprd_datamvto BETWEEN '{inicio}' AND '{fim}'
  AND m.mprd_dcto_tipo = 'EVP' AND m.mprd_status = 'N'
GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"""


def _d(v) -> Decimal:
    return Decimal(str(v)) if v not in (None, "") else ZERO


def ler_resultados(arquivos: Iterable[Path], avisos: list[str] | None = None) -> dict[str, list[dict]]:
    """Linhas de resultados de consulta em JSON ({"rows": [...]}), separadas por mês (AAAA-MM). Resultado cortado
    pelo teto de linhas da conexão ("truncated": true) perde o último mês, que pode ter vindo pela metade."""
    por_mes: dict[str, list[dict]] = defaultdict(list)
    for arquivo in arquivos:
        with open(arquivo, encoding="utf-8") as f:
            dados = json.load(f)
        linhas = dados.get("rows") or []
        if dados.get("truncated") and linhas:
            ultimo = max(l["mes"] for l in linhas)
            linhas = [l for l in linhas if l["mes"] != ultimo]
            if avisos is not None:
                avisos.append(f"{Path(arquivo).name}: resultado cortado; {ultimo} ignorado (pode estar incompleto).")
        for linha in linhas:
            por_mes[linha["mes"]].append(linha)
    return dict(por_mes)


def referencias(meses: Iterable[MesNFCe]) -> dict[str, Item]:
    """Por código de produto, o item de maior venda nas NFC-e das outras lojas: tratamento e unidade de referência."""
    ref: dict[str, Item] = {}
    for m in meses:
        for it in m.itens.values():
            atual = ref.get(it.codigo)
            if atual is None or it.valor_operacao > atual.valor_operacao:
                ref[it.codigo] = it
    return ref


def consolidar(linhas: Iterable[dict], emitente: Participante, referencia: dict[str, Item],
               cupons: int = 0) -> tuple[MesNFCe, Decimal]:
    """O mês consolidado como as NFC-e o dariam. Devolve também o valor dos produtos sem referência nas outras lojas
    (tratados pelas alíquotas e pelo cadastro)."""
    mes = MesNFCe(emitente, cupons)
    sem_referencia = ZERO
    for l in linhas:
        codigo = str(int(_d(l["cod"])))
        ref = referencia.get(codigo)
        icms_cst = (l.get("cst") or "")[-2:]
        quantidade, desconto, valor = _d(l["qtde"]), _d(l["desconto"]), _d(l["valor"])
        if ref:
            descricao, ncm, pis_cst = ref.descricao, ref.ncm or (l.get("ncm") or "").replace(".", ""), ref.pis_cst
            gtin, unidade, unidade_gtin = ref.gtin, ref.unidade, ref.unidade_gtin
            fator = ref.quantidade_gtin / ref.quantidade if ref.quantidade and ref.quantidade_gtin else Decimal(1 if gtin else 0)
        else:
            sem_referencia += valor
            descricao, ncm = l.get("descricao") or codigo, (l.get("ncm") or "").replace(".", "")
            pis_cst = "01" if _d(l.get("aliqpis")) > 0 else "06"
            gtin, unidade = gtin_global(l.get("ean") or ""), (l.get("emb") or "UN").strip().upper()
            unidade_gtin, fator = (unidade, Decimal(1)) if gtin else ("", ZERO)
        mes.adicionar_item(Item(
            n_item=0, codigo=codigo, descricao=descricao, ncm=ncm, cfop="5405" if icms_cst == "60" else "5102",
            unidade=unidade, quantidade=quantidade, v_prod=valor + desconto, v_desc=desconto, icms_cst=icms_cst,
            v_icms=_d(l["icms"]), v_fcp=_d(l["fcp"]), v_pis=_d(l["pis"]), v_cofins=_d(l["cofins"]), pis_cst=pis_cst,
            gtin=gtin, unidade_gtin=unidade_gtin, quantidade_gtin=(quantidade * fator).quantize(Decimal("0.0001")),
            emitente_raiz=emitente.cnpj[:8]))
    return mes, sem_referencia
