// pm2 配置：部署到 10.0.0.122 后，在项目根目录执行
//   pm2 start deploy/botler.config.js && pm2 save
// 注意：ANTHROPIC_* / GITLAB_BOT_TOKEN 等凭据由 .env 提供，
//       通过 --env 或在 pm2 之前 export 均可；不要在配置里写明文。

module.exports = {
  apps: [
    {
      name: 'botler',
      script: 'backend/.venv/bin/uvicorn',
      args: 'botler.main:app --host 0.0.0.0 --port 8000',
      cwd: __dirname + '/..',
      env: {
        BOTLER_CONFIG: __dirname + '/../backend/config.yaml',
        BOTLER_DB: __dirname + '/../backend/botler.db',
      },
      // 生产环境变量建议放 .env（backend/.env），后端启动时自动加载
      max_restarts: 10,
      restart_delay: 3000,
      out_file: 'logs/pm2-out.log',
      error_file: 'logs/pm2-error.log',
      time: true,
    },
  ],
}
