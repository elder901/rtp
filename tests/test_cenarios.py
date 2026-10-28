from decimal import Decimal as D
from pathlib import Path

import pytest

from app.engine.cenarios import Empresa, analisar
from app.engine.premissas import Premissas
from app.ingest.arquivos import ler_caminho
from app.parser.nfe import ler_nfe
from app.relatorios import agregacao
from app.relatorios.excel import exportar
from tests.conftest import CalculadoraFake

FX = Path(__file__).parent / "fixtures" / "xml"


@pytest.fixture
def analise():
    docs = ler_caminho(FX).documentos
    empresa = Empresa("11222333000181", "LOJA TESTE", "real", "SP", 3550308)
    return analisar(empresa, docs, Premissas(anos=[2027, 2033]), CalculadoraFake(), {"V-ARZ5": ("200", "200003")})


def _item(a, codigo):
    return next(r for r in a.itens if r.item.codigo == codigo)


def test_entrada_fornecedor_normal_lucro_real(analise):
    r = _item(analise, "NB-01")
    assert r.item.valor_liquido == D("744.15")
    assert r.atual.tributos == D("305.85")
    assert r.atual.creditos == D("180.00") + D("80.48")   # ICMS + 9,25% x (1000 + 50 IPI - 180 ICMS)
    v = r.anos[2033]
    assert (v.cbs, v.ibs) == (D("65.49"), D("131.71"))
    assert v.tributos == D("197.20")                     # ICMS, IPI, PIS e COFINS extintos
    assert v.creditos == v.cbs + v.ibs                   # crédito amplo
    assert v.custo_ou_receita(r.item.valor_liquido) == D("744.15")


def test_sem_credito_de_pis_cofins_em_compra_com_aliquota_zero(analise):
    arroz = _item(analise, "ARZ-5")               # PIS/COFINS CST 06 (alíquota zero) no fornecedor
    assert arroz.item.pis_cst == "06"
    assert arroz.atual.creditos == D("35.00")      # só o ICMS


def test_transicao_2027_mantem_icms(analise):
    v = _item(analise, "NB-01").anos[2027]
    assert v.cbs == (D("744.15") * D("8.7") / 100).quantize(D("0.01"))  # CBS - 0,1 p.p.
    # ICMS integral, mas recalculado sobre o preço de nota de 2027: sem PIS/COFINS, o mesmo valor líquido (744,15)
    # corresponde a uma nota de 744,15 / (1 - 0,18) = 907,50 -> ICMS 18% = 163,35. IPI/PIS/COFINS extintos.
    assert v.tributos == D("163.35") + v.cbs + v.ibs
    assert v.creditos == D("163.35") + v.cbs + v.ibs                   # crédito de ICMS acompanha a nota


def test_entrada_fornecedor_simples_perde_credito(analise):
    r = _item(analise, "POTE-10")
    assert r.atual.creditos == D("12.50") + D("46.25")
    assert r.anos[2033].creditos == D("20.00")         # 4% (premissa) sobre 500
    assert r.anos[2033].tributos == D("0")


def test_saida_classificacao_manual_e_cfop_ignorado(analise):
    arroz = _item(analise, "V-ARZ5")
    assert arroz.classificacao.origem == "manual"
    assert arroz.anos[2033].tributos == D("0")
    assert analise.ignorados == 1
    assert not any(r.item.codigo == "BRINDE" for r in analise.itens)
    nb = _item(analise, "V-NB01")
    assert nb.classificacao.origem == "fornecedor"      # o fornecedor declara 000001 para o mesmo EAN
    assert nb.anos[2033].tributos == D("105.42") + D("212.03")


def test_agregacoes_e_excel(analise, tmp_path):
    res = agregacao.resumo(analise)
    assert [l["cenario"] for l in res] == ["Atual", "2027", "2033"]
    prod = {l["codigo"]: l for l in agregacao.por_produto(analise)}
    assert prod["V-ARZ5"]["var_preco_2033_pct"] < 0
    forn = {l["cnpj"]: l for l in agregacao.por_fornecedor(analise)}
    assert forn["77888999000155"]["var_custo_2033_pct"] > 0      # fornecedor do Simples encarece
    assert any("sem cClassTrib" in t for t in analise.alertas)
    destino = exportar(analise, tmp_path / "r.xlsx")
    assert destino.stat().st_size > 0


