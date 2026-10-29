// 百度网盘登录态进共享池（2026-10-25 用户定档：「加入共享池，给其他用户一起用」）
//
// 背景：此前 BDUSS 只落本机钥匙串，代码注释写着「绝不进云端公共 Cookie 池」。
// 用户看到后决定开放共享，但**只接受显式贡献**。
//
// 三条硬约束（各对应一种真实事故）：
//   ① **绝不进自动同步**：`_sync_domains_to_cloud` 会遍历 `_root_domains()` 静默解密
//      每个桌面用户浏览器里的登录态推上云。百度一旦进那个集合，就等于替所有用户
//      做了「共享我的网盘账号」的决定 —— 这是同意权问题，不是技术问题。
//   ② **只走显式入口**：网盘页「贡献到共享池」按钮（带确认弹窗），且贡献的是用户
//      手动保存的 BDUSS。通用通道 /api/cookie/contribute 与 /api/cookie/sync/from-local
//      对百度必须拒收。
//   ③ **真验真后才共享**：问百度 uinfo（errno=0 才有效），过期/被踢的 BDUSS 直接拒，
//      不把废 Cookie 共享出去；顺带取 vip_type 告诉用户贡献的是不是会员账号。
//
// 取用顺序：手动 BDUSS > 本机缓存 > 浏览器解密 > 共享池（自己的永远优先于别人的）。
//
// 本测试读 server/ 与 web/ 真源码（不测副本）。
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const pool = readFileSync(join(repoRoot, 'server', 'cookie_pool.py'), 'utf8');
const appPy = readFileSync(join(repoRoot, 'server', 'app.py'), 'utf8');
const router = readFileSync(join(repoRoot, 'server', 'routers', 'cloud_drive.py'), 'utf8');
const appJs = readFileSync(join(repoRoot, 'web', 'app.js'), 'utf8');
const indexHtml = readFileSync(join(repoRoot, 'web', 'index.html'), 'utf8');
// 剥注释：注释里也写着这些标识符，不剥就会「注释里提一句」也算过
const poolJs = pool.replace(/"""[\s\S]*?"""/g, '').replace(/#.*$/gm, '');
const routerJs = router.replace(/"""[\s\S]*?"""/g, '').replace(/#.*$/gm, '');

let n = 0;
const ok = (cond, msg) => { assert.ok(cond, msg); n += 1; };

// ---- ① 域集合：百度在「仅贡献」集合里，且被 auto/from-local 排除 ----
ok(/_CONTRIB_ONLY_DOMAINS = \{"baidu\.com"\}/.test(poolJs),
  '缺少 _CONTRIB_ONLY_DOMAINS = {"baidu.com"}（显式贡献域集合）');
ok(!/_POOL_EXTRA_DOMAINS = \{[^}]*baidu\.com/.test(poolJs),
  'baidu.com 不得出现在 _POOL_EXTRA_DOMAINS（那个集合会被 _sync_domains_to_cloud 自动推送）');
ok(/ds \|= \{_strip_sub\(d\) for d in _CONTRIB_ONLY_DOMAINS if _strip_sub\(d\)\}/.test(poolJs),
  '_root_domains() 未纳入 _CONTRIB_ONLY_DOMAINS ⇒ is_allowed 会拒收百度');
ok(/def is_contrib_only\(/.test(poolJs), '缺少 is_contrib_only()（自动同步排除判据）');
ok(/def _sync_domains_to_cloud[\s\S]*?not is_contrib_only\(d\)[\s\S]*?for idx, domain in enumerate\(domains\)/.test(appPy.replace(/#.*$/gm, '')),
  '_sync_domains_to_cloud 未排除 contrib-only 域 ⇒ 会自动把用户浏览器里的百度 Cookie 推上云（同意权事故）');
ok(/if is_contrib_only\(domain\):/.test(appPy.replace(/#.*$/gm, '')),
  '/api/cookie/sync/from-local 未拒收 contrib-only 域（隐式读取浏览器登录态的通道不能碰百度）');
ok(/from cookie_pool import add_cookie, verify_cookie, is_allowed, is_contrib_only, _norm_domain/.test(appPy),
  'from-local 未导入 is_contrib_only');

// ---- ② 验真：真问站点，不是结构校验 ----
ok(/def baidu_probe\(/.test(poolJs), '缺少 baidu_probe()（问百度 uinfo）');
ok(/def verify_baidu\(/.test(poolJs), '缺少 verify_baidu()');
ok(/if d == "baidu\.com":\s*\n\s*return verify_baidu\(header\)/.test(poolJs),
  'verify_cookie 未把 baidu.com 分发给 verify_baidu（会退化成结构校验/yt-dlp 试解析）');
ok(/xpan\/nas\?method=uinfo/.test(pool), '验真接口不是 xpan/nas uinfo');

// ---- ③ 贡献端点：只此一个入口，且先验真后入池 ----
ok(/@router\.post\('\/api\/cloud-drive\/contribute'\)/.test(routerJs),
  '缺少 POST /api/cloud-drive/contribute 端点');
// 2026-10-27 文案定档：所有失败上屏统一 baidu.USER_ERROR_TEXT（细节只进日志），
// 所以这里断言「守卫结构 + 统一文案常量」，不再绑死旧的那句中文。
ok(/get_baidu_bduss\(\) or ""[\s\S]{0,400}if not stored:[\s\S]{0,300}return \{"ok": False, "error": baidu\.USER_ERROR_TEXT\}/.test(routerJs),
  '贡献前必须先有手动保存的 BDUSS（不能顺手抓一份就共享），无 BDUSS 须以统一文案拒绝');
ok(/ok, info = baidu_probe\(header\)[\s\S]{0,160}if ok is False:/.test(routerJs),
  '贡献前必须验真，验伪必须拒（不把过期 BDUSS 共享出去）');
ok(/add_cookie\("baidu\.com", header, source="contrib"\)/.test(routerJs),
  '贡献未写入共享池（add_cookie baidu.com）');

// ---- ④ 取用顺序：共享池排最后（自己的登录态优先） ----
{
  // 函数体切片内比对：手动 BDUSS 必须出现在任何「取池」之前（防换写法绕过）
  const body = (routerJs.match(/def _baidu_login_cookie\(\)[\s\S]*?(?=\n@router\.|\ndef |\Z)/) || [''])[0];
  const iStored = body.indexOf('get_baidu_bduss()');
  const iPools = Math.min(
    ...['get_cookie("pan.baidu.com")', 'cookie_pool'].map((k) => {
      const i = body.indexOf(k);
      return i === -1 ? Number.MAX_SAFE_INTEGER : i;
    }),
  );
  ok(iStored > -1 && iStored < iPools,
    '共享池必须排在 手动BDUSS 之后（自己的登录态永远优先于别人的；换别名调用也算绕过）');
}
ok(/from cookie_pool import get_cookie\s*\n\s*return get_cookie\("pan\.baidu\.com"\)/.test(routerJs),
  '共享池兜底未接上（_baidu_login_cookie 末尾应 return get_cookie("pan.baidu.com")）');

// ---- ⑤ 前端：按钮 + 确认弹窗 + 文案不再骗人 ----
ok(/id="cdCookieShare"/.test(indexHtml), '网盘页缺少「贡献到共享池」按钮');
ok(/cdCookieShare: \$\('cdCookieShare'\)/.test(appJs), 'el 表未登记 cdCookieShare');
ok(/el\.cdCookieShare\.addEventListener\('click', cdShareCookie\)/.test(appJs),
  '「贡献到共享池」按钮未绑定事件（点了没反应）');
ok(/const cdShareCookie = async \(\) => \{[\s\S]{0,600}?const go = await showConfirm\(/.test(appJs),
  '贡献前必须弹确认窗（BDUSS = 完整账号权限，不能一键直供）');
ok(/danger: true/.test(appJs), '确认弹窗应标 danger（这是有后果的操作）');
ok(/默认<b>只存这台电脑<\/b>；点下面的「贡献到共享池」才会把它加密分享给其他用户复用/.test(indexHtml),
  '网盘页文案必须改写：旧文案「只存这台电脑，不会上传」在开放共享后是假的');
ok(!/只存这台电脑，不会上传/.test(indexHtml), '旧文案「不会上传」必须删掉（与共享池矛盾）');

console.log(`✅ 百度登录态共享池守卫生效（app-dev）：仅显式贡献/禁自动同步/真验真/兜底排末位/前端确认（共 ${n} 项）`);
