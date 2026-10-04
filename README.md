# RTP — Planejamento da Reforma Tributária

Compara, a partir dos XMLs de NF-e, a carga **atual** (ICMS, ICMS-ST, FCP, IPI, PIS, COFINS) com a carga da
**reforma** (CBS/IBS) em cada ano da transição (2027–2033), **por produto (saídas)** e **por fornecedor
(entradas)**. O cálculo de CBS/IBS é feito pela **Calculadora de Tributos RTC** da Receita Federal.

## Como rodar

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
```

Interface web (http://localhost:8000):

```bash
.venv/Scripts/python -m uvicorn app.api.main:app --port 8000
```

Fluxo: cadastrar empresa → enviar certificado A1 e **Sincronizar** (entradas pela SEFAZ) e/ou **Importar XMLs**
(saídas e o que faltar) → revisar a aba **Classificação** → aba **Análise** (painel e Excel) → aba **Apuração
Receita** (conciliação com a apuração assistida da CBS).

Linha de comando (mesmas operações; `dfe` serve para o Agendador de Tarefas do Windows):

```bash
.venv/Scripts/python -m app.cli empresa criar --cnpj 11222333000181 --nome "Loja" --regime real --uf SP --municipio 3550308
.venv/Scripts/python -m app.cli importar --cnpj 11222333000181 --xml C:/xmls/loja
.venv/Scripts/python -m app.cli certificado --cnpj 11222333000181 --pfx C:/certs/loja.pfx
.venv/Scripts/python -m app.cli dfe
.venv/Scripts/python -m app.cli relatorio --cnpj 11222333000181 --inicio 2026-01-01 --fim 2026-06-30 --saida relatorios/loja.xlsx
```

Apuração assistida (`apuracao credencial | solicitar | verificar | importar | conciliar`):

```bash
.venv/Scripts/python -m app.cli apuracao importar --cnpj 11222333000181 --arquivo debitos.json
.venv/Scripts/python -m app.cli apuracao conciliar --cnpj 11222333000181 --pa-inicio 2026-03 --saida relatorios/conciliacao.xlsx
```

`analisar` continua disponível para uma análise avulsa de pasta/ZIP, sem gravar nada.

Testes (o teste contra a calculadora real só roda com `RTP_TESTE_ONLINE=1`):

```bash
.venv/Scripts/python -m pytest -q
```

Configurações em variáveis de ambiente ou `.env` — veja `.env.example`.

## Notas fiscais

- **SEFAZ (DF-e)** — `app/dfe/`: Distribuição de DF-e do Ambiente Nacional (NT 2014.002) com TLS mútuo pelo
  certificado A1. Baixa as NF-e em que a empresa é **destinatária** (entradas), registra cancelamentos e
  respeita a regra de 1 hora sem nova consulta quando não há documentos (ou após cStat 656).
  Notas que chegam só como **resumo** precisam da **Ciência da Operação** (evento 210210, assinado) para liberar
  o XML completo: é uma opção por empresa, **desligada por padrão**, porque registra um evento oficial em nome dela.
- **Importação**: XMLs soltos ou ZIP, com deduplicação pela chave de acesso. É o caminho para as **saídas**, que
  a distribuição DF-e não entrega ao emitente.
- Notas canceladas ou só com resumo ficam fora da análise.

## Certificado A1 e segurança

- Certificado e senha ficam **cifrados** no banco (Fernet). A chave vem de `RTP_CHAVE_CRIPTO`; sem ela, uma chave
  local é criada em `dados/chave.key` (só para desenvolvimento — em produção use um cofre/KMS).
- O certificado precisa ser da mesma raiz de CNPJ da empresa e estar no prazo.
- O servidor escuta só em `127.0.0.1`. Antes de expô-lo na rede, defina `RTP_USUARIO`/`RTP_SENHA` (login básico) e
  use HTTPS. Login por usuário e isolamento por cliente são a fase 4.

## Imposto Seletivo (IS)

Bebidas açucaradas e alcoólicas, fumo, bens minerais, veículos etc. As alíquotas vêm da base oficial da calculadora
(ex.: refrigerante 10%; cerveja 3% em 2027 subindo a 20% em 2033; destilados 17–19%; cigarro 13% + R$ 2,13 por
unidade). Só NCMs dos capítulos alcançados pela LC 214 são consultados.

- O IS é cobrado **uma vez, no fabricante** (cClassTrib IS 000001), identificado pelo CFOP de venda de produção própria
  (x101, x401...). Ele **integra a base da CBS/IBS** e **não gera crédito**: na compra do fabricante entra no custo.
- Na **revenda** (inclusive a venda do supermercado) o IS não incide (200007).
- Em compras de **revendedor/distribuidor**, o IS foi cobrado antes e está embutido no preço: é estimado com a
  alíquota oficial sobre o preço do revendedor × repasse (premissa, padrão 100% = limite superior).
- As telas de fornecedor/produto e o Excel mostram o IS no custo de cada compra.

## Fornecedores e produtos (negociação e preço)

Abas **Fornecedores** e **Produtos**, comparando hoje com o ano escolhido da transição (padrão 2027). Custo efetivo de
compra = preço pago − créditos aproveitáveis.

- **Dois cenários de compra**: *a preços de hoje* (o fornecedor mantém o preço de nota — em 2027 o fim do PIS/COFINS
  vira margem dele e o seu crédito cai de 9,25% para o da CBS) e *com repasse* (o fornecedor mantém o próprio valor
  líquido e o preço de nota cai).
- **Preço de nota de equilíbrio**: o preço de nota que mantém o seu custo de hoje no ano — o número a negociar
  (ex.: papel higiênico comprado a R$ 20,00 em MG: R$ 18,15 em 2027, −9,25%; mantido o preço, o custo sobe 10,2%).
- **Melhor fornecedor do mesmo EAN** (mesma unidade), a preços de hoje, e a economia se o volume fosse comprado dele.
- **Preço de venda e margem por EAN**: preço ao consumidor, custo e margem unitários hoje e no ano, em quatro
  combinações — manter a gôndola ou a margem; fornecedor mantém o preço ou repassa.
- Os tributos que continuam na transição (ICMS, ST, IPI) são recalculados sobre o preço de nota de cada ano.
- **Excel para negociação**: resumo por fornecedor e a planilha fornecedor × produto, com os dois cenários.

As telas reaproveitam a mesma análise, que fica em cache e é recalculada só quando notas, classificações, premissas ou
a base de regras mudam.

## Apuração assistida da CBS (API da Receita)

Documentação oficial: https://docs.receitafederal.gov.br/apuracao-cbs/ — `app/apuracao/`.

- Credencial: Client ID/Secret gerados no portal RTC ("Gerar Credencial para API"), guardados cifrados. Token
  OAuth2 (client credentials) em `api.receitafederal.gov.br/token`. Ambientes: produção restrita (piloto) e produção.
- A API é **assíncrona**: `POST /debitos|creditos/{cnpj base}` com uma `urlRetorno`; a Receita **testa essa URL com
  HEAD e só aceita se for HTTPS público**, processa (até 4 h) e chama o webhook com uma URL assinada (48 h) para baixar
  o JSON. O sistema expõe o webhook em `/webhooks/apuracao-cbs/{token}` (token aleatório por solicitação, fora do
  login básico) e usa `GET /situacao/{tíquete}` como plano B ("Verificar solicitações em aberto" ou o agendador).
  A URL assinada é tratada como segredo: nunca vai para banco ou log.
- Para solicitar direto da Receita, publique o servidor com HTTPS e defina `RTP_URL_PUBLICA`. Sem isso, **importe
  o arquivo JSON** (mesmo formato; exemplos em `tests/fixtures/apuracao/`).
- Limite da Receita: 4 solicitações por dia de cada tipo (controlado antes de chamar). Consultas são incrementais:
  a primeira traz o mês corrente; as seguintes, o que mudou (janela de 8 dias). Os registros são atualizados, não
  duplicados.
- **Conciliação** nota a nota: CBS destacada nos XMLs (saídas × débitos; entradas × créditos) contra a apurada pela
  Receita (origem NORMAL; devoluções, cancelamentos e afins aparecem como ajustes). Situações: valor divergente,
  só na Receita (falta o XML na base), cancelada na base, só no XML (ainda não apurada) e confere.

## Simples Nacional × CBS/IBS pelo regime regular

Para empresas do Simples, o painel compara, ano a ano, permanecer no **Simples puro** com **recolher CBS/IBS pelo
regime regular** (LC 214/2025): o DAS perde a parcela que CBS/IBS substituem (PIS+COFINS e, conforme a transição,
ICMS/ISS — percentuais do DAS editáveis, padrão Anexo I 1ª faixa), e a empresa passa a ter débitos e créditos de
CBS/IBS. Mostra também o crédito que cada opção transfere aos clientes PJ, que pesa na competitividade.

## Preço neutro com margem

Além do **Δ preço** (receita líquida constante), o painel calcula o **preço neutro**: mantém a margem unitária em R$,
repassando a mudança do custo de compra após créditos. Compra e venda do mesmo produto são ligadas pelo **GTIN**
(cEAN) com a mesma unidade; produtos sem GTIN ficam só com o Δ preço.

## Calculadora RTC

O cálculo de CBS/IBS de cada item é feito pela Calculadora de Tributos oficial. A versão usada (aplicativo e base
de regras) fica registrada em cada análise, no painel e na aba Premissas do Excel.

### Calculadora local (offline) — recomendada

```bash
.venv/Scripts/python -m app.cli calculadora atualizar     # instala ou atualiza, com validação
.venv/Scripts/python -m app.cli calculadora status        # versão local, se está atualizada, pacote oficial
.venv/Scripts/python -m app.cli calculadora iniciar       # após reiniciar o computador (o servidor web também inicia)
.venv/Scripts/python -m app.cli calculadora parar
```

Com `RTP_CALCULADORA_URL=http://localhost:8080/api` no `.env`, nenhuma consulta sai do computador.

