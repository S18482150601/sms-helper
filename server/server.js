#!/usr/bin/env node
/**
 * 短信助手本地服务（零依赖，Node.js 18+）
 *
 *   node server.js            使用 config.json
 *   node server.js --mock     强制模拟模式（不真发，测流程用）
 *
 * 能力：
 *   POST /api/sms/batch   批量下发（单次最多 1000 条，可改）
 *   GET  /api/health      健康检查 / 当前通道
 *   其余路径             直接托管本仓库静态页面，手机同 Wi-Fi 打开即可用
 *
 * 支持通道：mock（模拟）、aliyun（阿里云）、tencent（腾讯云）、custom（任意 HTTP 网关）
 */
'use strict';

const http = require('http');
const https = require('https');
const fs = require('fs');
const path = require('path');
const os = require('os');
const crypto = require('crypto');

const ROOT = path.resolve(__dirname, '..');
const CONFIG_PATH = path.join(__dirname, 'config.json');

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'application/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.webmanifest': 'application/manifest+json; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
  '.csv': 'text/csv; charset=utf-8',
  '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
};

function loadConfig() {
  let cfg = {};
  try {
    cfg = JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf8'));
  } catch (e) {
    if (e.code !== 'ENOENT') console.warn('[配置] config.json 解析失败：', e.message);
  }
  const env = process.env;
  const pick = (a, b) => (a === undefined || a === null || a === '' ? b : a);
  cfg.provider = pick(cfg.provider, 'mock');
  cfg.port = Number(pick(cfg.port, 8787));
  cfg.concurrency = Math.max(1, Math.min(20, Number(pick(cfg.concurrency, 6))));
  cfg.retries = Math.max(0, Math.min(5, Number(pick(cfg.retries, 2))));
  cfg.limitPerRequest = Math.max(1, Number(pick(cfg.limitPerRequest, 1000)));
  cfg.delayMs = Math.max(0, Number(pick(cfg.delayMs, 0)));
  cfg.token = pick(cfg.token, '');
  cfg.aliyun = cfg.aliyun || {};
  cfg.tencent = cfg.tencent || {};
  cfg.custom = cfg.custom || {};
  cfg.aliyun.accessKeyId = pick(cfg.aliyun.accessKeyId, env.SMS_AK_ID);
  cfg.aliyun.accessKeySecret = pick(cfg.aliyun.accessKeySecret, env.SMS_AK_SECRET);
  cfg.aliyun.signName = pick(cfg.aliyun.signName, env.SMS_SIGN_NAME);
  cfg.aliyun.templateCode = pick(cfg.aliyun.templateCode, env.SMS_TEMPLATE_CODE);
  cfg.aliyun.regionId = pick(cfg.aliyun.regionId, 'cn-hangzhou');
  cfg.aliyun.paramName = pick(cfg.aliyun.paramName, 'name');
  cfg.tencent.secretId = pick(cfg.tencent.secretId, env.TENCENT_SECRET_ID);
  cfg.tencent.secretKey = pick(cfg.tencent.secretKey, env.TENCENT_SECRET_KEY);
  cfg.tencent.sdkAppId = pick(cfg.tencent.sdkAppId, env.TENCENT_SDK_APP_ID);
  cfg.tencent.signName = pick(cfg.tencent.signName, env.SMS_SIGN_NAME);
  cfg.tencent.templateId = pick(cfg.tencent.templateId, env.SMS_TEMPLATE_ID);
  cfg.tencent.region = pick(cfg.tencent.region, 'ap-guangzhou');
  cfg.tencent.paramName = pick(cfg.tencent.paramName, 'name');
  cfg.custom.paramName = pick(cfg.custom.paramName, 'name');
  if (process.argv.includes('--mock')) cfg.provider = 'mock';
  return cfg;
}

const cfg = loadConfig();

/* ------------------------------ 通道实现 ------------------------------ */

