#!/usr/bin/env node
/**
 * 飞书妙记转写 API 连通性测试脚本
 * 用法: node scripts/test-minutes-transcript.mjs <appId> <appSecret> <minute_url_or_token>
 */

import https from 'node:https';

const [appId, appSecret, minuteInput] = process.argv.slice(2);
if (!appId || !appSecret || !minuteInput) {
  console.error('用法: node scripts/test-minutes-transcript.mjs <appId> <appSecret> <minute_url_or_token>');
  console.error('示例: node scripts/test-minutes-transcript.mjs cli_xxx SECRET_xxx https://xxx.feishu.cn/minutes/obcnxxxxxxxxxxxxxxxxxxxx');
  process.exit(1);
}

// 从 URL 或裸 token 中提取 minute_token
const minuteToken = minuteInput.match(/minutes\/(\w+)/)?.[1] ?? minuteInput.trim();
if (!minuteToken || minuteToken.length < 20) {
  console.error('无法解析 minute_token，请提供有效的妙记 URL 或 token');
  process.exit(1);
}

console.log(`[1/3] appId: ${appId.slice(0, 6)}...`);
console.log(`[1/3] minuteToken: ${minuteToken.slice(0, 8)}...`);

// ---- 1. 获取 tenant_access_token ----
function postJson(hostname, path, payload) {
  return new Promise((resolve, reject) => {
    const data = JSON.stringify(payload);
    const req = https.request({ hostname, path, method: 'POST', headers: { 'Content-Type': 'application/json', 'Content-Length': data.length } }, (res) => {
      let body = '';
      res.on('data', (c) => body += c);
      res.on('end', () => {
        try { resolve(JSON.parse(body)); } catch { resolve(body); }
      });
    });
    req.on('error', reject);
    req.write(data);
    req.end();
  });
}

function getWithAuth(hostname, path, token) {
  return new Promise((resolve, reject) => {
    const req = https.request({ hostname, path, method: 'GET', headers: { 'Authorization': `Bearer ${token}` } }, (res) => {
      let body = '';
      res.on('data', (c) => body += c);
      res.on('end', () => {
        try { resolve(JSON.parse(body)); } catch { resolve(body); }
      });
    });
    req.on('error', reject);
    req.end();
  });
}

async function main() {
  // 1. 获取 token
  const tokenRes = await postJson('open.feishu.cn', '/open-apis/auth/v3/tenant_access_token/internal', {
    app_id: appId,
    app_secret: appSecret,
  });

  if (tokenRes.code !== 0 || !tokenRes.tenant_access_token) {
    console.error('[2/3] 获取 tenant_access_token 失败:', tokenRes);
    process.exit(1);
  }
  const token = tokenRes.tenant_access_token;
  console.log(`[2/3] tenant_access_token 获取成功 (expire: ${tokenRes.expire}s)`);

  // 2. 先获取妙记基础信息（测试基本连通性）
  const infoRes = await getWithAuth('open.feishu.cn', `/open-apis/minutes/v1/minutes/${minuteToken}`, token);
  console.log('[2/3] 妙记基础信息响应:', JSON.stringify(infoRes, null, 2).slice(0, 500));

  // 3. 获取转写
  const query = new URLSearchParams({ file_format: 'txt', need_speaker: 'true', need_timestamp: 'true' });
  const transcriptRes = await getWithAuth('open.feishu.cn', `/open-apis/minutes/v1/minutes/${minuteToken}/transcript?${query}`, token);

  console.log('\n[3/3] 转写 API 原始响应:');
  console.log(JSON.stringify(transcriptRes, null, 2));

  // 4. 结果判断
  if (transcriptRes.code === 0) {
    console.log('\n✅ API 调通！');
    if (typeof transcriptRes.data === 'string') {
      console.log(`\n📄 转写内容前 500 字:\n${transcriptRes.data.slice(0, 500)}`);
    } else if (transcriptRes.data && typeof transcriptRes.data.content === 'string') {
      console.log(`\n📄 转写内容前 500 字:\n${transcriptRes.data.content.slice(0, 500)}`);
    } else {
      console.log('\n⚠️ 响应结构未知，请查看上方原始 JSON');
    }
  } else if (transcriptRes.code === 2091005 || String(transcriptRes.msg).toLowerCase().includes('permission')) {
    console.log('\n❌ 权限不足 (2091005)。需要在飞书开发者后台给 Bot 应用申请以下 scope:');
    console.log('   - minutes:minutes:readonly');
    console.log('   - minutes:minutes.transcript:export');
  } else if (transcriptRes.code === 2091003 || String(transcriptRes.msg).toLowerCase().includes('not ready')) {
    console.log('\n⚠️ 妙记转写尚未生成，请稍后再试。');
  } else if (transcriptRes.code === 2091002) {
    console.log('\n❌ 妙记不存在，请检查 token 是否正确。');
  } else {
    console.log(`\n❌ 未知错误 (code=${transcriptRes.code})，请查看上方原始响应。`);
  }
}

main().catch((err) => { console.error('脚本异常:', err); process.exit(1); });
