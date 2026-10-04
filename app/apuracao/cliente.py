"""Cliente da API de apuração da CBS (docs.receitafederal.gov.br/apuracao-cbs).

Fluxo assíncrono: POST /{debitos|creditos}/{cnpj8} com urlRetorno → tíquete; quando pronto, a Receita chama o
webhook (ou consulta-se GET /situacao/{tiquete}) com uma URL assinada, válida por 48 h, para baixar o JSON.
A URL assinada é um segredo temporário: nunca é gravada em banco nem em log.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from app.config import settings

TIPOS = ("debitos", "creditos")


class ErroApuracao(RuntimeError):
    pass


@dataclass
class Situacao:
    estado: str                # PENDENTE | EM_PROCESSAMENTO | CONCLUIDA | ERRO
    url_assinada: str = ""
    expira_em: str = ""
    erro: str = ""


def _erro(r: httpx.Response) -> str:
    try:
        corpo = r.json()
        return f"{corpo.get('codigoErro', r.status_code)}: {corpo.get('mensagemErro') or corpo.get('error_description') or r.text[:200]}"
    except ValueError:
        return f"HTTP {r.status_code}: {r.text[:200]}"


class ClienteApuracao:
    def __init__(self, client_id: str, client_secret: str, ambiente: str = "prr", http: httpx.Client | None = None):
        if ambiente not in ("prr", "pro"):
            raise ValueError("Ambiente deve ser 'prr' (produção restrita) ou 'pro' (produção).")
        self.client_id, self.client_secret = client_id, client_secret
        self.base = settings.apuracao_url_prr if ambiente == "prr" else settings.apuracao_url_pro
        self.http = http or httpx.Client(timeout=settings.apuracao_timeout)
        self._token: str | None = None
        self._expira = 0.0

    def token(self) -> str:
        if self._token and time.monotonic() < self._expira - 30:
            return self._token
        try:
            r = self.http.post(settings.apuracao_token_url, data={"grant_type": "client_credentials"},
                               auth=(self.client_id, self.client_secret))
        except httpx.HTTPError as e:
            raise ErroApuracao(f"Falha ao obter token: {e}") from e
        if r.status_code != 200:
            raise ErroApuracao(f"Token recusado — confira as credenciais ({_erro(r)})")
        corpo = r.json()
        self._token = corpo["access_token"]
        self._expira = time.monotonic() + float(corpo.get("expires_in", 3000))
        return self._token

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.token()}"}

    def solicitar(self, tipo: str, cnpj: str, url_retorno: str) -> tuple[str, int | None]:
        """Abre a solicitação; retorna (tíquete, tempo estimado em segundos)."""
        if tipo not in TIPOS:
            raise ValueError(f"Tipo inválido: {tipo}")
        try:
            r = self.http.post(f"{self.base}/{tipo}/{cnpj[:8]}", json={"urlRetorno": url_retorno},
                               headers=self._auth())
        except httpx.HTTPError as e:
            raise ErroApuracao(f"Falha de comunicação: {e}") from e
        if r.status_code not in (200, 201):
            raise ErroApuracao(_erro(r))
        corpo = r.json()
        tea = corpo.get("tEASegundos")
        return corpo["tiqueteSolicitacao"], int(tea) if str(tea or "").isdigit() else None

    def situacao(self, tiquete: str) -> Situacao:
        try:
            r = self.http.get(f"{self.base}/situacao/{tiquete}", headers={**self._auth(), "Accept": "application/json"})
        except httpx.HTTPError as e:
            raise ErroApuracao(f"Falha de comunicação: {e}") from e
        corpo = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200 and corpo.get("estado") != "ERRO":
            raise ErroApuracao(_erro(r))
        return Situacao(corpo.get("estado", ""), corpo.get("urlAssinada", ""), corpo.get("urlAssinadaExpiraEm", ""),
                        f"{corpo.get('codigoErro', '')}: {corpo.get('mensagemErro', '')}" if corpo.get("codigoErro") else "")

    def baixar(self, url_assinada: str) -> dict:
        """Baixa o JSON de retorno. URLs do próprio gateway da Receita levam o token; URLs pré-assinadas de storage
        não podem levar Authorization (a assinatura já está na URL)."""
        host = urlparse(url_assinada).hostname or ""
        if urlparse(url_assinada).scheme != "https":
            raise ErroApuracao("URL de download não é HTTPS — descartada.")
        headers = self._auth() if host == urlparse(self.base).hostname else {}
        try:
            r = self.http.get(url_assinada, headers=headers)
        except httpx.HTTPError as e:
            raise ErroApuracao(f"Falha no download do arquivo: {type(e).__name__}") from e
        if r.status_code != 200:
            raise ErroApuracao(f"Download recusado (HTTP {r.status_code}); a URL pode ter expirado.")
        return r.json()
