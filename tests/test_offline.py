import io
import tarfile
import zipfile
from pathlib import Path

import httpx
import pytest
import respx

from app.calculadora import offline
from app.calculadora.offline import ErroOffline, Pacote


def _pacote_falso(caminho, versao=b"jar-v1"):
    """calculadora.zip com um calculadora.tar.gz interno no mesmo layout do oficial."""
    tar_bytes = io.BytesIO()
    with tarfile.open(fileobj=tar_bytes, mode="w:gz") as tar:
        for nome, dados in (("./calculadora/api-regime-geral.jar", versao),
                            ("./calculadora/calculadora/db/calculadora-pro.db", b"db"),
                            ("./calculadora/api-simples-nacional.jar", b"nao-usado")):
            info = tarfile.TarInfo(nome)
            info.size = len(dados)
            tar.addfile(info, io.BytesIO(dados))
    with zipfile.ZipFile(caminho, "w") as z:
        z.writestr("calculadora.tar.gz", tar_bytes.getvalue())
        z.writestr("windows/1-instalar.bat", "wsl --import ...")
        fonte = io.BytesIO()
        with zipfile.ZipFile(fonte, "w") as f:
            f.writestr("pom.xml", "<project/>")
            f.writestr("src/main/java/br/gov/serpro/rtc/App.java", "class App {}")
        z.writestr("codigo-fonte-backend.zip", fonte.getvalue())
    return caminho


@respx.mock
def test_pacote_remoto():
    respx.get(f"{offline.URL_PUBLICA}/calculadora/download/url").mock(
        return_value=httpx.Response(200, json={"downloadUrl": "https://obs.serpro.exemplo/calculadora.zip"}))
    respx.head("https://obs.serpro.exemplo/calculadora.zip").mock(return_value=httpx.Response(
        200, headers={"ETag": '"abc123-22"', "Last-Modified": "Wed, 30 Sep 2026 16:18:36 GMT",
                      "Content-Length": "337833496"}))
    p = offline.pacote_remoto()
    assert (p.etag, p.tamanho) == ("abc123-22", 337833496)


def test_extrai_so_motor_e_base(tmp_path):
    destino = offline.extrair(_pacote_falso(tmp_path / "c.zip"), tmp_path / "v1")
    assert (destino / "api-regime-geral.jar").read_bytes() == b"jar-v1"
    assert (destino / "calculadora/db/calculadora-pro.db").read_bytes() == b"db"
    assert not (destino / "api-simples-nacional.jar").exists()


@pytest.fixture
def ambiente(tmp_path, monkeypatch):
    """Isola a pasta e simula processos: nada de Java nem rede."""
    monkeypatch.setattr(offline, "PASTA", tmp_path)
    monkeypatch.setattr(offline, "PASTA_FONTE", tmp_path / "fonte")   # nunca toca a pasta real do projeto
    estado = {"rodando": {}, "proximo_pid": 100, "falha_ao_subir": set(), "divergencias": []}

    def iniciar(pasta, porta_api=offline.PORTA_API, porta_gestao=offline.PORTA_GESTAO):
        estado["proximo_pid"] += 1
        pid = estado["proximo_pid"]
        if pasta.name not in estado["falha_ao_subir"]:
            estado["rodando"][porta_gestao] = (pid, pasta.name)
        return pid

    def parar(pid):
        for porta, (p, _) in list(estado["rodando"].items()):
            if p == pid:
                del estado["rodando"][porta]

    monkeypatch.setattr(offline, "iniciar", iniciar)
    monkeypatch.setattr(offline, "parar", parar)
    monkeypatch.setattr(offline, "aguardar", lambda porta, limite_s=120: porta in estado["rodando"])
    monkeypatch.setattr(offline, "saudavel", lambda porta=offline.PORTA_GESTAO: porta in estado["rodando"])
    monkeypatch.setattr(offline, "validar", lambda url: list(estado["divergencias"]))
    monkeypatch.setattr(offline.CalculadoraRTC, "versao",
                        lambda self: {"versaoApp": "1.5.4", "versaoDb": "V0059", "dataVersaoDb": "2026-09-30"})

    def publicar(etag):
        zip_ = _pacote_falso(tmp_path / f"origem-{etag}.zip", versao=etag.encode())
        pacote = Pacote("https://x/calculadora.zip", etag, "Wed, 30 Sep 2026 16:18:36 GMT", zip_.stat().st_size)
        monkeypatch.setattr(offline, "pacote_remoto", lambda: pacote)
        monkeypatch.setattr(offline, "baixar", lambda p: zip_)

    estado["publicar"] = publicar
    return estado