function httpRequest(urlStr, options, body) {
  return new Promise((resolve, reject) => {
    const u = new URL(urlStr);
    const lib = u.protocol === 'http:' ? http : https;
    const req = lib.request({
      protocol: u.protocol,
      hostname: u.hostname,
      port: u.port || (u.protocol === 'http:' ? 80 : 443),
      path: u.pathname + u.search,
      method: options.method || 'POST',
      headers: options.headers || {},
      timeout: 15000,
    }, (res) => {
      let data = '';
      res.setEncoding('utf8');
      res.on('data', (c) => { data += c; });
      res.on('end', () => resolve({ status: res.statusCode, body: data }));
    });
    req.on('timeout', () => req.destroy(new Error('请求超时')));
    req.on('error', reject);
    if (body) req.write(body);
    req.end();
  });
}

function parseJson(s) {
  try { return JSON.parse(s); } catch (e) { return null; }
}

/* 阿里云短信：RPC 风格 v1 签名（HMAC-SHA1） */
function percentEncode(s) {
  return encodeURIComponent(s)
    .replace(/\+/g, '%20')
    .replace(/\*/g, '%2A')
    .replace(/%7E/g, '~');
}

async function sendAliyun(item) {
  const c = cfg.aliyun;
  if (!c.accessKeyId || !c.accessKeySecret) throw new Error('未配置阿里云 AccessKey');
  if (!c.signName || !c.templateCode) throw new Error('未配置阿里云签名或模板 CODE');

  const params = {
    AccessKeyId: c.accessKeyId,
    Action: 'SendSms',
    Format: 'JSON',
    PhoneNumbers: item.phone,
    RegionId: c.regionId,
    SignName: c.signName,
    SignatureMethod: 'HMAC-SHA1',
    SignatureNonce: crypto.randomBytes(16).toString('hex'),
    SignatureVersion: '1.0',
    TemplateCode: c.templateCode,
    TemplateParam: JSON.stringify({ [c.paramName]: item.name || '' }),
    Timestamp: new Date().toISOString().replace(/\.\d{3}Z$/, 'Z'),
    Version: '2017-05-25',
  };
  const canonical = Object.keys(params).sort()
    .map((k) => percentEncode(k) + '=' + percentEncode(params[k]))
    .join('&');
  const stringToSign = 'POST&%2F&' + percentEncode(canonical);
  const signature = crypto.createHmac('sha1', c.accessKeySecret + '&')
    .update(stringToSign, 'utf8').digest('base64');

  const body = canonical + '&Signature=' + percentEncode(signature);
  const res = await httpRequest('https://dysmsapi.aliyuncs.com/', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/x-www-form-urlencoded',
      'Content-Length': Buffer.byteLength(body),
    },
  }, body);
  const json = parseJson(res.body) || {};
  if (json.Code === 'OK') return { ok: true, id: json.BizId || '', msg: json.Message || 'OK' };
  return { ok: false, msg: (json.Code ? json.Code + ' ' : '') + (json.Message || ('HTTP ' + res.status)) };
}

/* 腾讯云短信：TC3-HMAC-SHA256 签名 */
function sha256hex(s) { return crypto.createHash('sha256').update(s, 'utf8').digest('hex'); }
function hmac(key, s) { return crypto.createHmac('sha256', key).update(s, 'utf8').digest(); }

