# eKaza Wizard

Integração para **[Home Assistant OS](https://www.home-assistant.io/installation/)** que automatiza o provisionamento completo de câmeras **[eKaza](https://www.ekaza.com.br)** em um stack local com Frigate + LocalTuya — do scan de rede até o dashboard, sem editar arquivos manualmente.

> ⚠️ **As câmeras eKaza devem ser adicionadas pelo aplicativo [Smart Life](https://smart-life-app.com/)** (Android/iOS), e **não** pelo app eKaza. O wizard usa a API Tuya vinculada à conta Smart Life para descobrir os dispositivos e obter as chaves locais necessárias para o controle local.

---

## Pré-requisitos

| Componente | Versão mínima | Por quê |
|---|---|---|
| **[Home Assistant OS](https://www.home-assistant.io/installation/)** | 2024.1+ | A integração usa a Supervisor API para ler/gravar configurações |
| **[Frigate](https://github.com/blakeblackshear/frigate)** | 0.13+ | Recebe os streams RTSP e faz detecção de objetos |
| **[LocalTuya](https://github.com/rospogrigio/localtuya)** | 6.0+ | Controla os DPs da câmera localmente (PTZ, gravação, LED etc.) |
| **[Conta Tuya IoT Platform](https://iot.tuya.com)** | — | Necessária para obter o `device_id` e a `local_key` das câmeras |

> LocalTuya é instalado via **[HACS](https://hacs.xyz)**.

**Opcional:**

| Componente | Para quê |
|---|---|
| **[AdGuard Home](https://github.com/hassio-addons/addon-adguard-home)** | Aba Privacidade — bloquear servidores Tuya/SmartLife na rede local |

---

## Credenciais Tuya

> 📖 **Tutorial:** [Como criar uma conta de desenvolvedor Tuya e vincular ao Smart Life](https://developer.tuya.com/en/docs/iot/quick-start1?id=K95ztz9u9t89n)

1. Acesse [iot.tuya.com](https://iot.tuya.com) e crie uma conta de desenvolvedor gratuita
2. Crie um projeto em **Cloud → Development → Create Cloud Project** (tipo **Smart Home**)
3. Anote o **Access ID** e o **Access Secret** na aba **Overview** do projeto
4. Em **Devices → Link Tuya App Account**, vincule sua conta do **Smart Life** via QR code
5. Confirme a **região** onde seus dispositivos estão registrados (`us`, `eu`, `cn` ou `in`)

---

## Instalação via HACS

### 1. Adicionar repositório customizado

Em **HACS → Integrations → menu (⋮) → Custom repositories**, adicione:

```
https://github.com/felipearmat/ekaza-wizard
```

Tipo: **Integration**

### 2. Instalar

Pesquise **eKaza Wizard** no HACS e clique em **Download**. Reinicie o Home Assistant.

### 3. Configurar

Vá em **Settings → Integrations → Add integration → eKaza Wizard** e preencha as credenciais Tuya e a senha RTSP padrão das câmeras.

O wizard abre automaticamente no menu lateral após a configuração.

---

## Como usar

### Aba Instalar

**1. Verificar dependências**

Testa a comunicação com Frigate, LocalTuya e a API Tuya antes de prosseguir.

**2. Descobrir câmeras**

Clique em **Descobrir câmeras**. O wizard:
- Consulta a API Tuya Cloud para listar dispositivos da conta
- Faz scan de rede local (tinytuya broadcast) para obter IP e `local_key`
- Filtra pela categoria Tuya (`sp`/`ipc`) e presença de DPs de câmera
- Busca os DPs via `/v1.1/devices/{id}/specifications` — sem depender de `product_id`

**3. Provisionar**

Selecione as câmeras, ajuste os nomes e clique em **Provisionar**. O wizard executa:

| Etapa | O que faz |
|---|---|
| **ONVIF** | Ativa ONVIF (DP 237) e define senha RTSP (DP 238) via LocalTuya |
| **Frigate** | Adiciona stream go2rtc + bloco de câmera no config do Frigate e reinicia |
| **LocalTuya** | Cria config entry com ~20 switches, 10 selects, 2 numbers (PTZ, LED, zoom, gravação etc.) |
| **Scripts PTZ** | Gera scripts `ptz_up/down/left/right/home` + `zoom_in/out` |
| **Motion Bridge** | Monitora DP de movimento da câmera e dispara eventos no Frigate |
| **Dashboard** | Adiciona card da câmera no dashboard Lovelace selecionado |

---

### Aba Remover

Lista as câmeras configuradas no Frigate. Para cada câmera selecionada, **desfaz todas as etapas de provisionamento**:

- Remove streams go2rtc e bloco da câmera no Frigate (reinicia o Frigate)
- Remove config entry do LocalTuya
- Apaga scripts PTZ
- Remove motion bridge e `input_boolean` associado
- Remove card do dashboard Lovelace

---

### Aba Privacidade

Gerencia o bloqueio das câmeras à nuvem Tuya/SmartLife via **AdGuard Home**.

O AdGuard expõe sua API apenas em `localhost`, inacessível de fora. A integração usa a **Supervisor Backup API** como contorno:

1. Cria backup parcial do AdGuard
2. Extrai e modifica `AdGuardHome.yaml` em memória (adiciona/remove bloco de regras DNS)
3. Faz upload do backup modificado e restaura — AdGuard reinicia com as novas regras

A operação leva ~40 segundos.

**Domínios bloqueados:**

```
tuya.com  tuyaeu.com  tuyacn.com  tuyaus.com  tuyain.com
smart-life.com  smartlifeapp.com  fogcloud.io  nebulae-iot.com
```

> ⚠️ Com o bloqueio ativo não é possível parear novos dispositivos via Smart Life na mesma rede. Desative temporariamente para parear e reative em seguida.

---

## Câmeras compatíveis

O wizard detecta câmeras pela **categoria Tuya** (`sp`/`ipc`) e pelas **capacidades do dispositivo** — sem depender de `product_id`.

| Modelo | Tipo | Status |
|---|---|---|
| eKaza EKRW-T5293 | Dome PTZ | ✅ Testado com hardware físico |
| eKaza EKRW-T5394 | Dome PTZ | 🔍 Suportado — aguardando confirmação |
| eKaza EKGD-T4117 | Câmera externa | 🔍 Suportado — aguardando confirmação |
| eKaza EKGD-T5530 | Câmera externa | 🔍 Suportado — aguardando confirmação |
| eKaza EKGD-T2233 | Câmera externa | 🔍 Suportado — aguardando confirmação |
| eKaza EKJS-T3188 | Câmera interna | 🔍 Suportado — aguardando confirmação |
| eKaza EKJS-T3169 | Câmera interna | 🔍 Suportado — aguardando confirmação |

> Câmeras de outros fabricantes baseadas em Tuya (`sp`/`ipc`) também podem ser detectadas. Abra uma [issue](https://github.com/felipearmat/ekaza-wizard/issues) para reportar compatibilidade.

---

## Screenshots

<table>
<tr>
  <td align="center"><b>Aba Instalar — descoberta de câmeras</b><br><img src="screenshots/install.png" alt="Aba Instalar" width="420"></td>
  <td align="center"><b>Log de provisionamento em tempo real</b><br><img src="screenshots/provision-log.png" alt="Log de provisionamento" width="420"></td>
</tr>
<tr>
  <td align="center"><b>Aba Remover</b><br><img src="screenshots/remove.png" alt="Aba Remover" width="420"></td>
  <td align="center"><b>Aba Privacidade — bloqueio AdGuard</b><br><img src="screenshots/privacy.png" alt="Aba Privacidade" width="420"></td>
</tr>
</table>

---

## Licença

MIT

---

## Créditos

Desenvolvido com o auxílio do [Claude](https://claude.ai) (Anthropic), supervisionado em todas as etapas e validado com equipamentos físicos em ambiente local.
