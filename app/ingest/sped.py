"""Leitura do SPED fiscal (layout nacional): EFD ICMS/IPI e EFD-Contribuições.

Do SPED só interessam as NF-e de entrada (C100 com IND_OPER 0, modelo 55, situação regular) e o crédito que a empresa
de fato tomou em cada item (C170): ICMS na EFD ICMS/IPI e PIS + COFINS na EFD-Contribuições (CST 50 a 66). É isso que
substitui a regra montada a partir do XML do fornecedor, que não conhece a grade fiscal da empresa (produtos monofásicos,
alíquota zero na revenda, estornos estaduais).

Os arquivos são escolhidos pelo registro 0000 — tipo, CNPJ, período e finalidade —, nunca pelo nome ou pela pasta: a
retificadora (substituto) vale sobre a original e, entre arquivos equivalentes, o mais recente.
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

ZERO = Decimal("0")
SITUACOES_REGULARES = {"00", "01", "06", "07", "08"}      # regular, extemporâneo, complementar, regime especial
CST_PIS_COFINS_COM_CREDITO = {str(n) for n in range(50, 57)} | {str(n) for n in range(60, 67)}


@dataclass(frozen=True)
class Cabecalho:
    tipo: str            # fiscal | contribuicoes
    cnpj: str
    competencia: str     # AAAA-MM
    finalidade: str      # 0 original; 1 substituto/retificadora
    caminho: Path
    modificado: float


@dataclass
class Nota:
    chave: str
    cnpj: str                      # estabelecimento que escriturou
    competencia: str
    total_icms: Decimal = ZERO     # soma do C190 (fiscal) ou dos itens
    total_pis_cofins: Decimal = ZERO
    itens: dict[int, tuple[Decimal, Decimal, Decimal]] = field(default_factory=dict)   # n -> (valor, icms, pis+cofins)


def _dec(s: str) -> Decimal:
    return Decimal(s.replace(",", ".")) if s else ZERO


def _competencia(ddmmaaaa: str) -> str:
    return f"{ddmmaaaa[4:]}-{ddmmaaaa[2:4]}"


def cabecalho(caminho: Path) -> Cabecalho | None:
    """Registro 0000. Na EFD ICMS/IPI a data inicial vem logo depois da finalidade; na EFD-Contribuições há antes o
    indicador de situação especial e o recibo anterior."""
    try:
        with open(caminho, encoding="latin-1") as f:
            c = f.readline().strip().split("|")
    except OSError:
        return None
    if len(c) < 10 or c[1] != "0000":
        return None
    if len(c[4]) == 8 and c[4].isdigit():
        tipo, finalidade, inicio, cnpj = "fiscal", c[3], c[4], c[7]
    else:
        tipo, finalidade, inicio, cnpj = "contribuicoes", c[3], c[6], c[9]
    return Cabecalho(tipo, cnpj, _competencia(inicio), finalidade, Path(caminho), Path(caminho).stat().st_mtime)


def arquivos(caminhos: Iterable[Path], desde: str | None = None) -> list[Cabecalho]:
    """Os arquivos .txt de SPED em pastas (recursivo) ou listados um a um, um por (tipo, CNPJ, competência).
    `desde` (AAAA-MM) deixa de fora as competências anteriores."""
    escolhidos: dict[tuple, Cabecalho] = {}
    for caminho in caminhos:
        caminho = Path(caminho)
        for p in ([caminho] if caminho.is_file() else caminho.rglob("*.txt")):
            cab = cabecalho(p)
            if not cab or (desde and cab.competencia < desde):
                continue
            k = (cab.tipo, cab.cnpj, cab.competencia)
            atual = escolhidos.get(k)
            if atual is None or (cab.finalidade != "0", cab.modificado) > (atual.finalidade != "0", atual.modificado):
                escolhidos[k] = cab
    return sorted(escolhidos.values(), key=lambda c: (c.tipo, c.cnpj, c.competencia))


def notas(cab: Cabecalho) -> Iterator[Nota]:
    """NF-e de entrada escrituradas no arquivo, com o crédito de cada item."""
    estab, nota, oper = cab.cnpj, None, None
    with open(cab.caminho, encoding="latin-1") as f:
        for linha in f:
            c = linha.split("|")
            if len(c) < 3:
                continue
            reg = c[1]
            if reg == "9999":          # depois vem só a assinatura da remessa
                break
            if reg in ("C010", "A010", "F010", "D010", "I010", "P010") and cab.tipo == "contribuicoes":
                estab = c[2]
            elif reg == "C100":
                if nota:
                    yield nota
                oper, nota = c[2], None
                if oper == "0" and c[5] == "55" and c[6] in SITUACOES_REGULARES and len(c[9]) == 44:
                    nota = Nota(c[9], estab, cab.competencia)
            elif nota is None:
                continue
            elif reg == "C170":
                valor = _dec(c[7])
                if cab.tipo == "fiscal":
                    item = (valor, _dec(c[15]), ZERO)
                else:
                    pc = _dec(c[30]) + _dec(c[36]) if c[25] in CST_PIS_COFINS_COM_CREDITO else ZERO
                    item = (valor, ZERO, pc)
                    nota.total_pis_cofins += pc
                nota.itens[int(c[2])] = item
            elif reg == "C190" and cab.tipo == "fiscal":
                nota.total_icms += _dec(c[7])
            elif reg[:1] != "C" or reg in ("C990",):
                yield nota
                nota = None
        if nota:
            yield nota
