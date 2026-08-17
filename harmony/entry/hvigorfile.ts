// entry 模块 Hvigor 入口（HAP 打包任务）
import { hapTasks } from '@ohos/hvigor-ohos-plugin';

export default {
  system: hapTasks, /* 内置插件，不可修改 */
  plugins: []       /* 自定义插件扩展位 */
}
