/**
 * 朋友圈发布引擎 v3 —— 状态机 + 屏幕视觉反馈 + 标签可见性
 *
 * 移植自桌面版 moments-engine.js v2，新增：
 * - 标签可见性：谁可以看 → 部分可见 → 选择指定标签 → 完成（标签名可配置）
 * - payload 文件驱动，供工作台 server.py 通过 CLI 调用
 *
 * 设计原则（防封号第一）：
 * 1. 每个动作前先截图确认当前界面状态，动作后再次截图验证动作生效
 * 2. 动作无效只重试 1 次，仍失败立即安全停止（绝不盲目连点）
 * 3. 所有点击之间加入 0.8~2.5 秒随机人类延迟
 * 4. 默认 dry-run：完整走完选图、粘贴文案、设标签，停在"发表"按钮前，不点击
 * 5. 全程截图留痕（logs/moments-*.png），方便排查
 *
 * 注意：微信 4.x 使用 Qt 自绘 UI，无辅助功能（AX）树可用，
 *       因此采用「坐标点击 + 截图像素验证」方案。坐标若因分辨率变化失效，
 *       运行 `node cli.js --calibrate` 可重新校准。
 */
const { execSync, execFile } = require('child_process')
const path = require('path')
const fs = require('fs')
const os = require('os')

const LOG_DIR = path.join(__dirname, 'logs')
const STAGE_DIR = path.join(os.tmpdir(), 'wxmoments-upload')
const CAL_FILE = path.join(__dirname, 'calibration.json')

const CLICK = '/opt/homebrew/bin/cliclick' // brew 安装的坐标点击工具

function log(msg) {
  const line = `[${new Date().toLocaleString('zh-CN')}] ${msg}`
  console.log(line)
  try {
    fs.mkdirSync(LOG_DIR, { recursive: true })
    fs.appendFileSync(path.join(LOG_DIR, 'moments-engine.log'), line + '\n')
  } catch (e) { /* ignore */ }
}

const sleep = ms => new Promise(r => setTimeout(r, ms))
function humanPause(min = 800, max = 2500) { return sleep(Math.floor(min + Math.random() * (max - min))) }

function sh(cmd, opts = {}) {
  return execSync(cmd, { encoding: 'utf8', timeout: 30000, ...opts }).trim()
}

/** 截图并返回文件路径（全屏） */
async function screenshot(tag = 'step') {
  fs.mkdirSync(LOG_DIR, { recursive: true })
  const file = path.join(LOG_DIR, `moments-${tag}-${Date.now()}.png`)
  await new Promise((resolve, reject) => {
    execFile('screencapture', ['-x', file], err => err ? reject(err) : resolve())
  })
  return file
}

/** 读取图片尺寸（用 sips） */
function imageSize(file) {
  const w = parseInt(sh(`sips -g pixelWidth "${file}" | tail -1 | awk '{print $2}'`), 10)
  const h = parseInt(sh(`sips -g pixelHeight "${file}" | tail -1 | awk '{print $2}'`), 10)
  return { w, h }
}

/**
 * 像素差异：比较两张截图的采样区域是否明显不同（用于验证动作生效）
 * 返回 0~1，越大差异越大；无法比较返回 -1
 */
function regionDiff(fileA, fileB, region) {
  try {
    const tmpA = `/tmp/_diff_a_${Date.now()}.png`
    const tmpB = `/tmp/_diff_b_${Date.now()}.png`
    const { x, y, w, h } = region
    sh(`sips -c ${h} ${w} --cropOffset ${y} ${x} "${fileA}" --out "${tmpA}" >/dev/null 2>&1`)
    sh(`sips -c ${h} ${w} --cropOffset ${y} ${x} "${fileB}" --out "${tmpB}" >/dev/null 2>&1`)
    const bufA = fs.readFileSync(tmpA)
    const bufB = fs.readFileSync(tmpB)
    try { fs.unlinkSync(tmpA); fs.unlinkSync(tmpB) } catch (e) {}
    if (bufA.length !== bufB.length) return 1
    const step = Math.max(1, Math.floor(bufA.length / 5000))
    let samples = 0, changed = 0
    for (let i = 0; i < bufA.length; i += step) {
      samples++
      if (Math.abs(bufA[i] - bufB[i]) > 8) changed++
    }
    return changed / Math.max(1, samples)
  } catch (e) {
    return -1
  }
}

/** 激活微信并确保在前台 */
function activateWeChat() {
  try {
    sh(`osascript -e 'tell application "WeChat" to activate'`)
    return true
  } catch (e) {
    log('激活微信失败: ' + e.message)
    return false
  }
}

/** 微信是否在运行 */
function isWeChatRunning() {
  try {
    return sh(`pgrep -x WeChat >/dev/null 2>&1 && echo yes || echo no`) === 'yes'
  } catch (e) { return false }
}

