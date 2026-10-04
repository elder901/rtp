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


def exportar(a: Analise, destino: Path | BinaryIO) -> Path | BinaryIO:
    wb = Workbook()
    wb.remove(wb.active)
    _aba(wb, "Resumo", agregacao.resumo(a))
    _aba(wb, "Por produto (saídas)", agregacao.por_produto(a))
    _aba(wb, "Por fornecedor (entradas)", agregacao.por_fornecedor(a))
    _aba(wb, "Classificação", agregacao.classificacao(a))
    _aba(wb, "Itens", agregacao.itens(a))
    _aba(wb, "Premissas", agregacao.premissas(a))
    _aba(wb, "Alertas", [{"alerta": t} for t in a.alertas] or [{"alerta": "Nenhum"}])
    if isinstance(destino, Path):
        destino.parent.mkdir(parents=True, exist_ok=True)
    wb.save(destino)
    return destino