- **Origem**: não há repositório oficial no GitHub. O pacote oficial (`calculadora.zip`, ~340 MB, com o código-fonte
  do backend e a versão compilada) é distribuído pelo portal da Receita: `{calculadora pública}/calculadora/download/url`
  devolve o link no armazenamento do SERPRO. Repositórios no GitHub são espelhos de terceiros.
- **Como roda aqui**: o instalador oficial para Windows usa WSL (`wsl --install`, `wsl --shutdown`), o que altera o
  sistema e derruba outras distribuições WSL. Em vez disso o sistema extrai do pacote só o motor
  (`api-regime-geral.jar`, Spring Boot, Java 21) e a base de regras (`calculadora-pro.db`) e roda com um **Java 21
  portátil** (Eclipse Temurin, checksum conferido) em `dados/calculadora/jre` — sem instalar nada no Windows.
  API na porta 8080 (`/api`), saúde na 9101 (`/health`).
- **Atualização** (`calculadora atualizar`): compara o ETag do pacote oficial com o instalado → baixa e extrai numa
  pasta nova → sobe a nova versão em portas de teste (8090/9191) e compara casos de referência (integral, cesta
  básica, hortifruti, alimentos 60%, em 2027/2029/2033) com a calculadora pública → só se tudo bater, troca nas portas
  oficiais; se a nova não subir, volta a anterior. Guarda a versão atual e a anterior; estado em
  `dados/calculadora/atual.json`, logs em `dados/calculadora/logs/`.