async function sendTencent(item) {
  const c = cfg.tencent;
  if (!c.secretId || !c.secretKey) throw new Error('未配置腾讯云 SecretId/SecretKey');
  if (!c.sdkAppId || !c.signName || !c.templateId) throw new Error('未配置 SdkAppId / 签名 / 模板 ID');

  const host = 'sms.tencentcloudapi.com';
  const service = 'sms';
  const action = 'SendSms';
  const version = '2021-01-11';
  const payload = JSON.stringify({
    PhoneNumberSet: [item.phone.startsWith('+') ? item.phone : '+86' + item.phone],
    SmsSdkAppId: String(c.sdkAppId),
    SignName: c.signName,
    TemplateId: String(c.templateId),
    TemplateParamSet: [String(item.name || '')],
  });
  const ts = Math.floor(Date.now() / 1000);
  const date = new Date(ts * 1000).toISOString().slice(0, 10);
  const contentType = 'application/json; charset=utf-8';
  const canonicalRequest = [
    'POST', '/', '',
    'content-type:' + contentType, 'host:' + host, '',
    'content-type;host', sha256hex(payload),
  ].join('\n');
  const scope = date + '/' + service + '/tc3_request';
  const stringToSign = ['TC3-HMAC-SHA256', ts, scope, sha256hex(canonicalRequest)].join('\n');
  const kDate = hmac('TC3' + c.secretKey, date);
  const kService = hmac(kDate, service);
  const kSigning = hmac(kService, 'tc3_request');
  const sig = crypto.createHmac('sha256', kSigning).update(stringToSign, 'utf8').digest('hex');

  const auth = 'TC3-HMAC-SHA256 Credential=' + c.secretId + '/' + scope +
    ', SignedHeaders=content-type;host, Signature=' + sig;

  const res = await httpRequest('https://' + host + '/', {
    method: 'POST',
    headers: {
      'Authorization': auth,
      'Content-Type': contentType,
      'Host': host,
      'X-TC-Action': action,
      'X-TC-Version': version,
      'X-TC-Region': c.region,
      'X-TC-Timestamp': String(ts),
    },
  }, payload);
  const json = parseJson(res.body) || {};
  const r = json.Response || {};
  if (r.Error) return { ok: false, msg: (r.Error.Code || '') + ' ' + (r.Error.Message || '') };
  const st = (r.SendStatusSet || [])[0] || {};
  if (st.Code === 'Ok') return { ok: true, id: st.SerialNo || '', msg: 'Ok' };
  return { ok: false, msg: (st.Code || '未知') + ' ' + (st.Message || '') };
}

/* 通用 HTTP 网关：任何平台都能接（云片、容联、SUBMAIL、自建等） */
async function sendCustom(item) {
  const c = cfg.custom;
  if (!c.url) throw new Error('未配置 custom.url');
  const fill = (s) => String(s)
    .replace(/\{\{phone\}\}/g, item.phone)
    .replace(/\{\{name\}\}/g, item.name || '')
    .replace(/\{\{content\}\}/g, item.content || '')
    .replace(/\{\{templateCode\}\}/g, c.templateCode || '')
    .replace(/\{\{signName\}\}/g, c.signName || '');
  const headers = {};
  Object.keys(c.headers || {}).forEach((k) => { headers[k] = fill(c.headers[k]); });
  let body = c.body ? fill(c.body) : '';
  if (body && !headers['Content-Type'] && !headers['content-type']) {
    headers['Content-Type'] = 'application/json; charset=utf-8';
  }
  if (body) headers['Content-Length'] = Buffer.byteLength(body);
  const res = await httpRequest(fill(c.url), { method: c.method || 'POST', headers }, body || undefined);
  const json = parseJson(res.body);
  let ok = res.status >= 200 && res.status < 300;
  if (typeof c.okField === 'string' && c.okField && json) {
    const v = c.okField.split('.').reduce((o, k) => (o == null ? o : o[k]), json);
    ok = !!v && v !== '0' && v !== 'false' && v !== 'fail';
  }
  if (ok) return { ok: true, msg: 'ok', id: (json && (json.id || json.msgId || json.bizId)) || '' };
  return { ok: false, msg: 'HTTP ' + res.status + ' ' + String(res.body || '').slice(0, 160) };
}

async function sendMock(item) {
  await new Promise((r) => setTimeout(r, 30 + Math.random() * 40));
  if (!/^1[3-9]\d{9}$/.test(item.phone.replace(/^\+86/, ''))) {
    return { ok: false, msg: '模拟：号码格式不合法' };
  }
  return { ok: true, id: 'mock_' + crypto.randomBytes(4).toString('hex'), msg: '模拟发送成功' };
}

