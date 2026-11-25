#!/usr/bin/env node
/**
 * 用安卓手机当发送端：电脑通过 adb 调用手机系统短信服务，逐条用手机 SIM 卡发送。
 *
 * 前提：
 *   1. 安卓手机打开「开发者选项 → USB 调试」，用数据线连电脑，手机上点「允许」。
 *   2. 电脑装好 adb（platform-tools），能执行 adb devices 看到设备。
 *   3. 网页第 4 张卡「导出待发清单（JSON）」，把导出的 json 拿到电脑上。
 *
 * 用法：
 *   node send.js --jobs 待发清单.json --dry-run      只看要发什么，不发
 *   node send.js --jobs 待发清单.json                逐条发送（默认间隔 3 秒）
 *   node send.js --jobs 待发清单.json --manual       打开发送界面，人工点发送
 *   node send.js --jobs 待发清单.json --delay 5000 --count 20
 *
 * 发送记录写在 logs/ 下，已发过的号码记在 sent-log.json，重跑会自动跳过。
 * 运营商对单卡有频次限制，建议每小时不超过 20–30 条，否则会被封卡。
 */
'use strict';

const { execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');

/* ---------------------------- 参数 ---------------------------- */
const argv = process.argv.slice(2);
function has(name) { return argv.includes('--' + name); }
function opt(name, def) {
  const i = argv.indexOf('--' + name);
  if (i === -1) return def;
  const v = argv[i + 1];
  return (v === undefined || v.indexOf('--') === 0) ? true : v;
}

const jobsPath = opt('jobs', '');
const adbBin = String(opt('adb', 'adb'));
const serial = String(opt('serial', ''));
const delayMs = Math.max(0, Number(opt('delay', 3000)) || 0);
const limit = Math.max(0, Number(opt('count', 0)) || 0);
const manual = has('manual');
const dryRun = has('dry-run');
const force = has('force');

const LOG_PATH = path.join(__dirname, 'sent-log.json');
const LOG_DIR = path.join(__dirname, 'logs');

/* ---------------------------- 工具 ---------------------------- */
function fail(msg) {
  console.error('\n✗ ' + msg + '\n');
  process.exit(1);
}

function adb(args) {
  return execFileSync(adbBin, (serial ? ['-s', serial] : []).concat(args), {
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'pipe'],
    timeout: 30000
  });
}

function sleep(ms) {
  return new Promise(function (r) { setTimeout(r, ms); });
}

function normPhone(p) {
  let s = String(p == null ? '' : p).replace(/[^0-9]/g, '');
  if (s.indexOf('0086') === 0 && s.length > 13) s = s.slice(4);
  else if (s.indexOf('86') === 0 && s.length > 11) s = s.slice(2);
  if (s.length === 12 && s.charAt(0) === '0') s = s.slice(1);
  return s;
}