def test_nota_de_entrada_propria_de_produtor_rural(analise):
    tomate = _item(analise, "TOM-KG")              # nota emitida pela própria loja, fornecedor é o destinatário
    assert tomate.direcao == "entrada"
    assert (tomate.contraparte.nome, tomate.contraparte.regime) == ("JOSE PRODUTOR RURAL", "nao_contribuinte")
    assert tomate.atual.creditos == D("0")          # pessoa física: sem crédito de PIS/COFINS; ICMS isento
    assert tomate.anos[2033].creditos == D("0")     # crédito presumido padrão = 0 (premissa)
    assert any("não contribuinte" in t for t in analise.alertas)


def test_credito_presumido_de_produtor_rural_como_premissa():
    docs = ler_caminho(FX).documentos
    p = Premissas(anos=[2033], credito_presumido_nao_contribuinte_pct=D("5"))
    a = analisar(Empresa("11222333000181", "LOJA", "real", "SP", 3550308), docs, p, CalculadoraFake())
    assert _item(a, "TOM-KG").anos[2033].creditos == D("15.00")


def test_versao_da_calculadora_registrada(analise):
    assert analise.versao_calculadora.startswith("app 1.0-teste · base de regras V0000")
    premissas = {l["premissa"]: l["valor"] for l in agregacao.premissas(analise)}
    assert "V0000" in premissas["Calculadora RTC usada"]


def test_ncm_em_anexo_de_reducao_e_classificado_pela_calculadora():
    so_vendas = [d for d in ler_caminho(FX).documentos if d.emitente.cnpj == "11222333000181"]
    a = analisar(Empresa("11222333000181", "LOJA", "real", "SP", 3550308), so_vendas, Premissas(anos=[2033]),
                 CalculadoraFake())
    arroz = _item(a, "V-ARZ5")                        # sem compra do produto, sem cClassTrib no XML nem ajuste
    c = arroz.classificacao
    assert (c.cst, c.cclasstrib, c.origem) == ("200", "200003", "anexo")
    assert arroz.anos[2033].tributos == D("0")
    assert _item(a, "V-NB01").classificacao.origem == "padrao"   # notebook não está em anexo
    assert any("classificado(s) pelo NCM nos anexos" in t for t in a.alertas)


def test_mesma_classificacao_na_compra_e_na_venda_pelo_que_o_fornecedor_declara():
    a = analisar(Empresa("11222333000181", "LOJA", "real", "SP", 3550308), ler_caminho(FX).documentos,
                 Premissas(anos=[2033]), CalculadoraFake())
    compra, venda = _item(a, "ARZ-5"), _item(a, "V-ARZ5")          # mesmo EAN, códigos diferentes
    assert compra.classificacao == venda.classificacao
    assert (venda.classificacao.cclasstrib, venda.classificacao.origem) == ("200003", "fornecedor")


def test_ncm_fora_da_tabela_vigente_gera_um_alerta_por_ncm():
    from app.calculadora.client import AliquotaEfetiva

    class NcmExtinto(CalculadoraFake):
        def aliquotas(self, chaves, nominais_por_ano, uf, municipio):
            out = super().aliquotas(chaves, nominais_por_ano, uf, municipio)
            for c in out:
                if c.ncm == "84713012":
                    out[c] = AliquotaEfetiva(D(0), D(0), D(0), D(0), erro=f"HTTP 404: NCM de código {c.ncm} não "
                                                                          f"encontrada para a data {c.ano}-01-15")
            return out

    docs = ler_caminho(FX).documentos
    a = analisar(Empresa("11222333000181", "LOJA", "real", "SP", 3550308), docs, Premissas(anos=[2027, 2033]),
                 NcmExtinto())
    avisos = [t for t in a.alertas if "84713012" in t]
    assert len(avisos) == 1 and "não existe na tabela NCM vigente" in avisos[0] and "2 item(ns)" in avisos[0]


