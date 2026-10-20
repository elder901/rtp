import os
from decimal import Decimal as D

from sqlalchemy import select

from app import db, models, servicos
from app.ingest import sped
from tests.conftest import CNPJ_EMPRESA, FX, CalculadoraFake

NB01 = "35261044555666000177550010000012341000012345"     # compra de fornecedor normal: NB-01 (1000) e ARZ-5 (500)
POTE = "35261077888999000155550010000005671000056789"     # compra do Simples: fica fora do livro no teste
TOMATE = "35261011222333000181550010000000201000000202"   # nota de entrada própria (produtor rural)


def _reg(*campos, total=40) -> str:
    c = list(campos) + [""] * (total - len(campos))
    return "|" + "|".join(c) + "|\n"


def _campos(**pos) -> list[str]:
    c = [""] * 40
    for k, v in pos.items():
        c[int(k[1:])] = v
    return c[1:]


def _fiscal(caminho, finalidade="0", icms_nb01="150,00"):
    linhas = [
        _reg("0000", "019", finalidade, "01032026", "31032026", "LOJA TESTE", CNPJ_EMPRESA, "", "SP", "1", "3550308"),
        _reg(*_campos(c1="C100", c2="0", c3="1", c5="55", c6="00", c9=NB01, c12="1500,00")),
        _reg(*_campos(c1="C170", c2="1", c3="NB-01", c7="1000,00", c15=icms_nb01)),
        _reg(*_campos(c1="C170", c2="2", c3="ARZ-5", c7="500,00", c15="0,00")),
        _reg(*_campos(c1="C190", c2="000", c3="1102", c7=icms_nb01)),
        _reg(*_campos(c1="C100", c2="0", c3="0", c5="55", c6="00", c9=TOMATE, c12="300,00")),
        _reg(*_campos(c1="C170", c2="1", c3="TOM-KG", c7="300,00", c15="0,00")),
        _reg("9999", "8"),
        "ASSINATURA-DIGITAL-QUE-NAO-E-REGISTRO\n",
    ]
    caminho.write_text("".join(linhas), encoding="latin-1")


def _contribuicoes(caminho):
    linhas = [
        _reg("0000", "006", "0", "", "", "01032026", "31032026", "LOJA TESTE", CNPJ_EMPRESA, "SP", "3550308"),
        _reg("C010", CNPJ_EMPRESA, "2"),
        _reg(*_campos(c1="C100", c2="0", c3="1", c5="55", c6="00", c9=NB01)),
        _reg(*_campos(c1="C170", c2="1", c7="1000,00", c25="50", c30="13,20", c31="50", c36="60,80")),
        _reg(*_campos(c1="C170", c2="2", c7="500,00", c25="70", c30="0,00", c31="70", c36="0,00")),
        _reg("9999", "5"),
    ]
    caminho.write_text("".join(linhas), encoding="latin-1")


def test_escolhe_pelo_registro_0000_retificadora_sobre_original(tmp_path):
    (tmp_path / "a").mkdir()
    _fiscal(tmp_path / "a" / "original.txt", "0")
    _fiscal(tmp_path / "a" / "qualquer nome.txt", "1", icms_nb01="120,00")
    os.utime(tmp_path / "a" / "original.txt", (2_000_000_000, 2_000_000_000))   # mais nova, mas é a original
    _contribuicoes(tmp_path / "contrib.txt")
    escolhidos = sped.arquivos([tmp_path])
    assert [(c.tipo, c.competencia, c.finalidade, c.caminho.name) for c in escolhidos] == [
        ("contribuicoes", "2026-03", "0", "contrib.txt"), ("fiscal", "2026-03", "1", "qualquer nome.txt")]
    notas = {n.chave: n for n in sped.notas(escolhidos[1])}
    assert set(notas) == {NB01, TOMATE} and notas[NB01].itens[1] == (D("1000.00"), D("120.00"), D("0"))


def test_analise_usa_credito_escriturado_e_tira_nota_fora_do_livro(banco, tmp_path):
    _fiscal(tmp_path / "fiscal.txt")
    _contribuicoes(tmp_path / "contrib.txt")
    xmls = [(p.name, p.read_bytes()) for p in sorted((FX / "xml").glob("*.xml"))]
    with db.sessao() as s:
        e = servicos.criar_empresa(s, CNPJ_EMPRESA, "LOJA TESTE", "real", "SP", 3550308)
        servicos.importar_arquivos(s, e, xmls)
        relatorio = servicos.importar_sped(s, [tmp_path], progresso=lambda _: None)
        assert len(relatorio) == 2
        assert {(a.tipo, a.notas) for a in s.scalars(select(models.EscrituracaoArquivo))} == {
            ("fiscal", 2), ("contribuicoes", 1)}
        a = servicos.montar_analise(s, e, CalculadoraFake())

    por_codigo = {r.item.codigo: r for r in a.itens if r.direcao == "entrada"}
    assert "POTE-10" not in por_codigo                                   # não escriturada: fora da análise
    assert por_codigo["NB-01"].atual.creditos == D("150.00") + D("74.00")   # ICMS e PIS/COFINS do SPED, não do XML
    assert por_codigo["ARZ-5"].atual.creditos == D("0")                   # CST 70: sem crédito na grade da empresa
    assert por_codigo["TOM-KG"].atual.creditos == D("0")                  # fora da EFD-Contribuições: sem crédito
    texto = "\n".join(a.alertas)
    assert "1 nota(s) de entrada não escriturada(s) no SPED" in texto and "vêm do SPED" in texto


def test_avisa_o_mes_sem_efd_contribuicoes(banco, tmp_path):
    _fiscal(tmp_path / "fiscal.txt")
    xmls = [(p.name, p.read_bytes()) for p in sorted((FX / "xml").glob("*.xml"))]
    with db.sessao() as s:
        e = servicos.criar_empresa(s, CNPJ_EMPRESA, "LOJA TESTE", "real", "SP", 3550308)
        servicos.importar_arquivos(s, e, xmls)
        servicos.importar_sped(s, [tmp_path], progresso=lambda _: None)
        a = servicos.montar_analise(s, e, CalculadoraFake())
    texto = "\n".join(a.alertas)
    assert "Sem EFD-Contribuições importada em 03/2026 (LOJA TESTE)" in texto and "Sem EFD ICMS/IPI" not in texto
    nb = next(r for r in a.itens if r.item.codigo == "NB-01")
    assert nb.atual.creditos == D("150.00") + D("80.48")                 # ICMS do SPED; PIS/COFINS pela regra


def test_sem_sped_importado_continua_a_regra_do_xml(banco):
    xmls = [(p.name, p.read_bytes()) for p in sorted((FX / "xml").glob("*.xml"))]
    with db.sessao() as s:
        e = servicos.criar_empresa(s, CNPJ_EMPRESA, "LOJA TESTE", "real", "SP", 3550308)
        servicos.importar_arquivos(s, e, xmls)
        a = servicos.montar_analise(s, e, CalculadoraFake())
    nb = next(r for r in a.itens if r.item.codigo == "NB-01")
    assert nb.atual.creditos == D("180.00") + D("80.48")                 # mesma regra de test_cenarios
