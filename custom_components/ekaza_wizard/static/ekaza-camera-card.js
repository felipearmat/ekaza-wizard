(function () {
  'use strict';

  const CARD_TYPE = 'ekaza-camera-card';
  const VERSION   = '3.5.1';

  const DET_MODES = [
    { id: 'off',    icon: 'mdi:cancel',             label: 'Desativado',         motion: false, detect: false, bridge: false },
    { id: 'camera', icon: 'mdi:run-fast',            label: 'Apenas Câmera',     motion: true,  detect: false, bridge: false },
    { id: 'frigate',icon: 'mdi:robot',               label: 'Apenas Frigate',    motion: false, detect: true,  bridge: false },
    { id: 'dual',   icon: 'mdi:plus-circle-outline', label: 'Câmera + Frigate',  motion: true,  detect: true,  bridge: false },
    { id: 'cam_fr', icon: 'mdi:cctv-off',            label: 'Câmera → Frigate',  motion: true,  detect: false, bridge: true  },
  ];

  const FRIGATE_SWITCH_SUFFIX = new Set([
    'recordings', 'detect', 'motion', 'snapshots', 'audio_detection',
    'improve_contrast', 'object_descriptions', 'review_alerts',
    'review_descriptions', 'review_detections',
  ]);

  // PTZ-related entity endings — never show as controls
  const SKIP_TUYA_ENDS = [
    'ptz_home', 'ptz_parar', 'ptz_stop',
    'zoom_parar', 'zoom_stop',
    'controle_ptz',          // raw PTZ DP select (use PTZ buttons instead)
    'ponto_de_memoria_ptz',  // duplicate of ir_para_preset from old LocalTuya entry
  ];

  // Entities completely hidden — dangerous, wizard-managed, or redundant
  const HIDDEN_SUFFIXES = [
    'onvif_switch', 'onvif_change_pwd', 'onvif',       // ONVIF managed by wizard
    'sd_format',    'formatar_sd',                      // SD format — data loss
    'restart',      'reboot',    'reiniciar_camera',    // restart — disruptive
    'reset_switch', 'factory_reset',                    // destructive
    'zoom',         'nivel_de_zoom',                    // raw zoom DP (use Z+/Z- buttons instead)
    'zona_de_movimento', 'motion_area_switch',          // requires Smart Life app to configure
  ];

  const LABEL_MAP = {
    'led_indicador':         'LED Indicador',
    'basic_indicator':       'LED Indicador',
    'basic_flip':            'Espelhar Imagem',
    'imagem_espelhada':      'Espelhar Imagem',
    'basic_osd':             'Timestamp (OSD)',
    'osd':                   'Timestamp (OSD)',
    'basic_private':         'Modo Privado',
    'modo_privacidade':      'Modo Privado',
    'basic_nightvision':     'Visão Noturna',
    'ipc_work_mode':         'Modo Operação',
    'motion_switch':         'Detecção Mov.',
    'deteccao_de_movimento': 'Detecção Mov.',
    'motion_sensitivity':    'Sens. Movimento',
    'sensibilidade_movimento': 'Sens. Movimento',
    'motion_area_switch':    'Zona de Movimento',
    'zona_de_movimento':     'Zona de Movimento',
    'filtro_humano':         'Apenas Pessoas',
    'humanoid_detect':       'Apenas Pessoas',
    'humanoid_sensitivity':  'Sens. Pessoas',
    'record_switch':         'Gravar no SD',
    'gravacao_sd':           'Gravar no SD',
    'record_mode':           'Modo Gravação',
    'modo_de_gravacao':      'Modo Gravação',
    'gravacao_modo':         'Modo Gravação',
    'sd_status':             'Status SD',
    'sd_storge':             'Uso do SD',
    'siren_switch':          'Sirene Alarme',
    'audible_alarm':         'Sirene Alarme',
    'alarme_sonoro':         'Sirene Manual',
    'sirene_automatica':     'Sirene Automática',
    'alarm_message':         'Alertas Push',
    'ir_para_preset':        'Ir p/ Preset',
    'salvar_preset':         'Salvar em Preset',
    'object_outline':        'Contorno Objetos',
    'contorno_de_objetos':   'Contorno Objetos',
    'antioscilacao':         'Anti-oscilação',
    'flicker_detection':     'Anti-oscilação',
    'improve_contrast':      'Melh. Contraste',
    'wdr_contraste':         'WDR / Contraste',
    'object_descriptions':   'Desc. Objetos',
    'review_alerts':         'Alertas no Review',
    'review_detections':     'Detecções no Review',
    'snapshots':             'Snapshots Frigate',
    'audio_detection':       'Detec. Áudio',
    'deteccao_de_audio':     'Detec. Áudio',
    'recordings':            'Gravação Frigate',
    'rastreamento_automatico': 'Rastreamento Auto',
    'luz_floodlight':        'Luz de Iluminação',
    'tipo_de_evento':        'Filtros de Evento',
    'sensibilidade_de_audio': 'Sens. Áudio',
    'antioscilacao_select':  'Anti-oscilação',
    'visao_noturna':         'Visão Noturna',
  };

  // ── Helpers ───────────────────────────────────────────────────────────────────

  function esc(s) {
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function camNameFrom(entityId) { return entityId.replace(/^camera\./, ''); }

  function isFrigateSwitch(entityId, camName) {
    // Check by suffix relative to both possible prefixes (primary and short)
    const sfx = entityId.slice(`switch.${camName}_`.length);
    if (FRIGATE_SWITCH_SUFFIX.has(sfx)) return true;
    // Cross-prefix: check by entity ID ending against Frigate suffixes
    for (const s of FRIGATE_SWITCH_SUFFIX) {
      if (entityId.endsWith('_' + s)) return true;
    }
    return false;
  }

  function isTuyaPtzEntity(entityId, domainPrefix) {
    const suffix = entityId.slice(domainPrefix.length);
    // Also check entity ID ending directly for cross-prefix entities
    return SKIP_TUYA_ENDS.some(s =>
      suffix === s || suffix.endsWith('_' + s) || entityId.endsWith('_' + s)
    );
  }

  function entitySuffix(entityId, camName) {
    for (const domain of ['switch', 'select', 'number', 'input_boolean']) {
      const pfx = `${domain}.${camName}_`;
      if (entityId.startsWith(pfx)) return entityId.slice(pfx.length);
    }
    return null;
  }

  function isHidden(entityId, camName) {
    const sfx = entitySuffix(entityId, camName);
    if (sfx !== null) {
      // Exact match only — avoids false positives like nivel_de_zoom matching zoom
      return HIDDEN_SUFFIXES.some(h => sfx === h);
    }
    // Cross-prefix entity: exact ending match
    return HIDDEN_SUFFIXES.some(h => entityId.endsWith('_' + h));
  }

  function labelFor(hass, entityId, camName) {
    const sfx = entitySuffix(entityId, camName);
    if (sfx && LABEL_MAP[sfx]) return LABEL_MAP[sfx];
    // Check LABEL_MAP against the last segment(s) of the entity ID
    const idParts = entityId.split('_');
    for (let n = 3; n >= 1; n--) {
      const tail = idParts.slice(-n).join('_');
      if (LABEL_MAP[tail]) return LABEL_MAP[tail];
    }
    const friendlyName = hass.states[entityId]?.attributes?.friendly_name;
    if (friendlyName && camName) {
      const camFriendly = hass.states[`camera.${camName}`]?.attributes?.friendly_name || '';
      if (camFriendly && friendlyName.toLowerCase().startsWith(camFriendly.toLowerCase())) {
        const stripped = friendlyName.slice(camFriendly.length).trim();
        if (stripped) return stripped;
      }
    }
    if (friendlyName) return friendlyName;
    if (sfx) return sfx.split('_').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
    const parts = entityId.split('.').pop().split('_');
    return parts.slice(-2).join(' ').replace(/\b\w/g, c => c.toUpperCase());
  }

  function iconFor(entityId) {
    const id = entityId.toLowerCase();
    if (id.includes('recording') || id.includes('gravacao_rec')) return 'mdi:record-rec';
    if (id.includes('rastreamento') || id.includes('tracking'))   return 'mdi:motion-sensor';
    if (id.includes('deteccao_de_movimento') || id.includes('motion_switch')) return 'mdi:run-fast';
    if (id.includes('detec') && id.includes('audio') || id.includes('audio_detection')) return 'mdi:microphone';
    if (id.includes('detect'))             return 'mdi:cctv';
    if (id.includes('privacidade') || id.includes('privacy') || id.includes('modo_privacidade')) return 'mdi:eye-off';
    if (id.includes('led_indicador') || id.includes('basic_indicator')) return 'mdi:led-on';
    if (id.includes('luz_floodlight') || id.includes('floodlight')) return 'mdi:spotlight-beam';
    if (id.includes('brilho') || id.includes('brightness'))       return 'mdi:brightness-6';
    if (id.includes('wdr') || id.includes('contraste') || id.includes('contrast')) return 'mdi:contrast-box';
    if (id.includes('osd'))                return 'mdi:television-play';
    if (id.includes('volume'))             return 'mdi:volume-high';
    if (id.includes('noturna') || id.includes('night') || id.includes('visao_noturna')) return 'mdi:weather-night';
    if (id.includes('preset'))             return 'mdi:map-marker';
    if (id.includes('sensibilidade') || id.includes('sensitivity')) return 'mdi:tune';
    if (id.includes('antioscilacao') || id.includes('flicker'))   return 'mdi:sine-wave';
    if (id.includes('espelhada') || id.includes('mirror') || id.includes('flip') || id.includes('imagem_espelhada')) return 'mdi:flip-vertical';
    if (id.includes('gravacao_sd') || id.includes('record_switch')) return 'mdi:sd';
    if (id.includes('snapshot'))           return 'mdi:camera';
    if (id.includes('recordings'))         return 'mdi:record-rec';
    if (id.includes('motion_area') || id.includes('zona_de_movimento')) return 'mdi:vector-square';
    if (id.includes('filtro_humano') || id.includes('humanoid') || id.includes('human')) return 'mdi:human';
    if (id.includes('sirene') || id.includes('alarme') || id.includes('audible_alarm')) return 'mdi:alarm-light';
    if (id.includes('contorno') || id.includes('object_outline')) return 'mdi:rectangle-outline';
    if (id.includes('sd_status') || id.includes('sd_storge'))     return 'mdi:sd';
    if (id.includes('motion'))             return 'mdi:motion-sensor';
    if (id.includes('improve_contrast'))   return 'mdi:contrast-box';
    if (id.includes('object_descriptions')) return 'mdi:text-recognition';
    if (id.includes('tipo_de_evento') || id.includes('tipo_de_movimento')) return 'mdi:filter-outline';
    if (id.includes('gravacao') || id.includes('gravacao_sd'))    return 'mdi:sd';
    return 'mdi:tune';
  }

  function discoverEntities(hass, camName) {
    const states  = hass.states;
    const swPfx   = `switch.${camName}_`;
    const selPfx  = `select.${camName}_`;
    const numPfx  = `number.${camName}_`;

    // Entities may use a different integration prefix (old vs new LocalTuya entry).
    // Fall back to matching by device friendly_name prefix.
    const camFriendly = (states[`camera.${camName}`]?.attributes?.friendly_name || '').toLowerCase();

    const belongsTo = (domain, id) => {
      if (id.startsWith(`${domain}.${camName}_`)) return true;
      if (!camFriendly || !id.startsWith(domain + '.')) return false;
      const fn = (states[id]?.attributes?.friendly_name || '').toLowerCase();
      return fn.startsWith(camFriendly);
    };

    const ptz = {};
    ['up', 'down', 'left', 'right', 'home'].forEach(dir => {
      const id = `script.${camName}_ptz_${dir}`;
      if (states[id]) ptz[dir] = id;
    });
    ['in', 'out'].forEach(z => {
      const id = `script.${camName}_zoom_${z}`;
      if (states[id]) ptz[`zoom_${z}`] = id;
    });

    const record        = states[`${swPfx}recordings`] ? `${swPfx}recordings` : null;
    const frigateDetect = states[`${swPfx}detect`]     ? `${swPfx}detect`     : null;

    // Motion switch may live under a different prefix (old LocalTuya entry)
    const motionSw = Object.keys(states).find(id =>
      belongsTo('switch', id) &&
      (id.endsWith('_deteccao_de_movimento') || id.endsWith('_motion_switch') ||
       id.endsWith('_deteccao_movimento'))
    ) || null;

    // Detection-section special switches
    const filtroHumanoSw = Object.keys(states).find(id =>
      belongsTo('switch', id) &&
      (id.endsWith('_filtro_humano') || id.endsWith('_humanoid_detect'))
    ) || null;

    const sireneAutoSw = Object.keys(states).find(id =>
      belongsTo('switch', id) &&
      (id.endsWith('_sirene_automatica') || id.endsWith('_siren_switch') ||
       id.endsWith('_audible_alarm'))
    ) || null;

    const motionBridge = states[`input_boolean.${camName}_motion_bridge`]
      ? `input_boolean.${camName}_motion_bridge` : null;

    const frigateAdv = ['recordings', 'snapshots', 'audio_detection', 'improve_contrast',
                        'object_descriptions', 'review_alerts', 'review_detections']
      .map(n => `${swPfx}${n}`)
      .filter(id => !!states[id]);

    // Camera switches — includes cross-prefix entities from same device
    const tuyaSwitches = Object.keys(states).filter(id =>
      belongsTo('switch', id) &&
      !isFrigateSwitch(id, camName) &&
      !isTuyaPtzEntity(id, swPfx) &&
      !isHidden(id, camName) &&
      id !== motionSw &&
      id !== filtroHumanoSw &&
      id !== sireneAutoSw
    ).sort();

    const tuyaSelects = Object.keys(states)
      .filter(id => belongsTo('select', id) && !isTuyaPtzEntity(id, selPfx) && !isHidden(id, camName))
      .sort();

    const tuyaNumbers = Object.keys(states)
      .filter(id => belongsTo('number', id) && !isTuyaPtzEntity(id, numPfx) && !isHidden(id, camName))
      .sort();

    return { ptz, record, frigateAdv, frigateDetect, motionSw, motionBridge,
             filtroHumanoSw, sireneAutoSw,
             tuyaSwitches, tuyaSelects, tuyaNumbers };
  }

  // ── CSS ───────────────────────────────────────────────────────────────────────

  const CSS = `
    :host {
      display: block;
      border-radius: var(--ha-card-border-radius, 12px);
      overflow: hidden;
      box-shadow: var(--ha-card-box-shadow,
        0 2px 2px 0 rgba(0,0,0,.14),0 1px 5px 0 rgba(0,0,0,.12),0 3px 1px -2px rgba(0,0,0,.2));
      background: var(--card-background-color);
    }
    .scc-ctrl { padding: 6px; }
    .scc-row { display: grid; gap: 4px; margin-bottom: 4px; }
    .scc-r5 { grid-template-columns: repeat(5, 1fr); }
    .scc-r4 { grid-template-columns: repeat(4, 1fr); }
    .scc-r3 { grid-template-columns: repeat(3, 1fr); }
    .scc-r2 { grid-template-columns: 1fr 1fr; }
    .scc-btn {
      border: none; border-radius: 8px;
      background: var(--secondary-background-color); color: var(--primary-text-color);
      padding: 6px 4px; cursor: pointer;
      display: flex; flex-direction: column; align-items: center; justify-content: center;
      gap: 2px; font-size: 10px; line-height: 1.2; min-height: 48px; width: 100%;
      transition: background .12s; -webkit-tap-highlight-color: transparent;
      user-select: none; font-family: inherit;
    }
    .scc-btn ha-icon { --mdc-icon-size: 20px; pointer-events: none; }
    .scc-btn:active, .scc-btn.on { background: var(--primary-color); color: #fff; }
    @keyframes scc-pulse { 0%,100%{opacity:1;} 50%{opacity:.35;} }
    .scc-rec.on ha-icon { animation: scc-pulse 1.4s ease-in-out infinite; }
    .scc-sw-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 4px; margin-bottom: 6px; }
    .scc-set-list { display: flex; flex-direction: column; gap: 4px; }
    .scc-set-row {
      display: flex; align-items: center; gap: 8px; padding: 7px 10px;
      background: var(--secondary-background-color); border-radius: 8px; min-height: 44px;
    }
    .scc-set-row ha-icon { --mdc-icon-size: 18px; flex-shrink: 0; opacity: .65; }
    .scc-set-label { flex:1; font-size:12px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .scc-set-row select {
      font-size: 12px; background: var(--card-background-color); color: var(--primary-text-color);
      border: 1px solid var(--divider-color, rgba(0,0,0,.12));
      border-radius: 6px; padding: 3px 6px; max-width: 130px;
    }
    .scc-set-row input[type=range] { flex:1; min-width:0; accent-color: var(--primary-color); }
    .scc-section-title {
      font-size: 11px; font-weight: 600; color: var(--secondary-text-color);
      text-transform: uppercase; letter-spacing: .05em; margin: 8px 0 4px; padding: 0 2px;
    }

    /* ── Horizontal layout ──────────────────────────────────────── */
    .scc-ctrl-h .scc-h-basic  { display: flex; gap: 6px; align-items: stretch; }
    .scc-ctrl-h .scc-h-actions { display: flex; flex-direction: column; gap: 4px; flex: 0 0 72px; }
    .scc-ctrl-h .scc-h-ptz { flex: 1; display: grid; grid-template-columns: repeat(3, 1fr); gap: 4px; }
    .scc-ctrl-h .scc-ptz-spacer { min-height: 48px; border-radius: 8px; }
    .scc-ctrl-h .scc-adv-cols { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; align-items: start; }

    /* ── Preset save button ─────────────────────────────────────── */
    .scc-save-btn {
      border: none; border-radius: 6px;
      background: var(--primary-color); color: #fff;
      padding: 4px 10px; cursor: pointer; font-size: 12px;
      font-family: inherit; flex-shrink: 0; line-height: 1.4;
    }
    .scc-save-btn:active { opacity: 0.75; }
  `;

  // ── GUI Editor ────────────────────────────────────────────────────────────────

  class EkazaCameraCardEditor extends HTMLElement {
    constructor() {
      super();
      this._root = this.attachShadow({ mode: 'open' });
      this._config = {};
      this._hass   = null;
    }

    set hass(h) { this._hass = h; this._render(); }
    setConfig(config) { this._config = { ...config }; this._render(); }

    _fire(config) {
      this.dispatchEvent(new CustomEvent('config-changed', {
        detail: { config }, bubbles: true, composed: true,
      }));
    }

    _render() {
      if (!this._hass) return;
      const c      = this._config;
      const layout = c.layout || 'vertical';
      const cams   = Object.keys(this._hass.states)
        .filter(id => id.startsWith('camera.')).sort();
      const opts = ['<option value="">— selecionar câmera —</option>']
        .concat(cams.map(id => {
          const name = this._hass.states[id]?.attributes?.friendly_name || id;
          return `<option value="${esc(id)}"${id === c.entity ? ' selected' : ''}>${esc(name)}</option>`;
        })).join('');

      this._root.innerHTML = `
        <style>
          .ed { display:flex; flex-direction:column; gap:14px; padding:4px 0; }
          .ed label {
            display:block; font-size:11px; font-weight:600; margin-bottom:4px;
            color:var(--secondary-text-color); text-transform:uppercase; letter-spacing:.04em;
          }
          .ed select, .ed input[type=text] {
            width:100%; padding:8px 10px; border-radius:8px; box-sizing:border-box;
            border:1px solid var(--divider-color,rgba(0,0,0,.12));
            background:var(--card-background-color); color:var(--primary-text-color); font-size:14px;
          }
          .ed-radios { display:flex; gap:16px; }
          .ed-radios label {
            display:flex; align-items:center; gap:6px; font-size:13px;
            text-transform:none; letter-spacing:0; font-weight:400; cursor:pointer; margin-bottom:0;
          }
        </style>
        <div class="ed">
          <div><label>Câmera</label><select id="e-entity">${opts}</select></div>
          <div><label>Nome (opcional)</label>
            <input type="text" id="e-name" placeholder="Rótulo exibido no card"
              value="${esc(c.name || '')}">
          </div>
          <div><label>Frigate Host</label>
            <input type="text" id="e-host" placeholder="http://192.168.1.x:5000"
              value="${esc(c.frigate_host || '')}">
          </div>
          <div>
            <label>Layout dos controles</label>
            <div class="ed-radios">
              <label>
                <input type="radio" name="layout" value="vertical"${layout === 'vertical' ? ' checked' : ''}>
                Vertical (padrão)
              </label>
              <label>
                <input type="radio" name="layout" value="horizontal"${layout === 'horizontal' ? ' checked' : ''}>
                Horizontal
              </label>
            </div>
          </div>
        </div>`;

      this._root.querySelector('#e-entity').addEventListener('change', ev => {
        const next = { ...this._config, entity: ev.target.value };
        this._config = next; this._fire(next);
      });
      this._root.querySelector('#e-name').addEventListener('change', ev => {
        const val = ev.target.value.trim();
        const next = { ...this._config };
        val ? (next.name = val) : delete next.name;
        this._config = next; this._fire(next);
      });
      this._root.querySelector('#e-host').addEventListener('change', ev => {
        const val = ev.target.value.trim();
        const next = { ...this._config };
        val ? (next.frigate_host = val) : delete next.frigate_host;
        this._config = next; this._fire(next);
      });
      this._root.querySelectorAll('input[name=layout]').forEach(r => {
        r.addEventListener('change', ev => {
          const next = { ...this._config, layout: ev.target.value };
          this._config = next; this._fire(next);
        });
      });
    }
  }

  customElements.define('ekaza-camera-card-editor', EkazaCameraCardEditor);

  // ── Card ──────────────────────────────────────────────────────────────────────

  class EkazaCameraCard extends HTMLElement {
    constructor() {
      super();
      this._root           = this.attachShadow({ mode: 'open' });
      this._hass           = null;
      this._config         = null;
      this._built          = false;
      this._advanced       = false;
      this._ctrl           = null;
      this._pendingDetMode = null; // holds user-selected mode during async state transitions
    }

    static getConfigElement() {
      return document.createElement('ekaza-camera-card-editor');
    }

    static getStubConfig(hass) {
      const cam = Object.keys(hass?.states || {}).find(id => id.startsWith('camera.')) || '';
      return { entity: cam, layout: 'vertical' };
    }

    set hass(hass) {
      this._hass = hass;
      if (!this._built && this._config) {
        this._build();
      } else if (this._built) {
        this._syncStates();
      }
    }

    setConfig(config) {
      if (!config.entity) throw new Error(`${CARD_TYPE}: 'entity' é obrigatório`);
      const prev = this._config;
      this._config = config;
      if (this._hass) {
        if (!this._built || prev?.layout !== config.layout || prev?.entity !== config.entity) {
          this._built = false;
          this._advanced = false;
          while (this._root.firstChild) this._root.removeChild(this._root.firstChild);
          this._ctrl = null;
          this._build();
        } else {
          this._syncStates();
        }
      }
    }

    getCardSize() { return 3; }
    _isH()        { return this._config?.layout === 'horizontal'; }

    // ── Build DOM ─────────────────────────────────────────────────────────────

    _build() {
      this._built = true;
      const style = document.createElement('style');
      style.textContent = CSS;
      this._root.appendChild(style);

      const ctrl = document.createElement('div');
      ctrl.className = `scc-ctrl${this._isH() ? ' scc-ctrl-h' : ''}`;
      this._ctrl = ctrl;
      ctrl.innerHTML = this._htmlBasic() + this._htmlAdv();
      this._root.appendChild(ctrl);

      this._wireEvents();
      this._syncStates();
    }

    _htmlBasic() {
      if (this._isH()) {
        return `
          <div id="scc-basic" class="scc-h-basic">
            <div class="scc-h-actions">
              <button class="scc-btn" id="scc-cfg">
                <ha-icon icon="mdi:cog"></ha-icon><span>Avançado</span>
              </button>
              <button class="scc-btn" id="scc-snap">
                <ha-icon icon="mdi:camera-image"></ha-icon><span>Foto</span>
              </button>
              <button class="scc-btn scc-rec" id="scc-rec">
                <ha-icon icon="mdi:record-rec"></ha-icon><span>Gravar</span>
              </button>
            </div>
            <div class="scc-h-ptz">
              <div class="scc-ptz-spacer"></div>
              <button class="scc-btn" id="scc-up"><ha-icon icon="mdi:chevron-up"></ha-icon></button>
              <div class="scc-ptz-spacer"></div>
              <button class="scc-btn" id="scc-lt"><ha-icon icon="mdi:chevron-left"></ha-icon></button>
              <button class="scc-btn" id="scc-cal"><ha-icon icon="mdi:home"></ha-icon></button>
              <button class="scc-btn" id="scc-rt"><ha-icon icon="mdi:chevron-right"></ha-icon></button>
              <button class="scc-btn" id="scc-zm">
                <ha-icon icon="mdi:magnify-minus"></ha-icon><span>Z−</span>
              </button>
              <button class="scc-btn" id="scc-dn"><ha-icon icon="mdi:chevron-down"></ha-icon></button>
              <button class="scc-btn" id="scc-zp">
                <ha-icon icon="mdi:magnify-plus"></ha-icon><span>Z+</span>
              </button>
            </div>
          </div>`;
      }
      return `
        <div id="scc-basic">
          <div class="scc-row scc-r5">
            <button class="scc-btn" id="scc-cfg">
              <ha-icon icon="mdi:cog"></ha-icon><span>Avançado</span>
            </button>
            <button class="scc-btn" id="scc-snap">
              <ha-icon icon="mdi:camera-image"></ha-icon><span>Foto</span>
            </button>
            <button class="scc-btn scc-rec" id="scc-rec">
              <ha-icon icon="mdi:record-rec"></ha-icon><span>Gravar</span>
            </button>
            <button class="scc-btn" id="scc-zm">
              <ha-icon icon="mdi:magnify-minus"></ha-icon><span>Zoom −</span>
            </button>
            <button class="scc-btn" id="scc-zp">
              <ha-icon icon="mdi:magnify-plus"></ha-icon><span>Zoom +</span>
            </button>
          </div>
          <div class="scc-row scc-r4">
            <button class="scc-btn" id="scc-lt"><ha-icon icon="mdi:chevron-left"></ha-icon></button>
            <button class="scc-btn" id="scc-up"><ha-icon icon="mdi:chevron-up"></ha-icon></button>
            <button class="scc-btn" id="scc-dn"><ha-icon icon="mdi:chevron-down"></ha-icon></button>
            <button class="scc-btn" id="scc-rt"><ha-icon icon="mdi:chevron-right"></ha-icon></button>
          </div>
        </div>`;
    }

    _htmlAdv() {
      if (this._isH()) {
        return `
          <div id="scc-adv" style="display:none">
            <div class="scc-row scc-r2" style="margin-bottom:6px">
              <button class="scc-btn" id="scc-back">
                <ha-icon icon="mdi:arrow-left"></ha-icon><span>Voltar</span>
              </button>
              <div></div>
            </div>
            <div class="scc-adv-content"></div>
          </div>`;
      }
      return `
        <div id="scc-adv" style="display:none">
          <div class="scc-row scc-r2" style="margin-bottom:6px">
            <button class="scc-btn" id="scc-back">
              <ha-icon icon="mdi:arrow-left"></ha-icon><span>Voltar</span>
            </button>
            <button class="scc-btn" id="scc-cal">
              <ha-icon icon="mdi:home"></ha-icon><span>Posição Home</span>
            </button>
          </div>
          <div class="scc-adv-content"></div>
        </div>`;
    }

    // ── Events ────────────────────────────────────────────────────────────────

    _wireEvents() {
      const ctrl = this._ctrl;
      const svc  = (d, s, data) => this._hass.callService(d, s, data || {});
      const scr  = id => { if (id && this._hass.states[id]) { const [d, s] = id.split('.'); svc(d, s); } };
      const on   = (id, fn) => ctrl.querySelector('#' + id)?.addEventListener('click', fn);

      on('scc-cfg',  () => this._flip());
      on('scc-back', () => this._flip());
      on('scc-snap', async () => {
        const url = this._snapshotUrl();
        if (!url) return;
        try {
          let r;
          if (this._hass?.fetchWithAuth) {
            const path = url.startsWith(window.location.origin)
              ? url.slice(window.location.origin.length)
              : url;
            r = await this._hass.fetchWithAuth(path);
          } else {
            const token = this._hass?.connection?.options?.auth?.data?.access_token || '';
            const headers = token ? { Authorization: `Bearer ${token}` } : {};
            r = await fetch(url, { headers });
          }
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          const blob = await r.blob();
          const blobUrl = URL.createObjectURL(blob);
          const a = Object.assign(document.createElement('a'), {
            href: blobUrl, download: `snapshot_${Date.now()}.jpg`,
          });
          document.body.appendChild(a);
          a.click();
          document.body.removeChild(a);
          setTimeout(() => URL.revokeObjectURL(blobUrl), 30000);
        } catch (e) {
          window.open(url, '_blank');
        }
      });
      on('scc-rec',  () => { const d = this._getDiscovered(); if (d.record) svc('switch', 'toggle', { entity_id: d.record }); });
      on('scc-zm',   () => { const d = this._getDiscovered(); scr(d.ptz.zoom_out); });
      on('scc-zp',   () => { const d = this._getDiscovered(); scr(d.ptz.zoom_in); });
      on('scc-lt',   () => { const d = this._getDiscovered(); scr(d.ptz.left); });
      on('scc-up',   () => { const d = this._getDiscovered(); scr(d.ptz.up); });
      on('scc-dn',   () => { const d = this._getDiscovered(); scr(d.ptz.down); });
      on('scc-rt',   () => { const d = this._getDiscovered(); scr(d.ptz.right); });
      on('scc-cal',  () => { const d = this._getDiscovered(); scr(d.ptz.home); });

      ctrl.addEventListener('click', ev => {
        const psBtn = ev.target.closest('[data-preset-btn]');
        if (psBtn?.dataset.presetBtn) {
          const entityId = psBtn.dataset.presetBtn;
          const sel = ctrl.querySelector(`select[data-preset-sel="${entityId}"]`);
          if (sel) svc('select', 'select_option', { entity_id: entityId, option: sel.value });
          return;
        }
        const b = ev.target.closest('.scc-sw-btn');
        if (!b?.dataset.entity) return;
        svc(b.dataset.entity.split('.')[0], 'toggle', { entity_id: b.dataset.entity });
      });

      ctrl.addEventListener('change', ev => {
        const el = ev.target;
        // Detection mode dropdown
        if (el.dataset.detmode) {
          this._checkAndSetDetMode(el.value, el);
          return;
        }
        if (!el.dataset.entity) return;
        if (el.tagName === 'SELECT')
          svc('select', 'select_option', { entity_id: el.dataset.entity, option: el.value });
        else if (el.tagName === 'INPUT' && el.type === 'range')
          svc('number', 'set_value', { entity_id: el.dataset.entity, value: +el.value });
      });
    }

    // ── Toggle basic ↔ advanced ───────────────────────────────────────────────

    _flip() {
      this._advanced = !this._advanced;
      const basic = this._ctrl.querySelector('#scc-basic');
      const adv   = this._ctrl.querySelector('#scc-adv');
      if (this._advanced) {
        // Horizontal mode: capture basic height before hiding, apply to advanced so card doesn't grow taller
        const snapH = this._isH() ? (basic.getBoundingClientRect().height || basic.offsetHeight) : 0;
        this._buildAdv();
        basic.style.display = 'none';
        adv.style.display   = '';
        if (snapH > 0) { adv.style.height = snapH + 'px'; adv.style.overflowY = 'auto'; }
        this._syncStates();
      } else {
        this._pendingDetMode = null; // clear intent when panel closes
        adv.style.height = adv.style.overflowY = '';
        adv.style.display   = 'none';
        basic.style.display = '';
      }
    }

    // ── Build advanced panel ──────────────────────────────────────────────────

    _buildAdv() {
      const hass    = this._hass;
      const disc    = this._getDiscovered();
      const camName = camNameFrom(this._config.entity);
      const isH     = this._isH();

      const swBtn = id => {
        const on  = hass.states[id]?.state === 'on';
        const lbl = esc(labelFor(hass, id, camName));
        const ico = esc(iconFor(id));
        return `<button class="scc-btn scc-sw-btn${on ? ' on' : ''}" data-entity="${esc(id)}">
          <ha-icon icon="${ico}"></ha-icon><span>${lbl}</span>
        </button>`;
      };

      const setRow = id => {
        const domain = id.split('.')[0];
        const st     = hass.states[id];
        const icon   = `<ha-icon icon="${esc(iconFor(id))}"></ha-icon>`;
        const lbl    = `<span class="scc-set-label">${esc(labelFor(hass, id, camName))}</span>`;
        // Salvar Preset: select (slot choice only) + explicit "Salvar" button — no action on select change
        if (id.endsWith('_salvar_preset')) {
          const opts = (st?.attributes?.options || [])
            .map(o => `<option value="${esc(o)}"${st?.state === o ? ' selected' : ''}>${esc(o)}</option>`)
            .join('');
          return `<div class="scc-set-row">${icon}${lbl}
            <select data-preset-sel="${esc(id)}" style="max-width:100px">${opts}</select>
            <button class="scc-save-btn" data-preset-btn="${esc(id)}">Salvar</button>
          </div>`;
        }
        if (domain === 'number') {
          const { min = 0, max = 100, step = 1 } = st?.attributes || {};
          const val = parseFloat(st?.state) || 0;
          return `<div class="scc-set-row">${icon}${lbl}
            <input type="range" data-entity="${esc(id)}"
              min="${min}" max="${max}" step="${step}" value="${val}">
          </div>`;
        }
        if (domain === 'select') {
          const opts = (st?.attributes?.options || [])
            .map(o => `<option value="${esc(o)}"${st?.state === o ? ' selected' : ''}>${esc(o)}</option>`)
            .join('');
          return `<div class="scc-set-row">${icon}${lbl}
            <select data-entity="${esc(id)}">${opts}</select>
          </div>`;
        }
        return '';
      };

      // ── Detection section ─────────────────────────────────────────────────
      let detHtml = '';
      if (disc.motionSw || disc.frigateDetect) {
        const curMode  = this._currentDetMode(disc);
        const modeOpts = DET_MODES.map(m =>
          `<option value="${esc(m.id)}"${curMode === m.id ? ' selected' : ''}>${esc(m.label)}</option>`
        ).join('');
        detHtml = `
          <div class="scc-section-title">Detecção</div>
          <div class="scc-set-row" style="margin-bottom:4px">
            <ha-icon icon="mdi:motion-sensor"></ha-icon>
            <span class="scc-set-label">Modo</span>
            <select data-detmode="1" style="max-width:160px">${modeOpts}</select>
          </div>`;
        // Detection-linked toggles: Apenas Pessoas + Sirene Automática
        const detToggles = [disc.filtroHumanoSw, disc.sireneAutoSw].filter(Boolean);
        if (detToggles.length) {
          detHtml += `<div class="scc-sw-grid" style="margin-bottom:8px">
            ${detToggles.map(swBtn).join('')}
          </div>`;
        } else {
          detHtml += '<div style="margin-bottom:8px"></div>';
        }
      }

      // ── Frigate section (before Camera) ───────────────────────────────────
      let frigHtml = '';
      if (disc.frigateAdv.length) {
        frigHtml = '<div class="scc-section-title">Frigate</div>';
        frigHtml += `<div class="scc-sw-grid">${disc.frigateAdv.map(swBtn).join('')}</div>`;
      }

      // ── Camera section ────────────────────────────────────────────────────
      let camHtml = '';
      if (disc.tuyaSwitches.length || disc.tuyaSelects.length || disc.tuyaNumbers.length) {
        camHtml = '<div class="scc-section-title">Câmera</div>';
        if (disc.tuyaSwitches.length)
          camHtml += `<div class="scc-sw-grid">${disc.tuyaSwitches.map(swBtn).join('')}</div>`;
        if (disc.tuyaSelects.length || disc.tuyaNumbers.length)
          camHtml += `<div class="scc-set-list">
            ${disc.tuyaSelects.map(setRow).join('')}
            ${disc.tuyaNumbers.map(setRow).join('')}
          </div>`;
      }

      // Horizontal: detection left, frigate+camera right
      const html = isH
        ? `<div class="scc-adv-cols"><div>${detHtml}</div><div>${frigHtml}${camHtml}</div></div>`
        : detHtml + frigHtml + camHtml;

      this._ctrl.querySelector('.scc-adv-content').innerHTML = html;
    }

    // ── State sync ────────────────────────────────────────────────────────────

    _syncStates() {
      if (!this._built || !this._hass || !this._ctrl) return;
      const ctrl = this._ctrl;
      const st   = id => this._hass.states[id]?.state;
      const disc = this._getDiscovered();

      if (disc.record)
        ctrl.querySelector('#scc-rec')?.classList.toggle('on', st(disc.record) === 'on');

      ctrl.querySelectorAll('.scc-sw-btn[data-entity]').forEach(b =>
        b.classList.toggle('on', st(b.dataset.entity) === 'on'));
      ctrl.querySelectorAll('select[data-entity]').forEach(s => {
        const v = st(s.dataset.entity); if (v != null) s.value = v;
      });
      ctrl.querySelectorAll('select[data-preset-sel]').forEach(s => {
        const v = st(s.dataset.presetSel); if (v != null) s.value = v;
      });
      ctrl.querySelectorAll('input[type=range][data-entity]').forEach(i => {
        const v = st(i.dataset.entity); if (v != null) i.value = parseFloat(v);
      });

      if (this._advanced) {
        const actual = this._currentDetMode(disc);
        const detSel = ctrl.querySelector('select[data-detmode]');
        if (detSel) {
          if (this._pendingDetMode) {
            if (actual === this._pendingDetMode) this._pendingDetMode = null; // state converged
            detSel.value = this._pendingDetMode || actual;
          } else {
            detSel.value = actual;
          }
        }
      }
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    _getDiscovered() {
      if (!this._hass) return {
        ptz: {}, record: null, frigateAdv: [], frigateDetect: null,
        motionSw: null, motionBridge: null,
        filtroHumanoSw: null, sireneAutoSw: null,
        tuyaSwitches: [], tuyaSelects: [], tuyaNumbers: [],
      };
      return discoverEntities(this._hass, camNameFrom(this._config.entity));
    }

    _currentDetMode(disc) {
      const st     = id => this._hass?.states[id]?.state;
      const motion = disc.motionSw      ? st(disc.motionSw)      === 'on' : false;
      const detect = disc.frigateDetect ? st(disc.frigateDetect) === 'on' : false;
      const bridge = disc.motionBridge  ? st(disc.motionBridge)  === 'on' : false;
      if (!motion && !detect) return 'off';
      if (!motion)            return 'frigate';  // detect=true implied
      if ( motion && detect)  return 'dual';     // both on → dual (bridge off per truth table)
      return bridge ? 'cam_fr' : 'camera';       // motion only, bridge decides cam_fr vs camera
    }

    _setDetectionMode(modeId) {
      // Show user's intent immediately; _syncStates will clear once state converges
      this._pendingDetMode = modeId;
      const disc = this._getDiscovered();
      const cfg  = DET_MODES.find(m => m.id === modeId);
      if (!cfg) return;
      const svc = (d, s, e) => this._hass.callService(d, s, { entity_id: e });
      if (disc.motionSw)      svc('switch',        cfg.motion ? 'turn_on' : 'turn_off', disc.motionSw);
      if (disc.frigateDetect) svc('switch',        cfg.detect ? 'turn_on' : 'turn_off', disc.frigateDetect);
      if (disc.motionBridge)  svc('input_boolean', cfg.bridge ? 'turn_on' : 'turn_off', disc.motionBridge);
    }

    async _checkAndSetDetMode(modeId, selectEl) {
      const slug   = camNameFrom(this._config.entity);
      const fetch_ = (url, opts) => this._hass.fetchWithAuth(url, opts).then(r => r.json()).catch(() => null);
      const revert = () => {
        if (selectEl) selectEl.value = this._currentDetMode(this._getDiscovered());
        this._pendingDetMode = null;
      };

      if (modeId === 'cam_fr') {
        const companion = await fetch_('/api/ekaza_wizard/companion/status');
        if (!companion?.running) { this._showCompanionWarning(revert); return; }
        await fetch_('/api/ekaza_wizard/proxy/toggle', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ slug, enable: true }),
        });
      } else {
        await fetch_('/api/ekaza_wizard/proxy/toggle', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ slug, enable: false }),
        });
      }
      this._setDetectionMode(modeId);
    }

    _showCompanionWarning(onCancel) {
      this._root.querySelector('#scc-proxy-dlg')?.remove();
      const dlg = document.createElement('dialog');
      dlg.id = 'scc-proxy-dlg';
      dlg.innerHTML = `
        <style>
          #scc-proxy-dlg { border:2px solid #f5a623; border-radius:8px; background:#16213e;
            color:#eaeaea; padding:20px; max-width:360px; width:90vw; font-family:inherit; }
          #scc-proxy-dlg::backdrop { background:rgba(0,0,0,.65); }
          #scc-proxy-dlg h4 { color:#f5a623; margin:0 0 10px; font-size:.93rem; display:flex; align-items:center; gap:6px; }
          #scc-proxy-dlg p { font-size:.81rem; line-height:1.65; margin:0 0 14px; color:#c8c8d8; }
          #scc-proxy-dlg a { color:#4ecca3; }
          .scc-dlg-actions { display:flex; justify-content:flex-end; margin-top:4px; }
          .scc-dlg-actions button { padding:7px 13px; border:none; border-radius:4px;
            cursor:pointer; font-size:.8rem; font-weight:600; font-family:inherit;
            background:#0f3460; color:#eaeaea; }
        </style>
        <h4>⚠️ Tuya Proxy Companion não encontrado</h4>
        <p>O modo <strong>Câmera → Frigate</strong> requer o add-on
        <strong>Tuya Proxy Companion</strong> instalado e rodando no Home Assistant.</p>
        <p>Instale-o em: <a href="https://github.com/felipearmat/tuya-proxy-companion" target="_blank">
          github.com/felipearmat/tuya-proxy-companion</a></p>
        <div class="scc-dlg-actions">
          <button id="scc-dlg-cancel">Fechar</button>
        </div>`;
      this._root.appendChild(dlg);
      dlg.showModal();
      dlg.querySelector('#scc-dlg-cancel').addEventListener('click', () => {
        dlg.close(); dlg.remove(); onCancel();
      });
    }

    _snapshotUrl() {
      const c = this._config;
      if (c.snapshot_url) return c.snapshot_url;
      if (c.frigate_host) return `${c.frigate_host}/api/${camNameFrom(c.entity)}/latest.jpg`;
      return `${window.location.origin}/api/camera_proxy/${c.entity}?time=${Date.now()}`;
    }
  }

  customElements.define(CARD_TYPE, EkazaCameraCard);

  window.customCards = window.customCards || [];
  if (!window.customCards.find(c => c.type === CARD_TYPE)) {
    window.customCards.push({
      type:        CARD_TYPE,
      name:        'eKaza Camera Card',
      description: 'Painel eKaza — PTZ Tuya, LocalTuya, Frigate — descobre entidades automaticamente pelo nome da câmera',
      preview:     false,
    });
  }

  console.info(
    `%c EKAZA-CAMERA-CARD %c v${VERSION} `,
    'background:#03a9f4;color:#fff;font-weight:700;padding:1px 4px',
    'background:#555;color:#fff;font-weight:400;padding:1px 4px',
  );
})();
