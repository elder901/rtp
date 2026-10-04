from pathlib import Path

from fastapi.testclient import TestClient

from app.api import main
from tests.test_cenarios import CalculadoraFake

FX = Path(__file__).parent / "fixtures"


def test_fluxo_upload_painel_excel(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "calculadora", CalculadoraFake())
    monkeypatch.setattr(main, "PASTA_RELATORIOS", tmp_path)
    c = TestClient(main.app)
    assert c.get("/").status_code == 200

    arquivos = [("xmls", (p.name, p.read_bytes(), "text/xml")) for p in sorted((FX / "xml").glob("*.xml"))]
    arquivos.append(("classificacao", ("c.csv", (FX / "classificacao_exemplo.csv").read_bytes(), "text/csv")))
    r = c.post("/analises", data={"cnpj": "11.222.333/0001-81", "regime": "real", "uf": "sp", "municipio": "3550308"},
               files=arquivos, follow_redirects=False)
    assert r.status_code == 303
    painel = c.get(r.headers["location"])
    assert painel.status_code == 200
    assert "UTILIDADES PEQUENA ME" in painel.text and "ARROZ TIPO 1" in painel.text
    assert "1 arquivo(s) rejeitado(s)" in painel.text
    excel = c.get(r.headers["location"] + "/excel")
    assert excel.status_code == 200 and excel.content[:2] == b"PK"


def test_upload_sem_nfe_valida():
    c = TestClient(main.app)
    r = c.post("/analises", data={"cnpj": "1", "regime": "real", "uf": "SP", "municipio": "1"},
               files=[("xmls", ("x.xml", b"<a/>", "text/xml"))])
    assert r.status_code == 422
