"""Geometria do gráfico de barras da linha do tempo (saldo a recolher por cenário), renderizado em SVG no template."""
from __future__ import annotations

import math
from decimal import Decimal

LARGURA, ALTURA = 760, 280
ESQ, DIR, TOPO, BASE = 84, 16, 28, 34
RAIO = 4


def _passo(amplitude: float) -> float:
    bruto = amplitude / 4 or 1
    mag = 10 ** math.floor(math.log10(bruto))
    return next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= bruto)


def _barra(x: float, largura: float, y0: float, y1: float) -> str:
    """Caminho com cantos arredondados só na ponta do dado (não na linha de base)."""
    altura = abs(y1 - y0)
    r = min(RAIO, altura, largura / 2)
    if y1 <= y0:  # positiva: ponta em cima
        return (f"M{x:.1f},{y0:.1f} V{y1 + r:.1f} Q{x:.1f},{y1:.1f} {x + r:.1f},{y1:.1f} "
                f"H{x + largura - r:.1f} Q{x + largura:.1f},{y1:.1f} {x + largura:.1f},{y1 + r:.1f} V{y0:.1f} Z")
    return (f"M{x:.1f},{y0:.1f} V{y1 - r:.1f} Q{x:.1f},{y1:.1f} {x + r:.1f},{y1:.1f} "
            f"H{x + largura - r:.1f} Q{x + largura:.1f},{y1:.1f} {x + largura:.1f},{y1 - r:.1f} V{y0:.1f} Z")


def linha_do_tempo(resumo: list[dict]) -> dict:
    valores = [float(l["saldo_a_recolher"]) for l in resumo]
    vmin, vmax = min(0.0, *valores), max(0.0, *valores)
    passo = _passo(vmax - vmin)
    menor = vmin
    vmin, vmax = math.floor(vmin / passo) * passo, math.ceil(vmax / passo) * passo
    if menor < 0 and menor - vmin < passo * 0.4:  # espaço para o rótulo abaixo da barra negativa
        vmin -= passo
    if vmin == vmax:
        vmax = vmin + passo
    altura_util = ALTURA - TOPO - BASE

    def y(v: float) -> float:
        return TOPO + (vmax - v) / (vmax - vmin) * altura_util

    banda = (LARGURA - ESQ - DIR) / len(valores)
    larg = min(48.0, banda * 0.6)
    zero = y(0)
    barras = []
    for i, (l, v) in enumerate(zip(resumo, valores)):
        x = ESQ + i * banda + (banda - larg) / 2
        barras.append({
            "rotulo": l["cenario"], "valor": l["saldo_a_recolher"], "diferenca": l["diferenca_vs_atual"],
            "x": x, "largura": larg, "centro": x + larg / 2, "caminho": _barra(x, larg, zero, y(v)),
            "y_rotulo": y(v) - 8 if v >= 0 else y(v) + 16, "atual": i == 0,
            "alvo_x": ESQ + i * banda, "alvo_largura": banda,
            # rótulos diretos só no atual e no último ano (seletivos)
            "mostrar_valor": i in (0, len(valores) - 1),
        })
    n = round((vmax - vmin) / passo)
    marcas = [{"valor": Decimal(str(round(vmin + k * passo, 2))), "y": y(vmin + k * passo)} for k in range(n + 1)]
    return {"largura": LARGURA, "altura": ALTURA, "esq": ESQ, "dir": LARGURA - DIR, "base": ALTURA - BASE + 18,
            "zero": zero, "referencia": y(valores[0]), "barras": barras, "marcas": marcas}
