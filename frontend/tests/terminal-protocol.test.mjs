// Web 终端协议与纯逻辑测试（issue #183）：
// - protocol.js：terminado 标准 JSON 协议编解码 + WS 地址构造；
// - tabs.js：标签命名 / 状态文案 / 快捷键解析；
// - attach.js：等价 AttachAddon 的直连适配层（stdin/resize 编码 + 状态回调）。
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { createServer } from 'vite'

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const vite = await createServer({
  server: { middlewareMode: true },
  appType: 'custom',
  logLevel: 'error',
})
const { encodeStdin, encodeResize, decodeMessage, buildWsUrl } =
  await vite.ssrLoadModule('/src/terminal/protocol.js')
const { MAX_TABS, handleTerminalKey, nextTabName, statusLabel } =
  await vite.ssrLoadModule('/src/terminal/tabs.js')
const { TerminalAttach } = await vite.ssrLoadModule('/src/terminal/attach.js')

after(() => vite.close())

// ---- protocol.js ----
test('encodeStdin 编码 terminado stdin 消息', () => {
  assert.equal(encodeStdin('ls -la'), '["stdin","ls -la"]')
  assert.equal(encodeStdin('中文输入'), '["stdin","中文输入"]')
})

test('encodeResize 编码 terminado set_size 消息（rows, cols 顺序）', () => {
  assert.equal(encodeResize(120, 30), '["set_size",30,120]')
})

test('decodeMessage 解析文本消息（setup/stdout/disconnect）', () => {
  assert.deepEqual(decodeMessage('["setup",{}]'), { type: 'setup', payload: {} })
  assert.deepEqual(decodeMessage('["stdout","hello\\n"]'), { type: 'stdout', payload: 'hello\n' })
  assert.deepEqual(decodeMessage('["disconnect",1]'), { type: 'disconnect', payload: 1 })
})

test('decodeMessage 解析二进制消息并容错非法输入', () => {
  const enc = new TextEncoder().encode('["stdout","ok"]')
  assert.deepEqual(decodeMessage(enc.buffer), { type: 'stdout', payload: 'ok' })
  assert.deepEqual(decodeMessage(enc), { type: 'stdout', payload: 'ok' })
  assert.equal(decodeMessage('not-json'), null)
  assert.equal(decodeMessage('[]'), null)
  assert.equal(decodeMessage(null), null)
  assert.equal(decodeMessage(42), null)
})

test('buildWsUrl 构造同源反向代理地址（http→ws / https→wss + token）', () => {
  const httpLoc = { protocol: 'http:', host: '10.0.0.122:8000' }
  assert.equal(
    buildWsUrl(httpLoc, '终端 1', 'tok-1'),
    'ws://10.0.0.122:8000/api/terminal/ws/%E7%BB%88%E7%AB%AF%201?token=tok-1',
  )
  const httpsLoc = { protocol: 'https:', host: 'home.chenkaidi.top:509' }
  assert.equal(
    buildWsUrl(httpsLoc, 't1', ''),
    'wss://home.chenkaidi.top:509/api/terminal/ws/t1',
  )
})

// ---- tabs.js ----
test('nextTabName 依次编号且单调递增（关闭不回收）', () => {
  assert.equal(nextTabName([]), '终端 1')
  assert.equal(nextTabName([{ name: '终端 1' }]), '终端 2')
  assert.equal(nextTabName([{ name: '终端 1' }, { name: '终端 2' }]), '终端 3')
  // 关闭 1 后继续递增（编号不回收，避免歧义）
  assert.equal(nextTabName([{ name: '终端 2' }, { name: '终端 3' }]), '终端 4')
})

test('statusLabel 状态文案映射与兜底', () => {
  assert.equal(statusLabel('connecting'), '连接中')
  assert.equal(statusLabel('connected'), '已连接')
  assert.equal(statusLabel('closed'), '已断开')
  assert.equal(statusLabel('unknown'), 'unknown')
  assert.equal(statusLabel(''), '')
  assert.equal(statusLabel(null), '')
})

