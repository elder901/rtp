# RTP — Planejamento da Reforma Tributária

Compara, a partir dos XMLs de NF-e, a carga **atual** (ICMS, ICMS-ST, FCP, IPI, PIS, COFINS) com a carga da
**reforma** (CBS/IBS) ano a ano na transição, **por produto (saídas)** e **por fornecedor (entradas)**.
O cálculo de CBS/IBS é feito pela **Calculadora de Tributos RTC** da Receita Federal.

## Como rodar

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
```

Interface web (http://localhost:8000):

```bash
.venv/Scripts/python -m uvicorn app.api.main:app --port 8000
```

Linha de comando (gera Excel):

```bash
.venv/Scripts/python -m app.cli --cnpj 11222333000181 --regime real --uf SP --municipio 3550308 --xml C:/xmls/cliente --saida relatorios/cliente.xlsx
```

Testes (o teste de contrato com a calculadora real só roda com `RTP_TESTE_ONLINE=1`):

```bash
.venv/Scripts/python -m pytest -q
```

## Calculadora RTC

- Padrão: instância pública da Receita (`RTP_CALCULADORA_URL`, ver `.env.example`). Só são enviados NCM, CST,
  cClassTrib, base fictícia de R$ 1.000 e as alíquotas — nenhum CNPJ, valor ou dado do cliente.
- Produção: rodar a calculadora offline (Docker ou JAR, Java 17+) baixada em
  https://consumo.tributos.gov.br/servico/calcular-tributos-consumo/calculadora/calculadora-offline
  e apontar `RTP_CALCULADORA_URL` para ela.
- Swagger: https://consumo.tributos.gov.br/servico/calcular-tributos-consumo/api/swagger-ui/index.html

A calculadora é chamada uma vez por combinação (NCM, CST, cClassTrib, ano) e o resultado vira alíquota efetiva,
já com as reduções (cesta básica, 60%, 30% etc.) aplicadas pela própria Receita.

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
  2029–32 ICMS/ISS a 90/80/70/60% e IBS a 10/20/30/40%; 2033 só CBS/IBS.
- **Classificação (cClassTrib)**, em ordem: CSV manual (por código do produto ou NCM) → grupo IBSCBS do XML →
  padrão `000/000001` (tributação integral, com alerta). É a principal fonte de erro: revise a aba Classificação.

## Limitações conhecidas (MVP)

- Não identifica uso e consumo / ativo imobilizado: todo crédito de entrada é tratado como aproveitável.
- Não trata Imposto Seletivo, monofasia (combustíveis), ZFM nem PIS/COFINS monofásico — itens são sinalizados
  quando identificáveis.
- Empresas do Simples: saídas pelo DAS efetivo informado; a simulação "Simples × regime regular" é a fase 3.
- Só NF-e/NFC-e. CT-e e NFS-e entram depois.
- Análises ficam em memória no servidor web (persistência e multi-cliente: fase 4).

## Próximas fases

2. Busca automática por DF-e com certificado A1, linha do tempo 2026–2033 completa, tela de revisão de cClassTrib.
3. Conciliação com a API da Apuração Assistida, simulação Simples × regular, preço neutro com margem.
4. Multi-cliente (PostgreSQL com isolamento por empresa, login, cobrança).
