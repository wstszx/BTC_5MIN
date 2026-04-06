
const state = {
  config: null,
  market: null,
  summary: null,
  recent: null,
  countdownSnapshotAtMs: null,
  countdownBaseSeconds: null,
  showInternalKeys: false,
  helpOpen: false,
  helpTab: 'quickstart',
  helpReturnFocusId: 'btnHelp',
};

const POLL_MS = {
  market: 3000,
  summary: 20000,
  recent: 12000,
  clock: 1000,
};

const HELP_TABS = [
  { id: 'quickstart', label: '快速上手' },
  { id: 'pageguide', label: '页面说明' },
  { id: 'configdict', label: '配置字典' },
  { id: 'strategyguide', label: '策略说明' },
  { id: 'faq', label: '常见问题' },
];

const HELP_SECTIONS = {
  quickstart: {
    title: '快速上手',
    intro: '先确认基础策略、下注模式、最大下注金额，再观察 3~5 个轮次，不要频繁同时改动多项参数。',
    sections: [
      {
        title: '先看哪里',
        bullets: [
          '先看 行情与信号，确认当前轮次、方向判断和倒计时。',
          '再看 下注计划与风控，重点关注是否下单、跳过原因和预期收益。',
          '然后看 会话状态，确认累计盈亏、待回补亏损和连续亏损轮数。',
          '最后看 实时连接状态，判断 websocket 行情是否可靠。',
        ],
      },
      {
        title: '怎么安全改参数',
        bullets: [
          '先确认当前基础策略，固定节奏策略和策略 5 的可调参数不同。',
          '每次只改一类参数，不要同时改策略、阈值和下注金额。',
          '保存后以页面显示的有效值和字段提示为准，不要只看自己输入了什么。',
          '如果字段出现校验错误，说明该输入没有真正生效，需要先修正。',
        ],
      },
      {
        title: '怎么判断当前能不能跑',
        bullets: [
          '是否下单=执行，说明当前轮次、价格、风控和 WS 状态都允许。',
          '是否下单=跳过，先看跳过原因，不要先怀疑策略失效。',
          '价格超过阈值、信号太弱、金额超限，属于常见可接受跳过。',
          'WS 数据陈旧、当日亏损上限、连续亏损重置，属于要优先排查的跳过。',
        ],
      },
      {
        title: '出问题先看哪里',
        bullets: [
          '保存了但效果不对：先看参数区字段提示和有效值。',
          '一直不下单：先看 跳过原因 和 实时连接状态。',
          '方向看不懂：去 策略说明，对照固定节奏或动量逻辑。',
          '当天收益异常：看 纸面交易汇总 和 最近纸面交易明细。',
        ],
      },
    ],
  },
  pageguide: {
    title: '页面元素说明',
    sections: [
      {
        title: '参数引擎',
        bullets: [
          '用于查看并编辑运行参数。',
          '重点关注基础策略、下注模式、风控边界，以及哪些字段只对策略 5 生效。',
        ],
      },
      {
        title: '行情与信号',
        bullets: [
          '用于观察当前轮次市场状态和方向判断。',
          '重点关注方向、原因、阈值、偏移和是否已锁边。',
        ],
      },
      {
        title: '下注计划与风控',
        bullets: [
          '用于判断当前轮次是否允许执行。',
          '重点关注是否下单、买入价格、下单金额和跳过原因。',
        ],
      },
      {
        title: '会话状态',
        bullets: [
          '用于看累计收益和当前恢复状态。',
          '重点关注累计盈亏、待回补亏损、连续亏损轮数和当日已实现盈亏。',
        ],
      },
      {
        title: '实时连接状态',
        bullets: [
          '用于判断 websocket 行情是否可信。',
          '重点关注最近消息延迟、重连次数、最近错误和是否触发陈旧保护。',
        ],
      },
      {
        title: '纸面交易汇总',
        bullets: [
          '用于从日维度查看策略近期表现。',
          '适合看趋势，不适合解释某一笔具体异常。',
        ],
      },
      {
        title: '最近纸面交易明细',
        bullets: [
          '用于排查最近交易到底发生了什么。',
          '重点关注时间、方向、结果、跳过原因和信号偏移。',
        ],
      },
    ],
  },
};

const HELP_FAQ = [
  ['为什么我保存了参数，但感觉没生效？', '先看参数区字段提示和有效值；如果输入非法，系统会回退到有效配置，而不是按错误值运行。'],
  ['为什么当前显示不下单？', '先看下注计划与风控里的跳过原因，再区分是价格、风控、信号还是 WS 保护导致。'],
  ['为什么策略 5 经常没信号？', '策略 5 不是固定节奏，需要价格变化达到阈值；弱信号时会按 SKIP 或 FALLBACK 处理。'],
  ['为什么方向和我想的不一样？', '固定节奏策略先看轮次编号；动量策略则要看开盘价、当前价、阈值和偏移。'],
  ['为什么 WS 保护会触发？', '说明 websocket 行情太旧，系统为了避免使用过期数据下单而阻止执行。'],
  ['为什么当日已实现盈亏归零了？', '这是日切后的日内统计重置；累计盈亏仍然保留在会话状态里。'],
  ['为什么超过最大下注金额后一直跳过？', '当前恢复亏损和价格条件共同推高了所需下单金额，先看待回补亏损和 MAX_STAKE。'],
  ['新手最容易改错什么？', '一次改太多参数、没分清固定节奏和动量策略、把 WS 保护误以为是策略问题。'],
];

const STORAGE_KEYS = {
  showInternalKeys: 'dashboard_show_internal_keys',
};

const STRATEGY_LABELS = {
  1: '单轮交替',
  2: '双轮分组交替',
  3: '三轮分组交替',
  4: '四轮分组交替',
  5: '动量信号 V2',
};

const OPTION_LABELS = {
  ENABLE_LIVE_TRADING: {
    true: '开启',
    false: '关闭',
  },
  TRADE_MODE: {
    paper: '模拟盘',
    live: '实盘',
  },
  LIVE_TRADING_ENABLED: {
    true: '开启',
    false: '关闭',
  },
  BET_SIZING_MODE: {
    FIXED_BASE_COST: '固定金额模式',
    TARGET_PROFIT: '目标收益模式',
  },
  SIGNAL_WEAK_SIGNAL_MODE: {
    SKIP: '弱信号跳过',
    FALLBACK: '弱信号回退',
  },
  WS_ENABLED: {
    true: '开启',
    false: '关闭',
  },
};

