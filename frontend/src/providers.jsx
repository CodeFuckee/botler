// AI API 供应商预设清单与 logo（issue #46）：
// 设置页「AI API 供应商」卡片使用。预设决定默认 base_url / model 与
// logo 展示，选择后自动填充表单（均可修改）；custom = 用户自定义供应商
// （通用云朵图标）。logo 为内联 SVG（品牌色 + 品牌简化图形），不同
// 供应商显示各自 logo，而不是所有供应商共用一个图标。
//
// 后端 config.yaml 只存 provider key，logo 由前端按 key 映射渲染。
// 识图模型预设清单（issue #135）：设置页「识图模型」卡片使用。
// 预设决定默认 base_url / model（选择后自动填充表单，均可修改）；
// custom = 用户自定义类型（通用云朵图标）。logo 复用上面 AI 供应商
// 的 gemini / openai 品牌图标。
export const IMAGE_MODEL_PRESETS = [
  { key: 'gemini_nano_banana', name: 'Gemini Nano Banana Pro', baseUrl: 'https://generativelanguage.googleapis.com/v1beta', model: 'gemini-3-pro-image' },
  { key: 'openai_gpt_image', name: 'OpenAI GPT Image 2', baseUrl: 'https://api.openai.com/v1', model: 'gpt-image-2' },
  { key: 'custom', name: '自定义', baseUrl: '', model: '' },
]

// 按 key 取识图模型预设；未知 key（历史数据）回退 custom。
export function imageModelPresetOf(key) {
  return IMAGE_MODEL_PRESETS.find((p) => p.key === key) || IMAGE_MODEL_PRESETS.at(-1)
}

// 识图模型类型名称（列表展示用；未知 key 直接显示原 key）。
export function imageModelName(key) {
  const p = IMAGE_MODEL_PRESETS.find((x) => x.key === key)
  return p ? p.name : key || '自定义'
}


export const AI_PROVIDER_PRESETS = [
  { key: 'deepseek', name: 'DeepSeek', baseUrl: 'https://api.deepseek.com/v1', model: 'deepseek-chat' },
  { key: 'openai', name: 'OpenAI', baseUrl: 'https://api.openai.com/v1', model: 'gpt-4o' },
  { key: 'anthropic', name: 'Anthropic', baseUrl: 'https://api.anthropic.com/v1', model: 'claude-sonnet-5' },
  { key: 'gemini', name: 'Google Gemini', baseUrl: 'https://generativelanguage.googleapis.com/v1beta', model: 'gemini-2.5-pro' },
  { key: 'moonshot', name: 'Moonshot (Kimi)', baseUrl: 'https://api.moonshot.cn/v1', model: 'moonshot-v1-8k' },
  { key: 'qwen', name: '通义千问', baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen-max' },
  { key: 'zhipu', name: '智谱 GLM', baseUrl: 'https://open.bigmodel.cn/api/paas/v4', model: 'glm-4-plus' },
  { key: 'siliconflow', name: '硅基流动', baseUrl: 'https://api.siliconflow.cn/v1', model: 'deepseek-ai/DeepSeek-V3' },
  { key: 'ollama', name: 'Ollama', baseUrl: 'http://localhost:11434/v1', model: 'llama3.1' },
  { key: 'openrouter', name: 'OpenRouter', baseUrl: 'https://openrouter.ai/api/v1', model: 'openai/gpt-4o' },
  { key: 'custom', name: '自定义', baseUrl: '', model: '' },
]

/** 按 key 取预设；未知 key（历史数据）回退 custom。 */
export function presetOf(key) {
  return AI_PROVIDER_PRESETS.find((p) => p.key === key) || AI_PROVIDER_PRESETS.at(-1)
}

/** 供应商名称（列表展示用；未知 key 直接显示原 key）。 */
export function providerName(key) {
  const p = AI_PROVIDER_PRESETS.find((x) => x.key === key)
  return p ? p.name : key || '自定义'
}

// 各供应商 logo：品牌色圆底 + 品牌简化图形（24×24 viewBox）
const LOGOS = {
  deepseek: {
    bg: '#4D6BFE',
    node: <path d="M8 6.2h6a5.8 5.8 0 0 1 0 11.6H8V6.2z" fill="#fff" />,
  },
  openai: {
    bg: '#10A37F',
    node: (
      <polygon
        points="12,3.6 19.3,7.8 19.3,16.2 12,20.4 4.7,16.2 4.7,7.8"
        fill="none" stroke="#fff" strokeWidth="1.7" strokeLinejoin="round"
      />
    ),
  },
  anthropic: {
    bg: '#D97757',
    node: (
      <path
        d="M9 6.5l1.9 11M15 6.5l-1.9 11"
        stroke="#fff" strokeWidth="2.4" strokeLinecap="round"
      />
    ),
  },
  gemini: {
    bg: '#4285F4',
    node: (
      <polygon
        points="12,3.8 13.6,10.4 20.2,12 13.6,13.6 12,20.2 10.4,13.6 3.8,12 10.4,10.4"
        fill="#fff"
      />
    ),
  },
  moonshot: {
    bg: '#FFB300',
    node: (
      <>
        <circle cx="15" cy="9.6" r="6.1" fill="#fff" />
        <circle cx="12.8" cy="11.6" r="6.1" fill="#FFB300" />
      </>
    ),
  },
  qwen: {
    bg: '#615CED',
    node: (
      <text x="12" y="15.6" textAnchor="middle" fontSize="11.5" fontWeight="700"
        fill="#fff" fontFamily="sans-serif">Q</text>
    ),
  },
  zhipu: {
    bg: '#3859FF',
    node: (
      <text x="12" y="15.6" textAnchor="middle" fontSize="11.5" fontWeight="700"
        fill="#fff" fontFamily="sans-serif">Z</text>
    ),
  },
  siliconflow: {
    bg: '#FF6A00',
    node: (
      <path
        d="M5.2 13.2c2.3-2.6 4.5-2.6 6.8 0s4.5 2.6 6.8 0"
        fill="none" stroke="#fff" strokeWidth="2" strokeLinecap="round"
      />
    ),
  },
  ollama: {
    bg: '#111111',
    node: (
      <text x="12" y="15.6" textAnchor="middle" fontSize="11.5" fontWeight="700"
        fill="#fff" fontFamily="sans-serif">O</text>
    ),
  },
  openrouter: {
    bg: '#6C4CF1',
    node: (
      <circle
        cx="12" cy="12" r="6.4" fill="none" stroke="#fff" strokeWidth="2.6"
        strokeDasharray="31 9" strokeLinecap="round" transform="rotate(-90 12 12)"
      />
    ),
  },
  custom: {
    bg: '#8899AA',
    node: (
      <path
        d="M7.2 17.2a4 4 0 0 1-.7-7.95A5.4 5.4 0 0 1 17 8.6a4.4 4.4 0 0 1-.2 8.6H7.2z"
        fill="#fff"
      />
    ),
  },
}

/** 供应商 logo（24×24 内联 SVG）。未知 key 回退 custom 通用图标。 */
export function ProviderLogo({ provider, size = 20 }) {
  const def = LOGOS[provider] || LOGOS.custom
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" className="provider-logo" aria-hidden="true">
      <circle cx="12" cy="12" r="12" fill={def.bg} />
      {def.node}
    </svg>
  )
}
