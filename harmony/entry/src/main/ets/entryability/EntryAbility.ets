/**
 * 应用入口 Ability：负责窗口创建与首页（pages/Index）加载。
 * 生命周期日志统一使用 hilog（tag: BotlerHarmony）。
 */
import UIAbility from '@ohos.app.ability.UIAbility';
import AbilityConstant from '@ohos.app.ability.AbilityConstant';
import Want from '@ohos.app.ability.Want';
import hilog from '@ohos.hilog';
import window from '@ohos.window';

export default class EntryAbility extends UIAbility {
  onCreate(want: Want, launchParam: AbilityConstant.LaunchParam): void {
    hilog.info(0x0000, 'BotlerHarmony', 'Ability onCreate');
  }

  onDestroy(): void {
    hilog.info(0x0000, 'BotlerHarmony', 'Ability onDestroy');
  }

  onWindowStageCreate(windowStage: window.WindowStage): void {
    // 加载首页（pages/Index —— WebView 套壳页）
    windowStage.loadContent('pages/Index', (err) => {
      if (err.code) {
        hilog.error(0x0000, 'BotlerHarmony', 'Failed to load content: %{public}s', JSON.stringify(err));
        return;
      }
      hilog.info(0x0000, 'BotlerHarmony', 'Content loaded');
    });
  }

  onWindowStageDestroy(): void {
    hilog.info(0x0000, 'BotlerHarmony', 'Ability onWindowStageDestroy');
  }

  onForeground(): void {
    hilog.info(0x0000, 'BotlerHarmony', 'Ability onForeground');
  }

  onBackground(): void {
    hilog.info(0x0000, 'BotlerHarmony', 'Ability onBackground');
  }
}
