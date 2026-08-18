// 任务失败原因分类（issue #274）：与后端 botler/failure_classify.py 的
// CATEGORY_LABELS / CATEGORY_ADVICE 保持一致（env/engine/unsolvable/
// unknown），详情页分类徽章与处理建议文案共用本模块，避免多页面重复。
export const FAILURE_CATEGORY_LABELS = {
  env: '环境类',
  engine: '引擎类',
  unsolvable: '无法解决类',
  unknown: '未知',
}

// 分类徽章 CSS class（styles.css 中 .failure-cat-* 对应配色）
export function failureCategoryClass(category) {
  return FAILURE_CATEGORY_LABELS[category] ? `failure-cat-${category}` : 'failure-cat-unknown'
}

export function failureCategoryLabel(category) {
  return FAILURE_CATEGORY_LABELS[category] || '未知'
}
