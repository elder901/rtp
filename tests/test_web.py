from datetime import date
from urllib.parse import urlencode

from sqlalchemy import select

from app import classificacao, db, models, servicos
from app.api import main
from tests.conftest import CNPJ_EMPRESA, FX, CalculadoraFake, cliente_logado, gerar_pfx


def _cliente(monkeypatch):
    monkeypatch.setattr(main, "calculadora", CalculadoraFake())
    return cliente_logado()


def _xmls():
    return [("xmls", (p.name, p.read_bytes(), "text/xml")) for p in sorted((FX / "xml").glob("*.xml"))]


def test_fluxo_completo(banco, monkeypatch):
    c = _cliente(monkeypatch)
    r = c.post("/empresas", data={"cnpj": "11.222.333/0001-81", "nome": "LOJA TESTE", "regime": "real", "uf": "sp",
                                  "municipio": "3550308"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/grupos/11222333"   # a empresa, com a matriz dentro
    geral = c.get("/grupos/11222333").text
    assert "Empresa cadastrada" in geral and "LOJA TESTE" in geral and "Matriz" in geral
    with db.sessao() as s:
        url = f"/empresas/{s.scalar(select(models.Empresa.id))}"

    r = c.post(url + "/xmls", files=_xmls())
    assert "Importação: 4 novas" in r.text and "1 rejeitadas" in r.text
    r = c.post(url + "/xmls", files=_xmls())
    assert "4 já existiam" in r.text

    r = c.post(url + "/certificado", files={"pfx": ("c.pfx", gerar_pfx(), "application/x-pkcs12")},
               data={"senha": "segredo"})
    assert "Certificado de LOJA TESTE LTDA" in r.text and "Sincronizar agora" in r.text

    # Classificação: notebook e pote sem cClassTrib; arroz de saída recebe sugestão vinda do fornecedor
    tela = c.get(url + "/classificacao").text
    assert "V-ARZ5" in tela and "usar" in tela
    form = []
    with db.sessao() as s:
        e = s.scalar(select(models.Empresa))
        produtos = classificacao.produtos(servicos.documentos(s, e), e.cnpj, {})
    for p in produtos:
        cst, cct = ("200", "200003") if p.gtin in ("7890000000024", "7890000000017") else ("", "")
        form += [("gtin", p.gtin), ("codigo", p.codigo), ("ncm", p.ncm), ("escopo", p.escopo_ajuste), ("cst", cst),
                 ("cclasstrib", cct), ("chave_atual", "")]
    r = c.post(url + "/classificacao", content=urlencode(form),
               headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert "2 classificação(ões) gravada(s)" in r.text
    assert "não se aplica ao NCM 84713012" in r.text           # cesta básica num notebook: alerta

    csv = c.get(url + "/classificacao.csv").text
    assert "ean:7890000000024;200;200003" in csv
    conflitos = c.get(url + "/classificacao/conflitos.csv")
    assert conflitos.status_code == 200 and conflitos.text.lstrip("﻿").startswith("chave;ean;descricao;ncm;tipo")

    # Busca e paginação no servidor: uma loja grande tem dezenas de milhares de produtos
    filtrada = c.get(url + "/classificacao", params={"q": "v-arz5"}).text
    assert "V-ARZ5" in filtrada and "(filtrados de" in filtrada
    monkeypatch.setattr(main, "PRODUTOS_POR_PAGINA", 1)
    segunda = c.get(url + "/classificacao?pagina=2").text
    assert "Produtos 2–2 de" in segunda and "página 2 de" in segunda and "← Anterior" in segunda

    painel = c.get(url + "/analise?anos=2027,2033")
    assert painel.status_code == 200
    assert "Saldo a recolher por ano da transição" in painel.text and "<svg" in painel.text
    for ano in range(2027, 2034):
        assert f">{ano}</text>" in painel.text                # linha do tempo completa
    assert "UTILIDADES PEQUENA ME" in painel.text

    vazio = c.get(url + "/analise?inicio=2030-01-01")
    assert "Nenhuma saída no período" in vazio.text

    excel = c.get(url + "/analise/excel")
    assert excel.status_code == 200 and excel.content[:2] == b"PK"


def test_filtro_de_periodo(banco):
    with db.sessao() as s:
        e = servicos.criar_empresa(s, CNPJ_EMPRESA, "LOJA", "real", "SP", 3550308)
        servicos.importar_arquivos(s, e, [(p.name, p.read_bytes()) for p in (FX / "xml").glob("*.xml")])
        assert len(servicos.documentos(s, e)) == 4
        assert len(servicos.documentos(s, e, date(2026, 3, 11), date(2026, 3, 31))) == 3
        assert len(servicos.documentos(s, e, fim=date(2026, 3, 10))) == 1

