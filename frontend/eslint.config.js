// ESLint flat config（issue #203）：前端代码质量门禁
// 规则先宽松——error 级仅放高价值规则（未定义名 / 未用变量 / hooks deps），
// 避免一上来全仓库爆红；后续可逐步收紧（no-console / eqeqeq 等）。
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'

export default [
  {
    // 构建产物 / 依赖 / 覆盖率报告等不参与 lint
    ignores: [
      'dist/**',
      'node_modules/**',
      'coverage/**',
      'playwright-report/**',
      'test-results/**',
    ],
  },
  {
    files: ['**/*.{js,jsx,mjs}'],
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'module',
      globals: {
        // 浏览器全局（window/document/fetch 等）+ Node 全局（process/console 等，
        // 覆盖 vite.config.js / scripts / tests 用 node:test）+ ES 内置全局
        // （globalThis 等，tests 里 mock globalThis.document 需要）
        ...globals.browser,
        ...globals.node,
        ...globals.es2023,
      },
      parserOptions: {
        // .jsx 文件需要 JSX 语法支持
        ecmaFeatures: { jsx: true },
      },
    },
    plugins: {
      'react-hooks': reactHooks,
    },
    rules: {
      // 未定义名：拼写错误 / 漏导入在测试前拦截（issue #203）
      'no-undef': 'error',
      // 未用变量 / 未用参数：`_` 前缀视为有意忽略（issue #203）
      'no-unused-vars': ['error', {
        argsIgnorePattern: '^_',
        varsIgnorePattern: '^_',
        caughtErrorsIgnorePattern: '^_',
      }],
      // React Hooks 规则（issue #203）：hooks 调用顺序 + deps 完整性
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'error',
    },
  },
]