const PROVIDERS = {
  mock: sendMock,
  aliyun: sendAliyun,
  tencent: sendTencent,
  custom: sendCustom,
};

function isConfigured() {
  const p = cfg.provider;
  if (p === 'mock') return true;
  if (p === 'aliyun') return !!(cfg.aliyun.accessKeyId && cfg.aliyun.accessKeySecret &&
    cfg.aliyun.signName && cfg.aliyun.templateCode);
  if (p === 'tencent') return !!(cfg.tencent.secretId && cfg.tencent.secretKey &&
    cfg.tencent.sdkAppId && cfg.tencent.signName && cfg.tencent.templateId);
  if (p === 'custom') return !!cfg.custom.url;
  return false;
}

/* ------------------------------ HTTP 层 ------------------------------ */

function cors(res) {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, X-Api-Token');
  res.setHeader('Access-Control-Max-Age', '86400');
}

function sendJson(res, code, obj) {
  const s = JSON.stringify(obj);
  res.writeHead(code, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(s),
    'Cache-Control': 'no-store',
  });
  res.end(s);
}

function readBody(req, limit) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on('data', (c) => {
      size += c.length;
      if (size > limit) { reject(new Error('请求体过大')); req.destroy(); return; }
      chunks.push(c);
    });
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', reject);
  });
}

function normPhone(p) {
  let s = String(p || '').replace(/[^\d+]/g, '');
  if (s.startsWith('+86')) s = s.slice(3);
  else if (s.startsWith('0086')) s = s.slice(4);
  else if (s.startsWith('86') && s.length === 13) s = s.slice(2);
  return s;
}

const VALID_PHONE = /^1[3-9]\d{9}$/;

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

async function handleBatch(req, res) {
  if (cfg.token && req.headers['x-api-token'] !== cfg.token) {
    return sendJson(res, 401, { ok: false, error: '接口令牌不正确' });
  }
  let payload;
  try {
    payload = JSON.parse(await readBody(req, 8 * 1024 * 1024));
  } catch (e) {
    return sendJson(res, 400, { ok: false, error: 'JSON 解析失败：' + e.message });
  }
  const items = Array.isArray(payload.items) ? payload.items : [];
  if (!items.length) return sendJson(res, 400, { ok: false, error: 'items 为空' });
  if (items.length > cfg.limitPerRequest) {
    return sendJson(res, 400, {
      ok: false,
      error: '单次最多 ' + cfg.limitPerRequest + ' 条，本次 ' + items.length + ' 条，请分批发送',
    });
  }
  const fn = PROVIDERS[cfg.provider];
  if (!fn) return sendJson(res, 500, { ok: false, error: '未知通道：' + cfg.provider });
  if (!isConfigured()) return sendJson(res, 400, { ok: false, error: '通道未配置完整，请检查 server/config.json' });

  const results = new Array(items.length);
  const seen = new Set();
  let cursor = 0, sent = 0, failed = 0, skipped = 0;

  async function worker() {
    while (cursor < items.length) {
      const i = cursor++;
      const it = items[i] || {};
      const phone = normPhone(it.phone);
      const name = String(it.name || '').trim();
      if (!VALID_PHONE.test(phone)) {
        results[i] = { phone: phone || String(it.phone || ''), name, ok: false, msg: '号码格式不合法' };
        failed++;
        continue;
      }
      if (seen.has(phone)) {
        results[i] = { phone, name, ok: false, msg: '本批重复号码，已跳过' };
        skipped++;
        continue;
      }
      seen.add(phone);

      let last = { ok: false, msg: '未执行' };
      for (let attempt = 0; attempt <= cfg.retries; attempt++) {
        try {
          last = await fn({ phone, name, content: String(it.content || '') });
        } catch (e) {
          last = { ok: false, msg: '请求异常：' + (e.message || e) };
        }
        if (last.ok) break;
        if (attempt < cfg.retries) await sleep(400 * Math.pow(2, attempt));
      }
      results[i] = { phone, name, ok: !!last.ok, msg: last.msg || '', id: last.id || '' };
      last.ok ? sent++ : failed++;
      if (cfg.delayMs) await sleep(cfg.delayMs);
    }
  }

  const started = Date.now();
  await Promise.all(Array.from({ length: Math.min(cfg.concurrency, items.length) }, worker));

  console.log('[批量] ' + new Date().toLocaleTimeString() + ' 通道=' + cfg.provider +
    ' 总=' + items.length + ' 成功=' + sent + ' 失败=' + failed +
    ' 重复跳过=' + skipped + ' 耗时=' + (Date.now() - started) + 'ms');

  sendJson(res, 200, {
    ok: true,
    provider: cfg.provider,
    total: items.length,
    sent,
    failed,
    skipped,
    elapsedMs: Date.now() - started,
    results,
  });
}