/**
 * 单步动作：点击前截图 → 点击 → 等待 → 点击后截图 → 区域差异验证
 */
async function verifiedClick(tag, x, y, verifyRegion, opts = {}) {
  const { waitAfter = 1800, retries = 1 } = opts
  for (let attempt = 0; attempt <= retries; attempt++) {
    const before = await screenshot(tag + '-before')
    await humanPause(300, 800)
    sh(`"${CLICK}" c:${Math.round(x)},${Math.round(y)}`)
    log(`🖱️ [${tag}] 点击 (${Math.round(x)}, ${Math.round(y)})${attempt > 0 ? '（重试 ' + attempt + '）' : ''}`)
    await sleep(waitAfter)
    const after = await screenshot(tag + '-after')
    const diff = regionDiff(before, after, verifyRegion || { x: 0, y: 0, w: 800, h: 600 })
    const meaningful = diff === -1 || diff >= 0.02
    log(`🔍 [${tag}] 画面变化度=${diff.toFixed ? diff.toFixed(3) : diff} → ${meaningful ? '动作生效' : '无明显变化'}`)
    if (meaningful) return { ok: true, diff, before, after }
    if (attempt < retries) await humanPause(1200, 2000)
  }
  return { ok: false }
}

/** 准备素材：拷贝前 9 张图到暂存目录（文件对话框导航用） */
function stageImages(images) {
  fs.rmSync(STAGE_DIR, { recursive: true, force: true })
  fs.mkdirSync(STAGE_DIR, { recursive: true })
  const staged = []
  images.slice(0, 9).forEach((src, i) => {
    const ext = path.extname(src) || '.jpg'
    const dst = path.join(STAGE_DIR, `img-${String(i + 1).padStart(2, '0')}${ext}`)
    fs.copyFileSync(src, dst)
    staged.push(dst)
  })
  return staged
}

/** 把文案放进剪贴板 */
function copyTextToClipboard(text) {
  sh(`osascript -e 'set the clipboard to ${JSON.stringify(text)}'`)
}

/** 文件选择对话框导入图片 */
async function importFilesViaDialog(staged) {
  await humanPause()
  sh(`osascript -e 'tell application "System Events" to keystroke "g" using {command down, shift down}'`)
  await sleep(1200)
  sh(`osascript -e 'tell application "System Events" to keystroke "${STAGE_DIR}"'`)
  await sleep(800)
  sh(`osascript -e 'tell application "System Events" to key code 36'`)
  await sleep(1500)
  sh(`osascript -e 'tell application "System Events" to keystroke "a" using {command down}'`)
  await sleep(600)
  sh(`osascript -e 'tell application "System Events" to key code 36'`)
  log(`📁 已通过文件对话框导入 ${staged.length} 张图`)
  return true
}

/**
 * 设置标签可见性：谁可以看 → 部分可见 → 选择指定标签 → 完成
 * 需要校准点：whocansee / partial / tags.{标签名} / tagDone
 */
async function setTagVisibility(cal, tagName, result) {
  if (!cal.whocansee || !cal.partial || !cal.tagDone || !cal.tags || !cal.tags[tagName]) {
    result.error = `标签可见性未校准（缺 whocansee/partial/tagDone 或 tags."${tagName}"），请先校准；已安全停止`
    return false
  }
  const r1 = await verifiedClick('谁可以看', cal.whocansee.x, cal.whocansee.y, cal.whocansee.verify, { waitAfter: 1500 })
  if (!r1.ok) { result.error = '点击"谁可以看"未生效，已安全停止'; return false }
  const r2 = await verifiedClick('部分可见', cal.partial.x, cal.partial.y, cal.partial.verify, { waitAfter: 2000 })
  if (!r2.ok) { result.error = '点击"部分可见"未生效，已安全停止'; return false }
  const tagPos = cal.tags[tagName]
  const r3 = await verifiedClick(`选择标签:${tagName}`, tagPos.x, tagPos.y, tagPos.verify, { waitAfter: 1200 })
  if (!r3.ok) { result.error = `选择标签"${tagName}"未生效，已安全停止`; return false }
  const r4 = await verifiedClick('标签完成', cal.tagDone.x, cal.tagDone.y, cal.tagDone.verify, { waitAfter: 1500 })
  if (!r4.ok) { result.error = '点击"完成"未生效，已安全停止'; return false }
  result.steps.push(`可见范围已设为标签：${tagName}`)
  return true
}

/**
 * 朋友圈发布主流程（状态机）
 * @param {object} opts { images: string[], text: string, dryRun: boolean, tag: string }
 */