const REASON_LABELS = {
  entry_window_missed: '已错过入场时间',
  ws_stale: '连接数据陈旧',
  signal_unavailable: '信号不可用',
  signal_too_weak_skip: '信号太弱，按规则跳过',
  signal_too_weak: '信号太弱',
  price_above_threshold: '价格超过上限阈值',
  order_cost_above_max_stake: '下单金额超过单笔上限',
  order_size_not_positive: '下单份额无效',
  daily_loss_cap_reached: '触发当日亏损上限',
  max_consecutive_losses_reached: '达到连续亏损重置阈值',
  stop_loss_triggered: '触发止损重置',
  manual_skip: '人工跳过',
};

const CONFIG_KEY_NAMES = {
  ENABLE_LIVE_TRADING: '启用实盘',
  TRADE_MODE: '交易模式',
  LIVE_TRADING_ENABLED: '实盘交易开关',
  POLYMARKET_PRIVATE_KEY: '实盘私钥',
  POLYMARKET_FUNDER: '实盘钱包地址',
  STRATEGY_ID: '基础策略',
  TARGET_PROFIT: '每次目标净利',
  BET_SIZING_MODE: '下注模式',
  BASE_ORDER_COST: '固定起始下注金额',
  MAX_CONSECUTIVE_LOSSES: '连亏重置轮数',
  MAX_STAKE: '单笔最大下注金额',
  MAX_PRICE_THRESHOLD: '最高买入价格阈值',
  SIGNAL_MOMENTUM_THRESHOLD: '动量阈值',
  SIGNAL_WEAK_SIGNAL_MODE: '弱信号处理',
  SIGNAL_FALLBACK_STRATEGY_ID: '弱信号回退基础策略',
  SIGNAL_HISTORY_FIDELITY_SECONDS: '信号采样秒数',
  SIGNAL_ANCHOR_MAX_OFFSET_SECONDS: '开盘锚点最大偏移秒',
  SIGNAL_DYNAMIC_THRESHOLD_K: '动态阈值系数K',
  SIGNAL_DYNAMIC_THRESHOLD_MIN_POINTS: '动态阈值最少样本点',
  SIGNAL_LOCK_BEFORE_ENTRY_SECONDS: '入场前锁边秒数',
  MAX_STAKE_SKIP_ALERT_THRESHOLD: '超额跳过告警阈值',
  WS_ENABLED: '实时连接开关',
  WS_QUOTE_STALE_SECONDS: '行情过期秒',
  WS_TRADE_GUARD_STALE_SECONDS: '交易防陈旧阈值秒',
  WS_CONNECT_TIMEOUT_SECONDS: '实时连接超时秒',
};

function reasonText(reason) {
  if (!reason) {
    return '--';
  }
  if (REASON_LABELS[reason]) {
    return REASON_LABELS[reason];
  }
  return '未识别原因：' + String(reason) + '（可尝试刷新页面）';
}

function formatConfigLabel(key, labels) {
  const base = (labels && labels[key]) || CONFIG_KEY_NAMES[key] || key;
  if (state.showInternalKeys) {
    return base + '（' + key + '）';
  }
  return base;
}

function loadUiPrefs() {
  try {
    const raw = localStorage.getItem(STORAGE_KEYS.showInternalKeys);
    if (raw === null) {
      state.showInternalKeys = false;
      return;
    }
    state.showInternalKeys = raw === '1';
  } catch (_err) {
    state.showInternalKeys = false;
  }
}

function saveUiPrefs() {
  try {
    localStorage.setItem(STORAGE_KEYS.showInternalKeys, state.showInternalKeys ? '1' : '0');
  } catch (_err) {
    // Ignore storage failures (private mode / storage disabled)
  }
}

function syncToggleButtonText() {
  el('btnToggleKeys').textContent = '显示内部键名：' + (state.showInternalKeys ? '开' : '关');
}

function openHelpDrawer(tab = 'quickstart') {
  state.helpOpen = true;
  state.helpTab = tab;
  renderHelpDrawer();
  const drawer = el('helpDrawer');
  if (drawer) {
    drawer.focus();
  }
}

function closeHelpDrawer() {
  state.helpOpen = false;
  renderHelpDrawer();
  const trigger = el(state.helpReturnFocusId || 'btnHelp');
  if (trigger) {
    trigger.focus();
  }
}

function el(id) {
  return document.getElementById(id);
}

