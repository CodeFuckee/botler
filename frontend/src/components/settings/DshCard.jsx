// dsh 引擎配置卡片（issue #201 拆分）：从 Settings.jsx 抽出，推理等级
// 下拉（issue #84/#123，reasoningEffort：off / high / max，空 = 不设置），
// 保存后由 dsh 引擎执行时自动派生 Cordis 注入；数据与处理函数经 props
// 注入（useSettingsData hook），行为与拆分前一致。
export default function DshCard({ settings, setSettings }) {
  return (
    <div className="card">
      <h2>dsh 引擎</h2>
      <table className="table kv">
        <tbody>
          <tr>
            <th>推理等级 <code>reasoning_effort</code></th>
            <td>
              <select
                className="input"
                value={settings.dsh?.reasoning_effort || ''}
                onChange={(e) => setSettings((s) => ({
                  ...s,
                  dsh: { ...s.dsh, reasoning_effort: e.target.value },
                }))}
              >
                <option value="">默认（不设置，SDK 默认 high）</option>
                <option value="off">off — 关闭推理（更快更省）</option>
                <option value="high">high — 高</option>
                <option value="max">max — 最高（更严谨，更慢更贵）</option>
              </select>
            </td>
          </tr>
        </tbody>
      </table>
      <p className="muted small">
        dsh 引擎（deepseek-harness SDK）的推理等级（reasoningEffort）：控制模型思考深度，
        off = 关闭推理、high = 高、max = 最高。修改后点击上方「保存」生效，对新领取的
        dsh 引擎任务生效，运行中任务不受影响。留空 = 不设置（SDK 默认）。
      </p>
    </div>
  )
}
