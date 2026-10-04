"""Operações de negócio sobre o banco: empresas, certificados, importação de XML e montagem da análise."""
from __future__ import annotations

import gzip
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.calculadora.client import CalculadoraRTC
from app.dfe.certificado import CertificadoA1, CertificadoInvalido
from app.engine.cenarios import Analise, Empresa, analisar
from app.engine.premissas import Premissas
from app.ingest.arquivos import iterar_xmls
from app.parser.nfe import Documento, ler_nfe
from app.seguranca import cifrar, decifrar

CAMPOS_PREMISSAS = ["cbs_referencia", "ibs_uf_referencia", "ibs_mun_referencia", "credito_fornecedor_simples_pct",
                    "credito_fornecedor_mei_pct", "credito_presumido_nao_contribuinte_pct", "repasse_is_revendedor_pct", "aliquota_das_pct", "das_pis_cofins_pct", "das_icms_iss_pct"]


def so_digitos(s: str) -> str:
    return "".join(c for c in s if c.isdigit())


# --- Empresas ----------------------------------------------------------------------------------------------------

def criar_empresa(s: Session, cnpj: str, nome: str, regime: str, uf: str, cod_municipio: int,
                  industria: bool = False) -> models.Empresa:
    cnpj = so_digitos(cnpj)
    if len(cnpj) != 14:
        raise ValueError("CNPJ deve ter 14 dígitos.")
    if regime not in ("real", "presumido", "simples"):
        raise ValueError("Regime inválido.")
    if s.scalar(select(models.Empresa).where(models.Empresa.cnpj == cnpj)):
        raise ValueError(f"Empresa {cnpj} já cadastrada.")
    e = models.Empresa(cnpj=cnpj, nome=nome or cnpj, regime=regime, uf=uf.upper(), cod_municipio=cod_municipio,
                       industria=industria, premissas={})
    s.add(e)
    s.flush()
    return e


def premissas_da_empresa(e: models.Empresa, anos: list[int] | None = None) -> Premissas:
    p = Premissas(industria=e.industria)
    for campo in CAMPOS_PREMISSAS:
        if campo in (e.premissas or {}):
            setattr(p, campo, Decimal(str(e.premissas[campo])))
    if anos:
        p.anos = anos
    return p


def atualizar_premissas(e: models.Empresa, valores: dict[str, str]):
    novas = dict(e.premissas or {})
    for campo in CAMPOS_PREMISSAS:
        if valores.get(campo) not in (None, ""):
            novas[campo] = str(Decimal(valores[campo].replace(",", ".")))
    e.premissas = novas


def empresa_do_motor(e: models.Empresa) -> Empresa:
    return Empresa(e.cnpj, e.nome, e.regime, e.uf, e.cod_municipio)


# --- Certificado -------------------------------------------------------------------------------------------------

def salvar_certificado(s: Session, e: models.Empresa, pfx: bytes, senha: str) -> CertificadoA1:
    cert = CertificadoA1.carregar(pfx, senha)
    if cert.vencido:
        raise CertificadoInvalido(f"Certificado vencido em {cert.validade:%d/%m/%Y}.")
    if cert.cnpj and cert.cnpj[:8] != e.cnpj[:8]:
        raise CertificadoInvalido(f"O certificado é do CNPJ {cert.cnpj}, de outra raiz que não a da empresa {e.cnpj}.")
    if e.certificado:
        s.delete(e.certificado)
        s.flush()
    e.certificado = models.Certificado(pfx_cifrado=cifrar(pfx), senha_cifrada=cifrar(senha.encode()),
                                       titular=cert.titular[:300], cnpj_certificado=cert.cnpj,
                                       validade=cert.validade.replace(tzinfo=None))
    return cert


def carregar_certificado(e: models.Empresa) -> CertificadoA1:
    if not e.certificado:
        raise CertificadoInvalido("Empresa sem certificado A1 cadastrado.")
    return CertificadoA1.carregar(decifrar(e.certificado.pfx_cifrado), decifrar(e.certificado.senha_cifrada).decode())


# --- Documentos --------------------------------------------------------------------------------------------------

@dataclass
class ResultadoImportacao:
    novos: int = 0
    atualizados: int = 0
    ja_existentes: int = 0
    sem_participacao: int = 0
    rejeitados: list[str] = field(default_factory=list)


def gravar_documento(s: Session, e: models.Empresa, doc: Documento, xml: bytes, origem: str,
                     nsu: str | None = None) -> str:
    """Grava/atualiza uma NF-e completa. Retorna 'novo', 'atualizado', 'existente' ou 'sem_participacao'."""
    direcao = doc.direcao_para(e.cnpj)
    if direcao is None:
        return "sem_participacao"
    participante = doc.contraparte_para(e.cnpj)
    existente = s.scalar(select(models.DocumentoFiscal).where(
        models.DocumentoFiscal.empresa_id == e.id, models.DocumentoFiscal.chave == doc.chave))
    if existente and existente.situacao != "resumo":
        return "existente"
    reg = existente or models.DocumentoFiscal(empresa_id=e.id, chave=doc.chave)
    reg.origem = origem if not existente else existente.origem
    reg.situacao = "completo"
    reg.direcao = direcao
    reg.nsu = nsu or reg.nsu
    reg.emissao = doc.emissao.replace(tzinfo=None) if doc.emissao != datetime.min else None
    reg.participante_cnpj = participante.cnpj if participante else ""
    reg.participante_nome = (participante.nome if participante else "")[:200]
    reg.valor = sum((i.valor_operacao + i.tributos_por_fora for i in doc.itens), Decimal(0))
    reg.xml_gz = gzip.compress(xml)
    if not existente:
        s.add(reg)
    return "atualizado" if existente else "novo"


