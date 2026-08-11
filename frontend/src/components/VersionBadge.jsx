import { useEffect, useState } from 'react'

// 版本徽标（issue #9）：读取构建产物 version.json（由
// scripts/gen-version.mjs 在每次构建时生成，vite 复制进 dist/），
// 在导航栏右侧显示版本号与构建时间。开发模式（无该文件）静默隐藏。
export default function VersionBadge() {
  const [info, setInfo] = useState(null)

  useEffect(() => {
    let cancelled = false
    fetch('/version.json')
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (!cancelled && data && data.version) setInfo(data)
      })
      .catch(() => {
        // 开发模式 404，隐藏徽标即可
      })
    return () => {
      cancelled = true
    }
  }, [])

  if (!info) return null
  return (
    <span className="version-badge" title={`构建时间：${info.buildTime || '未知'}`}>
      v{info.version}
      {info.buildTime && <span className="version-badge-time"> · {info.buildTime}</span>}
    </span>
  )
}