def test_compra_com_icms_st_nao_da_credito_de_icms():
    xml = (FX / "entrada_fornecedor_normal.xml").read_text(encoding="utf-8").replace(
        "<ICMS00><orig>0</orig><CST>00</CST><vBC>1000.00</vBC><pICMS>18.00</pICMS><vICMS>180.00</vICMS></ICMS00>",
        "<ICMS10><orig>0</orig><CST>10</CST><vBC>1000.00</vBC><pICMS>18.00</pICMS><vICMS>180.00</vICMS>"
        "<vBCST>1400.00</vBCST><pICMSST>18.00</pICMSST><vICMSST>72.00</vICMSST></ICMS10>")
    doc = ler_nfe(xml.encode())
    a = analisar(Empresa("11222333000181", "LOJA", "real", "SP", 3550308), [doc], Premissas(anos=[2027, 2033]),
                 CalculadoraFake())
    nb = _item(a, "NB-01")
    assert nb.item.v_icms_st == D("72.00")
    assert nb.atual.creditos == D("80.48")             # só PIS/COFINS: o ICMS próprio e o ST não se creditam
    v = nb.anos[2027]
    assert v.creditos == v.cbs + v.ibs                 # nem o ICMS residual da transição


def test_cclasstrib_do_xml_incompativel_com_o_ncm_cai_para_anexo_ou_padrao():
    class CestaSoDeFeijao(CalculadoraFake):
        ANEXOS = {**CalculadoraFake.ANEXOS, "200003": ("0713",)}

    docs = ler_caminho(FX).documentos
    a = analisar(Empresa("11222333000181", "LOJA", "real", "SP", 3550308), docs, Premissas(anos=[2033]),
                 CestaSoDeFeijao())
    arroz = _item(a, "ARZ-5")                          # o fornecedor informou 200003 para o arroz (NCM 1006...)
    assert arroz.classificacao.origem == "padrao" and arroz.classificacao.cclasstrib == "000001"
    assert not any(al.erro for al in arroz.aliquotas.values())
    assert any("incompatível com o NCM" in t and "NCM 10063021 com 200003" in t for t in a.alertas)


def test_visao_do_grupo_inclui_as_notas_das_filiais():
    """Na análise consolidada a nota vale para o estabelecimento que a emitiu ou recebeu — antes, só a da matriz."""
    from dataclasses import replace

    docs = ler_caminho(FX).documentos
    matriz, filial = "11222333000181", "11222333000262"
    venda = next(d for d in docs if d.direcao_para(matriz) == "saida")
    da_filial = replace(venda, chave="9" * 44, emitente=replace(venda.emitente, cnpj=filial))
    empresa = Empresa(matriz, "LOJA TESTE", "real", "SP", 3550308)
    so_matriz = analisar(empresa, [*docs, da_filial], Premissas(anos=[2027]), CalculadoraFake())
    grupo = analisar(empresa, [*docs, da_filial], Premissas(anos=[2027]), CalculadoraFake(),
                     estabelecimentos={matriz, filial})
    assert not any(r.documento.chave == "9" * 44 for r in so_matriz.itens)
    da_filial_no_grupo = [r for r in grupo.itens if r.documento.chave == "9" * 44]
    assert da_filial_no_grupo and all(r.direcao == "saida" for r in da_filial_no_grupo)
    assert len(grupo.itens) == len(so_matriz.itens) + len(da_filial_no_grupo)