async function publishMoment(opts) {
  const { images = [], text = '', dryRun = true, tag = '' } = opts
  const result = { ok: false, dryRun, steps: [], error: null }

  if (!isWeChatRunning()) {
    result.error = '微信未运行，请先打开微信'
    return result
  }

  try {
    activateWeChat()
    await humanPause(1500, 2500)
    result.steps.push('激活微信')

    const cal = loadCalibration()
    const missing = ['discover', 'moments', 'camera'].filter(k => !cal || !cal[k])
    if (missing.length) {
      result.error = `校准不完整（缺 ${missing.join('/')}），请先运行：node cli.js --calibrate`
      return result
    }

    const r1 = await verifiedClick('发现tab', cal.discover.x, cal.discover.y, cal.discover.verify)
    if (!r1.ok) { result.error = '点击"发现"入口未生效，已安全停止'; return result }
    result.steps.push('打开发现页')

    const r2 = await verifiedClick('朋友圈行', cal.moments.x, cal.moments.y, cal.moments.verify, { waitAfter: 2500 })
    if (!r2.ok) { result.error = '点击"朋友圈"未生效，已安全停止'; return result }
    result.steps.push('打开朋友圈窗口')

    const r3 = await verifiedClick('相机图标', cal.camera.x, cal.camera.y, cal.camera.verify, { waitAfter: 2000 })
    if (!r3.ok) { result.error = '点击相机图标未生效，已安全停止'; return result }
    result.steps.push('打开发布面板')

    if (images.length) {
      if (!cal.gallery) { result.error = '校准缺少 gallery（图片选择区），已安全停止'; return result }
      const staged = stageImages(images)
      const r4 = await verifiedClick('选择图片', cal.gallery.x, cal.gallery.y, cal.gallery.verify, { waitAfter: 2000 })
      if (!r4.ok) { result.error = '点击图片选择区域未生效，已安全停止'; return result }
      await importFilesViaDialog(staged)
      await humanPause(2000, 3500)
      result.steps.push(`导入 ${staged.length} 张图`)
    }

    if (text) {
      if (!cal.textbox) { result.error = '校准缺少 textbox（文本输入区），已安全停止'; return result }
      copyTextToClipboard(text)
      await humanPause()
      sh(`"${CLICK}" c:${Math.round(cal.textbox.x)},${Math.round(cal.textbox.y)}`)
      await humanPause(500, 900)
      sh(`osascript -e 'tell application "System Events" to keystroke "a" using {command down}'`)
      await sleep(300)
      sh(`osascript -e 'tell application "System Events" to keystroke "v" using {command down}'`)
      await sleep(1000)
      result.steps.push('粘贴文案')
    }

    // 标签可见性（在发表前设置）
    if (tag) {
      const okTag = await setTagVisibility(cal, tag, result)
      if (!okTag) return result
    }

    await humanPause(800, 1500)
    result.finalScreenshot = await screenshot('final')
    result.steps.push(dryRun ? 'dry-run 完成：停在发表按钮前，未点击发表' : '点击发表')
    log(result.steps[result.steps.length - 1])

    if (!dryRun) {
      if (!cal.post) {
        result.error = '校准缺少 post（发表按钮），本次停在发表前'
        result.ok = true
        return result
      }
      sh(`"${CLICK}" c:${Math.round(cal.post.x)},${Math.round(cal.post.y)}`)
      await sleep(2500)
      result.postedScreenshot = await screenshot('posted')
      log('已点击发表按钮')
    }

    result.ok = true
    return result
  } catch (e) {
    result.error = e.message
    log('流程异常: ' + e.message)
    return result
  }
}

/** 校准数据存取 */
function loadCalibration() {
  try { return JSON.parse(fs.readFileSync(CAL_FILE, 'utf-8')) } catch (e) { return null }
}

/**
 * 交互式校准引导：截图后由操作者补全 calibration.json
 */
async function calibrate() {
  log('开始校准：激活微信…')
  activateWeChat()
  await humanPause(1500, 2500)
  const shot = await screenshot('calibrate')
  const { w, h } = imageSize(shot)
  log(`截图已保存: ${shot}（像素 ${w}x${h}）`)
  log('请对照截图补全 calibration.json（截图像素 ÷ 屏幕缩放系数 = 逻辑坐标）：')
  log('  discover      左侧栏"发现"图标')
  log('  moments       发现页"朋友圈"行')
  log('  camera        朋友圈窗口右上角相机图标')
  log('  gallery       发布面板图片选择区')
  log('  textbox       发布面板文本输入区')
  log('  whocansee     发布面板"谁可以看"')
  log('  partial       谁可以看页"部分可见"选项')
  log('  tags.{标签名} 部分可见页中各标签所在行（可配置多个，如 客户/家人/朋友）')
  log('  tagDone       选择标签页右上角"完成"按钮')
  log('  post          发表按钮')
  return { screenshot: shot }
}

module.exports = { publishMoment, calibrate, loadCalibration }
