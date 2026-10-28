"""Exportação da análise para Excel."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import BinaryIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app.engine.cenarios import Analise
from app.relatorios import agregacao

CABECALHO = PatternFill("solid", fgColor="1F3864")
FONTE_CAB = Font(color="FFFFFF", bold=True)


def _aba(wb: Workbook, titulo: str, linhas: list[dict]):
    ws = wb.create_sheet(titulo)
    if not linhas:
        ws.append(["Sem dados"])
        return
    colunas = list(linhas[0].keys())
    ws.append([c.replace("_", " ") for c in colunas])
    for cel in ws[1]:
        cel.fill, cel.font = CABECALHO, FONTE_CAB
        cel.alignment = Alignment(wrap_text=True, vertical="center")
    for l in linhas:
        ws.append([float(v) if isinstance(v, Decimal) else v for v in (l.get(c) for c in colunas)])
    for idx, c in enumerate(colunas, start=1):
        letra = get_column_letter(idx)
        largura = max(len(c), *(len(str(l.get(c) or "")) for l in linhas[:200]))
        ws.column_dimensions[letra].width = min(max(10, largura + 2), 60)
        fmt = "0.00" if c.endswith("_pct") else "#,##0.00"
        if any(isinstance(l.get(c), Decimal) for l in linhas[:50]):
            for cel in ws[letra][1:]:
                cel.number_format = fmt
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = ws.dimensions


def exportar(a: Analise, destino: Path | BinaryIO, extras: dict[str, list[dict]] | None = None) -> Path | BinaryIO:
    wb = Workbook()
    wb.remove(wb.active)
    _aba(wb, "Resumo", agregacao.resumo(a))
    _aba(wb, "Por produto (saídas)", agregacao.por_produto(a))
    _aba(wb, "Por fornecedor (entradas)", agregacao.por_fornecedor(a))
    _aba(wb, "Classificação", agregacao.classificacao(a))
    _aba(wb, "Itens", agregacao.itens(a))
    _aba(wb, "Premissas", agregacao.premissas(a))
    for titulo, linhas in (extras or {}).items():
        _aba(wb, titulo, linhas)
    _aba(wb, "Alertas", [{"alerta": t} for t in a.alertas] or [{"alerta": "Nenhum"}])
    if isinstance(destino, Path):
        destino.parent.mkdir(parents=True, exist_ok=True)
    wb.save(destino)
    return destino


def exportar_negociacao(compras, ano: int, destino: Path | BinaryIO) -> Path | BinaryIO:
    """Planilha para negociação: resumo por fornecedor e compras por fornecedor x produto, com preço de nota de
    equilíbrio e o melhor fornecedor do mesmo produto a preços de hoje."""
    from app.relatorios import negociacao

    wb = Workbook()
    wb.remove(wb.active)
    _aba(wb, "Fornecedores", [{
        "cnpj": f.cnpj, "fornecedor": f.nome, "regime": f.regime, "uf": f.uf, "produtos": f.produtos,
        "notas": f.notas, "compras_preco_de_nota": f.preco_nota_atual, "custo_efetivo_atual": f.custo_atual,
        f"custo_{ano}_a_precos_de_hoje": f.custo_precos_hoje, "variacao_custo": f.variacao_custo,
        "variacao_custo_pct": f.variacao_custo_pct, f"custo_{ano}_com_repasse": f.custo_ano,
        f"preco_de_nota_equilibrio_{ano}": f.preco_nota_equilibrio,
        "variacao_preco_de_nota_equilibrio_pct": f.variacao_preco_nota_equilibrio_pct,
        "economia_trocando_fornecedor": f.economia_trocando,
    } for f in negociacao.fornecedores(compras)])
    _aba(wb, "Fornecedor x produto", [{
        "cnpj": c.fornecedor, "fornecedor": c.fornecedor_nome, "regime": c.regime, "ean": c.gtin,
        "codigos": ", ".join(c.codigos), "descricao": c.descricao, "ncm": c.ncm, "cclasstrib": c.cclasstrib,
        "unidade": c.unidade, "quantidade": c.quantidade, "preco_de_nota_unit": c.preco_nota_unit,
        "custo_unit_atual": c.custo_unit_atual, f"custo_unit_{ano}_a_precos_de_hoje": c.custo_unit_precos_hoje,
        "variacao_custo_pct": c.variacao_custo_precos_hoje_pct, f"custo_unit_{ano}_com_repasse": c.custo_unit_ano,
        f"preco_de_nota_equilibrio_unit_{ano}": c.preco_nota_equilibrio_unit,
        "variacao_preco_de_nota_equilibrio_pct": c.variacao_preco_nota_equilibrio_pct,
        "equilibrio_sobre_valor_liquido_pct": c.preco_equilibrio_pct,
        f"imposto_seletivo_{ano}": c.imposto_seletivo_ano, "tratamento_is": c.tratamento_is,
        "melhor_fornecedor_precos_de_hoje": c.melhor.fornecedor_nome if c.melhor and c.melhor is not c else "",
        f"melhor_custo_unit_{ano}": c.melhor.custo_unit_precos_hoje if c.melhor and c.melhor is not c else None,
        "economia_trocando": c.economia_trocando,
    } for c in sorted(compras, key=lambda c: (c.fornecedor_nome, c.descricao))])
    if isinstance(destino, Path):
        destino.parent.mkdir(parents=True, exist_ok=True)
    wb.save(destino)
    return destino


def exportar_pauta(f, compras, metas, ano: int, fator_ano: Decimal, meses: int, periodo,
                   destino: Path | BinaryIO) -> Path | BinaryIO:
    """Pauta de negociação de um fornecedor: o que pedir no ano, o argumento (tributo destacado hoje que sai do preço),
    as metas de cada ano da transição e o preço de nota alvo de cada produto. "Por ano" = período x 12 / meses."""
    inicio, fim = periodo
    janela = f"{inicio:%d/%m/%Y} a {fim:%d/%m/%Y}" if inicio and fim else "todas as notas"
    wb = Workbook()
    wb.remove(wb.active)
    _aba(wb, "Pauta", [{"item": k, "valor": v} for k, v in (
        ("Fornecedor", f.nome), ("CNPJ", f.cnpj), ("Regime", f.regime), ("UF", f.uf),
        ("Período analisado", f"{janela} — {meses} mês(es) com compras; valores por ano = período x 12 / {meses}"),
        ("Compras por ano, a preço de nota (R$)", f.preco_nota_atual * fator_ano),
        (f"Meta de preço de nota em {ano} (%) — negativo é desconto a pedir", f.variacao_preco_nota_equilibrio_pct),
        (f"Em jogo por ano em {ano} se ele mantiver os preços (R$)", f.variacao_custo * fator_ano),
        ("ICMS próprio no preço de nota de hoje (%)", f.parte(f.icms)),
        ("ICMS-ST no preço de nota de hoje (%)", f.parte(f.icms_st)),
        ("IPI no preço de nota de hoje (%)", f.parte(f.ipi)),
        ("PIS/COFINS no preço de nota de hoje (%)", f.parte(f.pis_cofins)),
        (f"Tributo destacado hoje que sai do preço em {ano} (%)", f.parte(f.sai_do_preco(ano))),
        (f"CBS/IBS na compra em {ano} (%) — crédito integral, não é custo", f.parte(f.cbs_ibs_ano)),
        (f"Imposto Seletivo em {ano} (%) — sem crédito, é custo", f.parte(f.imposto_seletivo_ano)),
        ("Nosso crédito hoje (%)", f.parte(f.creditos_atual)),
        (f"Nosso crédito em {ano} (%)", f.parte(f.creditos_ano)),
        ("Volume com classificação presumida pelo NCM ou sem classificação (%)", f.presumido_pct),
        ("Volume com tributo pago antes na cadeia, fora da nota (%) — meta é o mínimo", f.imposto_anterior_pct),
    )])
    _aba(wb, "Metas por ano", [{
        "ano": m.ano, "sai_do_preco_de_nota_pct": m.sai_do_preco_pct, "meta_preco_de_nota_pct": m.preco_nota_equilibrio_pct,
        "em_jogo_por_ano_se_mantiver_precos": m.variacao_custo_por_ano,
    } for m in metas])
    _aba(wb, "Produtos", [{
        "ean": c.gtin, "codigos": ", ".join(c.codigos), "descricao": c.descricao, "ncm": c.ncm,
        "cclasstrib": c.cclasstrib, "unidade": c.unidade, "quantidade_por_ano": c.quantidade * fator_ano,
        "compras_por_ano": c.preco_nota_atual * fator_ano, "preco_de_nota_unit_hoje": c.preco_nota_unit,
        f"meta_preco_de_nota_unit_{ano}": c.preco_nota_equilibrio_unit,
        "variacao_pct": c.variacao_preco_nota_equilibrio_pct, f"imposto_seletivo_{ano}": c.imposto_seletivo_ano,
        "melhor_fornecedor_precos_de_hoje": c.melhor.fornecedor_nome if c.melhor and c.melhor is not c else "",
        f"custo_unit_dele_{ano}": c.melhor.custo_unit_precos_hoje if c.melhor and c.melhor is not c else None,
    } for c in compras])
    if isinstance(destino, Path):
        destino.parent.mkdir(parents=True, exist_ok=True)
    wb.save(destino)
    return destino