/* 内容要嵌进 adb shell 的双引号里，干掉会破坏引号/命令替换的字符 */
function sanitize(s) {
  return String(s == null ? '' : s)
    .replace(/\r?\n/g, ' ')
    .replace(/["`$\\]/g, "'")
    .replace(/\s+/g, ' ')
    .trim();
}

function loadLog() {
  try {
    return JSON.parse(fs.readFileSync(LOG_PATH, 'utf8'));
  } catch (e) {
    return {};
  }
}

function saveLog(log) {
  fs.writeFileSync(LOG_PATH, JSON.stringify(log, null, 2));
}

/* ---------------------------- 读取清单 ---------------------------- */
function loadJobs() {
  if (!jobsPath || jobsPath === true) fail('请用 --jobs 指定清单文件，例如：node send.js --jobs 待发清单.json');
  const abs = path.resolve(process.cwd(), String(jobsPath));
  if (!fs.existsSync(abs)) fail('找不到清单文件：' + abs);
  let data;
  try {
    data = JSON.parse(fs.readFileSync(abs, 'utf8'));
  } catch (e) {
    fail('清单不是合法 JSON：' + e.message);
  }
  const list = Array.isArray(data) ? data : (data.items || []);
  if (!Array.isArray(list) || !list.length) fail('清单里没有数据，请从网页重新导出');

  const seen = {};
  const out = [];
  let bad = 0;
  list.forEach(function (it) {
    const phone = normPhone(it && it.phone);
    if (!/^1[3-9]\d{9}$/.test(phone)) { bad++; return; }
    if (seen[phone]) return;
    seen[phone] = true;
    out.push({
      phone: phone,
      name: it.name ? String(it.name).trim() : '',
      content: sanitize(it.content)
    });
  });
  if (!out.length) fail('清单里没有有效手机号（需 11 位大陆手机号）');
  if (bad) console.log('  跳过无效号码 ' + bad + ' 条');
  return out;
}

/* ---------------------------- 设备检查 ---------------------------- */
function checkDevice() {
  let text;
  try {
    text = adb(['devices']);
  } catch (e) {
    fail('执行 adb 失败：' + (e.code === 'ENOENT'
      ? '电脑没装 adb，或没加到 PATH。装 Android platform-tools 后重试，或用 --adb 指定 adb.exe 路径'
      : e.message));
  }
  const lines = String(text).split('\n').slice(1).filter(function (l) { return l.trim(); });
  const ready = lines.filter(function (l) { return /\tdevice$/.test(l.trim()); }).map(function (l) { return l.split('\t')[0]; });
  const unauthorized = lines.filter(function (l) { return /unauthorized/.test(l); });

  if (!ready.length) {
    if (unauthorized.length) {
      fail('手机未授权：请解锁手机，在弹窗里点「允许 USB 调试」（勾选「始终允许」），再重试');
    }
    fail('没有检测到安卓设备。检查：数据线是否支持传输、是否开了 USB 调试、手机弹窗是否点了允许');
  }
  if (ready.length > 1 && !serial) {
    fail('检测到多台设备：' + ready.join(', ') + '，请用 --serial 指定一台');
  }
  console.log('✓ 设备已连接：' + (serial || ready[0]));
  return ready[0];
}

/* ---------------------------- 发送 ---------------------------- */
function buildServiceCmd(phone, content, code) {
  return 'service call isms ' + code + ' i32 0 '
    + 's16 "com.android.mms.service" '
    + 's16 "+86' + phone + '" '
    + 's16 "null" '
    + 's16 "' + content + '" '
    + 's16 "null" '
    + 's16 "null" '
    + 's16 "null"';
}

function sendSilent(phone, content) {
  const errors = [];
  /* isms 的 sendText 事务号随系统版本不同：常见 7，老系统是 5，两个都试 */
  for (const code of [7, 5]) {
    let out = '';
    try {
      out = adb(['shell', buildServiceCmd(phone, content, code)]);
    } catch (e) {
      errors.push('code=' + code + ' ' + String(e.message || e).split('\n')[0]);
      continue;
    }
    if (/Exception|Permission|denied|not found|Bad argument/i.test(out)) {
      errors.push('code=' + code + ' ' + out.trim().split('\n')[0]);
      continue;
    }
    if (/Result:\s*Parcel/i.test(out)) return { ok: true };
    errors.push('code=' + code + ' 返回异常：' + out.trim().slice(0, 120));
  }
  return { ok: false, err: errors.join(' | ') };
}

function sendManual(phone, content) {
  adb(['shell', 'am start -a android.intent.action.SENDTO -d "smsto:+86' + phone
    + '" --es sms_body "' + content + '"']);
  return { ok: true, manual: true };
}

/* ---------------------------- 主流程 ---------------------------- */
(async function main() {
  const jobs = loadJobs();
  console.log('清单：' + jobs.length + ' 条' + (limit ? '，本次只发前 ' + limit + ' 条' : ''));

  const log = loadLog();
  const pending = jobs.filter(function (j) { return force || !log[j.phone]; });
  const skipped = jobs.length - pending.length;
  if (skipped) console.log('已发过自动跳过：' + skipped + ' 条（要重发加 --force）');
  if (!pending.length) {
    console.log('\n没有需要发送的号码。');
    return;
  }
  const todo = limit ? pending.slice(0, limit) : pending;

  if (dryRun) {
    todo.slice(0, 10).forEach(function (j, i) {
      console.log('\n[' + (i + 1) + '] ' + j.phone + (j.name ? '（' + j.name + '）' : ''));
      console.log('    ' + j.content);
    });
    if (todo.length > 10) console.log('\n……共 ' + todo.length + ' 条，其余略');
    console.log('\n（--dry-run 只预览，没有真正发送）');
    return;
  }

  if (!manual) checkDevice();
  if (!fs.existsSync(LOG_DIR)) fs.mkdirSync(LOG_DIR, { recursive: true });

  const records = [];
  let okCount = 0;
  let failCount = 0;

  for (let i = 0; i < todo.length; i++) {
    const j = todo[i];
    const tag = '[' + (i + 1) + '/' + todo.length + '] ' + j.phone;
    let res;
    try {
      res = manual ? sendManual(j.phone, j.content) : sendSilent(j.phone, j.content);
    } catch (e) {
      res = { ok: false, err: String(e.message || e) };
    }

    if (res.ok) {
      okCount++;
      const t = new Date().toISOString();
      log[j.phone] = t;
      saveLog(log);
      records.push({ phone: j.phone, name: j.name, ok: true, msg: res.manual ? '已打开发送界面' : '已提交系统发送', time: t });
      console.log(tag + ' ' + (res.manual ? '→ 已打开发送界面，请在手机上点发送' : '✓ 已提交'));
    } else {
      failCount++;
      records.push({ phone: j.phone, name: j.name, ok: false, msg: res.err || '未知错误', time: new Date().toISOString() });
      console.log(tag + ' ✗ 失败：' + (res.err || '未知错误'));
      if (/SecurityException|Permission|denied/i.test(res.err || '')) {
        console.log('\n这台系统不允许电脑直接调短信接口（安卓 10 以上常见）。\n改用：node send.js --jobs 清单.json --manual\n会逐条打开发送界面，你只需点发送。\n');
        break;
      }
    }

    if (i < todo.length - 1) await sleep(delayMs);
  }

  /* 结果落盘 */
  const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
  const csv = ['号码,姓名,结果,说明,时间'];
  records.forEach(function (r) {
    const q = function (s) { return '"' + String(s == null ? '' : s).replace(/"/g, '""') + '"'; };
    csv.push([q(r.phone), q(r.name), q(r.ok ? '成功' : '失败'), q(r.msg), q(r.time)].join(','));
  });
  const outFile = path.join(LOG_DIR, 'result-' + stamp + '.csv');
  fs.writeFileSync(outFile, '\ufeff' + csv.join('\r\n'));

  console.log('\n完成：成功 ' + okCount + ' 条，失败 ' + failCount + ' 条');
  console.log('结果文件：' + outFile);
  if (okCount) console.log('提醒：运营商对单卡有频次限制，不要连续大量发送，间隔保持 3 秒以上。');
})().catch(function (e) {
  fail(e && e.stack ? e.stack : String(e));
});
