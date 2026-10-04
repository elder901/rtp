"""Identidade do produto: EAN/GTIN primeiro, código interno como alternativa.

O EAN liga a compra (código do fornecedor) e a venda (código da loja) do mesmo produto. Não servem para isso:
- "SEM GTIN" e códigos com dígito verificador inválido (cadastro errado);
- códigos de circulação restrita (prefixo 2 no EAN-13/UPC-12, 0 ou 2 no EAN-8): balança, açougue, padaria —
  cada empresa usa os seus, então não identificam o mesmo produto entre fornecedor e loja.
"""
from __future__ import annotations


def digito_verificador_ok(gtin: str) -> bool:
    if not gtin.isdigit() or len(gtin) not in (8, 12, 13, 14):
        return False
    corpo, dv = gtin[:-1], int(gtin[-1])
    soma = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(corpo)))
    return (10 - soma % 10) % 10 == dv


def circulacao_restrita(gtin: str) -> bool:
    if len(gtin) in (12, 13):
        return gtin[0] == "2"
    if len(gtin) == 8:
        return gtin[0] in "02"
    if len(gtin) == 14:  # GTIN-14 = indicador + EAN-13 base (sem o DV deste)
        return gtin[1] == "2"
    return False


def gtin_global(valor: str) -> str:
    """Retorna o GTIN normalizado se identifica o produto globalmente; senão, ""."""
    valor = (valor or "").strip()
    if not digito_verificador_ok(valor) or circulacao_restrita(valor):
        return ""
    return valor


def chave_produto(gtin: str, codigo: str) -> str:
    """Chave usada para agrupar o mesmo produto em compras e vendas."""
    return f"ean:{gtin}" if gtin else f"cod:{codigo}"


def rotulo_chave(chave: str) -> str:
    tipo, _, valor = chave.partition(":")
    return {"ean": "EAN", "cod": "Código", "ncm": "NCM"}.get(tipo, "") + f" {valor}"