- A própria calculadora local expõe `/api/versao/status`, que diz se aplicativo e base de regras estão atualizados
  em relação à Receita (o `status` mostra).
- Para atualizar todo dia, agende no Agendador de Tarefas do Windows o comando
  `.venv\Scripts\python -m app.cli calculadora atualizar` com a pasta do projeto como diretório inicial.

### Calculadora pública

Sem `.env`, usa a instância pública da Receita. Só são enviados NCM, CST, cClassTrib, base fictícia de R$ 1.000 e as
alíquotas — nenhum CNPJ, valor ou dado do cliente. Swagger:
https://consumo.tributos.gov.br/servico/calcular-tributos-consumo/api/swagger-ui/index.html

A calculadora é chamada uma vez por combinação (NCM, CST, cClassTrib, ano); o resultado vira alíquota efetiva já
com as reduções da Receita (cesta básica, 60%, 30% etc.). Os dados abertos dela também validam a classificação.

## Produtos e classificação (cClassTrib)

O produto é identificado pelo **EAN** (`cEANTrib`, o da unidade tributável, ou `cEAN`), que liga a compra (código
do fornecedor) e a venda (código da loja). Sem EAN global — "SEM GTIN", dígito verificador inválido ou código de
circulação restrita (prefixo 2: balança, açougue, padaria) — vale o código interno.

