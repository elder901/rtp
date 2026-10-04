"""Agregações da análise: resumo da empresa, por produto (saídas), por fornecedor (entradas) e classificação."""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from app.engine.cenarios import Analise, ResultadoItem

ZERO = Decimal("0")


def _pct(parte: Decimal, todo: Decimal) -> Decimal | None:
    return (parte / todo * 100).quantize(Decimal("0.01")) if todo else None


def _var(novo: Decimal, antigo: Decimal) -> Decimal | None:
    return ((novo - antigo) / antigo * 100).quantize(Decimal("0.01")) if antigo else None


def resumo(a: Analise) -> list[dict]:
    """Carga líquida da empresa: débitos nas saídas menos créditos nas entradas, por cenário."""
    linhas = []
    cenarios = [("Atual", None), *[(str(ano), ano) for ano in a.premissas.anos]]
    base_saldo = None
    for nome, ano in cenarios:
        deb = cred = vendas = compras = ZERO
        for r in a.itens:
            v = r.atual if ano is None else r.anos[ano]
            if r.direcao == "saida":
                deb += v.tributos
                vendas += r.item.valor_liquido
            else:
                cred += v.creditos
                compras += r.item.valor_liquido
        saldo = deb - cred
        base_saldo = saldo if base_saldo is None else base_saldo
        linhas.append({
            "cenario": nome, "vendas_liquidas": vendas, "compras_liquidas": compras,
            "debitos": deb, "creditos": cred, "saldo_a_recolher": saldo,
            "carga_sobre_vendas_pct": _pct(saldo, vendas),
            "diferenca_vs_atual": saldo - base_saldo if ano else None,
            # % só faz sentido com saldo atual positivo (com crédito acumulado o sinal se inverte)
            "variacao_vs_atual_pct": _var(saldo, base_saldo) if ano and base_saldo > 0 else None,
        })
    return linhas


def _custos_por_gtin(a: Analise) -> dict[tuple[str, str], dict]:
    """Custo efetivo unitário de compra (após créditos) por (EAN, unidade), no cenário atual e em cada ano.
    Usa a unidade e a quantidade do EAN escolhido (a tributável, quando o XML traz cEANTrib)."""
    grupos: dict[tuple[str, str], list[ResultadoItem]] = defaultdict(list)
    for r in a.itens:
        if r.direcao == "entrada" and r.item.gtin and r.item.quantidade_gtin:
            grupos[(r.item.gtin, r.item.unidade_gtin.upper())].append(r)
    custos = {}
    for k, rs in grupos.items():
        qtd = sum((r.item.quantidade_gtin for r in rs), ZERO)
        c = {"atual": sum((r.atual.custo_ou_receita(r.item.valor_liquido) for r in rs), ZERO) / qtd}
        for ano in a.premissas.anos:
            c[ano] = sum((r.anos[ano].custo_ou_receita(r.item.valor_liquido) for r in rs), ZERO) / qtd
        custos[k] = {kk: v.quantize(Decimal("0.0001")) for kk, v in c.items()}
    return custos


