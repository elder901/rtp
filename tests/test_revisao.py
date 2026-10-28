from decimal import Decimal as D

from openpyxl import load_workbook

from app.engine.cenarios import Empresa, analisar
from app.engine.premissas import Premissas
from app.ingest.arquivos import ler_caminho
from app.relatorios import revisao
from tests.conftest import FX, CalculadoraFake


def test_lista_por_venda_marca_as_classificacoes_presumidas(tmp_path):
    docs = ler_caminho(FX / "xml").documentos
    a = analisar(Empresa("11222333000181", "LOJA", "real", "SP", 3550308), docs, Premissas(anos=[2033]),
                 CalculadoraFake(), {"V-ARZ5": ("200", "200003")})
    linhas = revisao.linhas(a, cobertura=D("100"))
    assert [l["posicao"] for l in linhas] == list(range(1, len(linhas) + 1))
    assert [l["vendas"] for l in linhas] == sorted((l["vendas"] for l in linhas), reverse=True)
    assert linhas[-1]["pct_acumulado"] == D("100.00")
    por_codigo = {l["codigos"]: l for l in linhas}
    assert por_codigo["V-ARZ5"]["confirmar"] == "" and por_codigo["V-ARZ5"]["origem"] == "ajuste manual"
    assert all(l["confirmar"] == ("sim" if l["origem"] in ("presumida pelo NCM (anexo)", "sem classificação (integral)")
                                  else "") for l in linhas)
    n = revisao.exportar(a, tmp_path / "r.xlsx", "Revisão — LOJA")
    ws = load_workbook(tmp_path / "r.xlsx")["Revisão"]
    assert ws["A1"].value == "Revisão — LOJA" and ws["A4"].value == "Posição"
    assert n == sum(1 for l in revisao.linhas(a) if l["confirmar"])