def importar_arquivos(s: Session, e: models.Empresa, arquivos: list[tuple[str, bytes]]) -> ResultadoImportacao:
    res = ResultadoImportacao()
    for nome, xml in iterar_xmls(arquivos):
        try:
            doc = ler_nfe(xml, arquivo=nome)
        except ValueError as erro:
            res.rejeitados.append(str(erro))
            continue
        situacao = gravar_documento(s, e, doc, xml, "upload")
        if situacao == "novo":
            res.novos += 1
        elif situacao == "atualizado":
            res.atualizados += 1
        elif situacao == "existente":
            res.ja_existentes += 1
        else:
            res.sem_participacao += 1
        s.flush()
    return res


def documentos(s: Session, e: models.Empresa, inicio: date | None = None, fim: date | None = None) -> list[Documento]:
    q = select(models.DocumentoFiscal).where(models.DocumentoFiscal.empresa_id == e.id,
                                             models.DocumentoFiscal.situacao == "completo")
    if inicio:
        q = q.where(models.DocumentoFiscal.emissao >= datetime.combine(inicio, time.min))
    if fim:
        q = q.where(models.DocumentoFiscal.emissao <= datetime.combine(fim, time.max))
    return [ler_nfe(gzip.decompress(d.xml_gz), arquivo=d.chave) for d in s.scalars(q.order_by(models.DocumentoFiscal.emissao))]


def classificacoes(s: Session, e: models.Empresa) -> dict[str, tuple[str, str]]:
    return {c.chave: (c.cst, c.cclasstrib) for c in
            s.scalars(select(models.ClassificacaoProduto).where(models.ClassificacaoProduto.empresa_id == e.id))}


def formas_da_chave(chave: str) -> list[str]:
    """Chave com prefixo e, para código/NCM, a forma antiga sem prefixo (ajustes gravados antes do EAN)."""
    tipo, sep, valor = chave.partition(":")
    return [chave, valor] if sep and tipo in ("cod", "ncm") else [chave]


def salvar_classificacao(s: Session, e: models.Empresa, chave: str, cst: str, cclasstrib: str,
                         validacao: str = "") -> models.ClassificacaoProduto | None:
    """Grava a classificação manual (chave ean:/cod:/ncm:); cst/cclasstrib vazios removem o ajuste."""
    existentes = list(s.scalars(select(models.ClassificacaoProduto).where(
        models.ClassificacaoProduto.empresa_id == e.id,
        models.ClassificacaoProduto.chave.in_(formas_da_chave(chave)))))
    reg = next((r for r in existentes if r.chave == chave), None)
    for antigo in existentes:
        if antigo is not reg:
            s.delete(antigo)
    if not cst.strip() and not cclasstrib.strip():
        if reg:
            s.delete(reg)
        return None
    if not (cst.strip().isdigit() and cclasstrib.strip().isdigit()):
        raise ValueError(f"{chave}: CST e cClassTrib devem ser numéricos.")
    reg = reg or models.ClassificacaoProduto(empresa_id=e.id, chave=chave)
    reg.cst, reg.cclasstrib, reg.validacao = cst.strip().zfill(3), cclasstrib.strip().zfill(6), validacao
    s.add(reg)
    return reg


def montar_analise(s: Session, e: models.Empresa, calculadora: CalculadoraRTC, inicio: date | None = None,
                   fim: date | None = None, anos: list[int] | None = None,
                   docs: list[Documento] | None = None, regime: str | None = None) -> Analise:
    """`regime` permite simular a mesma base em outro regime (ex.: Simples optando por CBS/IBS regular)."""
    empresa = empresa_do_motor(e)
    if regime:
        empresa.regime = regime
    return analisar(empresa, docs if docs is not None else documentos(s, e, inicio, fim),
                    premissas_da_empresa(e, anos), calculadora, classificacoes(s, e))


def comparar_simples(s: Session, e: models.Empresa, calculadora: CalculadoraRTC, a_simples: Analise,
                     inicio: date | None = None, fim: date | None = None) -> list[dict]:
    from app.engine import simples

    a_regular = montar_analise(s, e, calculadora, inicio, fim, a_simples.premissas.anos,
                               docs=list({r.documento.chave: r.documento for r in a_simples.itens}.values()),
                               regime="presumido")
    return simples.comparar(a_simples, a_regular)

