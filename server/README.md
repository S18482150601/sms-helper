# 短信通道批量发送 · 本地服务

把「一键批量发送」跑通的正规做法：网页不碰密钥，密钥只存在你电脑上。

## 三步启动

1. 装 Node.js（18 或更高）：<https://nodejs.org> → 下载 LTS → 一路默认安装
2. 双击 `server\启动服务.bat`（Mac/Linux 执行 `sh server/start.sh`）
3. 窗口里会打印两个地址，手机用**同一个 Wi-Fi** 打开「手机打开」那个地址

第一次启动会自动生成 `config.json`，默认 `mock` 模拟模式——**不会真发短信**，只验证整条链路。

## 切到真实通道

用记事本打开 `server\config.json`，把 `provider` 改成对应值并填密钥：

### 阿里云
```json
"provider": "aliyun",
"aliyun": {
  "accessKeyId": "LTAI...",
  "accessKeySecret": "...",
  "signName": "你审核通过的签名",
  "templateCode": "SMS_123456789",
  "paramName": "name"
}
```
模板内容示例：`尊敬的${name}，……`（`${name}` 是变量名，和 `paramName` 对应）

### 腾讯云
```json
"provider": "tencent",
"tencent": {
  "secretId": "...", "secretKey": "...",
  "sdkAppId": "1400000000",
  "signName": "签名", "templateId": "123456",
  "paramName": "name"
}
```

### 任意其他平台（云片 / 容联 / SUBMAIL / 自建网关）
```json
"provider": "custom",
"custom": {
  "url": "https://平台接口地址",
  "method": "POST",
  "headers": { "Authorization": "Bearer xxx" },
  "body": "{\"mobile\":\"{{phone}}\",\"content\":\"{{content}}\"}",
  "okField": "code"
}
```
占位符：`{{phone}}` `{{name}}` `{{content}}` `{{templateCode}}` `{{signName}}`

改完保存，**关掉窗口重新双击启动**即生效。

## 参数含义

| 字段 | 作用 |
|------|------|
| `concurrency` | 同时发几条，默认 6。平台限流就调小 |
| `retries` | 失败重试次数，默认 2（指数退避） |
| `delayMs` | 每条之间停顿毫秒，默认 0 |
| `limitPerRequest` | 单次请求上限，默认 1000 |
| `token` | 设了就要求网页填同一个令牌，防同网段他人调用 |

## 页面怎么用

页面「4. 短信通道批量发送」卡片：

1. 接口地址默认填当前地址（同源），不用改
2. 点「检查通道」→ 显示 `mock` 或真实通道名
3. 输入模板 CODE（阿里云/腾讯云用；custom 通道可留空）
4. 填变量名，默认 `name`
5. 勾选联系人 → 点「一键批量发送」

结果表格显示每条的号码、姓名、成功/失败原因，可导出 CSV。

## 重要说明

- 网页里存的是联系人，服务里存的是密钥，两者不在一起，泄露风险低
- `config.json` 已加入 `.gitignore`，不会被推到 GitHub
- 单个请求上限 1000 条，超过请分批；实际能否发成功取决于签名与模板审核状态
- 营销类短信需接收人同意，并带退订方式，否则会被运营商拦截
