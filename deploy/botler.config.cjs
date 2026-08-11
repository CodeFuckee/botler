// pm2 配置：部署到 10.0.0.122 后，在项目根目录执行
//   pm2 start deploy/botler.config.cjs && pm2 save
// 注意：ANTHROPIC_* / GITLAB_BOT_TOKEN 等凭据由 .env 或环境变量提供，
//       通过 --env 或在 pm2 之前 export 均可；不要在配置里写明文。
//
// 数据目录：数据（config.yaml / botler.db / .env / workspace / logs /
// backups）固定在 $BOTLER_DATA_DIR（CI 部署时 export，不随
// gitlab-runner 构建目录漂移——构建目录可能被清理，历史部署数据曾
// 因此丢失）。未设置时回退到项目根目录 data/。
const path = require('path');
const ROOT = __dirname + '/..';
const DATA_DIR = process.env.BOTLER_DATA_DIR || path.join(ROOT, 'data');

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
        BOTLER_CONFIG: path.join(DATA_DIR, 'backend/config.yaml'),
        BOTLER_DB: path.join(DATA_DIR, 'backend/botler.db'),
        BOTLER_BACKUP_DIR: path.join(DATA_DIR, 'backups'),
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
  ],
}
