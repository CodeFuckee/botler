// 设置页（issue #201 拆分）：巨型组件（原 959 行）按板块拆分为独立卡片
// 组件（components/settings/*，AiProvidersCard 等原卡片继续复用），
// 状态 / 数据加载 / 全部处理函数收敛到 useSettingsData hook，本文件只做
// 组合编排（主文件 ≤400 行），行为与拆分前一致。
// 注意：设置区块（section.settings-section + 锚点 id）与分组标题
// （h2.settings-group-title）必须保留在本文件——SettingsNav 运行时读取
// 设置页 DOM 动态生成左侧导航（issue #155），测试也按源码结构断言。
import SettingsNav from '../components/SettingsNav.jsx'
import VersionBadge from '../components/VersionBadge.jsx'
import { Icon } from '../components/Icon.jsx'
import AiProvidersCard from '../components/AiProvidersCard.jsx'
import ImageModelsCard from '../components/ImageModelsCard.jsx'
import VisionModelsCard from '../components/VisionModelsCard.jsx'
import BackupManager from '../components/BackupManager.jsx'
import { useSettingsData } from '../hooks/useSettingsData.js'
import SsoCard from '../components/settings/SsoCard.jsx'
import MinioCard from '../components/settings/MinioCard.jsx'
import TasksCard from '../components/settings/TasksCard.jsx'
import UiCard from '../components/settings/UiCard.jsx'
import NotificationsCard from '../components/settings/NotificationsCard.jsx'
import WebhookCard from '../components/settings/WebhookCard.jsx'
import ClaudeCard from '../components/settings/ClaudeCard.jsx'
import DshCard from '../components/settings/DshCard.jsx'
import EnvironmentCard from '../components/settings/EnvironmentCard.jsx'
import OwnerTokenCard from '../components/settings/OwnerTokenCard.jsx'
import GitlabCredCard from '../components/settings/GitlabCredCard.jsx'

export default function Settings() {
  const data = useSettingsData()

  // HIG 匠心：加载态用 spinner，非裸文本
  if (!data.settings) return (
    <div className="loading-hint">
      <span className="spinner" aria-hidden="true" />
      <span className="muted">加载中…</span>
    </div>
  )

  return (
    <div className="settings-layout">
      <SettingsNav />
      <div className="settings-content">
        {/* 设置页分组标题（issue #139）：与左侧导航栏分组一一对应 */}
        <h2 className="settings-group-title">外部服务接入</h2>
        <section id="settings-sso" className="settings-section">
          <SsoCard {...data} />
        </section>

        {/* AI API 供应商（issue #46）：SSO 卡片后第二位，外部服务接入类配置聚合 */}
        <section id="settings-ai-providers" className="settings-section">
          <AiProvidersCard />
        </section>

        {/* 生图模型（issue #135）：AI 供应商卡片之后，同为外部服务接入类配置 */}
        <section id="settings-image-models" className="settings-section">
          <ImageModelsCard />
        </section>

        {/* 识图模型（issue #152）：独立区块（issue #155）——导航栏通过读取设置页
            区块动态生成，自动出现「识图模型」子选项，不再需要手工同步 */}
        <section id="settings-vision-models" className="settings-section">
          <VisionModelsCard />
        </section>

        {/* MinIO 对象存储（issue #170）：识图图片上传配置——启用后用户上传的
            图片先计算 SHA-256 哈希、以哈希值为对象名上传 MinIO public 桶
            （桶不存在自动创建并设为公开只读），识图请求传 http URL 而非
            base64（OpenAI 兼容网关拒绝 data: URL）；public_base_url 填
            https://<站点>/minio-public 即可——后端已内置 /minio-public/
            访问端点（issue #319，FastAPI 直接返回 MinIO 图片桶），无需
            再配 nginx location（nginx 分流见 deploy/nginx-minio-public.conf），
            无需暴露 9000 端口 */}
        <section id="settings-minio" className="settings-section">
          <MinioCard {...data} />
        </section>

        <h2 className="settings-group-title">系统设置</h2>
        {data.error && <div className="alert alert-error" onClick={() => data.setError('')}>{data.error}</div>}
        {data.saved && <div className="alert alert-ok"><Icon name="check" /> 已保存（已写回 config.yaml）</div>}

        <section id="settings-tasks" className="settings-section">
          <TasksCard {...data} />
        </section>
        <section id="settings-ui" className="settings-section">
          <UiCard {...data} />
        </section>
        <section id="settings-notifications" className="settings-section">
          <NotificationsCard {...data} />
        </section>
        <section id="settings-webhook" className="settings-section">
          <WebhookCard {...data} />
        </section>

        <h2 className="settings-group-title">执行引擎</h2>
        <section id="settings-claude" className="settings-section">
          <ClaudeCard {...data} />
        </section>

        {/* dsh 引擎（issue #84）：deepseek-harness SDK 推理等级设置（issue #123）。
            SDK 运行时 llm-deepseek adapter 支持 reasoningEffort（off / high / max），
            botler 在设置后自动派生 Cordis 注入，无需手工维护 cordis 文件 */}
        <section id="settings-dsh" className="settings-section">
          <DshCard {...data} />
        </section>

        <h2 className="settings-group-title">运维与数据</h2>
        <section id="settings-environment" className="settings-section">
          <EnvironmentCard {...data} />
        </section>

        <section id="settings-backup" className="settings-section">
          <BackupManager />
        </section>

        {/* Owner GitLab Token（issue #87）：专用于编辑 issue（评论/标签）的
            个人访问令牌，严禁用于推送代码与处理流水线。
            issue #130：系统架构层已隔离——所有 Agent 均不可使用，只允许
            在概览页面编辑 issue、添加 issue、关闭 issue、添加评论与回复
            评论时由平台使用；Agent 只能使用自己仓库的认证 token 编辑 issue */}
        <h2 className="settings-group-title">账号与安全</h2>
        <section id="settings-owner-token" className="settings-section" data-nav-label="Owner GitLab Token">
          <OwnerTokenCard {...data} />
        </section>
        <section id="settings-gitlab-cred" className="settings-section">
          <GitlabCredCard {...data} />
        </section>

        {/* 版本信息（issue #9 第二轮）：从导航栏移入设置页面底部，
            每次 CI/CD 构建自动更新版本号与构建时间 */}
        <h2 className="settings-group-title">关于</h2>
        <section id="settings-version" className="settings-section">
          <div className="card">
            <h2>版本信息</h2>
            <p className="muted small">当前版本、构建时间与提交（每次 CI/CD 构建自动更新，构建信息见 /version.json 与 /api/health）：</p>
            <div className="settings-version">
              <VersionBadge />
            </div>
          </div>
        </section>
      </div>
    </div>
  )
}