function esc(text) {
  return String(text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function toNum(value) {
  if (value === null || value === undefined || value === '') {
    return null;
  }
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function fmtNum(value, digits = 4) {
  const n = toNum(value);
  if (n === null) {
    return '--';
  }
  return n.toFixed(digits);
}

function fmtPnl(value, digits = 4) {
  const n = toNum(value);
  if (n === null) {
    return '--';
  }
  const sign = n > 0 ? '+' : '';
  return sign + n.toFixed(digits);
}

function fmtPct(value, digits = 2) {
  const n = toNum(value);
  if (n === null) {
    return '--';
  }
  return (n * 100).toFixed(digits) + '%';
}

function fmtIso(value) {
  if (!value) {
    return '--';
  }
  const dt = new Date(value);
  if (Number.isNaN(dt.getTime())) {
    return String(value);
  }
  return dt.toLocaleString('zh-CN', { hour12: false });
}

function fmtSeconds(value) {
  const n = toNum(value);
  if (n === null) {
    return '--';
  }
  const sign = n < 0 ? '-' : '';
  const abs = Math.abs(Math.floor(n));
  const mm = String(Math.floor(abs / 60)).padStart(2, '0');
  const ss = String(abs % 60).padStart(2, '0');
  return sign + mm + ':' + ss;
}

function fmtDuration(value) {
  const n = toNum(value);
  if (n === null) {
    return '--:--';
  }
  const abs = Math.abs(Math.floor(n));
  const mm = String(Math.floor(abs / 60)).padStart(2, '0');
  const ss = String(abs % 60).padStart(2, '0');
  return mm + ':' + ss;
}

function renderEntryCountdown(secondsToEntry) {
  const sec = toNum(secondsToEntry);
  if (sec === null) {
    el('entryCountdownLabel').textContent = '距离计划入场';
    el('entryCountdown').textContent = '--:--';
    el('entrySyncAt').textContent = '同步于 --';
    state.countdownSnapshotAtMs = null;
    state.countdownBaseSeconds = null;
    return;
  }
  if (sec >= 0) {
    el('entryCountdownLabel').textContent = '距离计划入场';
    el('entryCountdown').textContent = fmtDuration(sec);
  } else {
    el('entryCountdownLabel').textContent = '已过计划入场';
    el('entryCountdown').textContent = fmtDuration(sec);
  }
  state.countdownSnapshotAtMs = Date.now();
  state.countdownBaseSeconds = sec;
  el('entrySyncAt').textContent = '同步于 ' + new Date().toLocaleTimeString('zh-CN', { hour12: false });
}

function tickEntryCountdown() {
  if (state.countdownSnapshotAtMs === null || state.countdownBaseSeconds === null) {
    return;
  }
  const elapsed = (Date.now() - state.countdownSnapshotAtMs) / 1000;
  const liveSeconds = state.countdownBaseSeconds - elapsed;
  if (liveSeconds >= 0) {
    el('entryCountdownLabel').textContent = '距离计划入场';
    el('entryCountdown').textContent = fmtDuration(liveSeconds);
  } else {
    el('entryCountdownLabel').textContent = '已过计划入场';
    el('entryCountdown').textContent = fmtDuration(liveSeconds);
  }
}

function sideText(side) {
  if (side === 'UP') return '看涨';
  if (side === 'DOWN') return '看跌';
  if (side === 'SKIP') return '跳过';
  return '待定';
}

function strategyCatalog(payload) {
  return (payload && payload.strategy_catalog) || {};
}

function strategyMeta(payload, strategyId) {
  return strategyCatalog(payload)[String(strategyId || '')] || null;
}

function strategyShortLabel(payload, strategyId) {
  const meta = strategyMeta(payload, strategyId);
  if (meta && meta.label) {
    return meta.label;
  }
  if (STRATEGY_LABELS[String(strategyId || '')]) {
    return STRATEGY_LABELS[String(strategyId || '')];
  }
  return '策略 ' + String(strategyId || '--');
}

function strategyOptionLabel(key, opt, payload) {
  if (key === 'STRATEGY_ID' || key === 'SIGNAL_FALLBACK_STRATEGY_ID') {
    return String(opt) + ' | ' + strategyShortLabel(payload, opt);
  }
  const optMap = OPTION_LABELS[key] || {};
  return optMap[opt] || opt;
}

function strategyPreviewText(token) {
  if (token === 'UP') return '看涨';
  if (token === 'DOWN') return '看跌';
  if (token === 'MOMENTUM') return '动量判断';
  if (token === 'THRESHOLD') return '阈值过滤';
  if (token === 'FALLBACK') return '弱信号回退';
  return String(token || '--');
}

function strategyPreviewClass(token) {
  if (token === 'UP') return 'trade-up';
  if (token === 'DOWN') return 'trade-down';
  return 'strategy-info';
}

function renderStrategyPills(tokens) {
  if (!Array.isArray(tokens) || tokens.length === 0) {
    return '<span class="strategy-pill strategy-info">暂无节奏预览</span>';
  }
  return tokens.map((token) => {
    return '<span class="strategy-pill ' + esc(strategyPreviewClass(token)) + '">' + esc(strategyPreviewText(token)) + '</span>';
  }).join('');
}

function renderStrategyGuide(payload, values) {
  const node = el('strategyGuideCard');
  if (!node) {
    return;
  }

  const currentValues = values || {};
  const envValues = (payload && payload.env_values) || {};
  const strategyId = String(currentValues.STRATEGY_ID ?? envValues.STRATEGY_ID ?? '');
  const meta = strategyMeta(payload, strategyId);
  if (!meta) {
    node.innerHTML = '<div class="empty">暂无策略说明</div>';
    return;
  }

  let extra = '';
  if (strategyId === '5') {
    const weakModeRaw = String(currentValues.SIGNAL_WEAK_SIGNAL_MODE ?? envValues.SIGNAL_WEAK_SIGNAL_MODE ?? '--');
    const weakModeText = (OPTION_LABELS.SIGNAL_WEAK_SIGNAL_MODE || {})[weakModeRaw] || weakModeRaw;
    const fallbackId = String(currentValues.SIGNAL_FALLBACK_STRATEGY_ID ?? envValues.SIGNAL_FALLBACK_STRATEGY_ID ?? '');
    const fallbackMeta = strategyMeta(payload, fallbackId);
    const fallbackPreview = fallbackMeta && Array.isArray(fallbackMeta.preview) ? renderStrategyPills(fallbackMeta.preview) : '';
    extra =
      '<div class="strategy-guide-note">弱信号处理：' + esc(weakModeText) +
      '；回退策略：' + esc(strategyShortLabel(payload, fallbackId)) + '</div>' +
      '<div class="strategy-guide-meta">' + fallbackPreview + '</div>';
  }

  node.innerHTML =
    '<div class="strategy-guide-head">' +
      '<div>' +
        '<div class="strategy-guide-title">' + esc(strategyId + ' | ' + meta.label) + '</div>' +
        '<div class="strategy-guide-subtitle">' + esc(meta.summary || '') + '</div>' +
      '</div>' +
      '<span class="chip ok">配置解读</span>' +
    '</div>' +
    '<div class="strategy-guide-preview">' + renderStrategyPills(meta.preview || []) + '</div>' +
    '<div class="strategy-guide-note">' + esc(meta.detail || '') + '</div>' +
    extra;
}

function applyConfigFieldVisibility(values) {
  const strategyId = String((values && values.STRATEGY_ID) || '');
  const isStrategyFive = strategyId === '5';

  document.querySelectorAll('.field[data-field-scope]').forEach((node) => {
    const scope = node.getAttribute('data-field-scope') || 'all';
    const shouldMute = scope === 'strategy_5_only' && !isStrategyFive;
    node.classList.toggle('field-muted', shouldMute);
    const note = node.querySelector('.field-scope-note');
    if (note) {
      note.textContent = shouldMute ? '当前基础策略未使用此参数，仅策略 5 使用' : '';
    }
  });

  document.querySelectorAll('.config-group[data-group-scope]').forEach((node) => {
    const scope = node.getAttribute('data-group-scope') || 'all';
    const shouldMute = scope === 'strategy_5_only' && !isStrategyFive;
    node.classList.toggle('config-group-muted', shouldMute);
  });
}

function sourceText(source) {
  if (!source) {
    return '--';
  }
  const normalized = String(source).toLowerCase();
  if (normalized === 'websocket') {
    return '实时连接';
  }
  if (normalized === 'http') {
    return 'HTTP回退';
  }
  return String(source);
}

function marketDeadlineText(value) {
  const formatted = fmtIso(value);
  if (!formatted || formatted === "--") {
    return "结束时间 --";
  }
  return "结束时间 " + formatted;
}

function marketTitleText(title) {
  if (!title) {
    return '--';
  }
  const raw = String(title).trim();
  const m = raw.match(/^Bitcoin Up or Down\s*-\s*(.+)\s+ET$/i);
  if (m) {
    const timeRaw = m[1].trim();
    const t = timeRaw.match(/^([A-Za-z]+)\s+(\d{1,2}),\s*(\d{1,2}:\d{2})(AM|PM)\s*-\s*(\d{1,2}:\d{2})(AM|PM)$/i);
    if (t) {
      const monthMap = {
        january: '1月',
        february: '2月',
        march: '3月',
        april: '4月',
        may: '5月',
        june: '6月',
        july: '7月',
        august: '8月',
        september: '9月',
        october: '10月',
        november: '11月',
        december: '12月',
      };
      const monthCn = monthMap[String(t[1]).toLowerCase()] || t[1];
      const day = String(Number(t[2]));

      const to24h = (hhmm, ampm) => {
        const [hRaw, mRaw] = hhmm.split(':');
        let h = Number(hRaw);
        const m = Number(mRaw);
        const isPM = String(ampm).toUpperCase() === 'PM';
        if (isPM && h !== 12) {
          h += 12;
        }
        if (!isPM && h === 12) {
          h = 0;
        }
        return String(h).padStart(2, '0') + ':' + String(m).padStart(2, '0');
      };

      const start = to24h(t[3], t[4]);
      const end = to24h(t[5], t[6]);
      return '比特币涨跌（美东时间 ' + monthCn + day + '日 ' + start + '-' + end + '）';
    }
    return '比特币涨跌（美东时间 ' + timeRaw + '）';
  }
  return raw;
}

function sideClass(side) {
  if (side === 'UP') return 'trade-up';
  if (side === 'DOWN') return 'trade-down';
  return 'trade-skip';
}

const RUNTIME_LABELS = {
  ws_enabled: '实时连接开关',
  ws_available: '实时连接可用',
  ws_connected: '实时连接状态',
  ws_connect_attempts: '连接尝试次数',
  ws_reconnect_count: '重连次数',
  ws_invalid_operation_count: '异常操作次数',
  ws_subscribed_asset_count: '已订阅资产数',
  ws_cached_asset_count: '缓存资产数',
  ws_opened_at: '建连时间',
  ws_last_message_at: '最近消息时间',
  ws_last_message_age_seconds: '消息延迟(秒)',
  ws_last_error: '最近错误',
  reconnects: '重连次数',
  invalid_ops: '异常操作次数',
  connect_attempts: '连接尝试次数',
  subscribed_assets: '已订阅资产数',
  cached_assets: '缓存资产数',
  last_message_age_s: '消息延迟(秒)',
  last_error: '最近错误',
};

const STATUS_LABELS = {
  true: '是',
  false: '否',
};

function classifyPnl(value) {
  const n = toNum(value);
  if (n === null) return '';
  if (n > 0) return 'pnl-plus';
  if (n < 0) return 'pnl-minus';
  return '';
}

function setChip(id, text, kind = '') {
  const node = el(id);
  if (!node) {
    return;
  }
  node.textContent = text;
  node.className = 'chip';
  if (kind) {
    node.classList.add(kind);
  }
}

async function apiGet(path) {
  const resp = await fetch(path, { cache: 'no-store' });
  const data = await resp.json();
  if (!resp.ok) {
    throw buildApiError(data, resp.status);
  }
  return data;
}

function buildApiError(data, status) {
  const err = new Error((data && data.error) || ('HTTP ' + status));
  err.status = status;
  err.fieldErrors = (data && data.field_errors) || {};
  return err;
}

function setConfigError(message) {
  el('cfgError').textContent = message || '--';
}

let saveButtonResetTimer = null;
let savedAtFlashTimer = null;

function setSaveButtonState(state) {
  const button = el('btnSaveConfig');
  if (!button) {
    return;
  }
  if (saveButtonResetTimer) {
    clearTimeout(saveButtonResetTimer);
    saveButtonResetTimer = null;
  }
  button.disabled = state === 'saving';
  if (state === 'saving') {
    button.textContent = '保存中...';
    return;
  }
  if (state === 'saved') {
    button.textContent = '已保存';
    saveButtonResetTimer = setTimeout(() => {
      button.textContent = '保存参数';
      button.disabled = false;
    }, 1800);
    return;
  }
  if (state === 'error') {
    button.textContent = '保存失败';
    saveButtonResetTimer = setTimeout(() => {
      button.textContent = '保存参数';
      button.disabled = false;
    }, 2200);
    return;
  }
  button.textContent = '保存参数';
  button.disabled = false;
}

function flashSavedAt() {
  const node = el('cfgSavedAt');
  if (!node) {
    return;
  }
  if (savedAtFlashTimer) {
    clearTimeout(savedAtFlashTimer);
    savedAtFlashTimer = null;
  }
  node.classList.add('flash-saved');
  savedAtFlashTimer = setTimeout(() => {
    node.classList.remove('flash-saved');
  }, 1800);
}

async function apiPost(path, payload) {
  const resp = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await resp.json();
  if (!resp.ok) {
    throw buildApiError(data, resp.status);
  }
  return data;
}

function renderHelpSectionList(section) {
  return (section.sections || []).map((group) => {
    const items = (group.bullets || []).map((item) => '<li>' + esc(item) + '</li>').join('');
    return '<section class="help-section"><h3>' + esc(group.title || '') + '</h3><ul>' + items + '</ul></section>';
  }).join('');
}

function renderHelpQuickStart() {
  const section = HELP_SECTIONS.quickstart;
  return '<div class="help-intro">' + esc(section.intro || '') + '</div>' + renderHelpSectionList(section);
}

function renderHelpPageGuide() {
  return renderHelpSectionList(HELP_SECTIONS.pageguide);
}

function renderHelpConfigDictionary() {
  const payload = state.config || {};
  const groups = payload.field_groups || [];
  const help = payload.field_help || {};
  const scope = payload.field_scope || {};
  const labels = payload.labels || {};
  const helpGroups = typeof displayFieldGroups !== "undefined" && displayFieldGroups.length > 0 ? displayFieldGroups : groups;

  return helpGroups.map((group) => {
    const items = (group.keys || []).map((key) => {
      const scopeNote = scope[key] === 'strategy_5_only' ? '仅策略 5 重点使用' : '所有策略都可参考';
      return '<li>' +
        '<strong>' + esc(formatConfigLabel(key, labels)) + '</strong>' +
        '<div class="help-item-subkey">' + esc(key) + '</div>' +
        '<div>' + esc(help[key] || '暂无说明') + '</div>' +
        '<div class="help-item-scope">' + esc(scopeNote) + '</div>' +
        '</li>';
    }).join('');
    return '<section class="help-section">' +
      '<h3>' + esc(group.title || '参数分组') + '</h3>' +
      '<ul class="help-detail-list">' + items + '</ul>' +
      '</section>';
  }).join('');
}

function renderHelpStrategyGuide() {
  const payload = state.config || {};
  const envValues = payload.env_values || {};
  const activeId = String(envValues.STRATEGY_ID || '');
  const catalog = payload.strategy_catalog || {};

  return Object.entries(catalog).map(([strategyId, meta]) => {
    const activeCls = strategyId === activeId ? ' help-strategy-card-active' : '';
    const preview = renderStrategyPills(meta.preview || []);
    let extra = '';
    if (strategyId === '5') {
      const weakModeRaw = String(envValues.SIGNAL_WEAK_SIGNAL_MODE || '--');
      const weakModeText = (OPTION_LABELS.SIGNAL_WEAK_SIGNAL_MODE || {})[weakModeRaw] || weakModeRaw;
      extra = '<div class="help-strategy-extra">' +
        '弱信号模式：' + esc(weakModeText) +
        '；回退策略：' + esc(strategyShortLabel(payload, envValues.SIGNAL_FALLBACK_STRATEGY_ID)) +
        '</div>';
    }
    return '<section class="help-strategy-card' + activeCls + '">' +
      '<h3>' + esc(strategyId + ' | ' + (meta.label || '')) + '</h3>' +
      '<div class="help-strategy-summary">' + esc(meta.summary || '') + '</div>' +
      '<div class="help-strategy-preview">' + preview + '</div>' +
      '<div class="help-strategy-detail">' + esc(meta.detail || '') + '</div>' +
      extra +
      '</section>';
  }).join('');
}

function renderHelpFaq() {
  return HELP_FAQ.map(([question, answer]) => {
    return '<section class="help-section">' +
      '<h3>' + esc(question) + '</h3>' +
      '<p>' + esc(answer) + '</p>' +
      '</section>';
  }).join('');
}

function renderHelpDrawer() {
  const backdrop = el('helpBackdrop');
  const drawer = el('helpDrawer');
  const tabs = el('helpTabs');
  const body = el('helpBody');
  const footer = el('helpFooter');
  if (!backdrop || !drawer || !tabs || !body || !footer) {
    return;
  }

  backdrop.classList.toggle('open', state.helpOpen);
  drawer.classList.toggle('open', state.helpOpen);
  drawer.setAttribute('aria-hidden', state.helpOpen ? 'false' : 'true');

  tabs.innerHTML = HELP_TABS.map((tab) => {
    const active = tab.id === state.helpTab ? ' help-tab-active' : '';
    return '<button class="help-tab' + active + '" data-help-tab="' + esc(tab.id) + '" type="button">' + esc(tab.label) + '</button>';
  }).join('');

  if (state.helpTab === 'quickstart') {
    body.innerHTML = renderHelpQuickStart();
  } else if (state.helpTab === 'pageguide') {
    body.innerHTML = renderHelpPageGuide();
  } else if (state.helpTab === 'configdict') {
    body.innerHTML = renderHelpConfigDictionary();
  } else if (state.helpTab === 'strategyguide') {
    body.innerHTML = renderHelpStrategyGuide();
  } else {
    body.innerHTML = renderHelpFaq();
  }
  footer.innerHTML =
    '<a href="docs/dashboard_runbook.md" target="_blank" rel="noreferrer">Dashboard Runbook</a>' +
    '<a href="docs/operations_runbook.md" target="_blank" rel="noreferrer">Operations Runbook</a>' +
    '<a href="docs/daily_ops_checklist.md" target="_blank" rel="noreferrer">Daily Checklist</a>';

  tabs.querySelectorAll('[data-help-tab]').forEach((node) => {
    node.addEventListener('click', () => {
      state.helpTab = node.getAttribute('data-help-tab') || 'quickstart';
      renderHelpDrawer();
    });
  });
}

function formatModeLabel(value) {
  const normalized = String(value || 'paper').toLowerCase();
  return OPTION_LABELS.TRADE_MODE[normalized] || normalized;
}

function isSingleLiveToggleKey(key) {
  return key === 'TRADE_MODE' || key === 'LIVE_TRADING_ENABLED';
}

function buildLiveToggleValue(values) {
  const mode = String(values.TRADE_MODE || 'paper').toLowerCase();
  const enabled = String(values.LIVE_TRADING_ENABLED || 'false').toLowerCase();
  return mode === 'live' && enabled === 'true' ? 'true' : 'false';
}

function expandLiveToggleValues(values) {
  const expanded = { ...values };
  if (!Object.prototype.hasOwnProperty.call(expanded, 'ENABLE_LIVE_TRADING')) {
    return expanded;
  }
  const normalized = String(expanded.ENABLE_LIVE_TRADING || 'false').toLowerCase() === 'true' ? 'true' : 'false';
  expanded.TRADE_MODE = normalized === 'true' ? 'live' : 'paper';
  expanded.LIVE_TRADING_ENABLED = normalized;
  delete expanded.ENABLE_LIVE_TRADING;
  return expanded;
}

function renderRuntimeStatus(payload) {
  el('runtimeSavedMode').textContent = formatModeLabel(payload.saved_mode || 'paper');
  el('runtimeRunningMode').textContent = formatModeLabel(payload.running_mode || 'paper');
  el('runtimeRestartRequired').textContent = payload.restart_required ? '是' : '否';
  el('runtimeLiveReady').textContent = payload.live_ready ? '已就绪' : '未就绪';
  el('runtimeLiveError').textContent = payload.live_validation_error || '--';
}

function shouldConfirmLiveModeSwitch(previousMode, nextMode) {
  previousMode = String(previousMode || 'paper').toLowerCase();
  nextMode = String(nextMode || 'paper').toLowerCase();
  return previousMode !== 'live' && nextMode === 'live';
}

function renderConfig(payload) {
  state.config = payload;
  el('cfgEnvFile').textContent = payload.env_file || '--';
  el('cfgSavedAt').textContent = payload.saved_at ? fmtIso(payload.saved_at) : '--';
  renderRuntimeStatus(payload.runtime_status || {});

  const form = el('configForm');
  form.innerHTML = '';
  const keys = payload.editable_keys || [];
  const labels = payload.labels || {};
  const values = payload.env_values || {};
  const displayValues = { ...values, ENABLE_LIVE_TRADING: buildLiveToggleValue(values) };
  const options = payload.select_options || {};
  const fieldHelp = payload.field_help || {};
  const fieldScope = payload.field_scope || {};
  const validationErrors = payload.validation_errors || {};
  const fieldGroups = Array.isArray(payload.field_groups) && payload.field_groups.length > 0
    ? payload.field_groups
    : [{ title: '全部参数', description: '', keys }];
  const editableKeySet = new Set(['ENABLE_LIVE_TRADING', ...keys.filter((key) => !isSingleLiveToggleKey(key))]);
  const displayFieldGroups = fieldGroups.map((group) => {
    return {
      ...group,
      keys: (group.keys || [])
        .filter((key) => !isSingleLiveToggleKey(key) || key === 'TRADE_MODE')
        .map((key) => {
          const mappedKey = key === 'TRADE_MODE' ? 'ENABLE_LIVE_TRADING' : key;
          return mappedKey;
        })
        .filter((key, index, arr) => editableKeySet.has(key) && arr.indexOf(key) === index),
    };
  });

  for (const group of displayFieldGroups) {
    const groupKeys = (group.keys || []).filter((key) => editableKeySet.has(key));
    if (groupKeys.length === 0) {
      continue;
    }

    const section = document.createElement('section');
    section.className = 'config-group';
    if (group.scope) {
      section.dataset.groupScope = group.scope;
    }

    const head = document.createElement('div');
    head.className = 'config-group-head';
    head.innerHTML =
      '<div class="config-group-title">' + esc(group.title || '参数分组') + '</div>' +
      '<div class="config-group-desc">' + esc(group.description || '') + '</div>';
    section.appendChild(head);

    const grid = document.createElement('div');
    grid.className = 'group-grid';

    for (const key of groupKeys) {
      const wrap = document.createElement('div');
      wrap.className = 'field';
      wrap.dataset.fieldScope = fieldScope[key] || 'all';

      const label = document.createElement('label');
      label.setAttribute('for', 'cfg_' + key);
      label.textContent = formatConfigLabel(key, labels);
      wrap.appendChild(label);

      if (Array.isArray(options[key]) && options[key].length > 0) {
        const select = document.createElement('select');
        select.id = 'cfg_' + key;
        for (const opt of options[key]) {
          const option = document.createElement('option');
          option.value = opt;
          option.textContent = strategyOptionLabel(key, opt, payload);
          if (String(displayValues[key] ?? '') === String(opt)) {
            option.selected = true;
          }
          select.appendChild(option);
        }
        wrap.appendChild(select);
      } else {
        const input = document.createElement('input');
        input.id = 'cfg_' + key;
        input.type = 'text';
        input.value = String(displayValues[key] ?? '');
        wrap.appendChild(input);
      }

      if (fieldHelp[key]) {
        const help = document.createElement('div');
        help.className = 'field-help';
        help.textContent = fieldHelp[key];
        wrap.appendChild(help);
      }

      const scopeNote = document.createElement('div');
      scopeNote.className = 'field-scope-note';
      wrap.appendChild(scopeNote);

      if (validationErrors[key]) {
        const err = document.createElement('div');
        err.className = 'field-error';
        err.textContent = validationErrors[key];
        wrap.appendChild(err);
      }

      grid.appendChild(wrap);
    }

    section.appendChild(grid);
    form.appendChild(section);
  }

  form.oninput = () => {
    const liveValues = expandLiveToggleValues(collectConfigValues());
    renderStrategyGuide(state.config, liveValues);
    applyConfigFieldVisibility(liveValues);
  };
  form.onchange = form.oninput;

  renderStrategyGuide(payload, displayValues);
  applyConfigFieldVisibility(expandLiveToggleValues(displayValues));
  setConfigError('--');
  setChip('cfgStatus', '???', 'ok');
  setSaveButtonState('idle');
}

function collectConfigValues() {
  const payload = {};
  const keys = ['ENABLE_LIVE_TRADING', ...(((state.config && state.config.editable_keys) || []).filter((key) => !isSingleLiveToggleKey(key)))];
  for (const key of keys) {
    const node = el('cfg_' + key);
    if (node) {
      payload[key] = node.value;
    }
  }
  return payload;
}

function areConfigValuesEqual(left, right) {
  const keys = new Set([
    ...Object.keys(left || {}),
    ...Object.keys(right || {}),
  ]);
  for (const key of keys) {
    if (String((left || {})[key] ?? '') !== String((right || {})[key] ?? '')) {
      return false;
    }
  }
  return true;
}

async function refreshConfig() {
  try {
    const data = await apiGet('/api/config');
    renderConfig(data);
  } catch (err) {
    setConfigError(err && err.message ? err.message : '读取配置失败');
    setChip('cfgStatus', '读取失败', 'err');
    console.error(err);
  }
}

async function saveConfig() {
  let values = {};
  try {
    values = expandLiveToggleValues(collectConfigValues());
    const currentValues = expandLiveToggleValues({ ...(((state.config || {}).env_values) || {}) });
    if (areConfigValuesEqual(values, currentValues)) {
      setChip('cfgStatus', '没有变更', 'warn');
      setSaveButtonState('idle');
      return;
    }
    setChip('cfgStatus', '保存中', 'warn');
    setSaveButtonState('saving');
    const previousMode = String((((state.config || {}).env_values || {}).TRADE_MODE || 'paper')).toLowerCase();
    const nextMode = String((values.TRADE_MODE || previousMode || 'paper')).toLowerCase();
    if (shouldConfirmLiveModeSwitch(previousMode, nextMode) && !window.confirm('切换为实盘后，后续下单会按实盘配置执行。确认继续吗？')) {
      setChip('cfgStatus', '已取消', 'warn');
      setSaveButtonState('idle');
      return;
    }
    const data = await apiPost('/api/config', { env_values: values });
    renderConfig(data);
    flashSavedAt();
    setChip('cfgStatus', '已保存', 'ok');
    setSaveButtonState('saved');
  } catch (err) {
    const fieldErrors = err && err.fieldErrors ? err.fieldErrors : {};
    if (Object.keys(fieldErrors).length > 0 && state.config) {
      renderConfig({
        ...state.config,
        env_values: values,
        validation_errors: fieldErrors,
      });
      setChip('cfgStatus', '校验失败', 'err');
      setSaveButtonState('error');
    } else {
      setChip('cfgStatus', '保存失败', 'err');
      setSaveButtonState('error');
    }
    console.error(err);
  }
}

function renderWsRuntime(ws, staleGuard) {
  const list = el('wsRuntimeList');
  const basePairs = [
    ['ws_enabled', ws.ws_enabled],
    ['ws_available', ws.ws_available],
    ['ws_connected', ws.ws_connected],
    ['reconnects', ws.reconnects],
    ['invalid_ops', ws.invalid_ops],
    ['connect_attempts', ws.connect_attempts],
    ['subscribed_assets', ws.subscribed_assets],
    ['cached_assets', ws.cached_assets],
    ['last_message_age_s', ws.last_message_age_s],
    ['last_error', ws.last_error],
  ];

  const used = new Set(basePairs.map((item) => item[0]));
  const extraPairs = Object.entries(ws || {}).filter(([k]) => !used.has(k));
  const pairs = basePairs.concat(extraPairs);

  const rows = pairs.map(([key, value]) => {
    let shown = (value === null || value === undefined || value === '') ? '--' : String(value);
    if (key in STATUS_LABELS && (value === true || value === false)) {
      shown = STATUS_LABELS[value];
    }
    if (key === 'last_error') {
      shown = reasonText(shown);
    }
    if (key === 'last_message_age_s' && shown !== '--') {
      const n = toNum(shown);
      shown = n === null ? shown : n.toFixed(3);
    }
    const displayKey = RUNTIME_LABELS[key] || key;
    return '<div class="runtime-item"><span class="rk">' + esc(displayKey) + '</span><span class="rv">' + esc(shown) + '</span></div>';
  }).join('');

  list.innerHTML = rows || '<div class="empty">暂无 WS 运行数据</div>';

  if (staleGuard) {
    setChip('wsHealth', '已触发陈旧保护', 'err');
  } else if (ws && ws.ws_connected) {
    setChip('wsHealth', '连接正常', 'ok');
  } else {
    setChip('wsHealth', '连接异常', 'warn');
  }
}

function renderMarket(payload) {
  state.market = payload;
  const round = payload.round || null;
  const quote = payload.quote || {};
  const signal = payload.signal || {};
  const plan = payload.plan || {};
  const ss = payload.session_state || {};

  if (!round) {
    el('marketDeadline').textContent = '结束时间 --';
    el('marketSlug').textContent = '暂无可用轮次';
    el('marketTitle').textContent = payload.message || '当前时段没有可交易轮次';
    renderEntryCountdown(null);
    setChip('marketHealth', '无轮次', 'warn');
  } else {
    el('marketDeadline').textContent = marketDeadlineText(round.end_time);
    el('marketSlug').textContent = round.slug || '--';
    el('marketTitle').textContent = marketTitleText(round.title);
    renderEntryCountdown(round.seconds_to_entry);
    setChip('marketHealth', round.is_current ? '当前轮次' : '下一轮次', 'ok');
  }

  el('upPrice').textContent = fmtNum(quote.up_price, 4);
  el('downPrice').textContent = fmtNum(quote.down_price, 4);
  el('upAsk').textContent = fmtNum(quote.up_best_ask, 4);
  el('downAsk').textContent = fmtNum(quote.down_best_ask, 4);
  el('quoteSource').textContent = sourceText(quote.source);
  el('quoteAccepting').textContent = quote.accepting_orders ? '是' : '否';
  el('quoteFetchedAt').textContent = fmtIso(quote.fetched_at);

  const signalSide = signal.side || 'SKIP';
  const signalNode = el('signalSide');
  signalNode.textContent = sideText(signalSide);
  signalNode.className = 'value ' + sideClass(signalSide);

  el('signalReason').textContent = reasonText(signal.reason);
  el('signalOpenUp').textContent = fmtNum(signal.open_up, 4);
  el('signalCurrentUp').textContent = fmtNum(signal.current_up, 4);
  el('signalThreshold').textContent = fmtNum(signal.threshold, 4);
  const deltaNode = el('signalDelta');
  deltaNode.textContent = fmtPnl(signal.delta, 4);
  const dn = toNum(signal.delta);
  deltaNode.className = 'v ' + (dn > 0 ? 'pos' : (dn < 0 ? 'neg' : ''));
  el('signalLocked').textContent = signal.locked ? '是' : '否';

  el('planShouldTrade').textContent = plan.should_trade ? '执行' : '跳过';
  el('planSide').textContent = sideText(plan.side || signalSide);
  el('planPrice').textContent = fmtNum(plan.price, 4);
  el('planOrderCost').textContent = fmtNum(plan.order_cost, 4);
  el('planOrderSize').textContent = fmtNum(plan.order_size, 6);
  el('planExpectedProfit').textContent = fmtPnl(plan.expected_profit, 4);
  el('planSkipReason').textContent = reasonText(plan.skip_reason);
  el('planStopLoss').textContent = plan.stop_loss_triggered ? '是' : '否';

  el('ssRoundIndex').textContent = String(ss.round_index ?? '--');

  const cashNode = el('ssCashPnl');
  cashNode.textContent = fmtPnl(ss.cash_pnl, 4);
  cashNode.className = 'v ' + classifyPnl(ss.cash_pnl);

  const recNode = el('ssRecoveryLoss');
  recNode.textContent = fmtNum(ss.recovery_loss, 4);
  recNode.className = 'v ' + (toNum(ss.recovery_loss) > 0 ? 'warn' : '');

  el('ssConsecutiveLosses').textContent = String(ss.consecutive_losses ?? '--');
  el('ssStopLossCount').textContent = String(ss.stop_loss_count ?? '--');

  const dayNode = el('ssDailyPnl');
  dayNode.textContent = fmtPnl(ss.daily_realized_pnl, 4);
  dayNode.className = 'v ' + classifyPnl(ss.daily_realized_pnl);

  const guardNode = el('wsGuard');
  guardNode.textContent = payload.ws_stale_guard_triggered ? '触发' : '正常';
  guardNode.className = 'value ' + (payload.ws_stale_guard_triggered ? 'trade-down' : 'trade-up');

  el('marketUpdatedAt').textContent = fmtIso(payload.timestamp);

  renderWsRuntime(payload.ws_runtime || {}, !!payload.ws_stale_guard_triggered);
}

function renderSummary(payload) {
  state.summary = payload;
  const latest = payload.latest || null;

  if (!latest) {
    el('sumDate').textContent = '--';
    el('sumTrades').textContent = '--';
    el('sumHitRate').textContent = '--';
    el('sumTotalPnl').textContent = '--';
    el('sumDrawdown').textContent = '--';
    el('sumStrongRate').textContent = '--';
    el('daysTbody').innerHTML = '<tr><td colspan="5" class="empty">暂无纸面数据</td></tr>';
    setChip('paperStatus', '暂无数据', 'warn');
    return;
  }

  el('sumDate').textContent = latest.date || '--';
  el('sumTrades').textContent = String(latest.trade_rows ?? '--');
  el('sumHitRate').textContent = fmtPct(latest.hit_rate, 2);

  const totalNode = el('sumTotalPnl');
  totalNode.textContent = fmtPnl(latest.total_pnl, 4);
  totalNode.className = 'v ' + classifyPnl(latest.total_pnl);

  const ddNode = el('sumDrawdown');
  ddNode.textContent = fmtNum(latest.max_drawdown, 4);
  ddNode.className = 'v warn';

  el('sumStrongRate').textContent = fmtPct(latest.strong_signal_rate, 2);

  const days = (payload.days || []).slice(-14).reverse();
  const rows = days.map((day) => {
    const pnlCls = classifyPnl(day.total_pnl);
    return '<tr>' +
      '<td>' + esc(day.date || '--') + '</td>' +
      '<td>' + esc(String(day.trade_rows ?? '--')) + '</td>' +
      '<td>' + esc(fmtPct(day.hit_rate, 1)) + '</td>' +
      '<td class="' + esc(pnlCls) + '">' + esc(fmtPnl(day.total_pnl, 4)) + '</td>' +
      '<td>' + esc(fmtNum(day.max_drawdown, 4)) + '</td>' +
      '</tr>';
  }).join('');

  el('daysTbody').innerHTML = rows || '<tr><td colspan="5" class="empty">暂无纸面数据</td></tr>';
  setChip('paperStatus', '已更新', 'ok');
}

function renderRecent(payload) {
  state.recent = payload;
  const rows = payload.rows || [];
  const tbody = el('recentTbody');

  if (rows.length === 0) {
    tbody.innerHTML = '<tr><td colspan="10" class="empty">最近没有纸面交易记录</td></tr>';
    setChip('recentStatus', '0 行', 'warn');
    return;
  }

  const html = rows.map((row) => {
    const side = String(row.side || '').toUpperCase();
    const sideCls = sideClass(side);
    const pnlCls = classifyPnl(row.trade_pnl);
    const cashCls = classifyPnl(row.cash_pnl);

    return '<tr>' +
      '<td>' + esc(fmtIso(row.timestamp)) + '</td>' +
      '<td>' + esc(row.event_slug || '--') + '</td>' +
      '<td class="' + esc(sideCls) + '">' + esc(sideText(side)) + '</td>' +
      '<td>' + esc(fmtNum(row.price, 4)) + '</td>' +
      '<td>' + esc(fmtNum(row.order_cost, 4)) + '</td>' +
      '<td>' + esc(row.result || '--') + '</td>' +
      '<td class="' + esc(pnlCls) + '">' + esc(fmtPnl(row.trade_pnl, 4)) + '</td>' +
      '<td class="' + esc(cashCls) + '">' + esc(fmtPnl(row.cash_pnl, 4)) + '</td>' +
      '<td>' + esc(reasonText(row.skip_reason)) + '</td>' +
      '<td>' + esc(fmtPnl(row.signal_delta, 4)) + '</td>' +
      '</tr>';
  }).join('');

  tbody.innerHTML = html;
  setChip('recentStatus', rows.length + ' 行', 'ok');
}

async function refreshMarket() {
  try {
    const data = await apiGet('/api/market');
    renderMarket(data);
  } catch (err) {
    setChip('marketHealth', '刷新失败', 'err');
    console.error(err);
  }
}

async function refreshSummary() {
  try {
    const data = await apiGet('/api/paper/summary');
    renderSummary(data);
  } catch (err) {
    setChip('paperStatus', '刷新失败', 'err');
    console.error(err);
  }
}

async function refreshRecent() {
  try {
    const runningMode = String((((state.config || {}).runtime_status || {}).active_mode || (((state.config || {}).runtime_status || {}).running_mode) || 'paper')).toLowerCase();
    const recentEndpoint = runningMode === 'live' ? '/api/live/recent?limit=80' : '/api/paper/recent?limit=80';
    const data = await apiGet(recentEndpoint);
    renderRecent(data);
  } catch (err) {
    setChip('recentStatus', '刷新失败', 'err');
    console.error(err);
  }
}

async function refreshAll() {
  await Promise.allSettled([
    refreshConfig(),
    refreshMarket(),
    refreshSummary(),
    refreshRecent(),
  ]);
}

function tickClock() {
  const now = new Date();
  el('clockLocal').textContent = '本地 ' + now.toLocaleString('zh-CN', { hour12: false });
  el('clockUtc').textContent = 'UTC ' + now.toISOString().replace('T', ' ').slice(0, 19);
  tickEntryCountdown();
}

function bindActions() {
  syncToggleButtonText();
  el('btnHelp').addEventListener('click', () => {
    state.helpReturnFocusId = 'btnHelp';
    openHelpDrawer('quickstart');
  });
  el('btnHelpClose').addEventListener('click', closeHelpDrawer);
  el('helpBackdrop').addEventListener('click', closeHelpDrawer);
  el('btnToggleKeys').addEventListener('click', () => {
    state.showInternalKeys = !state.showInternalKeys;
    saveUiPrefs();
    syncToggleButtonText();
    if (state.config) {
      renderConfig(state.config);
    }
  });
  el('btnRefreshNow').addEventListener('click', () => {
    refreshAll();
  });
  el('btnReloadConfig').addEventListener('click', () => {
    refreshConfig();
  });
  el('btnSaveConfig').addEventListener('click', () => {
    saveConfig();
  });
}

function startPolling() {
  setInterval(refreshMarket, POLL_MS.market);
  setInterval(refreshSummary, POLL_MS.summary);
  setInterval(refreshRecent, POLL_MS.recent);
  setInterval(tickClock, POLL_MS.clock);
}

async function bootstrap() {
  loadUiPrefs();
  bindActions();
  renderHelpDrawer();
  tickClock();
  await refreshAll();
  startPolling();
}

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && state.helpOpen) {
    closeHelpDrawer();
  }
});

document.addEventListener('DOMContentLoaded', bootstrap);
