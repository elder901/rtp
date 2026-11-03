#!/usr/bin/env bash
# Instala ou atualiza o RTP num servidor Ubuntu 24.04: telas (Python), calculadora oficial (Java 21) e HTTPS (Caddy).
#
#   sudo RTP_DOMINIO=rtp.suaempresa.com.br bash deploy/instalar.sh
#
# Rode de dentro da pasta do código. Na primeira vez ele prepara tudo e para pedindo o /opt/rtp/.env; rode de novo
# depois de preenchê-lo. Para atualizar o sistema: traga o código novo (git pull) e rode outra vez.
set -euo pipefail

: "${RTP_DOMINIO:?defina RTP_DOMINIO, ex.: sudo RTP_DOMINIO=rtp.suaempresa.com.br bash deploy/instalar.sh}"
DESTINO=/opt/rtp
ORIGEM="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON=3.14

echo "== Pacotes do sistema"
apt-get update -qq
apt-get install -y -qq openjdk-21-jre-headless caddy curl rsync >/dev/null
if ! command -v uv >/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh
fi
id rtp >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/rtp --shell /usr/sbin/nologin rtp

echo "== Código em $DESTINO"
mkdir -p "$DESTINO/dados/logs"
rsync -a --delete --exclude .git --exclude .venv --exclude dados --exclude .env --exclude relatorios \
    --exclude calculadora-rtc --exclude __pycache__ "$ORIGEM"/ "$DESTINO"/
[ -f "$DESTINO/.env" ] || cp "$ORIGEM/deploy/env.exemplo" "$DESTINO/.env"
chown -R rtp:rtp "$DESTINO"
chmod 600 "$DESTINO/.env"

echo "== Python $PYTHON e dependências"
sudo -u rtp -H bash -c "cd $DESTINO && uv venv -q --allow-existing --python $PYTHON .venv \
    && uv pip install -q --python .venv/bin/python -r requirements.txt"

echo "== Serviços e HTTPS"
install -m 644 "$DESTINO/deploy/rtp.service" /etc/systemd/system/rtp.service
install -m 644 "$DESTINO/deploy/rtp-calculadora.service" /etc/systemd/system/rtp-calculadora.service
sed "s/{DOMINIO}/$RTP_DOMINIO/" "$DESTINO/deploy/Caddyfile" > /etc/caddy/Caddyfile
systemctl daemon-reload

if grep -q "SUA_SENHA\|^RTP_CHAVE_CRIPTO=$" "$DESTINO/.env"; then
    echo
    echo "Falta configurar: edite $DESTINO/.env (banco do Supabase e chave de cifragem) e rode este script de novo."
    exit 0
fi

echo "== Calculadora oficial (baixa o pacote da Receita só quando há versão nova e confere com a calculadora pública)"
systemctl stop rtp-calculadora 2>/dev/null || true
# a atualização deixa a versão escolhida rodando por conta própria; quem a mantém no ar é o serviço
sudo -u rtp -H bash -c "cd $DESTINO && set -a && . ./.env && set +a && .venv/bin/python -m app.cli calculadora atualizar \
    ; .venv/bin/python -m app.cli calculadora parar"

systemctl enable -q rtp-calculadora rtp
systemctl restart rtp-calculadora rtp
systemctl reload caddy || systemctl restart caddy
echo
echo "Pronto: https://$RTP_DOMINIO (a primeira tela calcula a análise do grupo e pode levar alguns minutos)."
echo "Logs: journalctl -u rtp -f    journalctl -u rtp-calculadora -f"