def por_produto(a: Analise) -> list[dict]:
    grupos: dict[str, list[ResultadoItem]] = defaultdict(list)
    for r in a.itens:
        if r.direcao == "saida":
            grupos[r.item.chave].append(r)   # EAN; sem EAN global, o código
    custos = _custos_por_gtin(a)
    linhas = []
    for chave, rs in grupos.items():
        i0 = rs[0]
        vl = sum((r.item.valor_liquido for r in rs), ZERO)
        trib_atual = sum((r.atual.tributos for r in rs), ZERO)
        qtd = sum((r.item.quantidade_gtin for r in rs), ZERO)
        custo = custos.get((i0.item.gtin, i0.item.unidade_gtin.upper())) if i0.item.gtin else None
        linha = {
            "chave": chave, "codigo": i0.item.codigo, "descricao": i0.item.descricao, "ncm": i0.item.ncm,
            "gtin": i0.item.gtin,
            "cst": i0.classificacao.cst, "cclasstrib": i0.classificacao.cclasstrib,
            "origem_classificacao": i0.classificacao.origem,
            "quantidade": sum((r.item.quantidade for r in rs), ZERO),
            "valor_liquido": vl, "tributos_atual": trib_atual, "carga_atual_pct": _pct(trib_atual, vl),
        }
        for ano in a.premissas.anos:
            t = sum((r.anos[ano].tributos for r in rs), ZERO)
            linha[f"tributos_{ano}"] = t
            linha[f"carga_{ano}_pct"] = _pct(t, vl)
            linha[f"var_preco_{ano}_pct"] = _var(vl + t, vl + trib_atual)
        # Preço neutro com margem: mantém a margem unitária em R$ (valor líquido - custo efetivo de compra),
        # repassando a mudança do custo após créditos e a nova carga sobre a venda.
        linha["custo_unit_atual"] = custo["atual"] if custo else None
        linha["margem_unit"] = (vl / qtd - custo["atual"]).quantize(Decimal("0.01")) if custo and qtd else None
        for ano in a.premissas.anos:
            if custo and qtd and vl:
                carga_atual, carga = trib_atual / vl, linha[f"tributos_{ano}"] / vl
                preco_atual = vl / qtd * (1 + carga_atual)
                neutro = (custo[ano] + linha["margem_unit"]) * (1 + carga)
                linha[f"preco_neutro_{ano}_pct"] = _var(neutro, preco_atual)
            else:
                linha[f"preco_neutro_{ano}_pct"] = None
        linhas.append(linha)
    ultimo = a.premissas.anos[-1]
    return sorted(linhas, key=lambda l: -(l[f"tributos_{ultimo}"] - l["tributos_atual"]))


def por_fornecedor(a: Analise) -> list[dict]:
    grupos: dict[str, list[ResultadoItem]] = defaultdict(list)
    for r in a.itens:
        if r.direcao == "entrada":
            grupos[r.contraparte.cnpj].append(r)
    linhas = []
    for cnpj, rs in grupos.items():
        f = rs[0].contraparte
        vl = sum((r.item.valor_liquido for r in rs), ZERO)
        custo_atual = sum((r.atual.custo_ou_receita(r.item.valor_liquido) for r in rs), ZERO)
        linha = {
            "cnpj": cnpj, "nome": f.nome, "regime": f.regime, "uf": f.uf,
            "notas": len({r.documento.chave for r in rs}), "valor_liquido": vl,
            "preco_atual": sum((r.atual.preco(r.item.valor_liquido) for r in rs), ZERO),
            "creditos_atual": sum((r.atual.creditos for r in rs), ZERO),
            "custo_efetivo_atual": custo_atual,
        }
        for ano in a.premissas.anos:
            custo = sum((r.anos[ano].custo_ou_receita(r.item.valor_liquido) for r in rs), ZERO)
            linha[f"creditos_{ano}"] = sum((r.anos[ano].creditos for r in rs), ZERO)
            linha[f"custo_efetivo_{ano}"] = custo
            linha[f"var_custo_{ano}_pct"] = _var(custo, custo_atual)
        linhas.append(linha)
    ultimo = a.premissas.anos[-1]
    return sorted(linhas, key=lambda l: -(l[f"custo_efetivo_{ultimo}"] - l["custo_efetivo_atual"]))


