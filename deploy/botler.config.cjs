// pm2 配置：部署到 your-server.example.com 后，在项目根目录执行
//   pm2 start deploy/botler.config.cjs && pm2 save
// 注意：ANTHROPIC_* / GITLAB_BOT_TOKEN 等凭据由 .env 或环境变量提供，
//       通过 --env 或在 pm2 之前 export 均可；不要在配置里写明文。
//
// 数据目录：数据（config.yaml / botler.db / .env / workspace / logs /
// backups）固定在 $BOTLER_DATA_DIR（CI 部署时 export，不随
// gitlab-runner 构建目录漂移——构建目录可能被清理，历史部署数据曾
// 因此丢失）。未设置时回退到项目根目录 data/。
//
// MinIO 服务（issue #160）：pm2 直接托管 minio server 二进制
// （botler-minio app，与 botler 一并 start/save）。二进制默认
// $HOME/.local/bin/minio（CI 部署 deploy/install-minio.sh 自动安装，
// 可用 MINIO_BIN 环境变量覆盖路径）；数据目录固定在
// $BOTLER_DATA_DIR/minio/data（与 botler 数据同根目录，备份/迁移
// 一并处理）；凭据优先读 data/backend/.env 的 MINIO_ROOT_USER /
// MINIO_ROOT_PASSWORD（CI 部署自动写入，与 docker compose 注入的
// 环境变量同源），缺失时回退 minioadmin 默认值。
const path = require('path');
const fs = require('fs');
const ROOT = __dirname + '/..';
const DATA_DIR = process.env.BOTLER_DATA_DIR || path.join(ROOT, 'data');
const MINIO_BIN = process.env.MINIO_BIN || path.join(process.env.HOME || '/root', '.local/bin/minio');

// 从 data/backend/.env 读取键值（与后端 load_dotenv 同源，CI 部署
// 会把 minio 凭据写入该文件；.env 缺失/未定义该键时返回 fallback）
function readDotEnv(key, fallback) {
  try {
    const content = fs.readFileSync(path.join(DATA_DIR, 'backend/.env'), 'utf-8');
    for (const line of content.split('\n')) {
      const m = line.match(new RegExp('^' + key + '=(.*)$'));
      if (m) return m[1].trim();
    }
  } catch (e) { /* .env 缺失时使用 fallback */ }
  return fallback;
}

module.exports = {
  apps: [
    {
      name: 'botler',
      script: 'backend/.venv/bin/uvicorn',
      // ⚠️ pm2 默认用 node 解释器执行 script（uvicorn 是 Python 脚本，
      // 会被当作 JS 解析报 SyntaxError），必须显式指定 Python 解释器
      interpreter: path.join(ROOT, 'backend/.venv/bin/python'),
      args: 'botler.main:app --host 0.0.0.0 --port 8000',
      cwd: ROOT,
      env: {
        // 持久数据目录（issue #309）：repo_logo.LOGO_DIR 按它解析
        // backend/data/logos——pm2 运行在 gitlab-runner 构建目录，
        // 不注入则 logo 落构建目录随轮换丢失（DB 有 logo_path 但文件
        // 丢失，其他设备/新部署看不到生成的图标）
        BOTLER_DATA_DIR: DATA_DIR,
        BOTLER_CONFIG: path.join(DATA_DIR, 'backend/config.yaml'),
        BOTLER_DB: path.join(DATA_DIR, 'backend/botler.db'),
        BOTLER_BACKUP_DIR: path.join(DATA_DIR, 'backups'),
        BOTLER_LOG_DIR: path.join(DATA_DIR, 'logs'),
        // 脚本方式执行 uvicorn 时 sys.path 不含项目目录，
        // 必须显式把 backend 加入 PYTHONPATH 才能 import botler
        PYTHONPATH: path.join(ROOT, 'backend'),
      },
      // 生产环境变量建议放 .env（data/backend/.env，后端启动时自动加载；
      // 构建目录 backend/.env 为指向它的 symlink，由 CI 部署脚本维护）
      max_restarts: 10,
      restart_delay: 3000,
      out_file: path.join(DATA_DIR, 'logs/pm2-out.log'),
      error_file: path.join(DATA_DIR, 'logs/pm2-error.log'),
      time: true,
    },
    {
      // Web 终端服务（issue #183）：Tornado + terminado 独立进程。
      // 默认只监听 127.0.0.1:8765（安全隔离，不对外暴露端口），对外经
      // botler 主后端 /api/terminal/* 反向代理（或 nginx 统一入口，
      // 见 deploy/nginx-terminal.conf）。与主后端共享同一份会话密钥
      // （backend/data/session_secret.key，懒生成持久化），WebSocket
      // 握手用主后端签发的短时效 token 校验（botler.auth 同密钥）。
      name: 'botler-terminal',
      script: path.join(ROOT, 'backend/terminal_service.py'),
      interpreter: path.join(ROOT, 'backend/.venv/bin/python'),
      cwd: ROOT,
      env: {
        // 终端服务进程独立监听端口/地址（安全隔离：默认仅本机）
        BOTLER_TERM_PORT: '8765',
        BOTLER_TERM_BIND: '127.0.0.1',
        // 脚本方式执行时 sys.path 不含项目目录，须显式加 backend
        PYTHONPATH: path.join(ROOT, 'backend'),
      },
      max_restarts: 10,
      restart_delay: 3000,
      out_file: path.join(DATA_DIR, 'logs/pm2-terminal-out.log'),
      error_file: path.join(DATA_DIR, 'logs/pm2-terminal-error.log'),
      time: true,
    },
    {
      // MinIO 对象存储服务（issue #160）：pm2 直接托管 minio 原生
      // 二进制（interpreter: none，否则被当作 JS 解析报 SyntaxError）
      name: 'botler-minio',
      script: MINIO_BIN,
      interpreter: 'none',
      // 数据目录固定 $BOTLER_DATA_DIR/minio/data；API 端口 9000、
      // console 端口 9001（与 docker compose 部署一致，两种形态互斥
      // 运行不会冲突）
      args: 'server ' + path.join(DATA_DIR, 'minio/data') + ' --address ":9000" --console-address ":9001"',
      cwd: ROOT,
      env: {
        MINIO_ROOT_USER: readDotEnv('MINIO_ROOT_USER', 'minioadmin'),
        MINIO_ROOT_PASSWORD: readDotEnv('MINIO_ROOT_PASSWORD', 'minioadmin'),
      },
      max_restarts: 10,
      restart_delay: 3000,
      out_file: path.join(DATA_DIR, 'logs/pm2-minio-out.log'),
      error_file: path.join(DATA_DIR, 'logs/pm2-minio-error.log'),
      time: true,
    },
  ],
}
