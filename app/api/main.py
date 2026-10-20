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
import threading
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, func, select

from app import classificacao as classif
from app import cobertura
from app import db, models, servicos
from app.engine.cenarios import anexos_por_ncm, consenso_fornecedores, declaracoes_fornecedores, xml_incompativeis
from app.api.grafico import linha_do_tempo
from app.apuracao import conciliacao
from app.apuracao import servico as apuracao
from app.apuracao.cliente import ErroApuracao
from app.calculadora.client import CalculadoraIndisponivel, CalculadoraRTC, ErroCalculadora
from app.config import settings
from app.dfe.certificado import CertificadoInvalido
from app.dfe.sincronizar import sincronizar, sincronizar_pendentes
from app.ingest.arquivos import ler_classificacoes
from app.relatorios import agregacao, negociacao
from app.relatorios.excel import exportar, exportar_negociacao

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


def _garantir_calculadora_local():
    """Se o sistema aponta para a calculadora local instalada e ela está parada, inicia."""
    from app.calculadora import offline

    if settings.calculadora_url.rstrip("/") != offline.url_local() or not offline.estado().get("versao_dir"):
        return
    try:
        offline.iniciar_atual()
    except offline.ErroOffline:
        log.exception("Não foi possível iniciar a calculadora local")


@asynccontextmanager
async def ciclo_de_vida(_app: FastAPI):
    db.criar_tabelas()
    await asyncio.to_thread(_garantir_calculadora_local)
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
    e = ctx.get("e")
    if isinstance(e, models.Empresa) and "grupo" not in ctx:
        # Nas telas do estabelecimento o base.html oferece o link da visão consolidada quando a pessoa
        # jurídica (raiz do CNPJ) tem mais de um estabelecimento cadastrado.
        raiz = servicos.raiz_cnpj(e.cnpj)
        with db.sessao() as s:
            n = len(servicos.estabelecimentos_da_raiz(s, raiz))
        if n > 1:
            ctx["grupo_da_empresa"] = {"raiz": raiz, "n": n}
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
    por_raiz: dict[str, list[dict]] = {}
    for linha in linhas:
        por_raiz.setdefault(servicos.raiz_cnpj(linha["e"].cnpj), []).append(linha)
    grupos = sorted(({"raiz": raiz, "nome": min(ls, key=lambda x: x["e"].cnpj[8:12])["e"].nome,  # nome da matriz
                      "linhas": ls, "varios": len(ls) > 1} for raiz, ls in por_raiz.items()),
                    key=lambda g: g["nome"])
    return _render(request, "inicio.html", empresas=linhas, grupos=grupos, calc=calculadora.identificacao(),
                   calc_local=settings.calculadora_url.startswith("http://localhost"))


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
        cob = cobertura.levantar(s, [e])[0]
        return _render(request, "empresa.html", e=e, premissas=servicos.premissas_da_empresa(e),
                       situacoes=situacoes, por_origem=por_origem, cobertura=cob,
                       alertas_cobertura=cobertura.alertas([cob]), avisos=AVISOS.pop(e.id, []), aba="geral")


