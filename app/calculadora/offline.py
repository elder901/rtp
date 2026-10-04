"""Calculadora RTC rodando localmente (offline), com processo de atualização.

O pacote oficial (calculadora.zip, ~340 MB, armazenamento do SERPRO) é obtido pelo portal da Receita em
{calculadora pública}/calculadora/download/url. Dele usamos só o motor de CBS/IBS (api-regime-geral.jar,
Spring Boot, Java 21) e a base de regras (calculadora-pro.db) — sem o WSL do instalador oficial. O Java 21 fica
portátil em dados/calculadora/jre.

Atualização (comando `calculadora atualizar`):
  1. compara o ETag do pacote oficial com o da versão instalada (e o /versao/status da própria calculadora);
  2. baixa e extrai a nova versão numa pasta própria, sem tocar na que está em uso;
  3. sobe a nova em portas de teste e compara casos de referência com a calculadora pública da Receita;
  4. só se tudo bater, para a atual e sobe a nova nas portas oficiais; se a nova não subir, volta a anterior;
  5. mantém a versão anterior em disco para rollback e registra tudo em dados/calculadora/atual.json.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
import time
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import httpx

from app.calculadora.client import CalculadoraRTC, Chave
from app.engine.premissas import Premissas

PASTA = Path("dados/calculadora")
URL_PUBLICA = "https://consumo.tributos.gov.br/servico/calcular-tributos-consumo/api"
PORTA_API, PORTA_GESTAO = 8080, 9101          # portas oficiais da calculadora offline
PORTA_API_TESTE, PORTA_GESTAO_TESTE = 8090, 9191
ARQUIVOS = {"calculadora/api-regime-geral.jar": "api-regime-geral.jar",
            "calculadora/calculadora/db/calculadora-pro.db": "calculadora/db/calculadora-pro.db"}
# Casos de referência: tributação integral, cesta básica (100%), hortifruti (100%) e alimentos (60%),
# em anos com alíquotas diferentes da transição.
CASOS_REFERENCIA = [("84713012", "000", "000001"), ("10063021", "200", "200003"),
                    ("07020000", "200", "200014"), ("04090000", "200", "200034")]
ANOS_REFERENCIA = (2027, 2029, 2033)


class ErroOffline(RuntimeError):
    pass


@dataclass
class Pacote:
    url: str
    etag: str
    ultima_modificacao: str
    tamanho: int


def url_local() -> str:
    return f"http://localhost:{PORTA_API}/api"


# --- Pacote oficial ----------------------------------------------------------------------------------------------

def pacote_remoto(http: httpx.Client | None = None) -> Pacote:
    http = http or httpx.Client(timeout=60)
    try:
        url = http.get(f"{URL_PUBLICA}/calculadora/download/url", params={"platform": "default"}).json()["downloadUrl"]
        cab = http.head(url)
        cab.raise_for_status()
    except (httpx.HTTPError, KeyError, ValueError) as e:
        raise ErroOffline(f"Não foi possível consultar o pacote oficial: {e}") from e
    return Pacote(url, cab.headers.get("etag", "").strip('"'), cab.headers.get("last-modified", ""),
                  int(cab.headers.get("content-length", 0)))


def baixar(pacote: Pacote, http: httpx.Client | None = None) -> Path:
    destino = PASTA / "pacotes" / f"calculadora-{pacote.etag[:12]}.zip"
    if destino.exists() and destino.stat().st_size == pacote.tamanho:
        return destino
    destino.parent.mkdir(parents=True, exist_ok=True)
    parcial = destino.with_suffix(".parcial")
    http = http or httpx.Client(timeout=httpx.Timeout(60, read=600))
    with http.stream("GET", pacote.url) as r:
        r.raise_for_status()
        with open(parcial, "wb") as f:
            for bloco in r.iter_bytes(1 << 20):
                f.write(bloco)
    if pacote.tamanho and parcial.stat().st_size != pacote.tamanho:
        parcial.unlink()
        raise ErroOffline("Download incompleto do pacote oficial.")
    with zipfile.ZipFile(parcial) as z:
        if z.testzip() is not None:
            raise ErroOffline("Pacote oficial corrompido.")
    parcial.replace(destino)
    return destino


def extrair(zip_oficial: Path, destino: Path) -> Path:
    """Extrai só o motor e a base de regras do calculadora.tar.gz interno."""
    encontrados = set()
    destino.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_oficial) as z, tarfile.open(fileobj=z.open("calculadora.tar.gz"), mode="r|gz") as tar:
        for m in tar:
            nome = m.name.lstrip("./")
            if nome in ARQUIVOS and m.isfile():
                saida = destino / ARQUIVOS[nome]
                saida.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(m) as origem, open(saida, "wb") as f:
                    shutil.copyfileobj(origem, f)
                encontrados.add(nome)
    faltando = set(ARQUIVOS) - encontrados
    if faltando:
        raise ErroOffline(f"Pacote oficial sem os arquivos esperados: {', '.join(sorted(faltando))}")
    return destino


# --- Processo ----------------------------------------------------------------------------------------------------

def java() -> Path:
    candidatos = sorted((PASTA / "jre").glob("*/bin/java.exe")) + sorted((PASTA / "jre").glob("*/bin/java"))
    if candidatos:
        return candidatos[-1]
    sistema = shutil.which("java")
    if sistema:
        return Path(sistema)
    raise ErroOffline("Java 21 não encontrado em dados/calculadora/jre nem no PATH.")


def iniciar(pasta_versao: Path, porta_api: int = PORTA_API, porta_gestao: int = PORTA_GESTAO) -> int:
    """Sobe a calculadora em segundo plano; retorna o PID. O log vai para logs/calculadora-<porta>.log."""
    logs = PASTA / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log = open(logs / f"calculadora-{porta_api}.log", "ab")
    flags = (subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008) if os.name == "nt" else 0  # DETACHED_PROCESS
    proc = subprocess.Popen(
        [str(java().resolve()), "-Djava.net.preferIPv4Stack=true", "-jar", "api-regime-geral.jar",
         "--spring.profiles.active=offline", f"--server.port={porta_api}", f"--management.server.port={porta_gestao}"],
        cwd=pasta_versao, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        creationflags=flags, start_new_session=os.name != "nt")
    return proc.pid


def parar(pid: int | None):
    if not pid:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass


def saudavel(porta_gestao: int = PORTA_GESTAO) -> bool:
    try:
        return httpx.get(f"http://localhost:{porta_gestao}/health", timeout=3).json().get("status") == "UP"
    except (httpx.HTTPError, ValueError):
        return False


def aguardar(porta_gestao: int, limite_s: int = 120) -> bool:
    fim = time.monotonic() + limite_s
    while time.monotonic() < fim:
        if saudavel(porta_gestao):
            return True
        time.sleep(2)
    return False


# --- Validação e estado ------------------------------------------------------------------------------------------

def validar(url_nova: str, url_referencia: str = URL_PUBLICA) -> list[str]:
    """Compara os casos de referência entre a versão nova e a referência. Lista vazia = tudo igual."""
    p = Premissas()
    nominais = {ano: p.aliquotas_nominais(ano) for ano in ANOS_REFERENCIA}
    chaves = {Chave(n, c, k, ano) for n, c, k in CASOS_REFERENCIA for ano in ANOS_REFERENCIA}
    nova = CalculadoraRTC(url_nova).aliquotas(chaves, nominais, "SP", 3550308)
    ref = CalculadoraRTC(url_referencia).aliquotas(chaves, nominais, "SP", 3550308)
    problemas = []
    for k in sorted(chaves, key=lambda c: (c.ncm, c.ano)):
        a, b = nova[k], ref[k]
        if b.erro.startswith("falha de comunicação"):
            return [f"Referência indisponível para comparar ({b.erro[:80]})."]
        if (a.cbs, a.ibs_uf, a.ibs_mun, a.reducao_pct, bool(a.erro)) != (b.cbs, b.ibs_uf, b.ibs_mun, b.reducao_pct, bool(b.erro)):
            problemas.append(f"NCM {k.ncm} / {k.cclasstrib} / {k.ano}: nova {a.total}% {a.erro[:60]} x referência {b.total}% {b.erro[:60]}")
    return problemas


def estado() -> dict:
    arq = PASTA / "atual.json"
    return json.loads(arq.read_text(encoding="utf-8")) if arq.exists() else {}


def _gravar_estado(dados: dict):
    PASTA.mkdir(parents=True, exist_ok=True)
    (PASTA / "atual.json").write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")


def status(consultar_remoto: bool = True) -> dict:
    atual = estado()
    info = {"instalada": atual.get("versao_dir"), "url_local": url_local(), "rodando": saudavel()}
    if info["rodando"]:
        try:
            calc = CalculadoraRTC(url_local())
            info["versao"] = calc.versao()
            info["versao_status"] = calc.http.get(f"{url_local()}/versao/status", timeout=30).json()
        except (httpx.HTTPError, ValueError, RuntimeError) as e:
            info["erro_versao"] = str(e)
    if consultar_remoto:
        try:
            remoto = pacote_remoto()
            info["pacote_remoto"] = asdict(remoto)
            info["atualizacao_disponivel"] = remoto.etag != atual.get("etag")
        except ErroOffline as e:
            info["erro_remoto"] = str(e)
    return info


def iniciar_atual() -> int:
    atual = estado()
    if not atual.get("versao_dir"):
        raise ErroOffline("Nenhuma versão instalada. Rode: python -m app.cli calculadora atualizar")
    if saudavel():
        return atual.get("pid", 0)
    pid = iniciar(Path(atual["versao_dir"]))
    if not aguardar(PORTA_GESTAO):
        parar(pid)
        raise ErroOffline("A calculadora não respondeu em 120 s; veja dados/calculadora/logs.")
    atual["pid"] = pid
    _gravar_estado(atual)
    return pid


def parar_atual():
    atual = estado()
    parar(atual.get("pid"))
    atual["pid"] = None
    _gravar_estado(atual)


def atualizar(forcar: bool = False, log=print) -> bool:
    """Instala ou atualiza a calculadora local. Retorna True se a versão em uso mudou."""
    atual = estado()
    remoto = pacote_remoto()
    if remoto.etag == atual.get("etag") and not forcar:
        log(f"Calculadora local já está na versão do pacote oficial ({remoto.ultima_modificacao}).")
        return False
    log(f"Baixando pacote oficial ({remoto.tamanho / 1e6:.0f} MB, publicado em {remoto.ultima_modificacao})...")
    zip_oficial = baixar(remoto)
    data = datetime.strptime(remoto.ultima_modificacao, "%a, %d %b %Y %H:%M:%S %Z").strftime("%Y-%m-%d") \
        if remoto.ultima_modificacao else datetime.now().strftime("%Y-%m-%d")
    pasta = PASTA / "versoes" / f"{data}_{remoto.etag[:8]}"
    extrair(zip_oficial, pasta)

    log("Subindo a nova versão em portas de teste para validar...")
    pid_teste = iniciar(pasta, PORTA_API_TESTE, PORTA_GESTAO_TESTE)
    try:
        if not aguardar(PORTA_GESTAO_TESTE):
            raise ErroOffline("A nova versão não respondeu; a versão atual foi mantida.")
        versao_nova = CalculadoraRTC(f"http://localhost:{PORTA_API_TESTE}/api").versao()
        problemas = validar(f"http://localhost:{PORTA_API_TESTE}/api")
    finally:
        parar(pid_teste)
    if problemas and not forcar:
        for p in problemas:
            log("  divergência: " + p)
        raise ErroOffline("A nova versão diverge da referência; a versão atual foi mantida.")
    log(f"Validação ok: app {versao_nova.get('versaoApp')} · base {versao_nova.get('versaoDb')} "
        f"({versao_nova.get('dataVersaoDb')}).")

    estava_rodando = saudavel()
    parar(atual.get("pid"))
    pid = iniciar(pasta)
    if not aguardar(PORTA_GESTAO):
        parar(pid)
        if atual.get("versao_dir") and estava_rodando:
            atual["pid"] = iniciar(Path(atual["versao_dir"]))
            aguardar(PORTA_GESTAO)
            _gravar_estado(atual)
        raise ErroOffline("A nova versão não subiu nas portas oficiais; a anterior foi restaurada.")

    _gravar_estado({"versao_dir": str(pasta), "etag": remoto.etag, "ultima_modificacao": remoto.ultima_modificacao,
                    "versao_app": versao_nova.get("versaoApp"), "versao_base": versao_nova.get("versaoDb"),
                    "data_base": versao_nova.get("dataVersaoDb"), "instalada_em": datetime.now().isoformat(timespec="seconds"),
                    "pid": pid, "anterior": atual.get("versao_dir")})
    # mantém só a versão nova e a anterior (rollback); remove pacotes antigos
    manter = {pasta.resolve(), Path(atual["versao_dir"]).resolve() if atual.get("versao_dir") else None}
    for d in (PASTA / "versoes").iterdir():
        if d.is_dir() and d.resolve() not in manter:
            shutil.rmtree(d, ignore_errors=True)
    for z in (PASTA / "pacotes").glob("*.zip"):
        if z != zip_oficial:
            z.unlink(missing_ok=True)
    log(f"Calculadora local atualizada e em uso em {url_local()}.")
    return True
