"""Linha de comando: analisa uma pasta/ZIP de XMLs e gera o Excel.

Exemplo:
  python -m app.cli --cnpj 11222333000181 --regime real --uf SP --municipio 3550308 \
      --xml C:/xmls/cliente --saida relatorios/cliente.xlsx
"""
from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path

from app.calculadora.client import CalculadoraRTC
from app.engine.cenarios import Empresa, analisar
from app.engine.premissas import TRANSICAO, Premissas
from app.ingest.arquivos import ler_caminho, ler_classificacoes
from app.relatorios import agregacao
from app.relatorios.excel import exportar


def main(argv: list[str] | None = None):
    p = argparse.ArgumentParser(description="Impacto da reforma tributária a partir de XMLs de NF-e")
    p.add_argument("--cnpj", required=True)
    p.add_argument("--nome", default="")
    p.add_argument("--regime", required=True, choices=["real", "presumido", "simples"])
    p.add_argument("--uf", required=True)
    p.add_argument("--municipio", required=True, type=int, help="código IBGE")
    p.add_argument("--xml", required=True, type=Path, help="pasta ou .zip com os XMLs")
    p.add_argument("--classificacao", type=Path, help="CSV codigo_ou_ncm;cst;cclasstrib")
    p.add_argument("--saida", type=Path, default=Path("relatorios/analise.xlsx"))
    p.add_argument("--anos", default="2027,2029,2033")
    p.add_argument("--industria", action="store_true")
    p.add_argument("--cbs", type=Decimal, help="CBS de referência (%%)")
    p.add_argument("--ibs-uf", type=Decimal)
    p.add_argument("--ibs-mun", type=Decimal)
    p.add_argument("--credito-simples", type=Decimal, help="crédito de fornecedor do Simples (%% do valor)")
    p.add_argument("--das", type=Decimal, help="alíquota efetiva do DAS (empresa do Simples)")
    p.add_argument("--calculadora-url")
    a = p.parse_args(argv)

    anos = [int(x) for x in a.anos.split(",")]
    invalidos = [x for x in anos if x not in TRANSICAO]
    if invalidos:
        p.error(f"anos fora da transição simulável (2027-2033): {invalidos}")

    premissas = Premissas(industria=a.industria, anos=anos)
    for campo, valor in [("cbs_referencia", a.cbs), ("ibs_uf_referencia", a.ibs_uf),
                         ("ibs_mun_referencia", a.ibs_mun), ("credito_fornecedor_simples_pct", a.credito_simples),
                         ("aliquota_das_pct", a.das)]:
        if valor is not None:
            setattr(premissas, campo, valor)

    leitura = ler_caminho(a.xml)
    print(f"XMLs lidos: {len(leitura.documentos)} | duplicados: {leitura.duplicados} | rejeitados: {len(leitura.rejeitados)}")
    for r in leitura.rejeitados[:10]:
        print("  -", r)

    overrides = ler_classificacoes(a.classificacao.read_text(encoding="utf-8-sig")) if a.classificacao else {}
    empresa = Empresa("".join(c for c in a.cnpj if c.isdigit()), a.nome or a.cnpj, a.regime, a.uf.upper(), a.municipio)
    analise = analisar(empresa, leitura.documentos, premissas, CalculadoraRTC(a.calculadora_url), overrides)

    print("\nResumo (saldo a recolher = débitos nas saídas - créditos nas entradas):")
    for l in agregacao.resumo(analise):
        var = f"  ({l['diferenca_vs_atual']:+,.2f} vs atual)" if l["diferenca_vs_atual"] is not None else ""
        print(f"  {l['cenario']:>6}: débitos {l['debitos']:>14,.2f}  créditos {l['creditos']:>14,.2f}  "
              f"saldo {l['saldo_a_recolher']:>14,.2f}{var}")
    for t in analise.alertas:
        print("  ! " + t)
    print(f"\nRelatório: {exportar(analise, a.saida).resolve()}")


if __name__ == "__main__":
    main()
