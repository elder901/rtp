from datetime import date, datetime
from decimal import Decimal as D

from app import cobertura, db, models, servicos
from tests.conftest import CNPJ_EMPRESA, FX, CalculadoraFake

FORNECEDOR = "99888777000166"


def _nota(s, e, n, emissao, direcao, valor, modelo="55", participante=FORNECEDOR):
    chave = f"{n:0>20}"[:20] + modelo + f"{n:0>22}"[:22]
    s.add(models.DocumentoFiscal(empresa_id=e.id, chave=chave, origem="upload", situacao="completo", direcao=direcao,
                                 emissao=emissao, participante_cnpj=participante, valor=D(valor)))


def test_alertas_de_loja_sem_vendas_mes_vazio_e_notas_isoladas(banco):
    with db.sessao() as s:
        e = servicos.criar_empresa(s, CNPJ_EMPRESA, "LOJA TESTE", "real", "SP", 3550308)
        for i, mes in enumerate([1, 2, 4], 1):                      # março sem nenhuma nota
            _nota(s, e, i, datetime(2026, mes, 10), "entrada", "1000")
        _nota(s, e, 10, datetime(2026, 1, 15), "saida", "1500", modelo="65", participante="")
        _nota(s, e, 20, datetime(2022, 12, 5), "entrada", "777")      # sobra de importação antiga
        s.flush()
        cob = cobertura.levantar(s, [e])
    meses = cob[0].meses
    assert list(meses) == ["2022-12", "2026-01", "2026-02", "2026-04"]
    assert meses["2026-01"].cupons == 1 and meses["2026-01"].vendas == D("1500")

    inicio, fim = cobertura.periodo_padrao(cob)
    assert (inicio, fim) == (date(2025, 5, 1), date(2026, 4, 30))   # 12 meses até o último mês com notas
    texto = "\n".join(cobertura.alertas(cob, inicio, fim))
    assert "1 nota(s) em 12/2022, isoladas" in texto
    assert "em 2 mês(es) (02/2026, 04/2026) as vendas importadas (nenhuma)" in texto
    assert "nenhuma nota em 03/2026" in texto


def test_estabelecimento_que_so_vende_para_o_grupo(banco):
    with db.sessao() as s:
        e = servicos.criar_empresa(s, CNPJ_EMPRESA[:8] + "000262", "INDUSTRIA", "real", "SP", 3550308)
        _nota(s, e, 1, datetime(2026, 3, 1), "entrada", "100")
        _nota(s, e, 2, datetime(2026, 3, 9), "saida", "300", participante=CNPJ_EMPRESA)
        s.flush()
        texto = "\n".join(cobertura.alertas(cobertura.levantar(s, [e])))
    assert "só emitiu notas para o próprio grupo" in texto and "muito abaixo" not in texto


def test_analise_sem_periodo_usa_os_ultimos_12_meses_e_deixa_notas_antigas_de_fora(banco):
    xmls = [(p.name, p.read_bytes()) for p in sorted((FX / "xml").glob("*.xml"))]
    with db.sessao() as s:
        e = servicos.criar_empresa(s, CNPJ_EMPRESA, "LOJA TESTE", "real", "SP", 3550308)
        servicos.importar_arquivos(s, e, xmls)
        a = servicos.montar_analise(s, e, CalculadoraFake())
        antiga = s.query(models.DocumentoFiscal).filter_by(empresa_id=e.id, situacao="completo").first()
        s.add(models.DocumentoFiscal(empresa_id=e.id, chave="1" * 44, origem="upload", situacao="completo",
                                     direcao=antiga.direcao, emissao=datetime(2015, 1, 1), xml_gz=antiga.xml_gz,
                                     participante_cnpj=antiga.participante_cnpj, valor=antiga.valor))
        s.flush()
        de_novo = servicos.montar_analise(s, e, CalculadoraFake())
    assert a.periodo[0] and a.periodo[1] and de_novo.periodo == a.periodo
    assert len(de_novo.itens) == len(a.itens)                       # a nota de 2015 fica fora do período padrão
    assert any("isoladas do restante" in t for t in de_novo.alertas)