def test_devolucao_fica_fora_e_o_alerta_mostra_o_peso_sobre_as_compras():
    from dataclasses import replace

    docs = ler_caminho(FX).documentos
    compra = next(d for d in docs if any(i.codigo == "NB-01" for i in d.itens))
    nb01 = next(i for i in compra.itens if i.codigo == "NB-01")
    devolucao = replace(compra, chave="8" * 44, emitente=compra.destinatario, destinatario=compra.emitente,
                        itens=[replace(nb01, cfop="5202")])          # a loja devolve o NB-01 ao fornecedor
    empresa = Empresa("11222333000181", "LOJA TESTE", "real", "SP", 3550308)
    sem = analisar(empresa, docs, Premissas(anos=[2027]), CalculadoraFake())
    com = analisar(empresa, [*docs, devolucao], Premissas(anos=[2027]), CalculadoraFake())
    assert not any(r.documento.chave == "8" * 44 for r in com.itens)
    assert not any(t.startswith("Devoluções") for t in sem.alertas)
    compras = sum(r.item.valor_operacao for r in com.itens if r.direcao == "entrada")
    pct = f"{nb01.valor_operacao / compras * 100:.1f}".replace(".", ",")
    aviso = next(t for t in com.alertas if t.startswith("Devoluções"))
    assert aviso.startswith(f"Devoluções de compra R$ 1.000,00 ({pct}% das compras) não foram abatidas.")
    assert "Passam de 2%" in aviso


def test_fornecedor_que_declara_tributacao_integral_cede_ao_anexo_do_ncm():
    """Ovos, arroz...: 000001 no XML do fornecedor costuma ser o padrão do ERP, não uma classificação."""
    xml = (FX / "entrada_fornecedor_normal.xml").read_text(encoding="utf-8").replace(
        "<CST>200</CST><cClassTrib>200003</cClassTrib>", "<CST>000</CST><cClassTrib>000001</cClassTrib>")
    a = analisar(Empresa("11222333000181", "LOJA", "real", "SP", 3550308), [ler_nfe(xml.encode())],
                 Premissas(anos=[2033]), CalculadoraFake())
    arroz, nb = _item(a, "ARZ-5"), _item(a, "NB-01")
    assert (arroz.classificacao.cclasstrib, arroz.classificacao.origem) == ("200003", "anexo")
    assert (nb.classificacao.cclasstrib, nb.classificacao.origem) == ("000001", "fornecedor")   # NCM fora de anexo
    assert any(t.startswith("1 produto(s) que o fornecedor declara com tributação integral") for t in a.alertas)


def test_anexo_que_vale_so_para_um_produto_exige_a_descricao():
    """2106.90.90 está no Anexo I só para fórmulas infantis; 1905.90.90 só para o pão francês."""
    from dataclasses import replace

    from app.engine.cenarios import anexo_do_produto

    class AnexoIAmplo(CalculadoraFake):
        ANEXOS = {**CalculadoraFake.ANEXOS, "200003": ("1006", "2106", "1905")}

    anexos = {"21069090": ("200", "200003"), "19059090": ("200", "200003")}
    assert anexo_do_produto("21069090", "Marmitex Tejotao Un", anexos) is None
    assert anexo_do_produto("21069090", "Fórmula Infantil Nan 800g", anexos) == ("200", "200003")
    assert anexo_do_produto("19059090", "Pao Frances Kg", anexos) == ("200", "200003")
    assert anexo_do_produto("19059090", "Cupcake Confeitado Kg", anexos) is None

    docs = ler_caminho(FX).documentos
    venda = next(d for d in docs if any(i.codigo == "V-NB01" for i in d.itens))
    marmita = replace(venda, chave="7" * 44, itens=[replace(venda.itens[0], codigo="MARMITA", gtin="",
                                                            descricao="Marmitex Tejotao Un", ncm="21069090",
                                                            ibscbs=None)])
    a = analisar(Empresa("11222333000181", "LOJA", "real", "SP", 3550308), [marmita], Premissas(anos=[2033]),
                 AnexoIAmplo())
    r = _item(a, "MARMITA")
    assert (r.classificacao.cclasstrib, r.classificacao.origem) == ("000001", "padrao")
    aviso = next(t for t in a.alertas if t.startswith("Produtos com NCM em que o anexo"))
    assert "NCM 2106.90.90" in aviso and "R$ " in aviso
