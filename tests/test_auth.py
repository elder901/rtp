"""Login, perfis e isolamento entre empresas na interface web."""
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import auth, db, models, servicos
from app.api import main
from tests.conftest import SENHA_TESTE, CalculadoraFake, cliente_logado

LOJA, OUTRA = "11222333", "12345678"


def _empresas():
    with db.sessao() as s:
        loja = servicos.criar_empresa(s, "11222333000181", "LOJA TESTE", "real", "SP", 3550308).id
        outra = servicos.criar_empresa(s, "12345678000195", "OUTRA EMPRESA", "real", "MG", 3103504).id
    return loja, outra


def test_sem_login_vai_para_a_tela_de_login(banco):
    c = TestClient(main.app)
    r = c.get("/empresas/1/analise?ano=2027", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?prox=%2Fempresas%2F1%2Fanalise%3Fano%3D2027"
    assert c.post("/empresas", data={}).status_code == 401
    assert c.get("/saude").status_code == 200


def test_primeiro_acesso_so_na_maquina_do_servidor(banco):
    remoto = TestClient(main.app, client=("10.0.0.7", 5000))
    assert remoto.get("/login", follow_redirects=False).headers["location"] == "/primeiro-acesso"
    assert "só pode ser criado na própria" in remoto.get("/primeiro-acesso").text
    dados = {"nome": "Admin", "email": "Admin@Empresa.com.br", "senha": "segredo-123", "confirmacao": "segredo-123"}
    assert remoto.post("/primeiro-acesso", data=dados).status_code == 403

    local = TestClient(main.app, client=("127.0.0.1", 5000))
    assert "não confere" in local.post("/primeiro-acesso", data={**dados, "confirmacao": "outra-123"}).text
    r = local.post("/primeiro-acesso", data=dados, follow_redirects=False)
    assert r.status_code == 303 and auth.COOKIE in r.cookies
    assert "Empresas" in local.get("/").text
    with db.sessao() as s:
        u = s.scalar(select(models.Usuario))
        assert (u.email, u.papel) == ("admin@empresa.com.br", "admin") and u.senha_hash.startswith("scrypt$")
    # com usuário criado, o primeiro acesso não existe mais
    assert local.get("/primeiro-acesso", follow_redirects=False).headers["location"] == "/login"


def test_login_senha_errada_e_sair(banco):
    main._TENTATIVAS.clear()
    with db.sessao() as s:
        auth.criar_usuario(s, "Ana", "ana@teste.com.br", SENHA_TESTE, "admin")
    c = TestClient(main.app)
    errado = c.post("/login", data={"email": "ana@teste.com.br", "senha": "errada-123"})
    assert errado.status_code == 401 and "E-mail ou senha incorretos" in errado.text
    r = c.post("/login", data={"email": "ANA@teste.com.br", "senha": SENHA_TESTE, "prox": "/conta"},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/conta"
    assert "Trocar a senha" in c.get("/conta").text
    token = c.cookies[auth.COOKIE]
    c.post("/sair")
    outro = TestClient(main.app, cookies={auth.COOKIE: token})      # o token antigo não vale mais
    assert outro.get("/", follow_redirects=False).status_code == 303
    # destino de fora do sistema é ignorado
    r = c.post("/login", data={"email": "ana@teste.com.br", "senha": SENHA_TESTE, "prox": "//site.com"},
               follow_redirects=False)
    assert r.headers["location"] == "/"


def test_muitas_tentativas_bloqueiam_o_ip(banco):
    main._TENTATIVAS.clear()
    c = TestClient(main.app)
    for _ in range(main.MAX_TENTATIVAS):
        c.post("/login", data={"email": "x@teste.com.br", "senha": "errada-123"})
    assert c.post("/login", data={"email": "x@teste.com.br", "senha": "errada-123"}).status_code == 429
    main._TENTATIVAS.clear()


def test_consulta_ve_so_a_empresa_liberada_e_nao_altera(banco, monkeypatch):
    monkeypatch.setattr(main, "calculadora", CalculadoraFake())
    loja, outra = _empresas()
    c = cliente_logado("consulta", raizes=[LOJA])
    inicio = c.get("/").text
    assert "LOJA TESTE" in inicio and "OUTRA EMPRESA" not in inicio and "Nova empresa" not in inicio
    assert c.get(f"/empresas/{loja}").status_code == 200
    assert c.get(f"/grupos/{LOJA}").status_code == 200
    for url in (f"/empresas/{outra}", f"/empresas/{outra}/analise", f"/grupos/{OUTRA}", f"/grupos/{OUTRA}/fornecedores"):
        assert c.get(url).status_code == 404, url                    # nem revela que existe
    assert c.post(f"/grupos/{LOJA}/dados", data={"regime": "presumido"}).status_code == 403
    assert c.get("/usuarios").status_code == 403
    assert c.get("/empresas/nova").status_code == 403


def test_gestor_cadastra_usuario_so_das_empresas_dele(banco, monkeypatch):
    monkeypatch.setattr(main, "calculadora", CalculadoraFake())
    _empresas()
    cliente_logado("admin")                                          # existe um administrador
    c = cliente_logado("gestor", raizes=[LOJA])
    tela = c.get("/usuarios").text
    assert "gestor@teste.com.br" in tela and "admin@teste.com.br" not in tela
    base = {"nome": "Comprador", "email": "comprador@teste.com.br", "senha": "senha-forte-1", "papel": "consulta"}
    assert "Você só pode liberar" in c.post("/usuarios", data={**base, "empresas": [OUTRA]}).text
    assert "Só um administrador" in c.post("/usuarios", data={**base, "papel": "admin", "empresas": [LOJA]}).text
    assert "Usuário comprador@teste.com.br criado" in c.post("/usuarios", data={**base, "empresas": [LOJA]}).text
    with db.sessao() as s:
        novo = s.scalar(select(models.Usuario).where(models.Usuario.email == "comprador@teste.com.br"))
        assert [a.raiz for a in novo.acessos] == [LOJA]
        id_novo = novo.id
        id_admin = s.scalar(select(models.Usuario.id).where(models.Usuario.papel == "admin"))
    assert c.get(f"/usuarios/{id_admin}").status_code == 404
    # desativar encerra as sessões do usuário
    comprador = TestClient(main.app)
    comprador.post("/login", data={"email": "comprador@teste.com.br", "senha": "senha-forte-1"})
    assert comprador.get("/", follow_redirects=False).status_code == 200
    c.post(f"/usuarios/{id_novo}", data={"nome": "Comprador", "papel": "consulta", "empresas": [LOJA]})
    assert comprador.get("/", follow_redirects=False).status_code == 303


def test_empresa_com_filiais_dentro(banco, monkeypatch):
    monkeypatch.setattr(main, "calculadora", CalculadoraFake())
    c = cliente_logado("gestor", raizes=[])
    erro = c.post("/empresas", data={"cnpj": "11222333000182", "nome": "LOJA", "regime": "real", "uf": "SP",
                                     "municipio": "3550308"})
    assert "CNPJ inválido" in erro.text
    r = c.post("/empresas", data={"cnpj": "11.222.333/0001-81", "nome": "LOJA TESTE", "regime": "real", "uf": "SP",
                                  "municipio": "3550308"}, follow_redirects=False)
    assert r.headers["location"] == f"/grupos/{LOJA}"            # o gestor que cadastrou passa a ver a empresa
    assert "LOJA TESTE" in c.get("/").text
    fora = c.post(f"/grupos/{LOJA}/estabelecimentos", data={"cnpj": "12345678000195", "nome": "X", "uf": "SP",
                                                             "municipio": "3550308"})
    assert "precisa começar com a raiz" in fora.text
    geral = c.post(f"/grupos/{LOJA}/estabelecimentos", data={"cnpj": "11222333000262", "nome": "LOJA 2 - CENTRO",
                                                             "uf": "SP", "municipio": "3550308"}).text
    assert "LOJA 2 - CENTRO incluído" in geral and "Filial" in geral and "Matriz" in geral
    with db.sessao() as s:
        filial = s.scalar(select(models.Empresa).where(models.Empresa.cnpj == "11222333000262"))
        assert filial.regime == "real"                            # herda o regime da empresa
        filial_id = filial.id
    # na tela do estabelecimento, o menu mostra a empresa e as filiais
    tela = c.get(f"/empresas/{filial_id}").text
    assert "LOJA TESTE</a> ›" in tela and "LOJA 2 - CENTRO" in tela and "Visão geral" in tela
    # regime muda na empresa, para todos os estabelecimentos
    c.post(f"/grupos/{LOJA}/dados", data={"regime": "presumido"})
    with db.sessao() as s:
        assert {e.regime for e in servicos.estabelecimentos_da_raiz(s, LOJA)} == {"presumido"}
    assert "já está cadastrada" in c.post("/empresas", data={"cnpj": "11222333000262", "nome": "X", "regime": "real",
                                                              "uf": "SP", "municipio": "3550308"}).text


def test_trocar_a_propria_senha_encerra_as_outras_sessoes(banco):
    c = cliente_logado("consulta", raizes=[LOJA])
    outra_sessao = cliente_logado("consulta", raizes=[LOJA])
    assert "não confere" in c.post("/conta", data={"senha_atual": "errada-123", "nova": "nova-senha-1",
                                                    "confirmacao": "nova-senha-1"}).text
    assert "pelo menos 8" in c.post("/conta", data={"senha_atual": SENHA_TESTE, "nova": "curta",
                                                     "confirmacao": "curta"}).text
    r = c.post("/conta", data={"senha_atual": SENHA_TESTE, "nova": "nova-senha-1", "confirmacao": "nova-senha-1"})
    assert "Senha alterada" in r.text                                # consulta também troca a própria senha
    assert outra_sessao.get("/", follow_redirects=False).status_code == 303
    novo = TestClient(main.app)
    assert novo.post("/login", data={"email": "consulta@teste.com.br", "senha": "nova-senha-1"},
                     follow_redirects=False).status_code == 303
