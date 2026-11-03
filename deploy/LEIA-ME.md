# RTP na nuvem — instalação

```
 usuários ── https://rtp.suaempresa.com.br ──► servidor na nuvem (Ubuntu)
                                               ├─ Caddy: HTTPS do domínio
                                               ├─ rtp: telas (só 127.0.0.1:8000)
                                               └─ rtp-calculadora: calculadora oficial da Receita (Java 21)
                                                        │
                                                        ▼
                                               Supabase (PostgreSQL): banco do RTP
                                                        ▲
 rede do escritório: PC coletor ───────────────────────┘
   importação mensal (banco do Flex, SPED em F:, XMLs da loja 03) e backup diário em E:\RTP\backup
```

O banco do Flex e as pastas do SPED ficam na rede do escritório: a importação continua rodando lá e grava direto no
Supabase (conexão de saída, sem abrir porta).

## 1. O que o TI providencia

**Supabase** — um projeto só para o RTP (as tabelas `empresa`, `usuario`, `documento`... não podem se misturar com as
de outros sistemas), região **São Paulo (sa-east-1)**, plano Pro (o banco tem ~1 GB). A senha do banco vai direto
para os arquivos `.env` (servidor e PC coletor), nunca por e-mail ou chat. Endereço: *Project Settings › Database ›
Connection string › Session pooler* (porta 5432), com `?sslmode=require` no fim.

**Servidor** — Ubuntu 24.04 LTS, 4 vCPU, **8 GB de RAM** (a análise do grupo inteiro em memória + a calculadora usam
~6 GB), 40 GB de disco, na mesma região do Supabase. Portas 80 e 443 abertas para a internet (o certificado HTTPS é
emitido sozinho); SSH só para o TI.

**DNS** — registro `A` de `rtp` (ou o nome escolhido) apontando para o IP do servidor, antes de instalar.

## 2. Levar o banco para o Supabase (uma vez, no PC atual)

1. No `.env` do PC, acrescente `RTP_DATABASE_URL_DESTINO=` com o endereço do Supabase (projeto vazio).
2. `.venv\Scripts\python -m app.cli backup` (cópia de segurança antes de tudo).
3. `.venv\Scripts\python -m app.cli migrar-banco` — copia tabela a tabela e confere a contagem de cada uma; recusa
   banco de destino que já tenha dados.
4. No `.env` do PC, troque `RTP_DATABASE_URL` pelo endereço do Supabase. O PC vira o **coletor**: as importações
   gravam no Supabase e o backup diário (tarefa "RTP - backup diario") passa a copiar o Supabase para
   `E:\RTP\backup` num arquivo SQLite — uma cópia independente da do próprio Supabase.
5. Desative a tarefa "RTP - servidor" do PC: as telas passam a ser as do domínio.

## 3. Instalar o servidor

```bash
git clone <repositório do RTP> rtp && cd rtp
sudo RTP_DOMINIO=rtp.suaempresa.com.br bash deploy/instalar.sh     # prepara e pede o .env
sudo nano /opt/rtp/.env                                            # banco, chave de cifragem, domínio
sudo RTP_DOMINIO=rtp.suaempresa.com.br bash deploy/instalar.sh     # baixa a calculadora e sobe tudo
```

`RTP_CHAVE_CRIPTO` é o conteúdo do arquivo `dados\chave.key` do PC atual: cifra o certificado A1 guardado no banco.
Sem a mesma chave, o certificado precisa ser enviado de novo.

**Atualizar o sistema**: `git pull` na pasta do código e rodar o `instalar.sh` de novo (ele também atualiza a
calculadora quando a Receita publica versão nova, só depois de conferir com a calculadora pública).

Logs: `journalctl -u rtp -f` e `journalctl -u rtp-calculadora -f`.

## 4. Primeiro acesso

O administrador é criado no PC coletor (o banco é o mesmo): `.venv\Scripts\python -m app.cli usuarios criar
--papel admin`. Pelo domínio a tela de primeiro acesso não funciona, de propósito. Depois, o administrador cadastra os
demais em *Usuários*: **consulta** (vê e exporta), **gestor** (confirma classificações e premissas), **admin**.

## Segurança já feita

- HTTPS com HSTS; as telas só respondem na própria máquina, atrás do Caddy.
- Login com senha (scrypt), sessão de 12 h, 5 tentativas erradas por IP a cada 10 minutos.
- Cada usuário vê só as empresas liberadas para ele; perfil de consulta não altera nada.
- Páginas automáticas da API (`/docs`, `/openapi.json`) desligadas; primeiro acesso só direto na máquina.
