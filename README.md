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
(saídas e o que faltar) → revisar a aba **Classificação** → aba **Análise** (painel e Excel).

Linha de comando (mesmas operações; `dfe` serve para o Agendador de Tarefas do Windows):

```bash
.venv/Scripts/python -m app.cli empresa criar --cnpj 11222333000181 --nome "Loja" --regime real --uf SP --municipio 3550308
.venv/Scripts/python -m app.cli importar --cnpj 11222333000181 --xml C:/xmls/loja
.venv/Scripts/python -m app.cli certificado --cnpj 11222333000181 --pfx C:/certs/loja.pfx
.venv/Scripts/python -m app.cli dfe
.venv/Scripts/python -m app.cli relatorio --cnpj 11222333000181 --inicio 2026-01-01 --fim 2026-06-30 --saida relatorios/loja.xlsx
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

## Calculadora RTC

- Padrão: instância pública da Receita. Só são enviados NCM, CST, cClassTrib, base fictícia de R$ 1.000 e as
  alíquotas — nenhum CNPJ, valor ou dado do cliente.
- Produção: rodar a calculadora offline (Docker ou JAR, Java 17+) de
  https://consumo.tributos.gov.br/servico/calcular-tributos-consumo/calculadora/calculadora-offline
  e apontar `RTP_CALCULADORA_URL` para ela.
- Swagger: https://consumo.tributos.gov.br/servico/calcular-tributos-consumo/api/swagger-ui/index.html

A calculadora é chamada uma vez por combinação (NCM, CST, cClassTrib, ano); o resultado vira alíquota efetiva já
com as reduções da Receita (cesta básica, 60%, 30% etc.). Os dados abertos dela também validam a classificação.

## Classificação (cClassTrib)

Ordem usada: **ajuste manual** (por código do produto ou por NCM) → **grupo IBSCBS do XML** → **padrão 000/000001**
(tributação integral, com alerta). Na aba Classificação, produtos sem código recebem como sugestão o cClassTrib que
os fornecedores usaram para o mesmo NCM; cada ajuste é conferido na calculadora (CST existe, cClassTrib pertence ao
CST e se aplica ao NCM). Exportação/importação em CSV `codigo_ou_ncm;cst;cclasstrib`.

## Metodologia

- **Valor líquido** = valor da operação − tributos "por dentro" (ICMS, FCP, PIS, COFINS, ISS). É mantido constante:
  sobre ele aplicam-se os tributos atuais e os da reforma.
- **Saídas**: Δ preço = quanto o preço ao cliente muda para manter a mesma receita líquida.
- **Entradas**: custo efetivo = preço pago − créditos aproveitáveis.
  - Hoje: Real credita ICMS + PIS/COFINS 9,25% (base sem ICMS); Presumido credita ICMS; Simples não credita.
    Fornecedor do Simples: ICMS só pelo `vCredICMSSN`.
  - Reforma: crédito amplo de CBS/IBS para Real e Presumido (ambos passam ao regime regular). Fornecedor do
    Simples gera crédito só da parcela de CBS/IBS do DAS (premissa editável, padrão 4%).
- **Transição** (`app/engine/premissas.py`): 2027–28 CBS cheia −0,1 p.p. e IBS 0,1%; PIS/COFINS e IPI extintos;
  2029–32 ICMS/ISS a 90/80/70/60% e IBS a 10/20/30/40%; 2033 só CBS/IBS. 2026 é ano de teste (CBS 0,9% e IBS
  0,1% compensáveis), portanto igual ao cenário atual.
- Alíquotas de referência (padrão CBS 8,8% + IBS 17,7%) são premissas editáveis por empresa.

## Limitações conhecidas

- Não identifica uso e consumo / ativo imobilizado: todo crédito de entrada é tratado como aproveitável.
- Não trata Imposto Seletivo, monofasia (combustíveis), ZFM nem PIS/COFINS monofásico — itens sinalizados quando
  identificáveis.
- Empresas do Simples: saídas pelo DAS efetivo informado; a simulação "Simples × regime regular" é a fase 3.
- Só NF-e/NFC-e. CT-e e NFS-e entram depois.
- A distribuição DF-e foi testada com respostas simuladas da SEFAZ; a primeira execução real com um certificado
  de cliente deve ser acompanhada (de preferência começando em homologação).

## Próximas fases

3. Conciliação com a API da Apuração Assistida, simulação Simples × regular, preço neutro com margem.
4. Multi-cliente (PostgreSQL com isolamento por empresa, login por usuário, cobrança).
