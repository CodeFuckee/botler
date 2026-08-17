/**
 * Botler 鸿蒙端主页面 —— WebView 套壳
 *
 * 通过系统 Web 组件加载 Botler Web 前端（地址见 common/AppConfig.ets 的
 * WEB_URL），并补充原生体验：
 *   - 加载动画：页面开始加载时展示 LoadingProgress，加载完成自动隐藏；
 *   - 失败提示：网络错误/资源加载失败时展示错误信息与「重试」按钮；
 *   - 返回键：WebView 存在可回退历史时先回退网页，否则退出应用。
 */
import webview from '@ohos.web.webview';
import { WEB_URL } from '../common/AppConfig';

@Entry
@Component
struct Index {
  controller: webview.WebviewController = new webview.WebviewController();
  /** 页面加载中标记（驱动加载动画） */
  @State loading: boolean = true;
  /** 加载失败提示文案（空字符串表示无错误） */
  @State errorMsg: string = '';

  /**
   * 系统返回键处理：WebView 可后退时先回退网页历史，避免误退出应用
   */
  onBackPress(): boolean {
    if (this.controller.accessBackward()) {
      this.controller.backward();
      return true;
    }
    return false;
  }

  build() {
    Stack({ alignContent: Alignment.Center }) {
      // WebView 套壳：加载 Botler Web 前端
      Web({ src: WEB_URL, controller: this.controller })
        .javaScriptAccess(true)
        .domStorageAccess(true)
        .zoomAccess(false)
        .onPageBegin((event: OnPageBeginEvent) => {
          this.loading = true;
          this.errorMsg = '';
        })
        .onPageEnd((event: OnPageEndEvent) => {
          this.loading = false;
        })
        .onErrorReceive((event: OnErrorReceiveEvent) => {
          this.loading = false;
          this.errorMsg = '页面加载失败：' + event.error.getErrorInfo();
        })
        .onHttpErrorReceive((event: OnHttpErrorReceiveEvent) => {
          this.loading = false;
        })

      // 加载动画
      if (this.loading) {
        Column({ space: 12 }) {
          LoadingProgress()
            .width(48)
            .height(48)
            .color('#1677ff')
          Text('正在加载 ' + WEB_URL)
            .fontSize(14)
            .fontColor('#666666')
        }
      }

      // 加载失败提示 + 重试
      if (!this.loading && this.errorMsg !== '') {
        Column({ space: 16 }) {
          Text(this.errorMsg)
            .fontSize(14)
            .fontColor('#333333')
            .textAlign(TextAlign.Center)
            .padding({ left: 24, right: 24 })
          Button('重试')
            .backgroundColor('#1677ff')
            .onClick(() => {
              this.controller.refresh();
            })
        }
      }
    }
    .width('100%')
    .height('100%')
    .backgroundColor(Color.White)
  }
}
