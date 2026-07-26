# 🚀 INSTALAÇÃO PASSO A PASSO — SIGEF Bot (Docker Swarm)

Feito sob medida pro seu ambiente: **Swarm + Portainer**, sem encostar na
Dra. Malu. Faça na ordem. Cada passo tem como **conferir** antes de seguir.

> Legenda: 💻 = comando no terminal da VPS (SSH) · 🖱️ = no Portainer (navegador)
> · 🏠 = na sua máquina de casa.

---

## 📋 Passo 0 — Coletar 3 informações (2 min)

Rode na VPS e anote os resultados:

💻 **0.1 — Nome da rede compartilhada** (a que o n8n e a Evolution usam):
```bash
docker network ls
```
Procure a rede `overlay` que aparece nas suas stacks (geralmente algo como
`network_public`, `traefik_public` ou parecido). **Anote o nome.**

Pra confirmar que é a certa, veja em qual rede o n8n está:
```bash
docker service inspect $(docker service ls --format '{{.Name}}' | grep -i n8n | head -1) \
  --format '{{range .Spec.TaskTemplate.Networks}}{{.Target}} {{end}}'
docker network ls --format '{{.ID}} {{.Name}}'
```
Cruze o ID que apareceu com o nome. **Esse é o valor de `REDE_EXTERNA`.**

💻 **0.2 — Nome do serviço do n8n** (pro webhook de avisos):
```bash
docker service ls --format '{{.Name}}' | grep -i n8n
```
Normalmente é `n8n_n8n` ou `n8n`. **Anote** — vamos usar como host do webhook.

💻 **0.3 — Seu IP de casa** (pra liberar o banco só pra você):
🏠 Abra https://meuip.com.br no navegador de casa e **anote o IPv4**.

---

## 🗄️ Passo 1 — Subir o código na VPS (5 min)

💻 Crie a pasta e mande os arquivos (use `scp`, `git`, ou o File Manager):
```bash
mkdir -p /opt/sigef-bot
# copie para /opt/sigef-bot todo o conteúdo do zip:
#   geoservice/   sql/   swarm/   n8n/   sync_windows/   README.md
ls /opt/sigef-bot/geoservice    # deve listar: app  Dockerfile  requirements.txt
```

✅ **Confere:** o comando `ls` acima mostra a pasta `app`, o `Dockerfile` e o
`requirements.txt`.

---

## 🔧 Passo 2 — Firewall da Hetzner (3 min)

🖱️ No painel da Hetzner Cloud → seu servidor → **Firewalls** → regra de entrada:
- **Porta 5433** (TCP) → permitir **apenas** o seu IP de casa (passo 0.3).

Isso deixa o banco do SIGEF acessível só da sua máquina, pro sync semanal.
Ninguém mais na internet alcança.

✅ **Confere:** a regra aparece na lista de "Inbound rules".

---

## 🏗️ Passo 3 — Construir a imagem do geoservice (10–15 min)

No Swarm a imagem precisa existir **antes** do deploy (não dá pra usar `build:`).

💻
```bash
cd /opt/sigef-bot/geoservice
docker build -t sigef-geoservice:latest .
```
A primeira build baixa o Chromium e demora um pouco. Tome um café. ☕

✅ **Confere:**
```bash
docker images | grep sigef-geoservice
```
Tem que listar `sigef-geoservice   latest`.

---

## ✏️ Passo 4 — Editar a stack (5 min)

💻 Abra `swarm/stack-sigef.yml` (editor de texto ou no Portainer no passo 5) e
preencha os campos marcados com `<<<`:

| Campo | O que colocar |
|---|---|
| `POSTGRES_PASSWORD` (postgis) | uma senha forte que você inventa |
| `PG_DSN` (geoservice) | **a mesma senha** acima |
| `GOVBR_CPF` | seu CPF (só números) |
| `GOVBR_SENHA` | sua senha gov.br |
| `WEBHOOK_NOTIFY` | `http://NOME_DO_N8N:5678/webhook/sigef-admin` (passo 0.2) |
| `REDE_EXTERNA` (2 lugares) | nome da rede do passo 0.1 |

