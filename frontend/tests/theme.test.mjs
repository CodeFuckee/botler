// 界面显示主题模块测试（issue #217）：三态——跟随系统 / 浅色 / 深色。
//
// 需求：夜间无人值守查看任务状态时浅色 UI 刺眼，设置页「界面显示」提供
// 三态切换（跟随系统 prefers-color-scheme / 浅色 / 深色），偏好持久化到
// localStorage（botler.theme）+ 后端 config.yaml（ui.theme）双向同步，
// 首屏不闪变（index.html inline 脚本 + 应用启动 applyTheme）。
//
// 测试层次：
// 1. loadThemePreference 边界（无存储/异常存储/非法值/合法三态读回）；
// 2. saveThemePreference（合法三态写回 / 非法值忽略 / 异常存储静默）；
// 3. resolveTheme（三态解析 + system 跟随系统 matchMedia）；
// 4. applyTheme（设置 <html data-theme> 与 color-scheme，无 DOM 兜底）；
// 5. watchSystemTheme（system 模式响应系统变化，手动模式不响应，可取消）。
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createServer } from 'vite'

const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const mod = await vite.ssrLoadModule('/src/theme.js')
const {
  THEME_MODES, THEME_STORAGE_KEY, loadThemePreference, saveThemePreference,
  resolveTheme, applyTheme, watchSystemTheme,
} = mod

after(() => vite.close())

/** 内存 storage 桩（可注入异常） */
function makeStorage(init = {}) {
  const map = new Map(Object.entries(init))
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => { map.set(k, String(v)) },
    dump: () => Object.fromEntries(map),
  }
}

const throwingStorage = {
  getItem() { throw new Error('denied') },
  setItem() { throw new Error('denied') },
}

/** 临时替换全局 window.matchMedia（恢复后原样还原） */
function withMatchMedia(matches, fn) {
  const original = global.window
  const matchMedia = () => ({ matches, addEventListener() {}, removeEventListener() {} })
  if (original) {
    global.window = { ...original, matchMedia }
  } else {
    global.window = { matchMedia }
  }
  try {
    return fn()
  } finally {
    if (original) global.window = original
    else delete global.window
  }
}

test('THEME_MODES 三态与存储键约定', () => {
  assert.deepEqual(THEME_MODES, ['system', 'light', 'dark'],
    '主题三态：跟随系统 / 浅色 / 深色')
  assert.equal(THEME_STORAGE_KEY, 'botler.theme',
    '本地偏好存储键应与 index.html 首屏 inline 脚本、App 启动共用')
})

test('loadThemePreference：无存储/异常存储/非法值一律回退 null（跟随系统）', () => {
  assert.equal(loadThemePreference(undefined), null, '无 storage 应默认 null')
  assert.equal(loadThemePreference(null), null, 'null storage 应默认 null')
  assert.equal(loadThemePreference(throwingStorage), null,
    'getItem 抛异常（隐私模式）应默认 null 且不抛错')
  assert.equal(loadThemePreference(makeStorage({ [THEME_STORAGE_KEY]: 'blue' })), null,
    '非法值（手改/旧版本）应回退 null')
  assert.equal(loadThemePreference(makeStorage({ [THEME_STORAGE_KEY]: '' })), null,
    '空串应回退 null')
})

test('loadThemePreference：合法三态原样读回', () => {
  for (const mode of THEME_MODES) {
    const s = makeStorage({ [THEME_STORAGE_KEY]: mode })
    assert.equal(loadThemePreference(s), mode, `${mode} 应原样读回`)
  }
})

test('saveThemePreference：合法三态写回，非法值忽略，异常存储静默', () => {
  const s = makeStorage()
  saveThemePreference(s, 'dark')
  saveThemePreference(s, 'light')
  saveThemePreference(s, 'system')
  assert.deepEqual(s.dump(), {
    [THEME_STORAGE_KEY]: 'system',
  }, '合法三态应写入（最后一次覆盖）')

  saveThemePreference(s, 'blue')
  assert.deepEqual(s.dump(), { [THEME_STORAGE_KEY]: 'system' },
    '非法值应忽略，不覆盖已有偏好')

  assert.doesNotThrow(() => saveThemePreference(undefined, 'dark'),
    '无 storage 应静默忽略')
  assert.doesNotThrow(() => saveThemePreference(throwingStorage, 'dark'),
    'setItem 抛异常（隐私模式）应静默忽略')
})