test('handleTerminalKey 快捷键：Alt+T 新建、Alt+W 关闭、其余忽略', () => {
  assert.equal(handleTerminalKey({ altKey: true, ctrlKey: false, shiftKey: false, metaKey: false, key: 't' }, true), 'new')
  assert.equal(handleTerminalKey({ altKey: true, ctrlKey: false, shiftKey: false, metaKey: false, key: 'T' }, true), 'new')
  assert.equal(handleTerminalKey({ altKey: true, ctrlKey: false, shiftKey: false, metaKey: false, key: 'w' }, true), 'close')
  // 无标签时 Alt+W 不关闭
  assert.equal(handleTerminalKey({ altKey: true, ctrlKey: false, shiftKey: false, metaKey: false, key: 'w' }, false), null)
  // 修饰键混用 / 其他按键忽略（避免干扰浏览器快捷键）
  assert.equal(handleTerminalKey({ altKey: true, ctrlKey: true, shiftKey: false, metaKey: false, key: 't' }, true), null)
  assert.equal(handleTerminalKey({ altKey: false, ctrlKey: true, shiftKey: true, metaKey: false, key: 't' }, true), null)
  assert.equal(handleTerminalKey({ altKey: true, ctrlKey: false, shiftKey: false, metaKey: false, key: 'x' }, true), null)
})

test('MAX_TABS 上限为 8', () => {
  assert.equal(MAX_TABS, 8)
})

// ---- attach.js：用假 WebSocket 验证 stdin/resize 编码与状态回调 ----
function fakeSocket() {
  const listeners = {}
  return {
    readyState: 1,
    sent: [],
    addEventListener(type, fn) { listeners[type] = fn },
    removeEventListener(type) { delete listeners[type] },
    send(data) { this.sent.push(data) },
    close() { this.closed = true },
    emit(type, ev) { if (listeners[type]) listeners[type](ev) },
  }
}

function fakeTerm() {
  const handlers = {}
  return {
    handlers,
    write() {},
    onData(fn) { handlers.data = fn; return { dispose() { handlers.data = null } } },
    onResize(fn) { handlers.resize = fn; return { dispose() { handlers.resize = null } } },
  }
}

test('TerminalAttach：onData 编码 stdin、onResize 编码 set_size', () => {
  const socket = fakeSocket()
  const term = fakeTerm()
  const attach = new TerminalAttach(socket)
  attach.activate(term)
  term.handlers.data('ls\n')
  term.handlers.resize({ cols: 120, rows: 30 })
  assert.deepEqual(socket.sent, ['["stdin","ls\\n"]', '["set_size",30,120]'])
  attach.dispose()
  assert.equal(socket.closed, true, 'dispose 应关闭 WebSocket')
})

test('TerminalAttach：stdout 写入终端、setup/disconnect 触发状态回调', () => {
  const socket = fakeSocket()
  const term = fakeTerm()
  const statuses = []
  const attach = new TerminalAttach(socket, (s) => statuses.push(s))
  let written = ''
  term.write = (t) => { written += t }
  attach.activate(term)
  socket.emit('message', { data: '["stdout","hello\\n"]' })
  assert.equal(written, 'hello\n')
  socket.emit('message', { data: '["setup",{}]' })
  socket.emit('close', {})
  assert.deepEqual(statuses, ['connected', 'closed'])
  attach.dispose()
})

test('TerminalAttach：非法消息与二进制 stdout 均正确处理', () => {
  const socket = fakeSocket()
  const term = fakeTerm()
  let written = ''
  term.write = (t) => { written += t }
  const attach = new TerminalAttach(socket)
  attach.activate(term)
  socket.emit('message', { data: 'not-json' })
  socket.emit('message', { data: new TextEncoder().encode('["stdout","bin"]').buffer })
  assert.equal(written, 'bin')
  attach.dispose()
})