> ⚠️ As credenciais gov.br ficam **só aqui, na sua VPS/Portainer**. Não mande
> pra ninguém. Eu (Craudio) trabalho com placeholder e tá ótimo. 😄

✅ **Confere:** não sobrou nenhum `<<< PREENCHER` nem `REDE_EXTERNA` sem trocar.

---

## 📦 Passo 5 — Deploy da stack no Portainer (3 min)

🖱️ Portainer → **Stacks** → **Add stack**:
1. Nome: `sigef`
2. **Web editor** → cole o conteúdo do `stack-sigef.yml` já preenchido
3. **Deploy the stack**

✅ **Confere:** em **Services**, aparecem `sigef_postgis`,
`sigef_redis` e `sigef_geoservice` com réplica **1/1**.

> Se o `sigef_geoservice` ficar `0/1`, veja os logs (passo 7) — quase sempre é
> senha do banco divergente ou nome da `REDE_EXTERNA` errado.

---

## 🧱 Passo 6 — Criar as tabelas no banco (3 min)

💻 Ache o container do postgis e rode o schema:
```bash
CID=$(docker ps --format '{{.ID}} {{.Names}}' | grep sigef_postgis | awk '{print $1}')

# cria as tabelas e o usuário do bot
docker exec -i $CID psql -U sigef -d sigef < /opt/sigef-bot/sql/schema.sql

# usuário de leitura pro bot (troque a senha)
docker exec -i $CID psql -U sigef -d sigef -c \
"CREATE USER sigef_bot WITH PASSWORD 'senha_bot'; \
 GRANT SELECT ON sigef_parcelas TO sigef_bot; \
 GRANT SELECT, INSERT ON sigef_sync_log TO sigef_bot; \
 GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO sigef_bot;"
```

> Dica: como o postgis já roda com o usuário `sigef` (dono do banco), você pode
> simplesmente usar `sigef` no `PG_DSN` e pular a criação do `sigef_bot`. O
> `sigef_bot` é só boa prática (privilégio mínimo). Se pular, mantenha o
> `PG_DSN` com `sigef:SENHA`.

✅ **Confere:**
```bash
docker exec -i $CID psql -U sigef -d sigef -c "\dt"
```
Tem que listar `sigef_parcelas` e `sigef_sync_log`.

---

## 🩺 Passo 7 — Saúde do geoservice (2 min)

💻
```bash
# logs (deve subir o uvicorn sem erro)
docker service logs sigef_geoservice --tail 30

# teste de saúde a partir de DENTRO do próprio geoservice:
GID=$(docker ps --format '{{.ID}} {{.Names}}' | grep sigef_geoservice | awk '{print $1}')
docker exec -i $GID python -c \
"import urllib.request,json; print(json.load(urllib.request.urlopen('http://localhost:8000/health')))"
```

✅ **Confere:** os logs mostram algo como `Uvicorn running on http://0.0.0.0:8000`,
e o teste imprime `{'ok': True, 'parcelas': 0}` (o 0 é normal — a base enche no
passo 9).

---

## 🔁 Passo 8 — Importar os workflows no n8n (5 min)

🖱️ n8n → **Import from File** (ou cole o JSON):
1. `n8n/sigef_avisos_admin.json` → seu número já vem preenchido
   (SUBSTITUA_SEU_NUMERO). Só ajuste o host/porta/instância/apikey da Evolution.
2. `n8n/sigef_bot_whatsapp.json` → no nó **Geoservice /mensagem**, confirme a
   URL `http://sigef_geoservice:8000/mensagem`. Nos 3 nós da Evolution, troque
   `SUBSTITUA_EVOLUTION` (host:porta da sua Evolution), `SUBSTITUA_INSTANCIA`
   (nome da instância nova) e `SUBSTITUA_APIKEY`.
