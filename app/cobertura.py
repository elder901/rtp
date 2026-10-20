"""Cobertura dos dados: o que foi importado por estabelecimento e mês, e alertas de dados que parecem faltar ou sobrar.

Uma análise só vale se as notas estiverem completas. Estes alertas pegam os buracos mais comuns sem depender de fonte
externa: loja com compras e sem nenhuma venda (cupons de um PDV que não foram importados), mês vazio no meio do
período e notas isoladas, longe do restante (importação de teste, empresa errada).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models

ZERO = Decimal("0")
MODELO_NFCE = "65"
MESES_DE_DISTANCIA_ISOLADA = 4      # notas a 4+ meses de qualquer outro mês com notas são tratadas como isoladas
VENDAS_MINIMAS_SOBRE_COMPRAS = Decimal("0.2")   # comércio vende mais do que compra; abaixo de 20% faltam vendas


@dataclass
class Mes:
    documentos: int = 0                  # notas e cupons de qualquer tipo
    cupons: int = 0
    vendas: Decimal = ZERO               # NFC-e + NF-e de saída a terceiros
    nfe_saidas: int = 0
    compras: Decimal = ZERO              # NF-e de entrada de terceiros
    nfe_entradas: int = 0
    saidas_grupo: Decimal = ZERO         # transferências e vendas para estabelecimentos da mesma raiz
    entradas_grupo: Decimal = ZERO


@dataclass
class Cobertura:
    empresa_id: int
    nome: str
    meses: dict[str, Mes] = field(default_factory=dict)

    def no_periodo(self, inicio: date | None, fim: date | None) -> dict[str, Mes]:
        ini, fim_ = (inicio.strftime("%Y-%m") if inicio else "0000-00"), (fim.strftime("%Y-%m") if fim else "9999-99")
        return {m: v for m, v in self.meses.items() if ini <= m <= fim_}


def _mes_seguinte(m: str, n: int = 1) -> str:
    ano, mes = int(m[:4]), int(m[5:]) - 1 + n
    return f"{ano + mes // 12:04d}-{mes % 12 + 1:02d}"


def _distancia(a: str, b: str) -> int:
    return abs((int(a[:4]) * 12 + int(a[5:])) - (int(b[:4]) * 12 + int(b[5:])))


def _faixa(meses: list[str]) -> str:
    """Meses em ordem, com as sequências contínuas resumidas: '01/2026 a 03/2026, 05/2026'."""
    fmt = lambda m: f"{m[5:]}/{m[:4]}"     # noqa: E731
    blocos: list[list[str]] = []
    for m in meses:
        if blocos and _distancia(blocos[-1][-1], m) == 1:
            blocos[-1].append(m)
        else:
            blocos.append([m])
    return ", ".join(fmt(b[0]) if len(b) == 1 else f"{fmt(b[0])} a {fmt(b[-1])}" for b in blocos)


def _reais(v: Decimal) -> str:
    return f"R$ {v / 1_000_000:.1f} mi".replace(".", ",") if v >= 100_000 else f"R$ {v:,.0f}".replace(",", ".")


def levantar(s: Session, empresas: list[models.Empresa]) -> list[Cobertura]:
    """Notas e cupons por estabelecimento e mês (só metadados: não lê os XMLs)."""
    D, C = models.DocumentoFiscal, models.ConsolidadoNFCe
    saida = []
    for e in empresas:
        raiz = e.cnpj[:8]
        meses: dict[str, Mes] = defaultdict(Mes)
        for emissao, chave, direcao, participante, valor in s.execute(
                select(D.emissao, D.chave, D.direcao, D.participante_cnpj, D.valor)
                .where(D.empresa_id == e.id, D.situacao == "completo")):
            if not emissao:
                continue
            m, valor = meses[emissao.strftime("%Y-%m")], valor or ZERO
            m.documentos += 1
            do_grupo = (participante or "")[:8] == raiz
            if direcao == "saida":
                if do_grupo:
                    m.saidas_grupo += valor
                elif chave[20:22] == MODELO_NFCE:
                    m.cupons += 1
                    m.vendas += valor
                else:
                    m.nfe_saidas += 1
                    m.vendas += valor
            elif do_grupo:
                m.entradas_grupo += valor
            else:
                m.nfe_entradas += 1
                m.compras += valor
        for competencia, cupons, valor in s.execute(select(C.competencia, C.cupons, C.valor).where(C.empresa_id == e.id)):
            meses[competencia].documentos += cupons
            meses[competencia].cupons += cupons
            meses[competencia].vendas += valor or ZERO
        saida.append(Cobertura(e.id, e.nome.strip(), dict(sorted(meses.items()))))
    return saida


def periodo_padrao(coberturas: list[Cobertura], meses: int = 12) -> tuple[date | None, date | None]:
    """Os `meses` até o último mês com notas, somando todos os estabelecimentos."""
    todos = sorted({m for c in coberturas for m in c.meses})
    if not todos:
        return None, None
    primeiro, seguinte = _mes_seguinte(todos[-1], -(meses - 1)), _mes_seguinte(todos[-1])
    return (date(int(primeiro[:4]), int(primeiro[5:]), 1),
            date(int(seguinte[:4]), int(seguinte[5:]), 1) - timedelta(days=1))


def alertas(coberturas: list[Cobertura], inicio: date | None = None, fim: date | None = None) -> list[str]:
    saida = []
    for c in coberturas:
        # Notas isoladas: blocos de meses separados do bloco principal (o de mais notas) por 4+ meses sem nada.
        # Olha o histórico todo, inclusive fora do período analisado.
        blocos: list[list[str]] = []
        for m in c.meses:
            if blocos and _distancia(blocos[-1][-1], m) < MESES_DE_DISTANCIA_ISOLADA:
                blocos[-1].append(m)
            else:
                blocos.append([m])
        if len(blocos) > 1:
            principal = max(blocos, key=lambda b: sum(c.meses[m].documentos for m in b))
            resto = principal
            isolados = [m for b in blocos if b is not principal for m in b]
            n = sum(c.meses[m].documentos for m in isolados)
            saida.append(f"{c.nome}: {n} nota(s) em {_faixa(isolados)}, isoladas do restante "
                         f"({_faixa(resto)}). Confira se são desta empresa ou sobra de uma importação de teste.")
        periodo = c.no_periodo(inicio, fim)
        if not periodo:
            continue
        sem_venda = [m for m, v in periodo.items() if v.compras > 0 and v.vendas < v.compras * VENDAS_MINIMAS_SOBRE_COMPRAS
                     and v.saidas_grupo < v.compras / 2]
        if sem_venda:
            compras = sum((periodo[m].compras for m in sem_venda), ZERO)
            vendas = sum((periodo[m].vendas for m in sem_venda), ZERO)
            saida.append(f"{c.nome}: em {len(sem_venda)} mês(es) ({_faixa(sem_venda)}) as vendas importadas "
                         f"({_reais(vendas) if vendas else 'nenhuma'}) ficam muito abaixo das compras de terceiros "
                         f"({_reais(compras)}). Faltam os cupons (NFC-e) ou as notas de saída deste estabelecimento? "
                         f"Sem eles a análise subestima as vendas.")
        so_grupo = [m for m, v in periodo.items() if not v.vendas and v.saidas_grupo >= max(v.compras / 2, Decimal("0.01"))]
        if so_grupo and len(so_grupo) == len(periodo):
            saida.append(f"{c.nome}: no período só emitiu notas para o próprio grupo — indústria ou centro de "
                         f"distribuição? Na visão do grupo essas notas são transferências internas.")
        meses_periodo = list(periodo)
        cursor, vazios = meses_periodo[0], []
        while cursor < meses_periodo[-1]:
            if cursor not in periodo:
                vazios.append(cursor)
            cursor = _mes_seguinte(cursor)
        if vazios:
            saida.append(f"{c.nome}: nenhuma nota em {', '.join(_faixa([m]) for m in vazios)}, no meio do período "
                         f"importado. Falta importar esse(s) mês(es)?")
    return saida
