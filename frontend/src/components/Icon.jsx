// 图标统一入口（issue #177）：项目内所有 UI 图标一律改用 Lucide 系列图标。
// - 通过 <Icon name="..." /> 按语义取图标，name → Lucide 组件映射见下方 ICONS；
// - 图标尺寸默认随字号缩放（styles.css 中 .lucide { width:1em; height:1em }），
//   需要固定大小时可传 size / width / height 覆盖；
// - 装饰性图标默认 aria-hidden（lucide-react 默认行为），需要可访问语义时
//   通过 aria-label 覆盖。
// - 例外：供应商品牌 logo（providers.jsx 的内联 SVG 圆底图形）属于品牌标识，
//   Lucide 无对应图形，不在图标替换范围内。
import {
  ArrowLeft, ArrowUp, BarChart3, Bot, Brain, Check, CheckCircle2, CheckSquare, ChevronDown,
  ChevronLeft, ChevronRight, ClipboardList, Coins, Compass, Download, ExternalLink, Eye,
  FileText, Flag, Folder, FolderOpen, GripVertical, Hourglass,
  Image as ImageIcon, Keyboard, LayoutGrid, LayoutList, Lightbulb, Lock, Menu, MessageCircle, Mic, Package,
  Pencil, Pin, Plus, RefreshCw,
  Rocket, Search, Settings, Sparkles, Square, Tag, Terminal as TerminalIcon,
  Trash2, TriangleAlert, Upload, User, Wallet, Wrench, X, XCircle,
} from 'lucide-react'

// 语义名 → Lucide 图标映射（全量 Lucide 系列）
export const ICONS = {
  arrowLeft: ArrowLeft,
  arrowUp: ArrowUp,
  chart: BarChart3,
  bot: Bot,
  brain: Brain,
  check: Check,
  checkCircle: CheckCircle2,
  checkSquare: CheckSquare,
  chevronDown: ChevronDown,
  chevronLeft: ChevronLeft,
  chevronRight: ChevronRight,
  clipboard: ClipboardList,
  coins: Coins,
  compass: Compass,
  download: Download,
  externalLink: ExternalLink,
  eye: Eye,
  fileText: FileText,
  flag: Flag,
  folder: Folder,
  folderOpen: FolderOpen,
  gripVertical: GripVertical,
  hourglass: Hourglass,
  keyboard: Keyboard,
  layoutGrid: LayoutGrid,
  layoutList: LayoutList,
  image: ImageIcon,
  lightbulb: Lightbulb,
  lock: Lock,
  menu: Menu,
  message: MessageCircle,
  mic: Mic,
  package: Package,
  pencil: Pencil,
  pin: Pin,
  plus: Plus,
  refresh: RefreshCw,
  rocket: Rocket,
  search: Search,
  settings: Settings,
  sparkles: Sparkles,
  square: Square,
  tag: Tag,
  terminal: TerminalIcon,
  trash: Trash2,
  upload: Upload,
  user: User,
  wallet: Wallet,
  warning: TriangleAlert,
  wrench: Wrench,
  x: X,
  xCircle: XCircle,
}

// 统一图标组件：按语义名渲染对应 Lucide 图标；未知名回退 ×（避免渲染空白）
export function Icon({ name, ...props }) {
  const Cmp = ICONS[name] || X
  return <Cmp {...props} />
}