3. **Ative** os dois workflows.

✅ **Confere:** os dois workflows aparecem como **Active**.

---

## 📲 Passo 9 — Instância da Evolution + carga inicial (10 min)

🖱️ **9.1 — Nova instância** na Evolution (separada da Dra. Malu):
- Crie a instância (ex: nome `sigef`).
- Conecte lendo o QR Code **com o chip 24 99219-3172** (o número do agente).
- No webhook da instância: `https://SEU_N8N/webhook/sigef-bot`, evento
  `MESSAGES_UPSERT` ligado.

🏠 **9.2 — Primeira carga da base** (na sua máquina, porque o INCRA bloqueia a VPS):
```powershell
cd sync_windows
copy .env.exemplo .env
notepad .env
```
Preencha no `.env`:
- `PG_DSN=postgresql://sigef:SENHA_POSTGIS@SEU_IP_VPS:5433/sigef`
  (porta **5433**, a publicada; usuário `sigef` ou `sigef_bot`)
- `SYNC_UF=RJ`
- `WEBHOOK_NOTIFY=https://SEU_N8N/webhook/sigef-admin`

Depois:
```powershell
py -m pip install geopandas pyogrio sqlalchemy psycopg2-binary requests python-dotenv
py sync_sigef.py
```

✅ **Confere:** ao final, chega no seu WhatsApp (98131-4554) o aviso
`✅ Base SIGEF-RJ atualizada: NNNNN parcelas`. E:
```bash
docker exec -i $CID psql -U sigef -d sigef -c "SELECT count(*) FROM sigef_parcelas;"
```
mostra o total (alguns milhares).

> Se der **404 no download**: abra o Acervo Fundiário → *Download de Shapefiles*,
> copie o link real do RJ e cole em `SYNC_URL_ZIP` no `.env`.

---

## ✅ Passo 10 — Teste de ponta a ponta (5 min)

📲 No WhatsApp, mande pro número do agente:
1. `oi` → vem o menu de boas-vindas.
2. **Localização** (clipe) de uma área que você sabe ser certificada no RJ →
   ficha + imagem de satélite + menu de documentos.
3. `2` → arquivo `.txt` de vértices. Veja a legenda: se disser **"Fonte: CSV
   oficial"**, a sessão gov.br está funcionando. Se disser "geometria local", o
   bot funciona mas o login gov.br precisa de ajuste (me chame com os logs).
4. `3` → `1` → KML → abra no Google Earth.
5. Mande um **UUID** de parcela direto (sem menu) → responde igual.

---

## 🔄 Passo 11 — Agendar o sync semanal (3 min)

🏠 Agendador de Tarefas do Windows → Criar Tarefa Básica:
- Disparo: **semanal**, domingo 03:00
- Ação: Programa `py`, argumento
  `C:\caminho\sync_windows\sync_sigef.py`
- Marque **"Executar assim que possível após um início agendado perdido"**
  (cobre PC desligado no horário).

✅ Pronto. Toda semana a base atualiza sozinha e você recebe o aviso no Zap.

---

## 🆘 Se algo travar

| Sintoma | Causa provável | Onde olhar |
|---|---|---|
| `sigef_geoservice` 0/1 | senha do banco divergente ou `REDE_EXTERNA` errada | `docker service logs sigef_geoservice` |
| n8n não fala com geoservice | nome do serviço na URL | use `http://sigef_geoservice:8000` |
| Evolution não envia mídia | Evolution não está na mesma rede do geoservice | confirme a `REDE_EXTERNA` |
| TXT diz "geometria local" | sessão gov.br/captcha | logs do geoservice + README seção 8 |
| Sync 404 | URL do ZIP mudou | `SYNC_URL_ZIP` no `.env` |
| Sync "connection refused" | porta 5433 / firewall Hetzner | passo 2 e IP de casa |

Me manda print do que aparecer que a gente resolve junto. 💪
