"""Interface web do MVP: envio de XMLs, painel por produto/fornecedor e download do Excel.

Executar: uvicorn app.api.main:app --reload
As análises ficam em memória e o Excel em ./relatorios (persistência multi-cliente é a fase 4).
"""
from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.calculadora.client import CalculadoraRTC
from app.engine.cenarios import Analise, Empresa, analisar
from app.engine.premissas import Premissas
from app.ingest.arquivos import ler_classificacoes, ler_conteudos
from app.relatorios import agregacao
from app.relatorios.excel import exportar

PASTA_RELATORIOS = Path("relatorios")
app = FastAPI(title="RTP — Planejamento da Reforma Tributária")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
calculadora = CalculadoraRTC()
ANALISES: dict[str, tuple[Analise, dict]] = {}


def _brl(v) -> str:
    if v is None:
        return "—"
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


templates.env.filters["brl"] = _brl


def _dec(valor: str, campo: str) -> Decimal:
    try:
        return Decimal(valor.replace(",", "."))
    except InvalidOperation:
        raise HTTPException(422, f"Valor inválido em {campo}: {valor}")


@app.get("/", response_class=HTMLResponse)
def inicio(request: Request):
    return templates.TemplateResponse(request, "inicio.html", {"premissas": Premissas()})


@app.post("/analises")
def criar_analise(
    cnpj: str = Form(...), nome: str = Form(""), regime: str = Form(...), uf: str = Form(...),
    municipio: int = Form(...), cbs: str = Form("8.8"), ibs_uf: str = Form("8.85"), ibs_mun: str = Form("8.85"),
    credito_simples: str = Form("4.0"), das: str = Form("6.0"), industria: bool = Form(False),
    xmls: list[UploadFile] = File(...), classificacao: UploadFile | None = File(None),
):
    if regime not in ("real", "presumido", "simples"):
        raise HTTPException(422, "Regime inválido")
    leitura = ler_conteudos([(f.filename or "arquivo.xml", f.file.read()) for f in xmls])
    if not leitura.documentos:
        raise HTTPException(422, "Nenhuma NF-e válida encontrada nos arquivos enviados.")
    overrides = {}
    if classificacao and classificacao.filename:
        overrides = ler_classificacoes(classificacao.file.read().decode("utf-8-sig"))

    premissas = Premissas(
        cbs_referencia=_dec(cbs, "CBS"), ibs_uf_referencia=_dec(ibs_uf, "IBS UF"),
        ibs_mun_referencia=_dec(ibs_mun, "IBS município"),
        credito_fornecedor_simples_pct=_dec(credito_simples, "crédito Simples"),
        aliquota_das_pct=_dec(das, "DAS"), industria=industria,
    )
    empresa = Empresa("".join(c for c in cnpj if c.isdigit()), nome or cnpj, regime, uf.upper(), municipio)
    analise = analisar(empresa, leitura.documentos, premissas, calculadora, overrides)
    if leitura.rejeitados:
        analise.alertas.insert(0, f"{len(leitura.rejeitados)} arquivo(s) rejeitado(s): " + "; ".join(leitura.rejeitados[:5]))

    id_ = uuid.uuid4().hex[:12]
    exportar(analise, PASTA_RELATORIOS / f"{id_}.xlsx")
    ANALISES[id_] = (analise, {"lidos": len(leitura.documentos), "duplicados": leitura.duplicados})
    return RedirectResponse(f"/analises/{id_}", status_code=303)


@app.get("/analises/{id_}", response_class=HTMLResponse)
def painel(request: Request, id_: str):
    if id_ not in ANALISES:
        raise HTTPException(404, "Análise não encontrada (o servidor pode ter sido reiniciado).")
    a, info = ANALISES[id_]
    return templates.TemplateResponse(request, "painel.html", {
        "id": id_, "a": a, "info": info, "anos": a.premissas.anos, "ultimo": a.premissas.anos[-1],
        "resumo": agregacao.resumo(a), "produtos": agregacao.por_produto(a)[:50],
        "fornecedores": agregacao.por_fornecedor(a)[:50],
    })


@app.get("/analises/{id_}/excel")
def baixar_excel(id_: str):
    caminho = PASTA_RELATORIOS / f"{id_}.xlsx"
    if id_ not in ANALISES or not caminho.exists():
        raise HTTPException(404)
    return FileResponse(caminho, filename=f"reforma_{ANALISES[id_][0].empresa.cnpj}.xlsx")


@app.get("/saude")
def saude():
    try:
        return {"ok": True, "calculadora": calculadora.versao()}
    except Exception as e:  # noqa: BLE001 — diagnóstico
        return {"ok": False, "erro": str(e)}
