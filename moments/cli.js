#!/usr/bin/env node
/**
 * 朋友圈引擎 CLI —— 供工作台 server.py 调用
 *
 * 用法：
 *   node cli.js --payload <payload.json>   # payload: {images:[], text:"", tag:"客户", dryRun:true}
 *   node cli.js --calibrate                # 坐标校准（截图引导）
 *
 * 输出：最后一行打印 JSON 结果（stdout 前面是过程日志）
 */
const fs = require('fs')
const { publishMoment, calibrate } = require('./engine')

async function main() {
  const args = process.argv.slice(2)

  if (args.includes('--calibrate')) {
    const r = await calibrate()
    console.log('RESULT_JSON:' + JSON.stringify({ ok: true, screenshot: r.screenshot }))
    return
  }

  const pi = args.indexOf('--payload')
  if (pi < 0 || !args[pi + 1]) {
    console.log('RESULT_JSON:' + JSON.stringify({ ok: false, error: '缺少 --payload <file.json>' }))
    process.exit(2)
  }
  let payload
  try {
    payload = JSON.parse(fs.readFileSync(args[pi + 1], 'utf-8'))
  } catch (e) {
    console.log('RESULT_JSON:' + JSON.stringify({ ok: false, error: 'payload 解析失败: ' + e.message }))
    process.exit(2)
  }

  const result = await publishMoment({
    images: payload.images || [],
    text: payload.text || '',
    tag: payload.tag || '',
    dryRun: payload.dryRun !== false, // 默认 dry-run，绝不自动点发表
  })
  console.log('RESULT_JSON:' + JSON.stringify(result, null, 2))
  process.exit(result.ok ? 0 : 1)
}

main().catch(e => {
  console.log('RESULT_JSON:' + JSON.stringify({ ok: false, error: e.message }))
  process.exit(1)
})