def classificacao(a: Analise) -> list[dict]:
    """Produtos distintos com a classificação usada — serve de base para revisão e para o CSV de ajustes."""
    vistos: dict[tuple, dict] = {}
    ultimo = a.premissas.anos[-1]
    for r in a.itens:
        k = (r.direcao, r.item.chave)
        if k in vistos:
            continue
        aliq = r.aliquotas.get(ultimo)
        vistos[k] = {
            "direcao": r.direcao, "chave": r.item.chave, "ean": r.item.gtin, "codigo": r.item.codigo,
            "descricao": r.item.descricao, "ncm": r.item.ncm,
            "cst": r.classificacao.cst, "cclasstrib": r.classificacao.cclasstrib,
            "origem": r.classificacao.origem,
            "reducao_pct": aliq.reducao_pct if aliq else None,
            f"aliquota_efetiva_{ultimo}_pct": aliq.total if aliq else None,
            "erro_calculadora": aliq.erro if aliq else "",
        }
    return sorted(vistos.values(), key=lambda l: (l["origem"] != "padrao", l["direcao"], l["chave"]))


def itens(a: Analise) -> list[dict]:
    linhas = []
    for r in a.itens:
        l = {
            "direcao": r.direcao, "data": r.documento.emissao.date(), "nf": r.documento.numero,
            "chave": r.documento.chave, "participante": r.contraparte.nome, "ean": r.item.gtin, "cnpj_participante": r.contraparte.cnpj,
            "regime_participante": r.contraparte.regime, "item": r.item.n_item, "codigo": r.item.codigo,
            "descricao": r.item.descricao, "ncm": r.item.ncm, "cfop": r.item.cfop,
            "cst": r.classificacao.cst, "cclasstrib": r.classificacao.cclasstrib,
            "valor_operacao": r.item.valor_operacao, "valor_liquido": r.item.valor_liquido,
            "icms": r.item.v_icms + r.item.v_fcp, "icms_st": r.item.v_icms_st + r.item.v_fcp_st,
            "ipi": r.item.v_ipi, "pis": r.item.v_pis, "cofins": r.item.v_cofins,
            "tributos_atual": r.atual.tributos, "creditos_atual": r.atual.creditos,
        }
        for ano, v in r.anos.items():
            l[f"cbs_{ano}"] = v.cbs
            l[f"ibs_{ano}"] = v.ibs
            l[f"tributos_{ano}"] = v.tributos
            l[f"creditos_{ano}"] = v.creditos
        linhas.append(l)
    return linhas


def premissas(a: Analise) -> list[dict]:
    p = a.premissas
    linhas = [
        {"premissa": "Empresa", "valor": f"{a.empresa.nome} ({a.empresa.cnpj})"},
        {"premissa": "Regime atual", "valor": a.empresa.regime},
        {"premissa": "UF / município", "valor": f"{a.empresa.uf} / {a.empresa.cod_municipio}"},
        {"premissa": "CBS referência (%)", "valor": p.cbs_referencia},
        {"premissa": "IBS UF referência (%)", "valor": p.ibs_uf_referencia},
        {"premissa": "IBS município referência (%)", "valor": p.ibs_mun_referencia},
        {"premissa": "Crédito de fornecedor do Simples (% do valor)", "valor": p.credito_fornecedor_simples_pct},
        {"premissa": "Crédito de fornecedor MEI (% do valor)", "valor": p.credito_fornecedor_mei_pct},
        {"premissa": "Indústria (credita IPI hoje)", "valor": "sim" if p.industria else "não"},
        {"premissa": "Calculadora RTC usada", "valor": a.versao_calculadora},
    ]
    if a.empresa.regime == "simples":
        linhas.append({"premissa": "Alíquota efetiva do DAS (%)", "valor": p.aliquota_das_pct})
    for ano in p.anos:
        usada = a.aliquotas_usadas.get(ano) or {"aliquotas": p.aliquotas_nominais(ano), "origens": {}}
        n, o = usada["aliquotas"], usada["origens"]
        linhas.append({"premissa": f"Alíquotas nominais {ano} (CBS / IBS UF / IBS mun.)",
                       "valor": " / ".join(f"{n[k]:.2f} ({o.get(k, 'premissa')})"
                                           for k in ("cbs", "ibsEstadual", "ibsMunicipal"))})
    return linhas