test('resolveTheme：light/dark 直接采用，system 跟随系统偏好', () => {
  assert.equal(resolveTheme('light'), 'light')
  assert.equal(resolveTheme('dark'), 'dark')
  withMatchMedia(true, () => {
    assert.equal(resolveTheme('system'), 'dark', '系统深色 + system → dark')
    assert.equal(resolveTheme(undefined), 'dark', '未配置 → 跟随系统深色')
    assert.equal(resolveTheme('bad'), 'dark', '非法值 → 跟随系统')
  })
  withMatchMedia(false, () => {
    assert.equal(resolveTheme('system'), 'light', '系统浅色 + system → light')
    assert.equal(resolveTheme('light'), 'light', '手动浅色不受系统影响')
  })
})

test('applyTheme：设置 <html data-theme> 与 color-scheme，返回实际主题', () => {
  // 用桩记录 setAttribute 调用
  const calls = []
  const el = {
    style: {},
    setAttribute: (k, v) => calls.push([k, v]),
  }
  assert.equal(applyTheme('dark', { documentElement: el }), 'dark')
  assert.deepEqual(calls, [['data-theme', 'dark']], '深色应设置 data-theme=dark')
  assert.equal(el.style.colorScheme, 'dark', '应同步 color-scheme（原生控件/滚动条）')

  applyTheme('light', { documentElement: el })
  assert.deepEqual(calls.at(-1), ['data-theme', 'light'], '手动浅色应设置 data-theme=light')
  assert.equal(el.style.colorScheme, 'light', '切回浅色应同步 color-scheme')

  assert.doesNotThrow(() => applyTheme('dark', null), '无 DOM 环境应静默跳过')
  assert.doesNotThrow(() => applyTheme('dark', {}), '无 documentElement 应静默跳过')
})

test('watchSystemTheme：system 模式响应系统变化，手动模式不响应，可取消', () => {
  // 构造带 add/removeEventListener 记录的 matchMedia 桩
  const listeners = new Set()
  const mq = {
    matches: false,
    addEventListener: (_t, fn) => { listeners.add(fn) },
    removeEventListener: (_t, fn) => { listeners.delete(fn) },
  }
  const original = global.window
  global.window = { matchMedia: () => mq }
  try {
    let fired = 0
    const un = watchSystemTheme(() => 'system', () => { fired += 1 })
    assert.equal(listeners.size, 1, 'system 模式应注册系统变化监听')
    for (const fn of [...listeners]) fn()
    assert.equal(fired, 1, 'system 模式下系统深色变化应触发重新应用')

    // 手动 dark：注册监听但 handler 内按 modeFn 过滤，不触发回调
    let fired2 = 0
    const un2 = watchSystemTheme(() => 'dark', () => { fired2 += 1 })
    assert.equal(listeners.size, 2, '每个 watcher 各注册一个监听（handler 内按 mode 过滤）')
    for (const fn of [...listeners]) fn()
    assert.equal(fired2, 0, '手动 light/dark 不响应系统变化')
    assert.equal(fired, 2, 'system 模式 watcher 继续响应系统变化')

    un()
    un2()
    assert.equal(listeners.size, 0, '取消监听后系统变化不再回调')
  } finally {
    if (original) global.window = original
    else delete global.window
  }

  // 无 matchMedia 环境（SSR）：注册即返回空操作，不抛错
  const original2 = global.window
  global.window = {}
  try {
    let fired3 = 0
    assert.doesNotThrow(() => {
      const un3 = watchSystemTheme(() => 'system', () => { fired3 += 1 })
      un3()
    })
    assert.equal(fired3, 0)
  } finally {
    if (original2) global.window = original2
    else delete global.window
  }
})