def test_instala_e_nao_repete(ambiente):
    ambiente["publicar"]("aaaaaaaa-1")
    assert offline.atualizar(log=lambda m: None) is True
    atual = offline.estado()
    assert atual["etag"] == "aaaaaaaa-1" and atual["versao_base"] == "V0059"
    assert ambiente["rodando"][offline.PORTA_GESTAO][1].endswith("aaaaaaaa")    # nas portas oficiais
    assert offline.PORTA_GESTAO_TESTE not in ambiente["rodando"]                  # instância de teste parada
    assert offline.atualizar(log=lambda m: None) is False                         # mesma versão: nada a fazer


def test_divergencia_mantem_versao_atual(ambiente):
    ambiente["publicar"]("aaaaaaaa-1")
    offline.atualizar(log=lambda m: None)
    ambiente["publicar"]("bbbbbbbb-2")
    ambiente["divergencias"].append("NCM 84713012: nova 25% x referência 26,5%")
    with pytest.raises(ErroOffline, match="diverge"):
        offline.atualizar(log=lambda m: None)
    assert offline.estado()["etag"] == "aaaaaaaa-1"
    assert ambiente["rodando"][offline.PORTA_GESTAO][1].endswith("aaaaaaaa")


def test_volta_a_anterior_se_a_nova_nao_sobe(ambiente):
    ambiente["publicar"]("aaaaaaaa-1")
    offline.atualizar(log=lambda m: None)
    ambiente["publicar"]("bbbbbbbb-2")
    # passa na porta de teste, mas falha ao subir nas portas oficiais
    original = offline.iniciar

    def iniciar_que_falha(pasta, porta_api=offline.PORTA_API, porta_gestao=offline.PORTA_GESTAO):
        if porta_gestao == offline.PORTA_GESTAO and pasta.name.endswith("bbbbbbbb"):
            ambiente["falha_ao_subir"].add(pasta.name)
        return original(pasta, porta_api, porta_gestao)

    offline.iniciar = iniciar_que_falha
    try:
        with pytest.raises(ErroOffline, match="restaurada"):
            offline.atualizar(log=lambda m: None)
    finally:
        offline.iniciar = original
    assert offline.estado()["etag"] == "aaaaaaaa-1"
    assert ambiente["rodando"][offline.PORTA_GESTAO][1].endswith("aaaaaaaa")


def test_mantem_so_versao_atual_e_anterior(ambiente, tmp_path):
    for etag in ("aaaaaaaa-1", "bbbbbbbb-2", "cccccccc-3"):
        ambiente["publicar"](etag)
        offline.atualizar(log=lambda m: None)
    versoes = sorted(d.name for d in (tmp_path / "versoes").iterdir())
    assert len(versoes) == 2 and versoes[-1].endswith("cccccccc") and any(v.endswith("bbbbbbbb") for v in versoes)
    assert offline.estado()["anterior"].endswith("bbbbbbbb")


def test_extrai_codigo_fonte_e_registra_versao(ambiente, tmp_path):
    ambiente["publicar"]("aaaaaaaa-1")
    offline.atualizar(log=lambda m: None)
    fonte = tmp_path / "fonte"
    assert (fonte / "backend" / "pom.xml").exists()
    assert (fonte / "backend/src/main/java/br/gov/serpro/rtc/App.java").exists()
    leia = (fonte / "LEIA-ME.md").read_text(encoding="utf-8")
    assert "V0059" in leia and "aaaaaaaa-1" in leia


def test_testes_nao_tocam_a_pasta_real(ambiente):
    assert offline.PASTA_FONTE != Path("calculadora-rtc")
