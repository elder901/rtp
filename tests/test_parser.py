from decimal import Decimal as D
from pathlib import Path

from app.ingest.arquivos import ler_caminho, ler_classificacoes
from app.parser.nfe import ler_arquivo

FX = Path(__file__).parent / "fixtures" / "xml"
EMPRESA = "11222333000181"


def test_nfe_fornecedor_normal():
    doc = ler_arquivo(FX / "entrada_fornecedor_normal.xml")
    assert doc.chave.startswith("3526104455566600017755")
    assert doc.direcao_para(EMPRESA) == "entrada"
    assert doc.emitente.regime == "normal"
    nb, arroz = doc.itens
    assert (nb.ncm, nb.v_icms, nb.v_ipi, nb.v_pis, nb.v_cofins) == ("84713012", D("180.00"), D("50.00"), D("13.53"), D("62.32"))
    assert nb.valor_liquido == D("1000.00") - D("180.00") - D("13.53") - D("62.32")
    assert nb.tributos_por_fora == D("50.00")
    assert (arroz.ibscbs.cst, arroz.ibscbs.cclasstrib) == ("200", "200003")


def test_nfe_fornecedor_simples():
    doc = ler_arquivo(FX / "entrada_fornecedor_simples.xml")
    item = doc.itens[0]
    assert doc.emitente.regime == "simples"
    assert item.icms_cst == "101"
    assert item.v_cred_icms_sn == D("12.50")
    assert item.valor_liquido == D("500.00")


def test_saida_e_pasta_com_xml_invalido():
    res = ler_caminho(FX)
    assert len(res.documentos) == 4
    assert len(res.rejeitados) == 1
    saida = next(d for d in res.documentos if d.numero == "10")
    assert saida.direcao_para(EMPRESA) == "saida"
    assert saida.direcao_para("99999999000199") is None


def test_csv_classificacao():
    csv = "codigo_ou_ncm;cst;cclasstrib\nV-ARZ5;200;200003\n10063021;200;200003\n"
    assert ler_classificacoes(csv) == {"V-ARZ5": ("200", "200003"), "10063021": ("200", "200003")}
