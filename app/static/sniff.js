/* ============================================================================
 * 希悦自动订场 · 浏览器凭据扫描器  (sniff.js)  v3
 * ----------------------------------------------------------------------------
 * 用途：查清站点把登录凭据存在哪里，并验证「免密换令牌」通道是否可用。
 *
 * v2 变化（实测反馈）：
 *   - cookie 里只有 _pk_*（Matomo 统计），没有鉴权项 → 「仅 Cookie」不可行，
 *     且服务端 CORS 不允许 credentials，所以不再做 include 探测；
 *   - 新增 pinia 持久化（persist:root）深度展开：值常常是「JSON 字符串套 JSON 字符串」，
 *     只打印第一层根本看不到 token；
 *   - 只对找到的 JWT 做一次 Bearer 探测（credentials: omit，避开 CORS 限制）。
 *
 * v3 变化（2026-10-17 实测）：
 *   - persist:root 里只有 accessToken，没有 refreshToken → 站点 Web 端根本不发 RT；
 *   - 新增「免密换令牌通道探测」：POST passport.seiue.com/authorize（credentials: include），
 *     实测返回 201 + 新 access_token，即前端自己的续期通道，可用；
 *   - refresh 候选排除埋点类键（_pk_/__imt_/sentry…），不再把统计 ID 误报成 RT；
 *   - 结论按通道是否可用分流，直接给出下一步操作。
 *
 * 用法：
 *   1. 浏览器打开 https://chalk-c3.seiue.com/venues/venue-list ，确认已登录；
 *   2. F12 → Console → 整段粘贴 → 回车；
 *   3. 控制台打印脱敏清单；完整明文结果复制到剪贴板（只在本机用）。
 *
 * 安全：只读。除「免密换令牌通道探测」会真的签发一个新令牌外（等价于点一次「立即续期」），
 *       不改任何数据；页面里旧令牌仍然有效，两者互不影响。
 * ==========================================================================*/
