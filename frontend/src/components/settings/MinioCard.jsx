import { Icon } from '../Icon.jsx'
// MinIO 对象存储配置卡片（issue #201 拆分）：从 Settings.jsx 抽出，
// 字段含启用开关 / API 地址 / HTTPS / Access Key / Secret Key / 桶名 /
// 公网访问前缀 / 校验证书与「保存 MinIO 配置」独立保存按钮；数据与
// 处理函数经 props 注入（useSettingsData hook），行为与拆分前一致。
export default function MinioCard({
  settings, minioAccessInput, setMinioAccessInput, minioSecretInput,
  setMinioSecretInput, minioSaveBusy, minioSaved, saveMinio, setMinioField,
}) {
  return (
    <div className="card">
      <h2>MinIO 对象存储</h2>
      <table className="table kv">
        <tbody>
          <tr>
            <th>启用 MinIO <code>minio.enabled</code></th>
            <td>
              <input
                type="checkbox"
                className="check-input"
                checked={settings.minio?.enabled === true}
                onChange={(e) => setMinioField('enabled', e.target.checked)}
              />
              <span className="muted small">
                {settings.minio?.enabled
                  ? '启用后识图模型调用时图片自动上传 MinIO public 桶'
                  : '关闭时 OpenAI 兼容识图模型将报错引导启用 MinIO（issue #164）'}
              </span>
            </td>
          </tr>
          <tr>
            <th>API 地址 <code>endpoint</code></th>
            <td>
              <input
                className="input grow"
                placeholder="127.0.0.1:9000"
                value={settings.minio?.endpoint || ''}
                onChange={(e) => setMinioField('endpoint', e.target.value.trim())}
              />
            </td>
          </tr>
          <tr>
            <th>HTTPS <code>secure</code></th>
            <td>
              <input
                type="checkbox"
                className="check-input"
                checked={settings.minio?.secure === true}
                onChange={(e) => setMinioField('secure', e.target.checked)}
              />
              <span className="muted small">endpoint 是否为 https（本机内网一般关闭）</span>
            </td>
          </tr>
          <tr>
            <th>Access Key <code>access_key</code></th>
            <td>
              <input
                type="password"
                className="input grow"
                placeholder={settings.minio?.access_key_masked
                  ? `已配置（${settings.minio.access_key_masked}），留空 = 保持现有`
                  : 'MinIO 根凭据（MINIO_ROOT_USER）'}
                value={minioAccessInput}
                onChange={(e) => setMinioAccessInput(e.target.value)}
              />
            </td>
          </tr>
          <tr>
            <th>Secret Key <code>secret_key</code></th>
            <td>
              <input
                type="password"
                className="input grow"
                placeholder={settings.minio?.secret_key_masked
                  ? `已配置（${settings.minio.secret_key_masked}），留空 = 保持现有`
                  : 'MinIO 根凭据（MINIO_ROOT_PASSWORD）'}
                value={minioSecretInput}
                onChange={(e) => setMinioSecretInput(e.target.value)}
              />
            </td>
          </tr>
          <tr>
            <th>桶名 <code>bucket</code></th>
            <td>
              <input
                className="input grow"
                placeholder="public"
                value={settings.minio?.bucket || 'public'}
                onChange={(e) => setMinioField('bucket', e.target.value.trim())}
              />
              <div className="muted small">不存在时自动创建并设为公开只读（匿名 s3:GetObject）</div>
            </td>
          </tr>
          <tr>
            <th>公网访问前缀 <code>public_base_url</code></th>
            <td>
              <input
                className="input grow"
                placeholder="https://home.chenkaidi.top:448/minio-public"
                value={settings.minio?.public_base_url || ''}
                onChange={(e) => setMinioField('public_base_url', e.target.value.trim())}
              />
              <div className="muted small">
                识图模型取图的 http(s) 前缀，对象 URL = public_base_url/bucket/哈希；
                建议填 nginx 代理地址（deploy/nginx-minio-public.conf），无需暴露 9000 端口；
                nginx location 只剥离 /minio-public/ 前缀、保留 bucket 段（issue #311 修复）
              </div>
            </td>
          </tr>
          <tr>
            <th>校验 endpoint 证书 <code>verify_ssl</code></th>
            <td>
              <input
                type="checkbox"
                className="check-input"
                checked={settings.minio?.verify_ssl !== false}
                onChange={(e) => setMinioField('verify_ssl', e.target.checked)}
              />
              <span className="muted small">endpoint 为自签 https 证书时取消勾选</span>
            </td>
          </tr>
        </tbody>
      </table>
      <div className="form-row">
        <button className="btn btn-primary" disabled={minioSaveBusy} onClick={saveMinio}>
          {minioSaveBusy ? '保存中…' : '保存 MinIO 配置'}
        </button>
        {minioSaved && <span className="saved-hint"><Icon name="check" /> MinIO 配置已保存（已写回 config.yaml）</span>}
      </div>
      <p className="muted small">
        识图模型图片上传（issue #163/#164）：启用后图片先计算 SHA-256 哈希、
        以哈希值为对象名上传 MinIO public 桶，识图请求传 http URL 而非 base64；
        凭据支持 ${'{'}ENV{'}'} 引用、留空回退环境变量 MINIO_ROOT_USER /
        MINIO_ROOT_PASSWORD（与部署写入 data/backend/.env 的凭据同源）。
        Gemini 官方接口仅支持 base64 inline_data，保持 base64 内联输入。
      </p>
    </div>
  )
}