Ordem usada no cálculo: **ajuste manual** (EAN → código → NCM) → **grupo IBSCBS do XML** → **padrão 000/000001**
(tributação integral, com alerta). Na aba Classificação cada linha reúne compra e venda do mesmo EAN; o "em uso"
reflete a venda. A sugestão vem do cClassTrib que o fornecedor informou **para o mesmo EAN** (evidência forte; há um
botão para aplicar todas de uma vez) ou, na falta, para o mesmo NCM. Cada ajuste é conferido na calculadora (CST
existe, cClassTrib pertence ao CST e se aplica ao NCM). NCM diferente entre fornecedor e loja para o mesmo EAN é
sinalizado. CSV: `chave;cst;cclasstrib` com chave `ean:...`, `cod:...` ou `ncm:...` (o formato antigo
`codigo_ou_ncm` continua aceito).

## Metodologia

- **Valor líquido** = valor da operação − tributos "por dentro" (ICMS, FCP, PIS, COFINS, ISS). É mantido constante:
  sobre ele aplicam-se os tributos atuais e os da reforma.
- **Saídas**: Δ preço = quanto o preço ao cliente muda para manter a mesma receita líquida.
- **Entradas**: custo efetivo = preço pago − créditos aproveitáveis.
  - Hoje: Real credita ICMS + PIS/COFINS 9,25% (base sem ICMS; sem crédito quando a compra não sofreu a
    contribuição — CST 04 a 09 do fornecedor); Presumido credita ICMS; Simples não credita.
    Fornecedor do Simples: ICMS só pelo `vCredICMSSN`.
  - Reforma: crédito amplo de CBS/IBS para Real e Presumido (ambos passam ao regime regular). Fornecedor do
    Simples gera crédito só da parcela de CBS/IBS do DAS (premissa editável, padrão 4%).
- **Transição** (`app/engine/premissas.py`): 2027–28 CBS cheia −0,1 p.p. e IBS 0,1%; PIS/COFINS e IPI extintos;
  2029–32 ICMS/ISS a 90/80/70/60% e IBS a 10/20/30/40%; 2033 só CBS/IBS. 2026 é ano de teste (CBS 0,9% e IBS
  0,1% compensáveis), portanto igual ao cenário atual.
- **Alíquotas**: a base de regras da calculadora só tem as alíquotas de 2026 (teste); de 2027 em diante ela exige
  que quem chama as informe. Para cada ano o sistema consulta primeiro a alíquota oficial na calculadora (União,
  UF e município da empresa, usando a alíquota própria do ente quando houver) e só na falta usa a alíquota de
  referência das premissas (padrão CBS 8,8% + IBS 17,7%). A origem de cada alíquota ("oficial" ou "premissa") aparece
  no painel e no Excel; quando a Receita publicar, as oficiais passam a valer sem mudança no sistema. As reduções por
  classificação são sempre da calculadora.
- **Premissas** são só o que a calculadora precisa receber e que ainda não está em lei ou nos XMLs: alíquotas de
  referência, crédito de fornecedor do Simples/MEI, crédito presumido de produtor rural e, para empresas do Simples,
  DAS efetivo e sua repartição (esses campos só aparecem quando o regime é Simples).

## Limitações conhecidas

- Não identifica uso e consumo / ativo imobilizado: todo crédito de entrada é tratado como aproveitável.
- Não trata Imposto Seletivo, monofasia (combustíveis), ZFM nem PIS/COFINS monofásico — itens sinalizados quando
  identificáveis.
- Empresas do Simples: o DAS efetivo e sua repartição são premissas informadas (não há XML do DAS).
- Preço neutro depende de EAN e unidade iguais na compra e na venda; usa o EAN/unidade tributável quando o XML traz
  `cEANTrib`, mas caixa × unidade sem `cEANTrib` não é convertida.
- IS ad rem (parte fixa por unidade, ex.: cigarros) só entra quando a unidade da nota equivale à do IS (maço = VN);
  senão fica de fora, com alerta. IS embutido em compras de revendedor é estimativa (premissa de repasse).
- A API de apuração foi implementada pela documentação oficial e testada com respostas simuladas; a primeira
  solicitação real deve ser feita em produção restrita (piloto).
- Só NF-e/NFC-e. CT-e e NFS-e entram depois.
- A distribuição DF-e foi testada com respostas simuladas da SEFAZ; a primeira execução real com um certificado
  de cliente deve ser acompanhada (de preferência começando em homologação).

## Próximas fases

4. Multi-cliente (PostgreSQL com isolamento por empresa, login por usuário, cobrança).