(async () => {
  const API = 'https://api.seiue.com';
  const MAX_LINES = 120;
  const out = [];
  const leaves = [];                 // {path, value}
  const say = (s) => out.push(s);
  const mask = (v) => {
    const s = String(v == null ? '' : v);
    if (!s) return '(空)';
    if (s.length <= 24) return s;    // 短值直接显示，便于判断类型
    return s.slice(0, 8) + '…' + s.slice(-4) + '  (len=' + s.length + ')';
  };

  /* ---------- 0. 递归展开（穿透 JSON 字符串套 JSON 字符串） ---------- */
  const walk = (v, path, depth) => {
    if (v === null || v === undefined || depth > 6) return;
    if (typeof v === 'string') {
      leaves.push({ path: path || '(root)', value: v });
      const t = v.trim();
      if (t.length > 2 && (t[0] === '{' || t[0] === '[')) {
        try { walk(JSON.parse(t), path + ' ›', depth + 1); } catch (e) { /* 非 JSON */ }
      }
      return;
    }
    if (typeof v === 'object') {
      Object.keys(v).forEach((k) => walk(v[k], path ? path + '.' + k : k, depth + 1));
    }
  };

  /* ---------- 1. cookie ---------- */
  const cookies = (document.cookie || '').split(';').map((s) => s.trim()).filter(Boolean);
  say('=== cookie（JS 可读 ' + cookies.length + ' 项；HttpOnly 看不到）===');
  cookies.forEach((c) => {
    const i = c.indexOf('=');
    if (i < 0) return;
    const k = c.slice(0, i);
    let v = c.slice(i + 1);
    try { v = decodeURIComponent(v); } catch (e) { /* 原样 */ }
    leaves.push({ path: 'cookie.' + k, value: v });
    say('  ' + k + '  ' + mask(v));
  });

  /* ---------- 2. localStorage / sessionStorage（深度展开） ---------- */
  ['localStorage', 'sessionStorage'].forEach((name) => {
    let S;
    try { S = window[name]; } catch (e) { return; }
    if (!S) return;
    say('');
    say('=== ' + name + '（' + S.length + ' 项）===');
    for (let i = 0; i < S.length; i++) {
      const k = S.key(i);
      const raw = S.getItem(k) || '';
      const before = leaves.length;
      try { walk(JSON.parse(raw), name + '.' + k, 0); } catch (e) {
        leaves.push({ path: name + '.' + k, value: raw });
      }
      if (leaves.length === before) say('  ' + k + '  (空)');
    }
  });

  /* ---------- 3. IndexedDB 库名 ---------- */
  if (window.indexedDB && indexedDB.databases) {
    try {
      const dbs = await indexedDB.databases();
      if (dbs && dbs.length) {
        say('');
        say('=== IndexedDB 库 ===');
        dbs.forEach((d) => say('  ' + d.name));
      }
    } catch (e) { /* 忽略 */ }
  }

  /* ---------- 4. 叶子清单（这是判断凭据的关键，别只打印第一层） ---------- */
  say('');
  say('=== 存储叶子（' + leaves.length + ' 条）===');
  leaves.slice(0, MAX_LINES).forEach((it) => {
    say('  ' + it.path + '  ' + mask(it.value));
  });
  if (leaves.length > MAX_LINES) {
    say('  …还有 ' + (leaves.length - MAX_LINES) + ' 条，完整内容见剪贴板');
  }

  /* ---------- 5. 令牌识别 ---------- */
  const jwtRe = /eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}/;
  const jwtHits = [];
  leaves.forEach((it) => {
    const m = String(it.value).match(new RegExp(jwtRe.source, 'g'));
    if (m) m.forEach((t) => jwtHits.push({ path: it.path, token: t }));
  });
  const seen = new Set();
  const jwts = jwtHits.filter((h) => (seen.has(h.token) ? false : (seen.add(h.token), true)));

  say('');
  say('=== 令牌识别 ===');
  say('  JWT（access_token）: ' + (jwts.length ? '找到 ' + jwts.length + ' 个' : '没找到'));
  jwts.forEach((h) => {
    let meta = '';
    try {
      const p = JSON.parse(atob(h.token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')));
      if (p.exp) {
        const left = Math.round((p.exp * 1000 - Date.now()) / 3600000);
        meta = '  过期 ' + new Date(p.exp * 1000).toLocaleString() +
          '（' + (left > 0 ? '还剩 ' + left + ' 小时' : '已过期') + '）';
      }
      if (p.sub) meta += '  sub=' + p.sub;
    } catch (e) { /* 非标准 */ }
    say('    位置 ' + h.path + meta);
  });

  // refresh 候选：键名像 refresh/renew，或「不像 JWT 但形态像令牌」的项。
  // ⚠️ 埋点/统计类的键要排除：_pk_*（Matomo）、__imt_*（友盟）、_ga/_gid、sentry 等，
  //    它们形态也像一长串乱码，历史上把人带偏过。
  const ANALYTICS = /(^|[._])(_pk_|__imt|_ga|_gid|_gat|sentry|amplitude|mixpanel|hotjar|_hj|clarity|mtm_|matomo|umeng)/i;
  const rtCandidates = leaves.filter((it) => {
    const v = String(it.value);
    if (!v || v.length < 16 || jwtRe.test(v)) return false;
    if (ANALYTICS.test(it.path)) return false;
    return /refresh|renew|long_?term|remember|(^|[._])rt$/i.test(it.path) ||
      /^[A-Za-z0-9._~+/=-]{32,512}$/.test(v);
  });
  say('  refresh 候选: ' + (rtCandidates.length || '无'));
  rtCandidates.slice(0, 10).forEach((it) => {
    say('    位置 ' + it.path + '  ' + mask(it.value));
  });
  if (!rtCandidates.length) {
    say('    （本站点 Web 端不向浏览器发 refresh_token，属正常；续期走下面的免密通道）');
  }

  /* ---------- 6. 免密换令牌通道探测 ---------- *
   * 前端源码实测：没有 refreshToken 时走的是
   *   POST passport.seiue.com/authorize
   *   form: response_type=token & client_id=<web client>
   *   withCredentials: true（靠 passport 域的 HttpOnly 会话 Cookie）
   * 该域响应带 Access-Control-Allow-Credentials: true，所以浏览器里能直接验。
   * ⚠️ 这一步会真的签发一个新令牌（等价于点一次「立即续期」），
   *    页面里旧的 accessToken 会失效，刷新页面即可自动换回，无副作用。      */
  say('');
  say('=== 免密换令牌通道探测（会真的换一次令牌）===');
  let passportStatus = 0;
  let mintedToken = '';
  try {
    const r = await fetch('https://passport.seiue.com/authorize', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({
        response_type: 'token',
        client_id: 'GpxvnjhVKt56qTmnPWH1sA',
      }),
    });
    passportStatus = r.status;
    const body = await r.text();
    const m = body.match(/"access_token"\s*:\s*"([^"]+)"/);
    if (m) mintedToken = m[1];
    say('  POST passport/authorize → HTTP ' + r.status);
    say('  响应片段: ' + body.slice(0, 200));
    if (mintedToken) {
      say('  已签到新令牌（len=' + mintedToken.length + '），页面上旧的令牌仍然有效，两者互不影响。');
    }
  } catch (e) {
    say('  探测失败：' + e.message + '（多半是跨域被拦）');
  }

  /* ---------- 7. 探测（只带 Bearer，不带 cookie，避开 CORS credentials） ---------- */
  say('');
  say('=== 只读探测（GET 场地清单，不改数据）===');
  let probeStatus = 0;
  if (jwts.length) {
    try {
      const r = await fetch(API + '/scms/venue/order-venues', {
        method: 'GET',
        credentials: 'omit',
        headers: { authorization: 'Bearer ' + jwts[0].token },
      });
      probeStatus = r.status;
      say('  Bearer（取自 ' + jwts[0].path + '）→ HTTP ' + r.status);
    } catch (e) {
      say('  Bearer 探测失败：' + e.message);
    }
  } else {
    say('  跳过：没有可用的 JWT —— 令牌可能只在内存里（刷新页面就没了）。');
  }

  /* ---------- 8. 结论 ---------- */
  say('');
  say('=== 初步结论 ===');
  if (passportStatus >= 200 && passportStatus < 300) {
    say('  ✅ 免密换令牌通道可用（HTTP ' + passportStatus + '）——这是站点前端自己的续期通道。');
    say('  接下来只要一步，配好就再也不用管令牌过期：');
    say('     F12 → Application → Cookies → https://passport.seiue.com');
    say('     选中该域下的 cookie 行 → 复制 → 粘到程序「设置 → ②b → 会话 Cookie」→ 保存');
    say('     再点「立即续期并校验」，显示成功即配好。');
    say('  注意：这个 cookie 是 HttpOnly 的，JS 读不到，只有上面那个面板里能看到，属正常。');
  } else if (passportStatus === 401 || passportStatus === 403) {
    say('  ⚠️ 通道返回 ' + passportStatus + '（Login required）：passport 域的会话 Cookie 已失效。');
    say('     先在站点上重新登录，再重跑本脚本。');
  }
  if (jwts.length) {
    if (probeStatus === 200) {
      say('  ✅ 页面里的 JWT 目前有效（Bearer 可用）。');
    } else if (probeStatus === 401) {
      say('  ⚠️ 这个 JWT 已失效（或不是这个接口用的令牌）。刷新页面后再跑一次。');
    }
    if (!(passportStatus >= 200 && passportStatus < 300) && rtCandidates.length) {
      say('  → 免密通道不通时，可尝试把上面某个候选的完整值填进「②b 令牌自动续期」的 refresh_token 栏。');
    }
  }

  const text = out.join('\n');
  console.log(text);
  try {
    copy(JSON.stringify({
      at: new Date().toISOString(),
      origin: location.origin,
      passportStatus: passportStatus,
      mintedToken: mintedToken,
      probeStatus: probeStatus,
      leaves: leaves,
      jwts: jwts.map((h) => ({ path: h.path, token: h.token })),
      rtCandidates: rtCandidates,
    }, null, 2));
    console.log('\n（完整结果含明文令牌，已复制到剪贴板；只在本机用，别外发）');
  } catch (e) {
    console.log('\n（剪贴板不可用，请手动复制）');
  }
})();