@app.post("/empresas/{empresa_id}/dados")
def salvar_dados(empresa_id: int, regime: str = Form(...), uf: str = Form(...), municipio: int = Form(...),
                 industria: bool = Form(False), manifestar_ciencia: bool = Form(False), ambiente_dfe: int = Form(1),
                 cbs_referencia: str = Form(""), ibs_uf_referencia: str = Form(""), ibs_mun_referencia: str = Form(""),
                 credito_fornecedor_simples_pct: str = Form(""), credito_fornecedor_mei_pct: str = Form(""),
                 credito_presumido_nao_contribuinte_pct: str = Form(""), repasse_is_revendedor_pct: str = Form(""),
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
                credito_fornecedor_mei_pct=credito_fornecedor_mei_pct,
                credito_presumido_nao_contribuinte_pct=credito_presumido_nao_contribuinte_pct,
                repasse_is_revendedor_pct=repasse_is_revendedor_pct,
                aliquota_das_pct=aliquota_das_pct,
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

def _chave_do_ajuste(escopo: str, gtin: str, codigo: str, ncm: str) -> str:
    if escopo == "ncm":
        return f"ncm:{ncm}"
    if escopo == "ean" and gtin:
        return f"ean:{gtin}"
    return f"cod:{codigo}"


# Base da tela de classificação só da última empresa aberta: os documentos ocupam muita memória.
_BASE_CLASSIFICACAO: dict[int, tuple] = {}


def _base_classificacao(s, e: models.Empresa):
    """Documentos e as peças da classificação do motor: anexos por NCM, pares do XML recusados e o que os
    fornecedores declaram por produto. Sem a calculadora a tela abre sem anexo e sem a checagem dos pares.
    Fica guardada enquanto as notas não mudam (ler os XMLs e consultar os anexos leva quase todo o tempo da tela):
    paginar, filtrar e salvar ajustes não relê as notas."""
    with _trava(("classificacao", e.id)):
        assinatura = _assinatura_das_notas(s, e)
        guardado = _BASE_CLASSIFICACAO.get(e.id)
        if guardado and guardado[0] == assinatura:
            return guardado[1]
        docs = servicos.documentos(s, e)
        try:
            anexos = anexos_por_ncm(calculadora, {it.ncm for d in docs for it in d.itens})
            incompativeis = xml_incompativeis(calculadora, {(it.ibscbs.cclasstrib, it.ncm) for d in docs
                                                             for it in d.itens if it.ibscbs and it.ibscbs.cclasstrib})
            completa = True
        except ErroCalculadora:
            anexos, incompativeis, completa = {}, set(), False
        entradas = (it for d in docs if d.direcao_para(e.cnpj) == "entrada" for it in d.itens)
        base = docs, anexos, incompativeis, declaracoes_fornecedores(entradas, incompativeis)
        _BASE_CLASSIFICACAO.clear()
        if completa:          # sem a calculadora não guarda: a próxima abertura tenta de novo
            _BASE_CLASSIFICACAO[e.id] = (assinatura, base)
        return base


def _produtos_classificacao(s, e: models.Empresa, ajustes: dict):
    """Produtos para revisão com a mesma classificação que o motor usa, e a lista de conflitos."""
    docs, anexos, incompativeis, declaracoes = _base_classificacao(s, e)
    premissas = servicos.premissas_da_empresa(e)
    aliquota = Decimal(str(premissas.cbs_referencia + premissas.ibs_uf_referencia + premissas.ibs_mun_referencia))
    produtos = classif.produtos(docs, e.cnpj, ajustes, anexos, incompativeis, consenso_fornecedores(declaracoes))
    return produtos, classif.conflitos(docs, e.cnpj, ajustes, anexos, declaracoes, aliquota)


PRODUTOS_POR_PAGINA = 100   # a tela inteira de uma loja grande passava de 15 mil linhas e travava o navegador


def _paginar(lista: list, pagina: int) -> dict:
    """Fatia da página pedida e os dados do rodapé de paginação (templates/_paginacao.html)."""
    paginas = max(1, -(-len(lista) // PRODUTOS_POR_PAGINA))
    pagina = min(max(pagina, 1), paginas)
    itens = lista[(pagina - 1) * PRODUTOS_POR_PAGINA:pagina * PRODUTOS_POR_PAGINA]
    return {"itens": itens, "pagina": pagina, "paginas": paginas, "filtrados": len(lista),
            "primeiro": (pagina - 1) * PRODUTOS_POR_PAGINA + 1, "ultimo": (pagina - 1) * PRODUTOS_POR_PAGINA + len(itens)}


def _bate_busca(busca: str, *campos) -> bool:
    return not busca or busca in " ".join(c for c in campos if c).lower()


@app.get("/empresas/{empresa_id}/classificacao", response_class=HTMLResponse)
def tela_classificacao(request: Request, empresa_id: int, q: str = "", padrao: bool = False, pagina: int = 1):
    with db.sessao() as s:
        e = _carregar(s, empresa_id)
        registros = {c.chave: c for c in s.scalars(select(models.ClassificacaoProduto)
                                                   .where(models.ClassificacaoProduto.empresa_id == e.id))}
        todos, conflitos = _produtos_classificacao(s, e, {k: (v.cst, v.cclasstrib) for k, v in registros.items()})
    busca = q.strip().lower()
    pag = _paginar([p for p in todos if (not padrao or p.origem == "padrao")
                    and _bate_busca(busca, p.gtin, *p.codigos, p.descricao, *p.ncms)], pagina)
    produtos = pag["itens"]
    validacoes = {}
    for p in produtos:
        reg = None
        if p.chave_manual:
            reg = next((registros[k] for k in servicos.formas_da_chave(p.chave_manual) if k in registros), None)
        validacoes[p.chave] = reg.validacao if reg else None
    try:
        situacoes = calculadora.situacoes_tributarias()
    except ErroCalculadora:
        situacoes = {}
    return _render(request, "classificacao.html", e=e, produtos=produtos, validacoes=validacoes,
                   situacoes=situacoes, sugestoes_ean=sum(1 for p in todos if p.sugestao_por_ean),
                   conflitos=conflitos, total_produtos=len(todos), pag=pag, q=q, padrao=padrao,
                   consulta=urlencode({"q": q, **({"padrao": 1} if padrao else {})}),
                   avisos=AVISOS.pop(e.id, []), aba="classificacao")


@app.post("/empresas/{empresa_id}/classificacao")
async def salvar_classificacoes(request: Request, empresa_id: int):
    form = await request.form()
    linhas = list(zip(form.getlist("gtin"), form.getlist("codigo"), form.getlist("ncm"), form.getlist("escopo"),
                      form.getlist("cst"), form.getlist("cclasstrib"), form.getlist("chave_atual")))

    def executar():
        salvos, problemas = 0, []
        with db.sessao() as s:
            e = _carregar(s, empresa_id)
            for gtin, codigo, ncm, escopo, cst, cct, chave_atual in linhas:
                chave = _chave_do_ajuste(escopo, gtin, codigo, ncm)
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
    return _ir(empresa_id, "/classificacao" + (f"?{request.url.query}" if request.url.query else ""))


@app.post("/empresas/{empresa_id}/classificacao/sugestoes-ean")
async def aplicar_sugestoes_ean(empresa_id: int):
    """Aceita, de uma vez, as sugestões vindas do cClassTrib que o fornecedor informou para o mesmo EAN."""
    def executar():
        aplicadas, problemas = 0, []
        with db.sessao() as s:
            e = _carregar(s, empresa_id)
            docs = _base_classificacao(s, e)[0]
            for p in classif.produtos(docs, e.cnpj, servicos.classificacoes(s, e)):
                if p.sugestao_por_ean and p.origem != "manual":
                    cst, cct = p.sugestao
                    motivo = classif.validar(calculadora, p.ncm, cst, cct)
                    servicos.salvar_classificacao(s, e, p.chave, cst, cct, motivo)
                    aplicadas += 1
                    if motivo:
                        problemas.append(f"EAN {p.gtin}: {motivo}")
        return aplicadas, problemas

    aplicadas, problemas = await run_in_threadpool(executar)
    _avisar(empresa_id, f"{aplicadas} sugestão(ões) por EAN aplicada(s) e validada(s).", *problemas)
    return _ir(empresa_id, "/classificacao")


@app.get("/empresas/{empresa_id}/classificacao.csv")
def exportar_classificacao(empresa_id: int):
    with db.sessao() as s:
        e = _carregar(s, empresa_id)
        produtos, _ = _produtos_classificacao(s, e, servicos.classificacoes(s, e))
    saida = io.StringIO()
    w = csv.writer(saida, delimiter=";")
    w.writerow(["chave", "cst", "cclasstrib", "descricao", "ean", "codigos", "ncm", "operacoes", "origem_atual",
                "em_uso", "sugestao"])
    for p in produtos:
        manual = p.origem == "manual"
        w.writerow([p.chave_manual or p.chave, p.cst if manual else "", p.cclasstrib if manual else "", p.descricao,
                    p.gtin, "|".join(p.codigos), "|".join(p.ncms), p.lados, p.origem, f"{p.cst}/{p.cclasstrib}",
                    "/".join(p.sugestao) if p.sugestao else ""])
    return Response(saida.getvalue().encode("utf-8-sig"), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="classificacao_{e.cnpj}.csv"'})


@app.get("/empresas/{empresa_id}/classificacao/conflitos.csv")
def exportar_conflitos(empresa_id: int):
    """Produtos em que a classificação usada diverge do anexo do NCM ou entre fornecedores, para o tributário revisar."""
    with db.sessao() as s:
        e = _carregar(s, empresa_id)
        _, conflitos = _produtos_classificacao(s, e, servicos.classificacoes(s, e))
    saida = io.StringIO()
    w = csv.writer(saida, delimiter=";")
    w.writerow(["chave", "ean", "descricao", "ncm", "tipo", "em_uso", "alternativa", "participacao_em_uso_pct",
                "valor_compra", "valor_venda_liquido", "cbs_ibs_a_mais_nas_vendas_2033"])
    for c in conflitos:
        w.writerow([c.chave, c.gtin, c.descricao, c.ncm, c.tipo, "/".join(c.em_uso), "/".join(c.alternativa),
                    str(c.participacao_pct).replace(".", ","), str(c.valor_compra).replace(".", ","),
                    str(c.valor_venda).replace(".", ","),
                    "" if c.impacto_vendas is None else str(c.impacto_vendas).replace(".", ",")])
    return Response(saida.getvalue().encode("utf-8-sig"), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="conflitos_{e.cnpj}.csv"'})


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


# Análises prontas, da usada há mais tempo para a mais recente. Cada uma ocupa centenas de MB: guardar uma por
# filtro de data, sem limite, esgotaria a memória da máquina.
_CACHE_ANALISES: OrderedDict[tuple, tuple] = OrderedDict()
_MAX_ANALISES_EM_CACHE = 4
_TRAVA_CACHE = threading.Lock()


def _do_cache(chave: tuple, assinatura: tuple):
    with _TRAVA_CACHE:
        guardado = _CACHE_ANALISES.get(chave)
        if not guardado or guardado[0] != assinatura:
            return None
        _CACHE_ANALISES.move_to_end(chave)
        return guardado[1]


def _guardar(chave: tuple, assinatura: tuple, analise):
    with _TRAVA_CACHE:
        _CACHE_ANALISES[chave] = (assinatura, analise)
        _CACHE_ANALISES.move_to_end(chave)
        while len(_CACHE_ANALISES) > _MAX_ANALISES_EM_CACHE:
            _CACHE_ANALISES.popitem(last=False)


_TRAVAS: dict[tuple, threading.Lock] = {}
_TRAVA_TRAVAS = threading.Lock()


def _trava(chave: tuple) -> threading.Lock:
    """Uma análise por vez para cada chave: cliques repetidos esperam o cálculo em andamento e pegam o resultado do
    cache, em vez de disparar em paralelo o mesmo cálculo de minutos."""
    with _TRAVA_TRAVAS:
        return _TRAVAS.setdefault(chave, threading.Lock())


def _assinatura_das_notas(s, e: models.Empresa) -> tuple:
    """Muda quando entram notas, NFC-e consolidadas ou SPED, ou quando muda a calculadora ou a sua base de regras."""
    docs = s.execute(select(func.count(), func.max(models.DocumentoFiscal.id),
                            func.sum(case((models.DocumentoFiscal.situacao == "completo", 1), else_=0)))
                     .where(models.DocumentoFiscal.empresa_id == e.id)).one()
    nfce = s.execute(select(func.count(), func.max(models.ConsolidadoNFCe.atualizado_em), func.sum(
        models.ConsolidadoNFCe.cupons)).where(models.ConsolidadoNFCe.empresa_id == e.id)).one()
    sped = s.execute(select(func.count(), func.max(models.EscrituracaoArquivo.importado_em))
                     .where(models.EscrituracaoArquivo.empresa_id == e.id)).one()
    return (tuple(docs), tuple(nfce), tuple(sped), settings.calculadora_url,
            calculadora.identificacao().get("versao_base"))  # nova base de regras (ex.: alíquotas publicadas)


def _assinatura_dos_dados(s, e: models.Empresa) -> tuple:
    """Muda sempre que notas, classificações ou premissas mudam — invalida o cache da análise."""
    classes = s.execute(select(func.count(), func.max(models.ClassificacaoProduto.atualizado_em))
                        .where(models.ClassificacaoProduto.empresa_id == e.id)).one()
    return (_assinatura_das_notas(s, e), tuple(classes), json.dumps(e.premissas or {}, sort_keys=True), e.regime,
            e.industria, e.uf, e.cod_municipio)


def _analise(empresa_id: int, inicio: date | None, fim: date | None):
    try:
        return _analise_sem_tratamento(empresa_id, inicio, fim)
    except CalculadoraIndisponivel:
        _garantir_calculadora_local()          # tenta subir a calculadora local uma vez
        try:
            return _analise_sem_tratamento(empresa_id, inicio, fim)
        except CalculadoraIndisponivel as erro:
            raise HTTPException(503, f"{erro}. A análise não foi feita para não calcular sem a calculadora oficial. "
                                     f"Inicie-a com: python -m app.cli calculadora executar") from erro


def _analise_sem_tratamento(empresa_id: int, inicio: date | None, fim: date | None):
    with db.sessao() as s:
        e = _carregar(s, empresa_id)
        chave = (empresa_id, inicio, fim)
        with _trava(chave):
            assinatura = _assinatura_dos_dados(s, e)
            a = _do_cache(chave, assinatura)
            if a is None:
                a = servicos.montar_analise(s, e, calculadora, inicio, fim)
                _guardar(chave, assinatura, a)
            return e, a


def _ano(ano: int | None, a) -> int:
    return ano if ano in a.premissas.anos else a.premissas.anos[0]


# --- Visão consolidada da pessoa jurídica (raiz do CNPJ) -----------------------------------------------------------

class _Grupo:
    """Fachada da pessoa jurídica para os templates: dados da matriz e a lista de estabelecimentos. Sem `id` —
    as telas analíticas montam os links com `base`, e o que é por estabelecimento (classificação) fica de fora."""

    def __init__(self, empresas: list[models.Empresa]):
        matriz = empresas[0]
        self.raiz = servicos.raiz_cnpj(matriz.cnpj)
        self.nome, self.regime, self.uf = matriz.nome, matriz.regime, matriz.uf
        self.estabelecimentos = empresas
        self.cnpj = f"raiz {self.raiz} · {len(empresas)} estabelecimentos"


def _analise_grupo(raiz: str, inicio: date | None, fim: date | None):
    try:
        return _analise_grupo_sem_tratamento(raiz, inicio, fim)
    except CalculadoraIndisponivel:
        _garantir_calculadora_local()          # tenta subir a calculadora local uma vez
        try:
            return _analise_grupo_sem_tratamento(raiz, inicio, fim)
        except CalculadoraIndisponivel as erro:
            raise HTTPException(503, f"{erro}. A análise não foi feita para não calcular sem a calculadora oficial. "
                                     f"Inicie-a com: python -m app.cli calculadora executar") from erro


def _analise_grupo_sem_tratamento(raiz: str, inicio: date | None, fim: date | None):
    with db.sessao() as s:
        empresas = servicos.estabelecimentos_da_raiz(s, raiz)
        if not empresas:
            raise HTTPException(404, "Nenhuma empresa com essa raiz de CNPJ")
        chave = ("grupo", raiz, inicio, fim)
        with _trava(chave):
            assinatura = tuple(_assinatura_dos_dados(s, e) for e in empresas)
            a = _do_cache(chave, assinatura)
            if a is None:
                a = servicos.montar_analise_grupo(s, empresas, calculadora, inicio, fim)
                _guardar(chave, assinatura, a)
            return _Grupo(empresas), a


# --- Fornecedores e produtos (negociação) --------------------------------------------------------------------------

def _pagina_fornecedores(request: Request, e, a, base: str, ano: int | None, inicio: str | None, fim: str | None,
                         regime: str, **extra):
    """Corpo comum da tela de fornecedores: `e` é a empresa ou a fachada do grupo e `base` o prefixo dos links."""
    ano = _ano(ano, a)
    lista = negociacao.fornecedores(negociacao.compras(a, ano))
    if regime:
        lista = [f for f in lista if f.regime == regime]
    return _render(request, "fornecedores.html", e=e, base=base, a=a, aba="fornecedores", ano=ano, fornecedores=lista,
                   inicio=inicio or "", fim=fim or "", regime=regime,
                   total_atual=sum((f.custo_atual for f in lista), negociacao.ZERO),
                   total_ano=sum((f.custo_precos_hoje for f in lista), negociacao.ZERO), **extra)


@app.get("/empresas/{empresa_id}/fornecedores", response_class=HTMLResponse)
async def tela_fornecedores(request: Request, empresa_id: int, ano: int | None = None, inicio: str | None = None,
                            fim: str | None = None, regime: str = ""):
    di, df = _periodo(inicio, fim)
    e, a = await run_in_threadpool(_analise, empresa_id, di, df)
    return _pagina_fornecedores(request, e, a, f"/empresas/{empresa_id}", ano, inicio, fim, regime)


def _pagina_fornecedor(request: Request, e, a, base: str, cnpj: str, ano: int | None, inicio: str | None,
                       fim: str | None, **extra):
    ano = _ano(ano, a)
    todas = negociacao.compras(a, ano)
    do_fornecedor = sorted((c for c in todas if c.fornecedor == cnpj), key=lambda c: -c.variacao_custo)
    if not do_fornecedor:
        raise HTTPException(404, "Fornecedor sem compras no período")
    resumo = negociacao.fornecedores(do_fornecedor)[0]
    return _render(request, "fornecedor.html", e=e, base=base, a=a, aba="fornecedores", ano=ano, f=resumo,
                   compras=do_fornecedor, inicio=inicio or "", fim=fim or "", **extra)


@app.get("/empresas/{empresa_id}/fornecedores/{cnpj}", response_class=HTMLResponse)
async def tela_fornecedor(request: Request, empresa_id: int, cnpj: str, ano: int | None = None,
                          inicio: str | None = None, fim: str | None = None):
    di, df = _periodo(inicio, fim)
    e, a = await run_in_threadpool(_analise, empresa_id, di, df)
    return _pagina_fornecedor(request, e, a, f"/empresas/{empresa_id}", cnpj, ano, inicio, fim)


def _pagina_produtos(request: Request, e, a, base: str, ano: int | None, inicio: str | None, fim: str | None,
                     q: str = "", multi: bool = False, pagina: int = 1, **extra):
    ano = _ano(ano, a)
    lista = negociacao.produtos(a, negociacao.compras(a, ano), ano)
    busca = q.strip().lower()
    pag = _paginar([p for p in lista if (not multi or (p.fornecedores or 0) > 1)
                    and _bate_busca(busca, p.gtin, *p.codigos, p.descricao, p.ncm)], pagina)
    consulta = urlencode({"ano": ano, "inicio": inicio or "", "fim": fim or "", "q": q, **({"multi": 1} if multi else {})})
    return _render(request, "produtos.html", e=e, base=base, a=a, aba="produtos", ano=ano, produtos=pag["itens"],
                   pag=pag, total_produtos=len(lista), q=q, multi=multi, consulta=consulta,
                   inicio=inicio or "", fim=fim or "", **extra)


@app.get("/empresas/{empresa_id}/produtos", response_class=HTMLResponse)
async def tela_produtos(request: Request, empresa_id: int, ano: int | None = None, inicio: str | None = None,
                        fim: str | None = None, q: str = "", multi: bool = False, pagina: int = 1):
    di, df = _periodo(inicio, fim)
    e, a = await run_in_threadpool(_analise, empresa_id, di, df)
    return _pagina_produtos(request, e, a, f"/empresas/{empresa_id}", ano, inicio, fim, q, multi, pagina)


def _pagina_produto(request: Request, e, a, base: str, chave: str, ano: int | None, inicio: str | None,
                    fim: str | None, **extra):
    ano = _ano(ano, a)
    produto = next((p for p in negociacao.produtos(a, negociacao.compras(a, ano), ano) if p.chave == chave), None)
    if not produto:
        raise HTTPException(404, "Produto sem movimento no período")
    compras = sorted(produto.compras, key=lambda c: (c.unidade, c.custo_unit_precos_hoje or 0))
    maior = max((max(c.custo_unit_atual or 0, c.custo_unit_precos_hoje or 0) for c in compras), default=0) or 1
    return _render(request, "produto.html", e=e, base=base, a=a, aba="produtos", ano=ano, p=produto, compras=compras,
                   maior=maior, inicio=inicio or "", fim=fim or "", **extra)


@app.get("/empresas/{empresa_id}/produtos/{chave}", response_class=HTMLResponse)
async def tela_produto(request: Request, empresa_id: int, chave: str, ano: int | None = None,
                       inicio: str | None = None, fim: str | None = None):
    di, df = _periodo(inicio, fim)
    e, a = await run_in_threadpool(_analise, empresa_id, di, df)
    return _pagina_produto(request, e, a, f"/empresas/{empresa_id}", chave, ano, inicio, fim)


def _excel_negociacao(a, ano: int, nome_arquivo: str) -> Response:
    buffer = io.BytesIO()
    exportar_negociacao(negociacao.compras(a, ano), ano, buffer)
    return Response(buffer.getvalue(),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{nome_arquivo}"'})


@app.get("/empresas/{empresa_id}/negociacao.xlsx")
async def excel_negociacao(empresa_id: int, ano: int | None = None, inicio: str | None = None,
                           fim: str | None = None):
    di, df = _periodo(inicio, fim)
    e, a = await run_in_threadpool(_analise, empresa_id, di, df)
    ano = _ano(ano, a)
    return _excel_negociacao(a, ano, f"negociacao_{e.cnpj}_{ano}.xlsx")


def _pagina_painel(request: Request, e, a, base: str, anos: str, inicio: str | None, fim: str | None, simples,
                   **extra):
    destaque = [int(x) for x in anos.split(",") if x.strip().isdigit() and int(x) in a.premissas.anos] or a.premissas.anos[-1:]
    resumo = agregacao.resumo(a)
    return _render(request, "painel.html", e=e, base=base, a=a, aba="analise", resumo=resumo, simples=simples,
                   grafico=linha_do_tempo(resumo) if a.itens else None, anos=destaque,
                   produtos=agregacao.por_produto(a)[:100], fornecedores=agregacao.por_fornecedor(a)[:100],
                   inicio=inicio or "", fim=fim or "", anos_texto=",".join(map(str, destaque)), **extra)


@app.get("/empresas/{empresa_id}/analise", response_class=HTMLResponse)
async def painel(request: Request, empresa_id: int, inicio: str | None = None, fim: str | None = None,
                 anos: str = "2027,2029,2033"):
    di, df = _periodo(inicio, fim)
    e, a = await run_in_threadpool(_analise, empresa_id, di, df)
    simples = await run_in_threadpool(_comparar_simples, empresa_id, e, a, di, df)
    return _pagina_painel(request, e, a, f"/empresas/{empresa_id}", anos, inicio, fim, simples)


def _excel_analise(a, simples, nome_arquivo: str) -> Response:
    buffer = io.BytesIO()
    exportar(a, buffer, {"Simples x regular": simples} if simples else None)
    return Response(buffer.getvalue(),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{nome_arquivo}"'})


@app.get("/empresas/{empresa_id}/analise/excel")
async def baixar_excel(empresa_id: int, inicio: str | None = None, fim: str | None = None):
    di, df = _periodo(inicio, fim)
    e, a = await run_in_threadpool(_analise, empresa_id, di, df)
    simples = await run_in_threadpool(_comparar_simples, empresa_id, e, a, di, df)
    return _excel_analise(a, simples, f"reforma_{e.cnpj}.xlsx")


# --- Grupo: as mesmas telas analíticas sobre a análise consolidada da pessoa jurídica ------------------------------

@app.get("/grupos/{raiz}/analise", response_class=HTMLResponse)
async def painel_grupo(request: Request, raiz: str, inicio: str | None = None, fim: str | None = None,
                       anos: str = "2027,2029,2033"):
    di, df = _periodo(inicio, fim)
    g, a = await run_in_threadpool(_analise_grupo, raiz, di, df)
    # Sem comparação com o Simples: o regime é uma condição de cada estabelecimento, não do grupo.
    return _pagina_painel(request, g, a, f"/grupos/{raiz}", anos, inicio, fim, simples=None, grupo=g)


@app.get("/grupos/{raiz}/analise/excel")
async def baixar_excel_grupo(raiz: str, inicio: str | None = None, fim: str | None = None):
    di, df = _periodo(inicio, fim)
    _, a = await run_in_threadpool(_analise_grupo, raiz, di, df)
    return _excel_analise(a, None, f"reforma_grupo_{raiz}.xlsx")


@app.get("/grupos/{raiz}/fornecedores", response_class=HTMLResponse)
async def tela_fornecedores_grupo(request: Request, raiz: str, ano: int | None = None, inicio: str | None = None,
                                  fim: str | None = None, regime: str = ""):
    di, df = _periodo(inicio, fim)
    g, a = await run_in_threadpool(_analise_grupo, raiz, di, df)
    return _pagina_fornecedores(request, g, a, f"/grupos/{raiz}", ano, inicio, fim, regime, grupo=g)


@app.get("/grupos/{raiz}/fornecedores/{cnpj}", response_class=HTMLResponse)
async def tela_fornecedor_grupo(request: Request, raiz: str, cnpj: str, ano: int | None = None,
                                inicio: str | None = None, fim: str | None = None):
    di, df = _periodo(inicio, fim)
    g, a = await run_in_threadpool(_analise_grupo, raiz, di, df)
    return _pagina_fornecedor(request, g, a, f"/grupos/{raiz}", cnpj, ano, inicio, fim, grupo=g)


@app.get("/grupos/{raiz}/produtos", response_class=HTMLResponse)
async def tela_produtos_grupo(request: Request, raiz: str, ano: int | None = None, inicio: str | None = None,
                              fim: str | None = None, q: str = "", multi: bool = False, pagina: int = 1):
    di, df = _periodo(inicio, fim)
    g, a = await run_in_threadpool(_analise_grupo, raiz, di, df)
    return _pagina_produtos(request, g, a, f"/grupos/{raiz}", ano, inicio, fim, q, multi, pagina, grupo=g)


@app.get("/grupos/{raiz}/produtos/{chave}", response_class=HTMLResponse)
async def tela_produto_grupo(request: Request, raiz: str, chave: str, ano: int | None = None,
                             inicio: str | None = None, fim: str | None = None):
    di, df = _periodo(inicio, fim)
    g, a = await run_in_threadpool(_analise_grupo, raiz, di, df)
    return _pagina_produto(request, g, a, f"/grupos/{raiz}", chave, ano, inicio, fim, grupo=g)


@app.get("/grupos/{raiz}/negociacao.xlsx")
async def excel_negociacao_grupo(raiz: str, ano: int | None = None, inicio: str | None = None,
                                 fim: str | None = None):
    di, df = _periodo(inicio, fim)
    _, a = await run_in_threadpool(_analise_grupo, raiz, di, df)
    ano = _ano(ano, a)
    return _excel_negociacao(a, ano, f"negociacao_grupo_{raiz}_{ano}.xlsx")


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

