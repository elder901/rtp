"""Interface web: empresas, certificado A1, sincronização DF-e, importação de XML, revisão de classificação e
painel de impacto.

Executar: python -m uvicorn app.api.main:app --port 8000   (escuta só em 127.0.0.1 por padrão)
Defina RTP_USUARIO e RTP_SENHA para exigir login básico — obrigatório se o servidor for exposto na rede.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import date
from decimal import InvalidOperation
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from app import classificacao as classif
from app import db, models, servicos
from app.api.grafico import linha_do_tempo
from app.apuracao import conciliacao
from app.apuracao import servico as apuracao
from app.apuracao.cliente import ErroApuracao
from app.calculadora.client import CalculadoraRTC, ErroCalculadora
from app.config import settings
from app.dfe.certificado import CertificadoInvalido
from app.dfe.sincronizar import sincronizar, sincronizar_pendentes
from app.ingest.arquivos import ler_classificacoes
from app.relatorios import agregacao
from app.relatorios.excel import exportar

log = logging.getLogger(__name__)
calculadora = CalculadoraRTC()
AVISOS: dict[int, list[str]] = {}  # mensagens exibidas uma vez na página da empresa


def _verificar_apuracoes():
    with db.sessao() as s:
        ids = set(s.scalars(select(models.SolicitacaoApuracao.empresa_id)
                            .where(models.SolicitacaoApuracao.estado.in_(apuracao.ABERTAS))))
    for id_ in ids:
        with db.sessao() as s:
            apuracao.verificar_pendentes(s, s.get(models.Empresa, id_))


async def _agendador():
    while True:
        for nome, tarefa in (("DF-e", lambda: sincronizar_pendentes(db.sessao)), ("apuração", _verificar_apuracoes)):
            try:
                await asyncio.to_thread(tarefa)
            except Exception:  # noqa: BLE001 — o agendador não pode morrer
                log.exception("Falha na tarefa agendada de %s", nome)
        await asyncio.sleep(settings.dfe_intervalo_minutos * 60)


@asynccontextmanager
async def ciclo_de_vida(_app: FastAPI):
    db.criar_tabelas()
    tarefa = asyncio.create_task(_agendador()) if settings.dfe_agendador else None
    yield
    if tarefa:
        tarefa.cancel()


_basic = HTTPBasic(auto_error=False)


def autenticar(request: Request, cred: HTTPBasicCredentials | None = Depends(_basic)):
    if not settings.usuario or request.url.path.startswith("/webhooks/"):
        return
    ok = cred and secrets.compare_digest(cred.username, settings.usuario) and \
        secrets.compare_digest(cred.password, settings.senha)
    if not ok:
        raise HTTPException(401, "Login necessário", headers={"WWW-Authenticate": "Basic"})


app = FastAPI(title="RTP — Planejamento da Reforma Tributária", lifespan=ciclo_de_vida,
              dependencies=[Depends(autenticar)])
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def _brl(v) -> str:
    if v is None:
        return "—"
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _pct(v) -> str:
    return "—" if v is None else f"{v:.2f}".replace(".", ",") + "%"


templates.env.filters["brl"] = _brl
templates.env.filters["pct"] = _pct


def _avisar(empresa_id: int, *mensagens: str):
    AVISOS.setdefault(empresa_id, []).extend(m for m in mensagens if m)


def _ir(empresa_id: int, pagina: str = "") -> RedirectResponse:
    return RedirectResponse(f"/empresas/{empresa_id}{pagina}", status_code=303)


def _carregar(s, empresa_id: int) -> models.Empresa:
    e = s.get(models.Empresa, empresa_id)
    if not e:
        raise HTTPException(404, "Empresa não encontrada")
    return e


def _render(request: Request, nome: str, **ctx):
    return templates.TemplateResponse(request, nome, ctx)


# --- Empresas ----------------------------------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def inicio(request: Request):
    with db.sessao() as s:
        contagem = dict(s.execute(select(models.DocumentoFiscal.empresa_id, func.count())
                                  .where(models.DocumentoFiscal.situacao == "completo")
                                  .group_by(models.DocumentoFiscal.empresa_id)).all())
        empresas = list(s.scalars(select(models.Empresa).order_by(models.Empresa.nome)))
        linhas = [{"e": e, "notas": contagem.get(e.id, 0), "cert": e.certificado} for e in empresas]
        return _render(request, "inicio.html", empresas=linhas)


@app.post("/empresas")
def criar_empresa(cnpj: str = Form(...), nome: str = Form(""), regime: str = Form(...), uf: str = Form(...),
                  municipio: int = Form(...), industria: bool = Form(False)):
    try:
        with db.sessao() as s:
            e = servicos.criar_empresa(s, cnpj, nome, regime, uf, municipio, industria)
            return _ir(e.id)
    except ValueError as erro:
        raise HTTPException(422, str(erro))


@app.get("/empresas/{empresa_id}", response_class=HTMLResponse)
def empresa(request: Request, empresa_id: int):
    with db.sessao() as s:
        e = _carregar(s, empresa_id)
        situacoes = dict(s.execute(select(models.DocumentoFiscal.situacao, func.count())
                                   .where(models.DocumentoFiscal.empresa_id == e.id)
                                   .group_by(models.DocumentoFiscal.situacao)).all())
        por_origem = s.execute(select(models.DocumentoFiscal.direcao, models.DocumentoFiscal.origem, func.count(),
                                      func.min(models.DocumentoFiscal.emissao), func.max(models.DocumentoFiscal.emissao))
                               .where(models.DocumentoFiscal.empresa_id == e.id,
                                      models.DocumentoFiscal.situacao == "completo")
                               .group_by(models.DocumentoFiscal.direcao, models.DocumentoFiscal.origem)).all()
        return _render(request, "empresa.html", e=e, premissas=servicos.premissas_da_empresa(e),
                       situacoes=situacoes, por_origem=por_origem, avisos=AVISOS.pop(e.id, []), aba="geral")


@app.post("/empresas/{empresa_id}/dados")
def salvar_dados(empresa_id: int, regime: str = Form(...), uf: str = Form(...), municipio: int = Form(...),
                 industria: bool = Form(False), manifestar_ciencia: bool = Form(False), ambiente_dfe: int = Form(1),
                 cbs_referencia: str = Form(""), ibs_uf_referencia: str = Form(""), ibs_mun_referencia: str = Form(""),
                 credito_fornecedor_simples_pct: str = Form(""), credito_fornecedor_mei_pct: str = Form(""),
                 aliquota_das_pct: str = Form(""), das_pis_cofins_pct: str = Form(""),
                 das_icms_iss_pct: str = Form("")):
    if regime not in ("real", "presumido", "simples") or ambiente_dfe not in (1, 2):
        raise HTTPException(422, "Regime ou ambiente inválido")
    with db.sessao() as s:
        e = _carregar(s, empresa_id)
        e.regime, e.uf, e.cod_municipio, e.industria = regime, uf.upper(), municipio, industria
        e.manifestar_ciencia, e.ambiente_dfe = manifestar_ciencia, ambiente_dfe
        try:
            servicos.atualizar_premissas(e, dict(
                cbs_referencia=cbs_referencia, ibs_uf_referencia=ibs_uf_referencia,
                ibs_mun_referencia=ibs_mun_referencia, credito_fornecedor_simples_pct=credito_fornecedor_simples_pct,
                credito_fornecedor_mei_pct=credito_fornecedor_mei_pct, aliquota_das_pct=aliquota_das_pct,
                das_pis_cofins_pct=das_pis_cofins_pct, das_icms_iss_pct=das_icms_iss_pct))
        except InvalidOperation:
            raise HTTPException(422, "Premissa com valor inválido")
    _avisar(empresa_id, "Dados e premissas salvos.")
    return _ir(empresa_id)


@app.post("/empresas/{empresa_id}/certificado")
def enviar_certificado(empresa_id: int, pfx: UploadFile = File(...), senha: str = Form("")):
    with db.sessao() as s:
        e = _carregar(s, empresa_id)
        try:
            cert = servicos.salvar_certificado(s, e, pfx.file.read(), senha)
            _avisar(empresa_id, f"Certificado de {cert.titular} gravado, válido até {cert.validade:%d/%m/%Y}.")
        except CertificadoInvalido as erro:
            _avisar(empresa_id, f"Certificado não aceito: {erro}")
    return _ir(empresa_id)


@app.post("/empresas/{empresa_id}/dfe")
async def sincronizar_dfe(empresa_id: int):
    def executar():
        with db.sessao() as s:
            return sincronizar(s, _carregar(s, empresa_id))

    r = await run_in_threadpool(executar)
    if r.executada:
        _avisar(empresa_id, f"DF-e: {r.completos} NF-e completas, {r.resumos} resumos, {r.cancelados} canceladas, "
                            f"{r.manifestados} ciências enviadas, {r.xml_obtidos} XML obtidos após ciência.")
    _avisar(empresa_id, *r.mensagens)
    return _ir(empresa_id)


@app.post("/empresas/{empresa_id}/xmls")
def importar_xmls(empresa_id: int, xmls: list[UploadFile] = File(...)):
    with db.sessao() as s:
        e = _carregar(s, empresa_id)
        r = servicos.importar_arquivos(s, e, [(f.filename or "arquivo.xml", f.file.read()) for f in xmls])
    _avisar(empresa_id, f"Importação: {r.novos} novas, {r.atualizados} completaram resumos, {r.ja_existentes} já "
                        f"existiam, {r.sem_participacao} sem participação da empresa, {len(r.rejeitados)} rejeitadas.",
            *r.rejeitados[:5])
    return _ir(empresa_id)


# --- Classificação -----------------------------------------------------------------------------------------------

@app.get("/empresas/{empresa_id}/classificacao", response_class=HTMLResponse)
def tela_classificacao(request: Request, empresa_id: int):
    with db.sessao() as s:
        e = _carregar(s, empresa_id)
        ajustes = {c.chave: c for c in s.scalars(select(models.ClassificacaoProduto)
                                                 .where(models.ClassificacaoProduto.empresa_id == e.id))}
        produtos = classif.produtos(servicos.documentos(s, e), e.cnpj,
                                    {k: (v.cst, v.cclasstrib) for k, v in ajustes.items()})
    try:
        situacoes = calculadora.situacoes_tributarias()
    except ErroCalculadora:
        situacoes = {}
    return _render(request, "classificacao.html", e=e, produtos=produtos, ajustes=ajustes, situacoes=situacoes,
                   avisos=AVISOS.pop(e.id, []), aba="classificacao")


@app.post("/empresas/{empresa_id}/classificacao")
async def salvar_classificacoes(request: Request, empresa_id: int):
    form = await request.form()
    linhas = zip(form.getlist("codigo"), form.getlist("ncm"), form.getlist("escopo"), form.getlist("cst"),
                 form.getlist("cclasstrib"), form.getlist("chave_atual"))

    def executar():
        salvos, problemas = 0, []
        with db.sessao() as s:
            e = _carregar(s, empresa_id)
            for codigo, ncm, escopo, cst, cct, chave_atual in linhas:
                chave = ncm if escopo == "ncm" else codigo
                if chave_atual and chave_atual != chave:
                    servicos.salvar_classificacao(s, e, chave_atual, "", "")
                if not cst.strip() and not cct.strip():
                    if chave_atual:
                        servicos.salvar_classificacao(s, e, chave_atual, "", "")
                    continue
                cst, cct = cst.strip().zfill(3), cct.strip().zfill(6)
                motivo = classif.validar(calculadora, ncm, cst, cct)
                try:
                    servicos.salvar_classificacao(s, e, chave, cst, cct, motivo)
                    salvos += 1
                    if motivo:
                        problemas.append(f"{chave}: {motivo}")
                except ValueError as erro:
                    problemas.append(str(erro))
                s.flush()
        return salvos, problemas

    salvos, problemas = await run_in_threadpool(executar)
    _avisar(empresa_id, f"{salvos} classificação(ões) gravada(s).", *problemas)
    return _ir(empresa_id, "/classificacao")


@app.get("/empresas/{empresa_id}/classificacao.csv")
def exportar_classificacao(empresa_id: int):
    with db.sessao() as s:
        e = _carregar(s, empresa_id)
        ajustes = servicos.classificacoes(s, e)
        produtos = classif.produtos(servicos.documentos(s, e), e.cnpj, ajustes)
    saida = io.StringIO()
    w = csv.writer(saida, delimiter=";")
    w.writerow(["codigo_ou_ncm", "cst", "cclasstrib", "descricao", "ncm", "direcao", "origem_atual", "sugestao"])
    for p in produtos:
        manual = p.origem == "manual"
        w.writerow([p.chave_manual or p.codigo, p.cst if manual else "", p.cclasstrib if manual else "", p.descricao,
                    p.ncm, p.direcao, p.origem, "/".join(p.sugestao) if p.sugestao else ""])
    return Response(saida.getvalue().encode("utf-8-sig"), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="classificacao_{e.cnpj}.csv"'})


@app.post("/empresas/{empresa_id}/classificacao/csv")
def importar_classificacao(empresa_id: int, arquivo: UploadFile = File(...)):
    dados = ler_classificacoes(arquivo.file.read().decode("utf-8-sig"))
    with db.sessao() as s:
        e = _carregar(s, empresa_id)
        for chave, (cst, cct) in dados.items():
            servicos.salvar_classificacao(s, e, chave, cst, cct)
    _avisar(empresa_id, f"{len(dados)} classificação(ões) importada(s) do CSV (sem validação; salve na tela para validar).")
    return _ir(empresa_id, "/classificacao")


# --- Análise -----------------------------------------------------------------------------------------------------

def _periodo(inicio: str | None, fim: str | None) -> tuple[date | None, date | None]:
    try:
        return (date.fromisoformat(inicio) if inicio else None, date.fromisoformat(fim) if fim else None)
    except ValueError:
        raise HTTPException(422, "Data inválida (use AAAA-MM-DD)")


def _comparar_simples(empresa_id: int, e: models.Empresa, a, inicio: date | None, fim: date | None):
    if e.regime != "simples" or not a.itens:
        return None
    with db.sessao() as s:
        return servicos.comparar_simples(s, s.get(models.Empresa, empresa_id), calculadora, a, inicio, fim)


def _analise(empresa_id: int, inicio: date | None, fim: date | None):
    with db.sessao() as s:
        e = _carregar(s, empresa_id)
        return e, servicos.montar_analise(s, e, calculadora, inicio, fim)


@app.get("/empresas/{empresa_id}/analise", response_class=HTMLResponse)
async def painel(request: Request, empresa_id: int, inicio: str | None = None, fim: str | None = None,
                 anos: str = "2027,2029,2033"):
    di, df = _periodo(inicio, fim)
    e, a = await run_in_threadpool(_analise, empresa_id, di, df)
    destaque = [int(x) for x in anos.split(",") if x.strip().isdigit() and int(x) in a.premissas.anos] or a.premissas.anos[-1:]
    resumo = agregacao.resumo(a)
    simples = await run_in_threadpool(_comparar_simples, empresa_id, e, a, di, df)
    return _render(request, "painel.html", e=e, a=a, aba="analise", resumo=resumo, simples=simples,
                   grafico=linha_do_tempo(resumo) if a.itens else None, anos=destaque,
                   produtos=agregacao.por_produto(a)[:100], fornecedores=agregacao.por_fornecedor(a)[:100],
                   inicio=inicio or "", fim=fim or "", anos_texto=",".join(map(str, destaque)))


@app.get("/empresas/{empresa_id}/analise/excel")
async def baixar_excel(empresa_id: int, inicio: str | None = None, fim: str | None = None):
    di, df = _periodo(inicio, fim)
    e, a = await run_in_threadpool(_analise, empresa_id, di, df)
    simples = await run_in_threadpool(_comparar_simples, empresa_id, e, a, di, df)
    buffer = io.BytesIO()
    exportar(a, buffer, {"Simples x regular": simples} if simples else None)
    return Response(buffer.getvalue(),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="reforma_{e.cnpj}.xlsx"'})


# --- Apuração assistida (API da Receita) -------------------------------------------------------------------------

@app.get("/empresas/{empresa_id}/apuracao", response_class=HTMLResponse)
def tela_apuracao(request: Request, empresa_id: int, pa_inicio: str = "", pa_fim: str = ""):
    with db.sessao() as s:
        e = _carregar(s, empresa_id)
        solicitacoes = list(s.scalars(select(models.SolicitacaoApuracao)
                                      .where(models.SolicitacaoApuracao.empresa_id == e.id)
                                      .order_by(models.SolicitacaoApuracao.criado_em.desc()).limit(20)))
        conc = conciliacao.conciliar(s, e, pa_inicio or None, pa_fim or None)
        return _render(request, "apuracao.html", e=e, aba="apuracao", credencial=apuracao.credencial(s, e),
                       solicitacoes=solicitacoes,
                       uso={t: apuracao.solicitacoes_hoje(s, e, t) for t in ("debitos", "creditos")},
                       limite=settings.apuracao_limite_diario, url_publica=settings.url_publica,
                       resumo=conc.resumo(), linhas=conc.linhas[:500], total_linhas=len(conc.linhas),
                       pa_inicio=pa_inicio, pa_fim=pa_fim, avisos=AVISOS.pop(e.id, []))


@app.post("/empresas/{empresa_id}/apuracao/credencial")
def salvar_credencial_apuracao(empresa_id: int, client_id: str = Form(...), client_secret: str = Form(...),
                               ambiente: str = Form("prr")):
    with db.sessao() as s:
        try:
            apuracao.salvar_credencial(s, _carregar(s, empresa_id), client_id, client_secret, ambiente)
            _avisar(empresa_id, "Credencial da API de apuração gravada (cifrada).")
        except ValueError as erro:
            _avisar(empresa_id, str(erro))
    return _ir(empresa_id, "/apuracao")


@app.post("/empresas/{empresa_id}/apuracao/solicitar")
async def solicitar_apuracao(empresa_id: int, tipo: str = Form(...)):
    def executar():
        with db.sessao() as s:
            sol = apuracao.solicitar(s, _carregar(s, empresa_id), tipo)
            return sol.estado, sol.mensagem
    try:
        estado, mensagem = await run_in_threadpool(executar)
        _avisar(empresa_id, f"Solicitação de {tipo}: {estado}. {mensagem}".strip())
    except (ErroApuracao, ValueError) as erro:
        _avisar(empresa_id, str(erro))
    return _ir(empresa_id, "/apuracao")


@app.post("/empresas/{empresa_id}/apuracao/verificar")
async def verificar_apuracao(empresa_id: int):
    def executar():
        with db.sessao() as s:
            return apuracao.verificar_pendentes(s, _carregar(s, empresa_id))
    try:
        _avisar(empresa_id, *await run_in_threadpool(executar))
    except ErroApuracao as erro:
        _avisar(empresa_id, str(erro))
    return _ir(empresa_id, "/apuracao")


@app.post("/empresas/{empresa_id}/apuracao/importar")
def importar_apuracao(empresa_id: int, arquivos: list[UploadFile] = File(...)):
    with db.sessao() as s:
        e = _carregar(s, empresa_id)
        for f in arquivos:
            try:
                n = apuracao.processar_arquivo(s, e, json.loads(f.file.read().decode("utf-8-sig")))
                _avisar(empresa_id, f"{f.filename}: {n} registro(s) importado(s).")
            except (ValueError, KeyError, AttributeError) as erro:
                _avisar(empresa_id, f"{f.filename}: arquivo não reconhecido ({erro}).")
    return _ir(empresa_id, "/apuracao")


@app.head("/webhooks/apuracao-cbs/{token}")
def webhook_validacao(token: str):
    """A Receita valida a urlRetorno com HEAD antes de aceitar a solicitação."""
    with db.sessao() as s:
        existe = s.scalar(select(models.SolicitacaoApuracao.id).where(models.SolicitacaoApuracao.token_webhook == token))
    return Response(status_code=200 if existe else 404)


@app.post("/webhooks/apuracao-cbs/{token}")
async def webhook_apuracao(token: str, request: Request):
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(400, "JSON inválido")
    if not isinstance(payload, dict):
        raise HTTPException(400, "JSON inválido")

    def executar():
        with db.sessao() as s:
            return apuracao.receber_webhook(s, token, payload)
    try:
        aceito = await run_in_threadpool(executar)
    except ErroApuracao:
        log.exception("Webhook de apuração: falha ao processar")
        raise HTTPException(503, "Falha temporária")  # não-2xx: a Receita tenta de novo
    if not aceito:
        raise HTTPException(404)
    return {"recebido": True}


@app.get("/saude")
def saude():
    try:
        return {"ok": True, "calculadora": calculadora.versao(), "agendador_dfe": settings.dfe_agendador}
    except ErroCalculadora as erro:
        return {"ok": False, "erro": str(erro)}