function serveStatic(req, res, urlPath) {
  let rel = decodeURIComponent(urlPath.split('?')[0]);
  if (rel === '/' || rel === '') rel = '/index.html';
  const file = path.join(ROOT, path.normalize(rel).replace(/^(\.\.[/\\])+/, ''));
  if (!file.startsWith(ROOT)) { res.writeHead(403); res.end('Forbidden'); return; }
  fs.stat(file, (err, st) => {
    if (err || !st.isFile()) {
      res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' });
      res.end('404 Not Found');
      return;
    }
    const ext = path.extname(file).toLowerCase();
    res.writeHead(200, {
      'Content-Type': MIME[ext] || 'application/octet-stream',
      'Content-Length': st.size,
      'Cache-Control': 'no-cache',
    });
    fs.createReadStream(file).pipe(res);
  });
}

const server = http.createServer(async (req, res) => {
  cors(res);
  const url = new URL(req.url, 'http://' + (req.headers.host || 'localhost'));

  if (req.method === 'OPTIONS') { res.writeHead(204); res.end(); return; }

  if (url.pathname === '/api/health') {
    sendJson(res, 200, {
      ok: true,
      provider: cfg.provider,
      configured: isConfigured(),
      limitPerRequest: cfg.limitPerRequest,
      concurrency: cfg.concurrency,
      version: '1.0.0',
    });
    return;
  }
  if (url.pathname === '/api/sms/batch') {
    if (req.method !== 'POST') { sendJson(res, 405, { ok: false, error: '请使用 POST' }); return; }
    try { await handleBatch(req, res); } catch (e) {
      console.error('[错误]', e);
      if (!res.headersSent) sendJson(res, 500, { ok: false, error: e.message || String(e) });
    }
    return;
  }
  if (url.pathname.startsWith('/api/')) { sendJson(res, 404, { ok: false, error: '未知接口' }); return; }
  if (req.method !== 'GET' && req.method !== 'HEAD') { res.writeHead(405); res.end(); return; }
  serveStatic(req, res, url.pathname);
});

function lanIps() {
  const out = [];
  Object.values(os.networkInterfaces()).forEach((list) => {
    (list || []).forEach((n) => {
      if (n.family === 'IPv4' && !n.internal) out.push(n.address);
    });
  });
  return out;
}

server.listen(cfg.port, '0.0.0.0', () => {
  const ips = lanIps();
  console.log('');
  console.log('  短信助手本地服务已启动');
  console.log('  通道：' + cfg.provider + (isConfigured() ? '（已配置）' : '（未配置完整，仅能测试流程）'));
  console.log('  并发：' + cfg.concurrency + '  重试：' + cfg.retries + '  单次上限：' + cfg.limitPerRequest);
  console.log('');
  console.log('  本机打开：   http://localhost:' + cfg.port + '/');
  ips.forEach((ip) => console.log('  手机打开：   http://' + ip + ':' + cfg.port + '/'));
  console.log('');
  console.log('  手机需与本电脑连同一个 Wi-Fi。Ctrl+C 停止服务。');
  console.log('');
});
