# eKaza Wizard

Add-on para Home Assistant OS que automatiza o provisionamento completo de câmeras **eKaza EKRW-T5293** (dome PTZ) em um stack local com Frigate + LocalTuya — do scan de rede até o dashboard, sem editar arquivos manualmente.

---

## Pré-requisitos

### Obrigatórios

| Componente | Versão mínima | Por quê |
|---|---|---|
| **Home Assistant OS** | 2024.1+ | O add-on usa a Supervisor API para ler/gravar configurações |
| **[Frigate](https://github.com/blakeblackshear/frigate)** | 0.13+ | Recebe os streams RTSP e faz detecção de objetos |
| **[LocalTuya](https://github.com/rospogrigio/localtuya)** | 6.0+ | Controla os DPs da câmera localmente (PTZ, gravação, LED etc.) |
| **Conta Tuya IoT Platform** | — | Necessária para obter o `device_id` e a `local_key` das câmeras |

> LocalTuya é instalado via **HACS** (Home Assistant Community Store).

### Opcionais

| Componente | Para quê |
|---|---|
| **[AdGuard Home](https://github.com/hassio-addons/addon-adguard-home)** | Aba Privacidade — bloquear servidores Tuya/SmartLife |

---

## Credenciais Tuya (obrigatórias)

1. Acesse [iot.tuya.com](https://iot.tuya.com) e crie um projeto em **Cloud → Development → Create Cloud Project**
2. Anote o **Access ID** e o **Access Secret**
3. Em **Devices → Link Tuya App Account**, vincule sua conta do Smart Life
4. Confirme a **região** onde seus dispositivos estão registrados (`us`, `eu`, `cn` ou `in`)

---

## Instalação

### 1. Adicionar o repositório de add-ons

```
Settings → Add-ons → Add-on Store → ⋮ → Repositories
```

Adicione:

```
https://github.com/felipearmat/ekaza-wizard
```

### 2. Instalar e iniciar

Procure **eKaza Wizard** na loja, clique em **Install** e depois em **Start**. O add-on aparecerá no menu lateral.

### 3. (Opcional) Pré-configurar credenciais

Em **Settings → Add-ons → eKaza Wizard → Configuration**:

```yaml
tuya_access_id: "SEU_ACCESS_ID"
tuya_access_secret: "SEU_ACCESS_SECRET"
tuya_region: "us"          # us | eu | cn | in
rtsp_password: "SUA_SENHA"
```

Se preferir, todos os campos podem ser preenchidos diretamente na interface do wizard.

---

## Como usar

### Aba Instalar

**1. Verificar dependências**

O wizard testa se consegue se comunicar com o Frigate, o LocalTuya e a API Tuya antes de prosseguir.

**2. Descobrir câmeras**

Preencha as credenciais Tuya e a senha RTSP desejada, depois clique em **Descobrir câmeras**.

O que acontece por baixo:
- Consulta a API Tuya Cloud para listar todos os dispositivos da conta
- Faz um scan de rede local (tinytuya broadcast) para obter o IP e a `local_key` de cada dispositivo
- Filtra apenas os dispositivos eKaza pelo `product_id`
- Exibe a lista com nome, IP, `device_id` e status de alcançabilidade local

**3. Provisionar**

Selecione as câmeras, ajuste os nomes se necessário e clique em **Provisionar**. O wizard executa as etapas abaixo em sequência, com log em tempo real:

| Etapa | O que faz |
|---|---|
| **ONVIF** | Ativa o ONVIF na câmera via LocalTuya (DP 237 → `true`) e define a senha RTSP (DP 238 → `{"pwd":"..."}`) |
| **go2rtc** | Adiciona o stream principal e sub-stream ao Frigate: `ffmpeg:rtsp://admin:SENHA@IP:8554/stream0#video=copy` |
| **Frigate** | Insere a seção da câmera no `frigate.yaml` (detecção desabilitada por padrão, resolução 640×360, 5 fps) |
| **LocalTuya** | Cria a config entry com todas as entidades: ~20 switches, 10 selects, 2 numbers (PTZ, gravação, LED, zoom, modo noturno etc.) |
| **Scripts PTZ** | Gera `script.{nome}_ptz_up/down/left/right/home` + `zoom_in/out` usando os DPs 119/116/132/163/164 |
| **Motion Bridge** | Configura o monitoramento do DP 185 (alarme bruto da câmera) para disparar eventos no Frigate como se fosse detecção nativa |
| **Dashboard** | Cria ou atualiza um dashboard Lovelace com card de controle por câmera (streams, PTZ, switches avançados) |
| **Restart Frigate** | Reinicia o Frigate para aplicar a nova configuração |

---

### Aba Remover

Lista todas as câmeras eKaza atualmente configuradas no Frigate. Para cada câmera selecionada, o wizard **desfaz todas as etapas de provisionamento**:

- Remove os streams go2rtc (principal + sub)
- Remove a seção da câmera no Frigate e reinicia
- Remove a config entry do LocalTuya
- Apaga os scripts PTZ gerados
- Remove o motion bridge e o `input_boolean` associado
- Remove o card do dashboard Lovelace

---

### Aba Privacidade

Gerencia o bloqueio das câmeras à nuvem Tuya/SmartLife via **AdGuard Home**.

**Como funciona:**

O AdGuard Home expõe sua API HTTP apenas em `localhost`, inacessível de dentro do container do add-on. O wizard contorna isso usando a **Supervisor Backup API**:

1. Cria um backup parcial contendo apenas o add-on AdGuard
2. Baixa o backup (arquivo `.tar`)
3. Extrai e modifica o `AdGuardHome.yaml` em memória — adiciona ou remove o bloco de regras DNS
4. Faz upload do backup modificado e restaura — o AdGuard reinicia com as novas regras

A operação leva ~40 segundos. Nenhum arquivo é escrito em disco além do cache de status em `/config/.ekaza_adguard_status`.

**Domínios bloqueados:**

```
tuya.com  tuyaeu.com  tuyacn.com  tuyaus.com  tuyain.com
smart-life.com  smartlifeapp.com  fogcloud.io  nebulae-iot.com
```

> ⚠️ Com o bloqueio ativo, não é possível parear novos dispositivos via Smart Life na mesma rede. Remova as regras temporariamente para parear e ative novamente em seguida.

---

## Câmeras compatíveis

| Modelo | Product ID Tuya | Testado |
|---|---|---|
| eKaza EKRW-T5293 (dome PTZ) | `wg808xnwx1zeavq2` | ✅ |

Para adicionar suporte a outros modelos eKaza, inclua o `product_id` em `app/constants.py` (`EKAZA_PRODUCT_IDS`) e mapeie os DPs em `EKAZA_ENTITIES`.

---

## Arquitetura interna

```
addons/ekaza-wizard/
├── app/
│   ├── main.py           # FastAPI — entrypoint, rotas HTTP e SSE
│   ├── discovery.py      # Tuya Cloud + scan local de rede (tinytuya)
│   ├── provisioner.py    # Orquestra todas as etapas de provisionamento
│   ├── frigate.py        # Lê e escreve config do Frigate via REST API
│   ├── localtuya_flow.py # Cria config entries LocalTuya via HA WebSocket API
│   ├── scripts_gen.py    # Gera scripts PTZ como entidades no HA
│   ├── dashboard.py      # Cria/atualiza dashboard Lovelace via HA API
│   ├── motion_bridge.py  # Monitora DP 185 e dispara eventos no Frigate
│   ├── ha_client.py      # Cliente WebSocket / REST para o Home Assistant
│   ├── schema_store.py   # Cache de schemas Tuya por product_id
│   ├── constants.py      # DPs, entidades e scripts da eKaza EKRW-T5293
│   └── models.py         # Pydantic models (CameraInfo, ProvisionRequest etc.)
└── schemas/              # Schemas Tuya em JSON (cache persistente por product_id)
```

O add-on roda como servidor FastAPI na porta `7788` com ingress habilitado — acessível diretamente pelo menu lateral do HA sem expor portas externas.

---

## Licença

MIT
