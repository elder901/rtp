"""Lista para a revisão fiscal da classificação: produtos vendidos do maior para o menor valor, com o % acumulado
das vendas, a classificação usada e de onde ela veio. "Confirmar" marca a presumida (anexo pelo NCM ou tributação
integral por falta de informação): é onde o resultado de 2033 mais pode mudar."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import BinaryIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app.engine.cenarios import Analise

ORIGENS = {"manual": "ajuste manual", "fornecedor": "declarada pelo fornecedor", "xml": "XML do item",
           "anexo": "presumida pelo NCM (anexo)", "padrao": "sem classificação (integral)"}
PRESUMIDAS = {"anexo", "padrao"}


def linhas(a: Analise, cobertura: Decimal = Decimal("95")) -> list[dict]:
    """Produtos até `cobertura` % das vendas, do maior para o menor."""
    vendas: dict[str, Decimal] = defaultdict(Decimal)
    primeiro: dict[str, tuple] = {}
    codigos: dict[str, set] = defaultdict(set)
    for r in a.itens:
        if r.direcao != "saida":
            continue
        k = r.item.chave
        vendas[k] += r.item.valor_liquido
        primeiro.setdefault(k, (r.item, r.classificacao))
        codigos[k].add(r.item.codigo)
    total = sum(vendas.values(), Decimal(0)) or Decimal(1)
    saida, acumulado = [], Decimal(0)
    for posicao, (k, valor) in enumerate(sorted(vendas.items(), key=lambda kv: -kv[1]), start=1):
        if acumulado >= cobertura:
            break
        acumulado += valor / total * 100
        item, c = primeiro[k]
        saida.append({
            "posicao": posicao, "confirmar": "sim" if c.origem in PRESUMIDAS else "", "ean": item.gtin,
            "codigos": ", ".join(sorted(codigos[k])), "descricao": item.descricao, "ncm": item.ncm,
            "cst": c.cst, "cclasstrib": c.cclasstrib, "origem": ORIGENS.get(c.origem, c.origem),
            "vendas": valor.quantize(Decimal("0.01")), "pct_vendas": (valor / total * 100).quantize(Decimal("0.01")),
            "pct_acumulado": acumulado.quantize(Decimal("0.01")),
        })
    return saida


def exportar(a: Analise, destino: Path | BinaryIO, titulo: str) -> int:
    """Grava a planilha e devolve quantos produtos precisam de confirmação."""
    dados = linhas(a)
    wb = Workbook()
    ws = wb.active
    ws.title = "Revisão"
    a_confirmar = sum(1 for d in dados if d["confirmar"])
    inicio, fim = a.periodo
    ws.append([titulo])
    ws.append([f"Vendas de {inicio:%d/%m/%Y} a {fim:%d/%m/%Y}" if inicio and fim else "Todas as vendas importadas",
               "", f"{len(dados)} produtos até 95% das vendas; {a_confirmar} com classificação presumida (coluna Confirmar)."])
    ws.append(["Como usar: confirme ou corrija o CST e o cClassTrib das linhas marcadas em Confirmar, começando pelo "
               "topo, e grave na aba Classificação do estabelecimento matriz (vale para a visão da empresa)."])
    ws["A1"].font = Font(bold=True, size=13)
    colunas = [("Posição", "posicao", 8), ("Confirmar", "confirmar", 10), ("EAN", "ean", 15), ("Código(s)", "codigos", 14),
               ("Descrição", "descricao", 42), ("NCM", "ncm", 10), ("CST", "cst", 6), ("cClassTrib", "cclasstrib", 11),
               ("Origem da classificação", "origem", 28), ("Vendas no período (R$)", "vendas", 16),
               ("% das vendas", "pct_vendas", 11), ("% acumulado", "pct_acumulado", 11)]
    ws.append([c[0] for c in colunas])
    cab = ws.max_row
    for cel in ws[cab]:
        cel.fill, cel.font = PatternFill("solid", fgColor="1F3864"), Font(color="FFFFFF", bold=True)
        cel.alignment = Alignment(wrap_text=True, vertical="center")
    destaque = PatternFill("solid", fgColor="FFF4CE")
    for d in dados:
        ws.append([float(d[k]) if isinstance(d[k], Decimal) else d[k] for _, k, _ in colunas])
        if d["confirmar"]:
            for cel in ws[ws.max_row]:
                cel.fill = destaque
    for i, (_, k, largura) in enumerate(colunas, start=1):
        letra = get_column_letter(i)
        ws.column_dimensions[letra].width = largura
        if k in ("vendas", "pct_vendas", "pct_acumulado"):
            for cel in ws[letra][cab:]:
                cel.number_format = "#,##0.00"
    ws.freeze_panes = ws.cell(row=cab + 1, column=1)
    ws.auto_filter.ref = f"A{cab}:{get_column_letter(len(colunas))}{ws.max_row}"
    wb.save(destino)
    return a_confirmar
