// web/js/desktop-app.js — 桌面版(pywebview)专属行为脚本。
//
// 由 index.html 在桌面壳环境加载（pywebview 注入 或 打包指纹 isDesktopBuild 命中）。
// ⚠️ 纯 web 环境（Chrome 等浏览器打开 127.0.0.1:8321）也会加载本文件——但浏览器里的
//    pollPicked 已经无害：服务端 cdp_sniffer.mark_desktop_auth 对匿名调用是 no-op（不会
//    把「桌面端登录态」信号写成 False），且 /api/sniffer/picked 仅对带令牌的桌面端出队
//    （匿名轮询返回空、抢不走扩展条目）。所以这里**不再**用 `if (!window.pywebview) return`
//    去拦截——pywebview 注入时机不稳定，过早 return 会连桌面壳自己的轮询一起误杀。
//
// ⚠️ 这是 app 端窗口的专属编辑文件：以后桌面版的新功能（退出/原生桥接/系统托盘等）
//    只改这里，不要写回共享的 web/app.js，也不要让 web 窗口碰本文件。
//
// 复用 web/app.js 暴露的共享能力：window.VDL（el/$/escHtml/request/showError/
// createTaskCard/switchView）。app.js 必须先于本文件执行（index.html 已保证顺序）。
(function () {
  'use strict';

  if (!window.VDL || !window.VDL.el) {
    console.error('[desktop-app] window.VDL 未就绪，跳过桌面专属初始化');
    return;
  }
  const el = window.VDL.el;

  // ---- 剪贴板自动识别的「自复制」标记（2026-10-02） ----
  // App 自己往剪贴板写过的链接（用户点「复制链接」）不该再触发「检测到视频链接」
  // 提示条——否则用户每复制一次就被自己弹一次。watcher 与本标记通过闭包共享。
  const selfCopiedUrls = [];
  const noteSelfCopied = (url) => {
    if (typeof url !== 'string' || !url) return;
    if (selfCopiedUrls.indexOf(url) < 0) selfCopiedUrls.push(url);
    while (selfCopiedUrls.length > 20) selfCopiedUrls.shift();
  };
  const isSelfCopied = (url) => selfCopiedUrls.indexOf(url) >= 0;

  // 显式「退出」按钮：仅桌面版(pywebview)显示；浏览器回退模式隐藏（无原生窗口可退）。
  // 📌 2026-10-31 删掉右上角「返回桌面」按钮：它只是 window.minimize()，而 Windows/macOS
  //    窗口标题栏本来就有最小化（Windows 是 —，mac 是黄色圆点），页面里再放一个是重复入口，
  //    还容易被误点（点完窗口缩到任务栏，用户以为软件退出了）。后端 hide_to_desktop 保留，
  //    红叉/Cmd+W 的最小化语义不变。
  (function initQuitButton() {
    const wire = () => {
      const api = window.pywebview && window.pywebview.api;
      if (api && typeof api.quit_app === 'function') {
        el.quitAppBtn.hidden = false;
        el.quitAppBtn.addEventListener('click', () => {
          if (window.confirm('确定退出 VideoDownloader？')) api.quit_app();
        });
      }
    };
    if (window.pywebview && window.pywebview.api) {
      wire();
    } else {
      document.addEventListener('pywebviewready', wire, { once: true });
    }
  })();

  // 桌面增强命名空间：app.js 通过 window.VDL.desktop 委托纯桌面能力；
  // 纯 web 环境不加载本文件，故 window.VDL.desktop 为 undefined，app.js 自动走 web 回退。
  // 这是 Phase2「前端按端拆分」的增强层归宿——所有「无 web 等价」的桌面行为集中于此。
  const desktop = {
    // 在系统浏览器打开外部链接（OAuth 授权页等）。返回 true=已用原生桥接打开，
    // false=无原生桥接（调用方应回退到 window.open）。
    openExternal(url) {
      const api = window.pywebview && window.pywebview.api;
      if (!(api && typeof api.open_external === 'function')) return Promise.resolve(false);
      const norm = (r) => (typeof r === 'string') ? !r.startsWith('ERROR') : true;
      try {
        const r = api.open_external(url);
        if (r && typeof r.then === 'function') return r.then(norm).catch(() => false);
        return Promise.resolve(norm(r));
      } catch (e) {
        return Promise.resolve(false); // 桥接异常：告知调用方回退
      }
    },
    // 弹出系统文件夹选择框，返回所选目录绝对路径；无桥接或用户取消返回空串。
    // 注意：pywebview 的 api.* 调用返回 Promise，必须 await/then 取值。
    chooseFolder() {
      const api = window.pywebview && window.pywebview.api;
      if (!(api && typeof api.choose_folder === 'function')) return Promise.resolve('');
      const norm = (r) => (typeof r === 'string') ? (r.startsWith('ERROR') ? '' : r) : (r || '');
      try {
        const r = api.choose_folder();
        if (r && typeof r.then === 'function') return r.then(norm).catch(() => '');
        return Promise.resolve(norm(r));
      } catch (e) {
        return Promise.resolve('');
      }
    },
    // 弹出系统文件夹选择框，**专用于「浏览器扩展加载目录」**（提示语与用途一致）。
    // 与 chooseFolder 分成两条桥，是因为那个面板的提示语写死成「剪映草稿导出目录」，
    // 拿来选扩展目录会让用户以为点错了。
    chooseExtensionDir() {
      const api = window.pywebview && window.pywebview.api;
      if (!(api && typeof api.choose_extension_dir === 'function')) return Promise.resolve('');
      const norm = (r) => (typeof r === 'string') ? (r.startsWith('ERROR') ? '' : r) : (r || '');
      try {
        const r = api.choose_extension_dir();
        if (r && typeof r.then === 'function') return r.then(norm).catch(() => '');
        return Promise.resolve(norm(r));
      } catch (e) {
        return Promise.resolve('');
      }
    },
    // 弹出系统多文件选择框，返回绝对路径数组；无桥接或用户取消返回空数组。
    // 注意：pywebview 的 api.* 调用返回 Promise，必须 await/then 取值。
    // kind: 'media'(默认，视频+音频) | 'image'(图片) | 'any'(视频+音频+图片)。
    // 无 kind 的旧调用行为不变；旧二进制不认 kind 时按 media 处理。
    chooseFiles(kind) {
      const api = window.pywebview && window.pywebview.api;
      if (!(api && typeof api.choose_files === 'function')) { return Promise.resolve([]); }
      const norm = (r) => {
        if (!r) return [];
        if (typeof r === 'string') return r.startsWith('ERROR') ? [] : r.split('\n').filter(Boolean);
        if (Array.isArray(r)) return r.filter(Boolean);
        return [];
      };
      try {
        const r = kind ? api.choose_files(kind) : api.choose_files();
        if (r && typeof r.then === 'function') {
          return r.then(norm).catch(() => []);
        }
        return Promise.resolve(norm(r));
      } catch (e) {
        return Promise.resolve([]);
      }
    },
    // 保存抠图结果：弹系统保存面板，用户自选透明 PNG 位置；取消返回 "CANCELLED"，
    // 失败返回 "ERROR: ..."。无桥接时返回空串让调用方走 <a download> 兜底。
    saveMattingFile(jobId, suggestedName) {
      const api = window.pywebview && window.pywebview.api;
      if (!(api && typeof api.save_matting_file_dialog === 'function')) return Promise.resolve('');
      const norm = (r) => (typeof r === 'string') ? r : (r || '');
      try {
        const r = api.save_matting_file_dialog(jobId, suggestedName || 'matting.png');
        if (r && typeof r.then === 'function') return r.then(norm).catch(() => '');
        return Promise.resolve(norm(r));
      } catch (e) {
        return Promise.resolve('');
      }
    },
    // 保存二维码 PNG：弹系统保存面板，用户自选位置。
    // 必须走原生桥——WKWebView 不支持 <a download> 的 blob 下载，直接点会把主框架
    // 导航到 blob: 图片、整个 App 界面被二维码替换（用户 2026-09-21 报的缺陷）。
    // 入参 dataUrl 为 data:image/png;base64,... ；取消返回 "CANCELLED"，失败返回 "ERROR: ..."，
    // 无桥接（网页版）返回空串让调用方走 <a download> 兜底。
    saveQrImage(dataUrl, suggestedName) {
      const api = window.pywebview && window.pywebview.api;
      if (!(api && typeof api.save_qr_image_dialog === 'function')) return Promise.resolve('');
      const norm = (r) => (typeof r === 'string') ? r : (r || '');
      try {
        const r = api.save_qr_image_dialog(dataUrl, suggestedName || '分享二维码.png');
        if (r && typeof r.then === 'function') return r.then(norm).catch(() => '');
        return Promise.resolve(norm(r));
      } catch (e) {
        return Promise.resolve('');
      }
    },
    // 「我的音色」：弹原生文件框选一段参考录音，返回绝对路径；取消返回 ""。
    // 无桥接（网页版）返回 ""，调用方回退为手填路径。
    pickVoiceSample() {
      const api = window.pywebview && window.pywebview.api;
      if (!(api && typeof api.pick_voice_sample === 'function')) return Promise.resolve('');
      const norm = (r) => (typeof r === 'string') ? r : '';
      try {
        const r = api.pick_voice_sample();
        if (r && typeof r.then === 'function') return r.then(norm).catch(() => '');
        return Promise.resolve(norm(r));
      } catch (e) {
        return Promise.resolve('');
      }
    },
    // Qwen3-TTS 本地语音克隆服务起停（选中即起、切走即停），返回 {ok, msg}。
    // 与「用户不用懂端口/服务」的同一套约定；无桥接时返回可读提示。
    startQwen3Tts() {
      const api = window.pywebview && window.pywebview.api;
      if (api && typeof api.start_qwen3tts === 'function') {
        try {
          const r = api.start_qwen3tts();
          if (r && typeof r.then === 'function') return r;
          return Promise.resolve(r);
        } catch (e) {
          return Promise.resolve({ ok: false, msg: '启动失败：' + e });
        }
      }
      return Promise.resolve({ ok: false, msg: '当前环境不支持自动开启，请在桌面版中使用。' });
    },
    stopQwen3Tts() {
      const api = window.pywebview && window.pywebview.api;
      if (api && typeof api.stop_qwen3tts === 'function') {
        try {
          const r = api.stop_qwen3tts();
          if (r && typeof r.then === 'function') return r;
          return Promise.resolve(r);
        } catch (e) {
          return Promise.resolve({ ok: false, msg: '停止失败：' + e });
        }
      }
      return Promise.resolve({ ok: false, msg: '' });
    },
  };
  window.VDL.desktop = desktop;

  // 「Cookie 同步到云端」：仅 App 端，纯后台自动同步，按钮/徽标对用户不可见。
  // - 把本机浏览器各强反爬站(抖音/B站/快手/小红书等)的登录态自动推送到网页版(Railway)
  //   公共池，让网页版访客无需手动粘贴即可复用。
  // - 桌面版常驻时每 30 分钟自动刷新一次（启动后立即同步一次），使网页版共享登录态
  //   保持新鲜。完全后台执行，不打扰用户。
  (function initSyncCookie() {
    const btn = document.createElement('button');
    btn.id = 'syncCookieBtn';
    btn.textContent = '同步 Cookie 到云端';
    btn.type = 'button';
    // 不写内联样式 — 交给 .sidebar-cookie-sync button 类控制（padding/width/font 等）
    // 若未来 sidebar 兜底失效落到 header/body，那时需要 inject 完整样式，目前先不写
    btn.style.cssText = '';
    const badge = document.createElement('span');
    badge.id = 'syncCookieBadge';
    badge.textContent = '';
    badge.style.cssText = '';

    async function syncToCloud(showAlert, force) {
      // force=true：全量推送（超管手动按钮）。否则走 /sync/auto —— 由后端比对
      // 浏览器 Cookies 库的 mtime/size，登录态没变化就毫秒级 skipped。
      // 所以高频轮询几乎零成本，用户登录完最多一轮就能自动上云。
      const old = btn.textContent;
      btn.disabled = true;
      btn.textContent = '同步中…';
      try {
        const resp = await fetch(force ? '/api/cookie/sync/to-cloud' : '/api/cookie/sync/auto', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({}),
        });
        const data = await resp.json().catch(() => ({}));
        if (resp.ok) {
          // 登录态没变化：不动徽标，避免每 2 分钟刷一次无意义的「已同步」。
          if (data.skipped) return;
          const t = new Date().toLocaleTimeString();
          badge.textContent = `云端登录态已同步 ${t}（${data.pushed}/${data.total} 站）`;
          if (showAlert) {
            const lines = (data.results || [])
              .map((r) => `· ${r.domain}: ${r.pushed ? '已推送' : '未推送' + (r.reason ? '(' + r.reason + ')' : '')}`)
              .join('\n');
            window.alert(`同步完成（${data.pushed}/${data.total} 站成功）\n\n${lines}`);
          }
        } else {
          badge.textContent = '云端同步未配置/失败';
          if (showAlert) window.alert('同步失败：' + (data.detail || '未知错误'));
        }
      } catch (e) {
        badge.textContent = '云端同步出错';
        if (showAlert) window.alert('同步出错：' + e.message);
      } finally {
        btn.disabled = false;
        btn.textContent = old;
      }
    }

    // 用户视角：按钮/徽标均不可见。wrap 创建后立即隐藏，避免在 sidebar 留下空白
    // （display:none 不占布局空间，连带 badge 一起消失）。
    // 即使完全隐藏 DOM，setInterval 依然按 30min 节奏在后端触发同步。
    const sidebar = document.querySelector('aside.sidebar') || document.querySelector('.sidebar');
    if (sidebar) {
      const wrap = document.createElement('div');
      wrap.id = 'syncCookieWrap';
      wrap.className = 'sidebar-cookie-sync';
      wrap.style.display = 'none';   // ← 用户要求：界面不可见
      wrap.appendChild(btn);
      wrap.appendChild(badge);
      sidebar.insertBefore(wrap, sidebar.firstElementChild);
    } else {
      // sidebar 还不存在时，兜底：对象保留在内存，IIFE 末尾的定时器仍会触发同步；
      // 元素不挂到可视 DOM，对用户依然不可见。
      // 不 append 到 header/body，避免污染布局。
      void btn; void badge;
    }

    // 桌面版就绪后自动同步一次，之后每 2 分钟检查「登录态是否有变化」。
    // 2026-10-21：原为每 30 分钟一次全量（to-cloud 每次都要实时解密浏览器，
    // 14 站实测 16.7s），用户登录完只能干等半小时。现在由后端比对 Cookies 库
    // 的 mtime/size：没变化毫秒返回，有变化才解密 + 推送。
    const wire = () => { syncToCloud(false, false); };
    if (window.pywebview && window.pywebview.api) {
      wire();
    } else {
      document.addEventListener('pywebviewready', wire, { once: true });
    }
    setInterval(() => syncToCloud(false, false), 2 * 60 * 1000);

    // 2026-09-25：超管手动入口。默认保持隐藏（普通用户零痕迹）；
    // app.js 的 updateAdminTabVisibility() 在确认 is_admin 后调 setVisible(true)
    // 把按钮显示为「Cookie 池自动上传」，点击走同一 syncToCloud(true) 带结果弹窗。
    window.VDL.cookieSync = {
      // 超管手动点「Cookie 池自动上传」= 强制全量（force=true），
      // 不能走 auto 那套「没变化就 skipped」，否则用户点了没反应会以为坏了。
      run: (showAlert = true) => syncToCloud(showAlert, true),
      setVisible(v) {
        const wrap = document.getElementById('syncCookieWrap');
        if (wrap) wrap.style.display = v ? '' : 'none';
        const b = document.getElementById('syncCookieBtn');
        if (b && v) b.textContent = 'Cookie 池自动上传';
      },
    };
  })();

  /* =======================================================================
   * 浏览器嗅探（CDP + 悬浮球，2026-09-27 对标 DataTool）：
   * 连接带 --remote-debugging-port 的浏览器 → 监听各标签页的媒体请求
   * （HLS/DASH 清单、mp4/webm 直链）→ 面板一键下载（带来源页 Referer 防
   * 防盗链 403）；页面内悬浮球点「下载」的项经后端 outbox 自动回流到这里。
   * ======================================================================= */
  (function initSniffer() {
    if (!window.VDL || !window.VDL.request) return;
    const { $, escHtml, request, showError, createTaskCard, trackTask } = window.VDL;

    const POLL_ITEMS_MS = 2000;
    const POLL_PICKED_MS = 3000;
    let panelOpen = false;
    let itemsTimer = null;
    let pickedTimer = null;

    // ---- 样式（一次性注入，类名 vdl-sniff-* 避免冲突） ----
    const css = document.createElement('style');
    css.textContent = `
.vdl-sniff-panel{position:fixed;top:0;right:0;width:420px;max-width:92vw;height:100%;
  background:#fff;box-shadow:-8px 0 32px rgba(0,0,0,.18);z-index:2147483000;display:flex;
  flex-direction:column;font:13px/1.5 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;color:#222;}
.vdl-sniff-head{padding:14px 16px;border-bottom:1px solid #eee;display:flex;align-items:center;gap:8px;}
.vdl-sniff-head b{font-size:15px;flex:1;}
.vdl-sniff-body{flex:1;overflow:auto;padding:10px 16px;}
.vdl-sniff-status{padding:6px 10px;border-radius:8px;background:#f4f5f7;color:#555;margin-bottom:10px;}
.vdl-sniff-status.on{background:#e8f7ef;color:#0a7d43;}
.vdl-sniff-extbanner{padding:8px 10px;border-radius:8px;background:#eef4ff;border:1px solid #d9e4ff;color:#2b3a55;font-size:12px;line-height:1.6;margin-bottom:10px;}
.vdl-sniff-extbanner.on{background:#e8f7ef;border-color:#bfe8d2;color:#0a7d43;}
.vdl-sniff-extbanner.warn{background:#fff7e0;border-color:#f0dd9e;color:#7a5b00;}
.vdl-sniff-extbanner button{margin-left:8px;padding:3px 10px;border:0;border-radius:6px;background:#b8860b;color:#fff;font-size:12px;cursor:pointer;}
.vdl-sniff-extbanner button:hover{background:#9a7009;}
.vdl-sniff-actions{display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap;}
.vdl-sniff-actions button{border:0;border-radius:8px;padding:7px 12px;cursor:pointer;font-size:12px;}
.vdl-sniff-actions .main{background:#4f46e5;color:#fff;}
.vdl-sniff-actions .ghost{background:#eef;background:#eef0f4;color:#333;}
.vdl-sniff-actions .danger{background:#fdecec;color:#c0392b;}
.vdl-sniff-q{display:flex;align-items:center;gap:6px;margin-bottom:10px;font-size:12px;color:#555;}
.vdl-sniff-q select{border:1px solid #dfe3ea;border-radius:6px;padding:3px 6px;font-size:12px;background:#fff;color:#333;cursor:pointer;}
.vdl-sniff-q .q-note{color:#999;font-size:11px;margin-left:auto;}
.vdl-sniff-auto{display:flex;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:10px;font-size:12px;color:#555;}
.vdl-sniff-auto .txt{flex:1 1 100%;line-height:1.6;}
.vdl-sniff-auto .txt b{color:#333;}
.vdl-sniff-auto .path{display:block;color:#999;font-size:11px;word-break:break-all;}
.vdl-sniff-auto .ok{color:#0a7d43;}
.vdl-sniff-auto .warn{color:#c0392b;}
.vdl-sniff-auto button{border:0;border-radius:6px;padding:3px 10px;font-size:12px;cursor:pointer;background:#eef0f4;color:#333;}
.vdl-sniff-auto button.primary{background:#4f46e5;color:#fff;}
.vdl-sniff-auto button:disabled{opacity:.55;cursor:default;}
.vdl-sniff-item{border:1px solid #eee;border-radius:10px;padding:8px 10px;margin-bottom:8px;}
.vdl-sniff-item .k{font-size:11px;font-weight:600;border-radius:4px;padding:1px 6px;margin-right:6px;}
.vdl-sniff-item .k.playlist{background:#e8f7ef;color:#0a7d43;}
.vdl-sniff-item .k.media{background:#eef0ff;color:#4f46e5;}
.vdl-sniff-item .k.segment{background:#fff7e6;color:#b26a00;}
.vdl-sniff-item .u{color:#666;font-size:12px;word-break:break-all;margin:4px 0;}
.vdl-sniff-item .t{font-size:13px;font-weight:600;color:#222;line-height:1.45;margin:2px 0 3px;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;word-break:break-word;}
.vdl-sniff-item .t .dim{color:#aaa;font-weight:500;}
.vdl-sniff-item .m{color:#888;font-size:11px;margin-bottom:4px;}
.vdl-sniff-item button{border:0;border-radius:6px;padding:4px 10px;cursor:pointer;font-size:12px;margin-right:6px;}
.vdl-sniff-item .dl{background:#4f46e5;color:#fff;}
.vdl-sniff-item .cp{background:#eef0f4;}
.vdl-sniff-empty{color:#999;text-align:center;padding:30px 0;}
`;
    document.head.appendChild(css);

    // ---- 面板骨架 ----
    const badgeBtn = document.createElement('button');
    badgeBtn.type = 'button';
    badgeBtn.className = 'badge';
    badgeBtn.id = 'sniffBadge';
    badgeBtn.title = '连接浏览器嗅探视频流（HLS/直链），含页面悬浮球';
    badgeBtn.textContent = '🔍 浏览器嗅探';
    // 不再等 pywebviewready 才显示：本文件只在桌面壳环境加载，徽标直接常显。
    // （此前靠 wire() 解锁，pywebviewready 事件在动态脚本执行前就已派发时会永远隐藏）
    badgeBtn.hidden = false;

    // 入口徽标的显隐（2026-10-23 用户要求「常显」）：
    //   历史行为是「扩展在线（全自动模式）就自动收起」——用户找不到入口，反馈「嗅探是隐藏了吗」。
    //   改为一律常显：🔍 浏览器嗅探 固定留在顶栏，点开即面板（面板内仍按扩展状态切换 CDP/下载按钮）。
    //   回退方式：把 applyBadgeVisibility 换回
    //     badgeBtn.hidden = !!snifferExtOnline && !snifferNeedsAttention;
    //   snifferExtOnline / snifferNeedsAttention 仍照旧计算，供面板状态与将来回退使用。
    let snifferExtOnline = false;
    // 「还有事要做」：浏览器里加载的不是受管目录那个 / 版本落后 / 磁盘已更新待重载 ——
    // update-status 的 needs_setup 正好覆盖这几种（全都满足时才是 false）。
    let snifferNeedsAttention = false;
    const applyBadgeVisibility = () => {
      badgeBtn.hidden = false;
    };

    const panel = document.createElement('div');
    panel.className = 'vdl-sniff-panel';
    panel.hidden = true;
    panel.innerHTML = `
      <div class="vdl-sniff-head"><b>浏览器嗅探</b>
        <button type="button" class="ghost" id="sniffClose" style="border:0;background:#eef0f4;border-radius:8px;padding:6px 10px;cursor:pointer;">关闭</button>
      </div>
      <div class="vdl-sniff-body">
        <div class="vdl-sniff-status" id="sniffStatus">未连接</div>
        <div class="vdl-sniff-extbanner" id="sniffExtBanner">推荐：安装浏览器扩展，在你<b>日常的浏览器</b>里直接嗅探，无需另开浏览器。</div>
        <div class="vdl-sniff-actions">
          <button type="button" class="main" id="sniffStart">开始嗅探</button>
          <button type="button" class="ghost" id="sniffDownloadExt">下载浏览器扩展</button>
          <button type="button" class="danger" id="sniffStop" hidden>停止嗅探</button>
        </div>
        <div class="vdl-sniff-q">清晰度
          <select id="sniffQuality" title="「视频页 / HLS 清单」类条目按这个清晰度解析下载（直链本身就是单一流，不受影响）">
            <option value="best">最佳画质（自动）</option>
            <option value="2160">4K 2160P</option>
            <option value="1440">2K 1440P</option>
            <option value="1080">1080P 高清</option>
            <option value="720">720P 高清</option>
            <option value="480">480P 标清</option>
            <option value="360">360P 流畅</option>
            <option value="audio">仅音频 MP3</option>
          </select>
          <span class="q-note">对「视频页 / HLS 清单」类条目生效</span>
        </div>
        <div class="vdl-sniff-auto" id="sniffAutoRow" hidden>
          <span class="txt" id="sniffAutoText">扩展自动更新：未开启</span>
          <button type="button" class="primary" id="sniffAutoBtn">开启（自动维护目录）</button>
          <button type="button" id="sniffAutoCopy" hidden>复制目录路径</button>
          <button type="button" id="sniffAutoSync" hidden>立即同步</button>
          <button type="button" id="sniffAutoPick" hidden>手动指定目录…</button>
        </div>
        <div class="vdl-sniff-status" style="margin-top:0" id="sniffHint">提示：点「开始嗅探」后，若浏览器里还没装扩展，会自动打开一个独立调试浏览器（与你日常浏览器的登录态互不相通，Chrome 安全策略限制）。正在播放的流要重新播放一次才能被截到；悬浮球出现在视频页右下角。</div>
        <div id="sniffList"><div class="vdl-sniff-empty">还没有嗅探到媒体流</div></div>
        <div id="sniffExtHelp" hidden></div>
      </div>`;
    // 插入顶栏徽标行（「会员中心」一排，桌面壳专属行）；找不到顶栏才兜底挂 body
    const badgeRow = document.querySelector('#engineBadge')?.parentElement;
    if (badgeRow) badgeRow.insertBefore(badgeBtn, badgeRow.querySelector('#memberBadge'));
    else document.body.appendChild(badgeBtn);
    document.body.appendChild(panel);

    const statusEl = panel.querySelector('#sniffStatus');
    const listEl = panel.querySelector('#sniffList');
    const stopBtn = panel.querySelector('#sniffStop');
    const startBtn = panel.querySelector('#sniffStart');
    const dlExtBtn = panel.querySelector('#sniffDownloadExt');
    const hintEl = panel.querySelector('#sniffHint');

    const extBanner = panel.querySelector('#sniffExtBanner');

    // 嗅探面板默认清晰度（2026-10-01 用户反馈「目前没法选择分辨率」）：
    // 面板里「下载 / 解析并下载」的条目、以及扩展/悬浮球推来的条目，原本都被硬编码成
    // best，用户无从选择。现在面板给一个下拉并持久化；来源方（扩展 popup）显式带了
    // quality 时以来源方为准（见 sniffQuality）。
    const SNIFF_Q_KEY = 'vdl.sniff.quality';
    const qualitySel = panel.querySelector('#sniffQuality');
    let savedQuality = '';
    try { savedQuality = localStorage.getItem(SNIFF_Q_KEY) || ''; } catch (e) { /* 隐私模式 */ }
    if (savedQuality && qualitySel.querySelector('option[value="' + savedQuality + '"]')) {
      qualitySel.value = savedQuality;
    }
    qualitySel.addEventListener('change', () => {
      try { localStorage.setItem(SNIFF_Q_KEY, qualitySel.value); } catch (e) { /* 忽略 */ }
    });
    /** 建任务用的清晰度：只对「视频页 / HLS 清单」有意义（可解析出多档）；直链/分片
     *  本身就是单一流，一律走 best 原逻辑 —— 否则给 4K 直链选 1080 反而挑不到流。
     *  条目自带（扩展 popup 里选的）优先，其次面板默认。 */
    const sniffQuality = (it) => {
      // ① 来源方明确指定了清晰度（扩展 popup 里选的）→ 一律采信。只有「页面 / 清单」
      //    这类可能多档的来源才会带上它（扩展侧 qualityForItem 已把关，直链不带）。
      //    2026-10-02 真机实测：页面 URL 经服务端分类后可能落成 media，若这里再按
      //    kind 过滤就会把用户选的清晰度无声丢掉，故显式值优先于 kind 判断。
      const explicit = (it && it.quality) || '';
      if (explicit) return explicit;
      // ② 面板默认值只对可能多档的来源生效；直链/分片本身就是单一流，强塞 1080
      //    反而挑不到流，一律走 best。
      const kind = (it && it.kind) || '';
      if (kind !== 'page' && kind !== 'playlist') return 'best';
      return qualitySel.value || 'best';
    };

    // 包内扩展版本（缓存）：与扩展心跳自报版本比对 → 决定更新横幅
    let _extPkgVer = '';
    // 扩展自动更新状态（/api/extension/update-status）：提前声明，让下面的更新横幅
    // 也能读到（横幅文案在「已开启自动更新」时要换成「文件已自动写入」）。
    let autoSt = null;
    // 版本比较：仅当 a 严格新于 b 才返回 true；任一侧缺失/非法一律 false。
    // —— 只在「App 内置扩展比已装的更新」时提示升级，否则会把用户的新版覆盖成旧版。
    const cmpExtVer = (a, b) => {
      const pa = String(a || '').split('.').map((n) => parseInt(n, 10));
      const pb = String(b || '').split('.').map((n) => parseInt(n, 10));
      if (!pa.length || !pb.length || pa.some(isNaN) || pb.some(isNaN)) return false;
      for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
        const x = pa[i] || 0, y = pb[i] || 0;
        if (x !== y) return x > y;
      }
      return false;
    };
    const getExtPkgVer = async () => {
      if (_extPkgVer) return _extPkgVer;
      try {
        const info = await request('/api/extension/info');
        _extPkgVer = (info && info.version) || '?';
      } catch (err) { _extPkgVer = '?'; }
      return _extPkgVer;
    };

    // 下载扩展包（桌面壳走 pywebview 原生保存面板；浏览器模式 blob 兜底）
    const downloadExtPackage = async () => {
      const info = await request('/api/extension/info');
      const fname = (info && info.version)
        ? '视频工坊浏览器扩展-' + info.version + '.zip'
        : '视频工坊浏览器扩展.zip';
      // 桌面壳（WKWebView）里 blob + <a download> 会被静默吞掉（文件根本不落盘，
      // toast 却照弹）——必须走 pywebview 原生桥：Python 拉本机服务器的 zip，
      // 弹系统保存面板（默认「下载」文件夹）写盘，返回真实保存路径。
      const api = window.pywebview && window.pywebview.api;
      if (api && typeof api.save_direct_url === 'function') {
        const res = await api.save_direct_url(location.origin + '/api/extension/package', fname);
        if (typeof res === 'string' && res.startsWith('ERROR:')) {
          throw new Error(res.replace(/^ERROR:\s*/, ''));
        }
        if (res === 'CANCELLED') return; // 用户在保存面板点了取消：不提示不弹步骤
        renderExtHelp(info);
        sniffToast('✓ 扩展包已保存：' + res);
        return;
      }
      // 浏览器模式兜底：fetch→blob→临时 a 点击
      const resp = await fetch('/api/extension/package');
      if (!resp.ok) throw new Error('打包失败(' + resp.status + ')');
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = fname;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 4000);
      renderExtHelp(info);
      sniffToast('✓ 扩展包已下载，按面板步骤安装到浏览器');
    };

    const renderStatus = (st) => {
      const cdpBusy = st.state === 'running';
      const extOnline = !!(st && st.ext_online) && !cdpBusy;
      // 扩展在线（且 CDP 未在跑）＝全自动模式：收起 CDP 按钮 / 下载按钮，提示换成自动说明
      if (startBtn) startBtn.hidden = extOnline;
      // 同步「入口徽标」显隐：全自动模式下收起徽标（详见本模块顶部注释）
      snifferExtOnline = extOnline;
      applyBadgeVisibility();
      if (dlExtBtn) dlExtBtn.hidden = extOnline;
      if (hintEl) {
        hintEl.textContent = extOnline
          ? '全自动：在你日常的浏览器里播放视频即可，识别到的媒体流或视频页会实时出现在下方；点列表里的「下载」即建任务（YouTube 等站点推送的是页面链接，由桌面端解析）。'
          : '提示：点「开始嗅探」后，若浏览器里还没装扩展，会自动打开一个独立调试浏览器（与你日常浏览器的登录态互不相通，Chrome 安全策略限制）。正在播放的流要重新播放一次才能被截到；悬浮球出现在视频页右下角。';
      }
      // 扩展在线（用户日常浏览器）：绿色横幅替代推荐语——装完立刻有确定性反馈；
      // 心跳自报版本与包内版本不一致 → 黄色更新横幅（一键重新下载 zip）
      if (extBanner) {
        if (st.ext_online) {
          const installed = st.ext_version || '';
          const cap = (typeof st.ext_captured === 'number') ? st.ext_captured : null;
          extBanner.className = 'vdl-sniff-extbanner on';
          extBanner.innerHTML = '✓ <b>扩展已连接</b>——正在你日常的浏览器中嗅探，播放视频即可，无需另开浏览器。' +
            (cap !== null
              ? '<span style="display:block;margin-top:2px;">扩展本地已捕获 <b>' + cap + '</b> 条媒体流' +
                (cap === 0
                  ? (typeof st.ext_seen === 'number' && st.ext_seen > 0
                    ? '——已观察 ' + st.ext_seen + ' 个请求、识别到 ' + (st.ext_media || 0) + ' 条媒体' +
                      (st.ext_last_mime ? '（最近类型 ' + escHtml(String(st.ext_last_mime)) + '）' : '') +
                      '。把视频页<b>刷新或重新播放一次</b>即可（只抓新流量）。'
                    : '——列表为空时把视频页<b>刷新或重新播放一次</b>即可（只抓新流量）。')
                  : '。') +
                '</span>'
              : '');
          getExtPkgVer().then((pkgVer) => {
            if (pkgVer === '?' || pkgVer === installed) return; // 拿不到版本不误报
            if (!cmpExtVer(pkgVer, installed)) {
              // App 内置版本不高于已装版本 → 绝不提示「更新」（否则是降级）。
              // 若恰好是「已装的比内置的新」（App 尚未跟进更新），明说无需操作，免得用户困惑。
              if (installed && cmpExtVer(installed, pkgVer)) {
                extBanner.innerHTML += '<span style="display:block;margin-top:2px;color:#2e7d32;">' +
                  '（你的扩展 v' + escHtml(installed) + ' 比 App 内置的 v' + escHtml(pkgVer) + ' 更新，无需操作）</span>';
              }
              return;
            }
            extBanner.className = 'vdl-sniff-extbanner warn';
            extBanner.innerHTML =
              '⚠ 扩展有新版本（已装 v' + escHtml(installed || '旧版') + ' → 最新 v' + escHtml(pkgVer) + '）。' +
              '<button type="button" id="sniffExtUpdateBtn">更新扩展</button>' +
              (autoSt && autoSt.auto
                // 已开启自动更新：文件由桌面端直接写进扩展目录，扩展稍后自己重载
                // （见 extension/background.js 的 maybeAutoReload），用户无需任何操作。
                // 例外：浏览器里跑的还不是受管目录那一个 → 自动重载永远不会发生，
                // 必须让用户按面板提示重新加载一次（否则用户会一直等一个不会来的更新）。
                ? (autoSt.needs_setup
                  ? '<span style="display:block;margin-top:4px;color:#9a7c1a;">已开启自动更新，但浏览器里当前加载的还不是 App 维护的那个目录。请到下面「扩展自动更新」一行点<b>「复制目录路径」</b>，按提示到 chrome://extensions <b>重新加载一次</b>（仅此一次），此后所有升级都会自动完成。</span>'
                  : '<span style="display:block;margin-top:4px;color:#2e7d32;">已开启自动更新：新文件会自动写入扩展目录，扩展稍后自己重载生效（通常 1 分钟内）。若几分钟后仍显示旧版，再点下面按钮按手动流程走一次。</span>')
                : '<span style="display:block;margin-top:4px;color:#9a7c1a;">把解压出的文件<b>覆盖到原来加载的那个文件夹</b>（不要重新「加载已解压」，否则会装出两份），再到 chrome://extensions 点该扩展卡片上的刷新 ↻ 即完成升级。也可以到下面「扩展自动更新」一行开启自动更新，之后就完全不用管了。</span>');
            const ub = extBanner.querySelector('#sniffExtUpdateBtn');
            if (ub) ub.addEventListener('click', async () => {
              ub.disabled = true;
              try { await downloadExtPackage(); } finally { ub.disabled = false; }
            });
          }).catch(() => {});
        } else {
          extBanner.className = 'vdl-sniff-extbanner';
          extBanner.textContent = '推荐：安装浏览器扩展，在你日常的浏览器里直接嗅探，无需另开浏览器。';
        }
      }
      if (st.state === 'running') {
        statusEl.className = 'vdl-sniff-status on';
        statusEl.textContent = `嗅探中 · 端口 ${st.port} · 已捕获 ${st.items} 条`;
        stopBtn.hidden = false;
      } else if (st.ext_online) {
        statusEl.className = 'vdl-sniff-status on';
        statusEl.textContent = '扩展已连接 ✓（你的浏览器）';
        stopBtn.hidden = true;
      } else if (st.state === 'error') {
        statusEl.className = 'vdl-sniff-status';
        statusEl.textContent = '出错：' + (st.error || '未知错误');
        stopBtn.hidden = true;
      } else {
        statusEl.className = 'vdl-sniff-status';
        statusEl.textContent = '未连接';
        stopBtn.hidden = true;
      }
    };

    const KIND_LABEL = { playlist: 'HLS/DASH', media: '直链', segment: '分片', page: '视频页' };
    // 清晰度回显文案（与 index.html 的 batchQuality / 后端 downloader.quality_label 口径一致）：
    // 建任务后 toast 里回显一次，用户才能确认「我选的分辨率确实生效了」。
    const Q_LABEL = {
      best: '最佳画质（自动）', 2160: '4K 2160P', 1440: '2K 1440P', 1080: '1080P 高清',
      720: '720P 高清', 480: '480P 标清', 360: '360P 流畅',
      audio: '仅音频 MP3', webm: 'WebM', m4a: 'M4A 音频',
    };

    // 复制文本到剪贴板：桌面壳（WKWebView）里 navigator.clipboard 常不存在、
    // execCommand('copy') 也被禁用 —— 必须优先走 pywebview 原生桥，否则「复制链接」
    // 点了**毫无反应**（2026-10-01 用户实测）。可见反馈用 sniffToast，绝不静默。
    // what：被复制内容的称呼（默认「链接」；扩展目录路径传「目录路径」）。
    const copyText = (text, btn, what) => {
      const label = what || '链接';
      const markDone = () => {
        // 标记为「App 自己写到剪贴板的」→ 剪贴板 watcher 不再为这条链接弹提示条
        noteSelfCopied(text);
        if (btn) {
          const old = btn.textContent;
          btn.textContent = '已复制';
          setTimeout(() => { btn.textContent = old; }, 1200);
        }
        sniffToast('已复制' + label);
      };
      const markFail = () => sniffToast('复制失败，请手动选中' + label + '复制');
      const api = window.pywebview && window.pywebview.api;
      if (api && api.copy_to_clipboard) {
        Promise.resolve(api.copy_to_clipboard(text)).then((r) => {
          if (typeof r === 'string' && r.indexOf('ERROR') === 0) markFail(); else markDone();
        }).catch(markFail);
        return;
      }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(markDone).catch(() => {
          try {
            const ta = document.createElement('textarea');
            ta.value = text;
            ta.style.position = 'fixed';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            ta.select();
            const ok = document.execCommand('copy');
            ta.remove();
            ok ? markDone() : markFail();
          } catch (e) { markFail(); }
        });
        return;
      }
      markFail();
    };

    /** 时长（秒 → 12:34 / 1:02:03）；0/非法 → ''（调用方据此不显示该项）。2026-10-02 */
    const fmtDuration = (sec) => {
      const s = Math.round(Number(sec) || 0);
      if (!(s > 0)) return '';
      const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
      const p = (n) => (n < 10 ? '0' : '') + n;
      return h > 0 ? `${h}:${p(m)}:${p(r)}` : `${m}:${p(r)}`;
    };

    /** 大小（字节 → 128.4 MB）；0/非法 → ''。2026-10-02 */
    const fmtSize = (n) => {
      let b = Number(n) || 0;
      if (!(b > 0)) return '';
      const u = ['B', 'KB', 'MB', 'GB', 'TB'];
      let i = 0;
      while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
      return `${i === 0 ? b : (b >= 100 ? b.toFixed(0) : b.toFixed(1))} ${u[i]}`;
    };

    const renderItem = (it) => {
      const div = document.createElement('div');
      div.className = 'vdl-sniff-item';
      const short = it.url.length > 150 ? it.url.slice(0, 150) + '…' : it.url;
      // 「视频页」条目是 YouTube 这类加密流站点的正解：它本身不是媒体地址，
      // 点下去走的是解析（yt-dlp）而不是直下 —— 按钮文案要跟着变，否则用户以为点错了。
      const isPage = it.kind === 'page';
      // 标题 / 时长 / 大小（2026-10-02 用户「嗅探到的视频信息也加上：标题、时长、大小」）：
      //   标题 = 页面标题（page_title，扩展拿到 tab.title 后回填；SPA 换页时后到覆盖）；
      //   时长 = 页面侧 <video>.duration（服务端 page 条目「后到即覆盖」）；
      //   大小 = 响应头 Content-Length（加密流站点抓不到 → 该项自动不显示，不写 0）。
      const title = String(it.page_title || '').trim();
      let host = '';
      try { host = new URL(it.url).hostname; } catch (e) { host = ''; }
      const metaParts = [];
      const du = fmtDuration(it.duration);
      if (du) metaParts.push(`时长 ${du}`);
      const sz = fmtSize(it.size);
      if (sz) metaParts.push(sz);
      if ((it.count || 1) > 1) metaParts.push(`×${it.count}`);
      div.innerHTML =
        `<span class="k ${escHtml(it.kind)}">${KIND_LABEL[it.kind] || escHtml(it.kind)}</span>` +
        `<div class="t">${title ? escHtml(title) : `<span class="dim">${escHtml(host || '未命名')}</span>`}</div>` +
        (metaParts.length ? `<div class="m">${escHtml(metaParts.join(' · '))}</div>` : '') +
        `<div class="u">${escHtml(short)}</div>` +
        `<button type="button" class="dl">${isPage ? '解析并下载' : '下载'}</button><button type="button" class="cp">复制链接</button>`;
      div.querySelector('.dl').addEventListener('click', () => downloadItem(it, div));
      div.querySelector('.cp').addEventListener('click', () => copyText(it.url, div.querySelector('.cp')));
      return div;
    };

    const renderItems = (items) => {
      listEl.innerHTML = '';
      if (!items.length) {
        listEl.innerHTML = '<div class="vdl-sniff-empty">还没有嗅探到媒体流</div>';
        return;
      }
      items.forEach((it) => listEl.appendChild(renderItem(it)));
    };

    // 轻量提示条（替代 alert：成功类信息不打断操作，也不用「警告」标题吓人）
    const sniffToast = (msg) => {
      let t = document.getElementById('vdl-sniff-toast');
      if (!t) {
        t = document.createElement('div');
        t.id = 'vdl-sniff-toast';
        t.style.cssText =
          'position:fixed;left:50%;bottom:32px;transform:translateX(-50%);background:#222a38;color:#eaeaea;padding:10px 16px;border-radius:8px;font-size:13px;z-index:10000;box-shadow:0 6px 20px rgba(0,0,0,.4);max-width:80vw;display:none;';
        document.body.appendChild(t);
      }
      t.textContent = msg;
      t.style.display = 'block';
      clearTimeout(t._timer);
      t._timer = setTimeout(() => { t.style.display = 'none'; }, 2600);
    };

    // ---- 会员中心挂载点（2026-10-02）----
    // 本文件在 app.js 的闭包之外，「弹出会员中心」只能走挂载点（web/app.js 里
    // window.__vdlOpenMemberCenter = openMemberCenter）。万一挂载点缺失，退回点
    // 右上角常驻徽标（同一个入口）。
    const openMember = () => {
      try {
        if (typeof window !== 'undefined' && typeof window.__vdlOpenMemberCenter === 'function') {
          window.__vdlOpenMemberCenter();
          return true;
        }
        const badge = document.getElementById('memberBadge');
        if (badge) { badge.click(); return true; }
      } catch (e) { /* 会员中心打不开也不能影响下载主流程 */ }
      return false;
    };

    // 会员态缓存（60s，避免每次建任务都打一次心跳）：免费用户的「自动」档要封顶 1080P。
    // 读不到一律按会员放行（fail-open）—— 宁可漏封一次，也不能把付费用户的高清档封掉。
    let _memCache = { at: 0, member: true };
    const isDownloadMember = async () => {
      const now = Date.now();
      if (now - _memCache.at < 60000) return _memCache.member;
      let member = true;
      try {
        const st = await request('/api/member/status');
        member = !!(st && st.download_member && st.download_member.active);
      } catch (e) { /* 读不到 → 按会员放行 */ }
      _memCache = { at: now, member: member };
      return member;
    };

    /** 建任务真正下发的清晰度（2026-10-02 用户定档「免费用户 1080 以上要开会员」）：
     *  - 显式 2K/4K：照原样下发，由**后端**权威拦下（402 + 前端弹会员中心）——
     *    前端不再重复判一遍档位，免得两处判据分叉；
     *  - 「最佳画质（自动）」+ 免费用户 → 落 1080P（静默封顶），**只对 kind=page**：
     *    页面交给 yt-dlp 能拿到完整档位表，钉 1080 是安全的；而直链/分片/HLS 清单
     *    本身就是单一流或按清单走单一变体，1080 的选择器（末尾是 b[height<=1080]，
     *    没有无条件下探）会「挑不到流」——宁可留这个边界，也不能把现在能下的下载弄坏。
     *    显式选 2K/4K 的清单条目仍由后端拦住，不受影响。 */
    const qualityForDownload = async (it) => {
      const q = sniffQuality(it);
      if (q !== 'best') return q;
      if (((it && it.kind) || '') !== 'page') return q;
      if (await isDownloadMember()) return q;
      return '1080';
    };

    const downloadItem = async (it, div, silent = false) => {
      const btn = div.querySelector('.dl');
      btn.disabled = true;
      btn.textContent = '创建中…';
      try {
        const dlQuality = await qualityForDownload(it);
        const data = await request('/api/download', {
          method: 'POST',
          body: JSON.stringify({
            url: it.url,
            quality: dlQuality,
            title: it.page_title || '',
            cookie: it.cookie || '',
            proxy: '',
            extract_script: '',
            format_id: '',
            concurrent_fragments: 0,
            downloader: 'native',
            play_url: '',
            watch_options: [],
            is_hls: it.kind === 'playlist',
            referer: it.referer || '',
          }),
        });
        const refs = createTaskCard(data.task_id, {
          title: it.page_title || '(嗅探流)',
          platform: '嗅探',
        });
        trackTask(data.task_id, refs, '');
        btn.textContent = '已加入下载 ✓';
        sniffToast('✓ 已加入下载队列（' + (Q_LABEL[dlQuality] || dlQuality) +
          '）：' + (it.page_title || it.url.slice(0, 60)));
        return { ok: true, message: '' };
      } catch (e) {
        btn.disabled = false;
        btn.textContent = '下载';
        let msg = (e && e.message) || '未知错误';
        // 会员墙（清晰度档位 / 每日次数）必须**弹出会员中心**：扩展送来的条目走 silent
        // 分支（面板上没有可点的按钮），不弹的话用户在浏览器里只看到一行原因，根本不知道
        // 要去开会员（2026-10-02 用户明确要求「免费用户要弹出会员才能下载」）。
        if (msg.indexOf('MEMBER_QUOTA|') === 0) {
          openMember();
          // 回执要给扩展显示，去掉内部前缀（MEMBER_QUOTA| 是前后端约定码，不是人话）
          msg = msg.split('|').slice(1).join('|') || msg;
        }
        // silent（扩展/悬浮球回流的项，没有可点的面板按钮）：不弹桌面端错误框，
        // 由调用方把原因通过回执送回来源方——否则用户在浏览器里只看到「已发送 ✓」，
        // 桌面端却什么都没发生（用户实测抱怨「点下载没反应」）。
        if (!silent) showError('嗅探下载失败', msg);
        return { ok: false, message: msg };
      }
    };

    // 把后端 /api/extension/info 的「安装步骤」渲染进面板，引导用户把扩展装进浏览器
    const renderExtHelp = (info) => {
      const box = panel.querySelector('#sniffExtHelp');
      if (!box) return;
      const steps = (info && info.install_steps) || [];
      const name = (info && info.name) || '视频工坊媒体嗅探';
      const ver = (info && info.version) || '';
      const zipName = ver ? '视频工坊浏览器扩展-' + ver + '.zip' : '视频工坊浏览器扩展.zip';
      box.hidden = false;
      box.style.cssText =
        'margin-top:10px;padding:10px 12px;background:#f4f6fa;border:1px solid #e3e7ee;border-radius:10px;font-size:12px;line-height:1.7;color:#2b3442;';
      box.innerHTML =
        '<div style="font-weight:600;margin-bottom:6px;">已下载「' + escHtml(name) + '」' +
        (ver ? ' v' + escHtml(ver) : '') + '，按以下步骤装到浏览器：</div>' +
        '<div style="margin-bottom:6px;color:#5a6472;">安装包 <b>' + escHtml(zipName) +
        '</b> 已保存（保存面板默认「下载」文件夹，可自选位置），解压后按下面步骤加载。</div>' +
        (steps.length
          ? '<ol style="margin:0;padding-left:18px;">' + steps.map((s) => '<li>' + escHtml(s) + '</li>').join('') + '</ol>'
          : '<div>打开 chrome://extensions → 开发者模式 → 加载已解压的扩展程序，选择刚下载的文件夹即可。</div>');
    };

    const refresh = async () => {
      try {
        const st = await request('/api/sniffer/status');
        renderStatus(st);
        const list = await request('/api/sniffer/items?limit=60');
        renderItems(list.items || []);
      } catch (e) { /* 面板开着但后端忙：下次再刷 */ }
    };

    // 回执：把「建任务结果」写回服务端，浏览器扩展据此显示成功 / 失败原因
    // （未登录 / 不支持 / 超配额）。没有这条，扩展只能谎报「已发送 ✓」。
    const reportSendResult = (sendId, res) => {
      if (!sendId) return;
      request('/api/sniffer/send-result', {
        method: 'POST',
        body: JSON.stringify({
          send_id: sendId,
          ok: !!(res && res.ok),
          message: (res && res.message) || '',
        }),
      }).catch(() => { /* 回执尽力而为，不影响本地任务 */ });
    };

    // ---- 悬浮球 outbox / 浏览器扩展回流：来源方点「下载」→ 这里自动建任务 ----
    const pollPicked = async () => {
      try {
        const data = await request('/api/sniffer/picked');
        (data.items || []).forEach(async (it) => {
          const res = await downloadItem(Object.assign({}, it, { kind: it.kind || 'media' }),
            { querySelector: () => ({ disabled: false, textContent: '' }) }, true);
          reportSendResult(it.send_id, res);
        });
      } catch (e) { /* 静默 */ }
    };

    // ---- 扩展零点击自动更新（2026-10-02 用户问「扩展程序更新怎么办」）----
    // 背景：解压版（Load unpacked）扩展 Chrome **不会自动更新** —— 改了文件也必须到
    // chrome://extensions 点一次 ↻。而官方文档明确「解压版被 reload 视为一次 update」，
    // `chrome.runtime.reload()` 同样有效 → 于是拆成两半：
    //   ① 桌面端把内置新版**写进**扩展目录（服务端 extension_sync.py，带 manifest.name
    //      校验，只增不删）；
    //   ② 心跳响应告诉扩展「磁盘上已是新版」→ 扩展自己 chrome.runtime.reload()
    //      （见 extension/background.js::maybeAutoReload）
    // 本段只做「显示状态 + 开/关 + 手动同步 + 复制目录路径」，落盘与安全边界全在服务端。
    //
    // ⚠️ 落点为什么是 App 自己的目录（不是用户原来那个「下载」里的）：
    // macOS 把「下载 / 桌面 / 文稿」列为隐私保护目录，App 一访问就弹系统授权框；本项目
    // 是 ad-hoc 签名、每次重建 cdhash 都变 → 每次都要重新弹框，而弹框出现在后台线程里
    // 没人点 → open() 在内核里**无限期阻塞**（真机实测该端点 90s 不返回，进程栈停在
    // os_scandir→__opendir2→open$NOCANCEL）。所以落点改到 ~/视频工坊浏览器扩展
    // （主目录根，不属保护范围）；代价是用户首次要**从新目录重新加载一次**扩展。
    const autoRow = panel.querySelector('#sniffAutoRow');
    const autoText = panel.querySelector('#sniffAutoText');
    const autoBtn = panel.querySelector('#sniffAutoBtn');
    const autoCopyBtn = panel.querySelector('#sniffAutoCopy');
    const autoSyncBtn = panel.querySelector('#sniffAutoSync');
    const autoPickBtn = panel.querySelector('#sniffAutoPick');

    // 路径太长会把一行撑爆，缩成 ~/... 形式并截断
    const shortPath = (p) => {
      let s = String(p || '');
      if (s.startsWith('/Users/') || s.startsWith('/home/')) {
        s = '~' + s.slice(s.indexOf('/', 1));
      }
      return s.length > 44 ? '…' + s.slice(-43) : s;
    };

    // 一次性加载引导。为什么必须手把手：扩展的**加载目录**决定它的扩展 ID（Chrome 按
    // 路径派生），ID 变则设置与网站授权全丢 —— 所以从旧目录搬到受管目录只能用户自己做一次。
    const SETUP_STEPS = '首次使用（仅此一次）：① 点上面的「复制目录路径」'
      + ' ② 打开 chrome://extensions，开启右上角「开发者模式」'
      + ' ③ 点「加载已解压的扩展程序」，在路径栏按 ⌘⇧G 粘贴该路径并选中这个文件夹'
      + ' ④ 若之前装过旧版（一般在「下载」文件夹里），请把它移除，只留这一个';

    const renderAutoRow = () => {
      const st = autoSt;
      if (!st) { autoRow.hidden = true; return; }
      autoRow.hidden = false;
      const src = st.source_version || '?';
      const inst = st.installed_version || '';
      const dir = st.load_dir || st.managed_dir || '';
      const head = st.auto
        ? '扩展自动更新：<b>已开启</b>（浏览器内 v' + escHtml(inst || '未连接')
          + ' → 最新 v' + escHtml(src) + '）'
        : '扩展自动更新：<b>未开启</b>';
      // tail 里含 <span>，所以各段自行转义，不能整体 escHtml
      let tail;
      if (!st.auto) {
        tail = '开启后 App 会把新版写进它自己维护的扩展目录，扩展随后自动重载 —— 你不用再覆盖文件、点 ↻';
      } else if (st.error) {
        tail = '<span class="warn">' + escHtml(st.error) + '</span>';
      } else if (st.needs_setup) {
        tail = SETUP_STEPS;
      } else if (st.pending) {
        tail = '磁盘已是 v' + escHtml(st.on_disk_version) + '，等浏览器里的扩展自动重载（最多 1 分钟）';
      } else {
        tail = '已是最新，无需操作';
      }
      const dirLine = st.auto
        ? '<span class="path">目录：' + escHtml(shortPath(dir)) + '</span>'
        : '';
      autoText.innerHTML = head + dirLine + '<span class="path">' + tail + '</span>';
      autoBtn.textContent = st.auto ? '关闭自动更新' : '开启（自动维护目录）';
      autoCopyBtn.hidden = !st.auto;
      autoSyncBtn.hidden = !st.auto;
      autoPickBtn.hidden = !st.auto;
    };

    const refreshAutoStatus = async () => {
      try { autoSt = await request('/api/extension/update-status'); }
      catch (e) { autoSt = null; }
      // needs_setup 同时决定徽标要不要露出来（还有迁移/更新没做完时不能藏入口）
      snifferNeedsAttention = !!(autoSt && autoSt.needs_setup);
      renderAutoRow();
      applyBadgeVisibility();
    };

    const autoToastFrom = (r) => {
      const st = (r && r.status) || {};
      // update-config / update-now 把同步结果放在**顶层** sync（与 status 平级），
      // 不是嵌在 status 里 —— 两种形状都认，免得哪天挪了位置就悄悄丢提示。
      const synced = (r && r.sync) || st.sync || {};
      if (synced.reload_to) {
        sniffToast('扩展文件已更新到 v' + synced.reload_to + '，浏览器内会自动重载');
      } else if (synced.ok === false && synced.error) {
        sniffToast('同步未完成：' + synced.error);
      } else if (st.needs_setup) {
        sniffToast('目录已就绪 v' + (st.source_version || '?') + '：请按提示把扩展加载一次');
      } else {
        sniffToast('已开启扩展自动更新');
      }
    };

    // 把受管目录路径复制到剪贴板（用户要粘贴到 chrome://extensions 的文件夹选择框里）
    autoCopyBtn.addEventListener('click', () => {
      const st = autoSt || {};
      const dir = st.load_dir || st.managed_dir || '';
      if (!dir) { sniffToast('还没有可用目录，请先「开启」'); return; }
      copyText(dir, autoCopyBtn, '目录路径');
    });

    autoBtn.addEventListener('click', async () => {
      autoBtn.disabled = true;
      try {
        if (autoSt && autoSt.auto) {
          await request('/api/extension/update-config', {
            method: 'POST', body: JSON.stringify({ load_dir: '', auto: false }),
          });
          sniffToast('已关闭扩展自动更新');
        } else {
          // load_dir='auto' → 用 App 自己维护的扩展目录（服务端必要时就地初始化）
          const r = await request('/api/extension/update-config', {
            method: 'POST', body: JSON.stringify({ load_dir: 'auto', auto: true }),
          });
          autoToastFrom(r);
        }
      } catch (err) {
        showError('开启失败', (err && err.message) || '没能准备扩展目录');
      } finally {
        autoBtn.disabled = false;
        await refreshAutoStatus();
      }
    });

    autoPickBtn.addEventListener('click', async () => {
      const d = window.VDL && window.VDL.desktop && window.VDL.desktop.chooseExtensionDir;
      const dir = d ? await d() : '';
      if (!dir) return;                       // 用户取消（bridge 返回空串）
      autoPickBtn.disabled = true;
      try {
        const r = await request('/api/extension/update-config', {
          method: 'POST', body: JSON.stringify({ load_dir: dir, auto: true }),
        });
        autoToastFrom(r);
      } catch (err) {
        showError('设置失败', (err && err.message)
          || '该目录里没有本扩展的 manifest.json（或它属于系统保护的桌面/文稿/下载目录）');
      } finally {
        autoPickBtn.disabled = false;
        await refreshAutoStatus();
      }
    });

    autoSyncBtn.addEventListener('click', async () => {
      autoSyncBtn.disabled = true;
      try {
        const r = await request('/api/extension/update-now', { method: 'POST', body: '{}' });
        const res = (r && r.result) || {};
        sniffToast(res.ok
          ? ('已同步扩展文件到 v' + (res.version || '?') + '（新写入 ' + (res.written || 0) + ' 个文件）')
          : ('同步未完成：' + (res.error || '未知原因')));
      } catch (err) {
        showError('同步失败', (err && err.message) || '未知错误');
      } finally {
        autoSyncBtn.disabled = false;
        await refreshAutoStatus();
      }
    });

    badgeBtn.addEventListener('click', () => {
      panel.hidden = !panel.hidden;
      panelOpen = !panel.hidden;
      clearInterval(itemsTimer);
      clearInterval(pickedTimer);
      if (panelOpen) {
        refresh();
        refreshAutoStatus();
        itemsTimer = setInterval(refresh, POLL_ITEMS_MS);
      }
    });
    panel.querySelector('#sniffClose').addEventListener('click', () => {
      panel.hidden = true;
      panelOpen = false;
      clearInterval(itemsTimer);
    });
    panel.querySelector('#sniffStart').addEventListener('click', async (e) => {
      const btn = e.currentTarget;
      btn.disabled = true;
      // 智能连接（2026-10-01）：后端 start() 本身就是「先探测 9222——已有调试浏览器
      // 就直连，没有才启动」；launch=true 一发即覆盖两种情形，无需两个按钮。
      try { renderStatus(await request('/api/sniffer/connect', {
        method: 'POST', body: JSON.stringify({ port: 9222, launch: true }),
      })); } catch (err) { showError('连接失败', (err && err.message) || '未知错误'); }
      btn.disabled = false;
    });
    stopBtn.addEventListener('click', async () => {
      try { renderStatus(await request('/api/sniffer/disconnect', { method: 'POST', body: '{}' })); }
      catch (err) { showError('停止失败', (err && err.message) || '未知错误'); }
    });
    panel.querySelector('#sniffDownloadExt').addEventListener('click', async (e) => {
      const btn = e.currentTarget;
      btn.disabled = true;
      try { await downloadExtPackage(); } catch (err) {
        showError('下载扩展失败', (err && err.message) || '未知错误');
      } finally {
        btn.disabled = false;
      }
    });

    // picked 轮询常驻（悬浮球点击不依赖面板是否打开）
    pickedTimer = setInterval(pollPicked, POLL_PICKED_MS);

    // 嗅探状态轮询常驻：renderStatus 原先只在「打开面板」时被调用（见上方 refresh 的调用点），
    // 于是面板关着时前端根本不知道扩展在不在线 → 刚加的「扩展在线就收起入口徽标」在冷启动
    // 不会生效（徽标会一直挂着，直到用户手动开一次面板）。故这里独立低频拉一次，让徽标显隐
    // 从冷启动起就正确、并能跟随扩展上下线实时切换。
    // 只拉轻量的 /api/sniffer/status；**绝不**在这里拉 /api/sniffer/items（那是被限流的端点，
    // 关着面板时没必要请求）。getExtPkgVer 自带缓存，重复渲染不会重复请求。
    const POLL_STATUS_MS = 5000;
    const refreshStatusOnly = async () => {
      try { renderStatus(await request('/api/sniffer/status')); } catch (e) { /* 静默 */ }
      // 顺带刷新自动更新状态：徽标显隐要用 needs_setup（见 applyBadgeVisibility）
      try { await refreshAutoStatus(); } catch (e) { /* 静默 */ }
    };
    refreshStatusOnly();                                   // 冷启动立刻判定一次
    setInterval(refreshStatusOnly, POLL_STATUS_MS);

    // 桌面壳就绪后按当前状态决定入口徽标是否可见
    // （扩展在线**且**没有待办的迁移/更新时才收起，免得把引导入口一起藏掉）
    const wire = () => { applyBadgeVisibility(); };
    if (window.pywebview && window.pywebview.api) wire();
    else document.addEventListener('pywebviewready', wire, { once: true });
  })();

  // ---- 剪贴板自动识别：复制视频链接 → 切回 App → 一键下载（2026-10-02 用户需求） ----
  // 用户原话：「想复制链接复制完在 app 上粘贴下载」。做法是连粘贴都省掉：复制完切回 App
  // 就自动弹一条提示，点「下载」直接建任务，点「解析并选择」走原来的解析流程挑清晰度。
  //
  // 为什么「轮询 + focus」两条通道都挂：桌面壳 WKWebView 里 window 的 focus 事件触发
  // 时机不稳（窗口激活不一定派发 DOM focus），只靠它会出现「复制了却没反应」；只靠轮询
  // 又最多有 2s 延迟。谁先到算谁，命中后置 lastHandled 去重。
  // 读取一律走原生桥 read_clipboard（原因见 desktop_launcher.py 该方法的注释）。
  // 纯 web 环境（浏览器开 127.0.0.1:8321）没有 pywebview.api → 天然不启用。
  (function initClipboardWatcher() {
    const POLL_MS = 2000;
    const MAX_URL_LEN = 2048;
    const Q_KEY = 'vdl.sniff.quality';   // 与嗅探面板共用「上次选的清晰度」
    const QUALITY_CHOICES = [
      { v: 'best', t: '最佳画质' }, { v: '2160', t: '4K 2160P' }, { v: '1440', t: '2K 1440P' },
      { v: '1080', t: '1080P' }, { v: '720', t: '720P' }, { v: '480', t: '480P' },
      { v: '360', t: '360P' }, { v: 'audio', t: '仅音频 MP3' },
    ];
    let lastHandled = '';   // 最近一次「提示过 / 处理过 / 被忽略」的链接，用于去重
    let currentUrl = '';    // 当前提示条对应的链接（空=没挂提示条）
    let bar = null;
    let busy = false;       // 防桥调用重入

    const rememberQuality = (v) => { try { localStorage.setItem(Q_KEY, v); } catch (e) {} };
    const lastQuality = () => { try { return localStorage.getItem(Q_KEY) || 'best'; } catch (e) { return 'best'; } };

    // 严格判定「像一条视频链接」：单个 http(s) URL 且不含空白（带标题的复制会被拒，
    // 免得把一整段文字当链接）。宁可少弹，不要误弹。
    const asVideoUrl = (text) => {
      if (typeof text !== 'string') return '';
      const t = text.trim();
      if (!t || t.length > MAX_URL_LEN) return '';
      if (/\s/.test(t)) return '';
      return /^https?:\/\/\S+$/i.test(t) ? t : '';
    };

    const ensureBar = () => {
      if (bar) return bar;
      const css = document.createElement('style');
      css.textContent =
        '#vdl-clip-bar{position:fixed;left:50%;bottom:84px;transform:translateX(-50%);z-index:10001;' +
        'display:flex;align-items:center;gap:10px;max-width:92vw;background:#1f2735;color:#eaeaea;' +
        'border:1px solid rgba(255,255,255,.12);border-radius:10px;padding:9px 12px;' +
        'box-shadow:0 8px 26px rgba(0,0,0,.45);font-size:13px;}' +
        '#vdl-clip-bar .cb-ico{font-size:15px;flex:0 0 auto;}' +
        '#vdl-clip-bar .cb-txt{display:flex;flex-direction:column;min-width:0;max-width:46vw;}' +
        '#vdl-clip-bar .cb-t1{font-weight:600;}' +
        '#vdl-clip-bar .cb-t2{color:#9fb0c6;font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}' +
        '#vdl-clip-bar select{background:#2a3444;color:#eaeaea;border:1px solid rgba(255,255,255,.16);' +
        'border-radius:6px;padding:3px 6px;font-size:12px;}' +
        '#vdl-clip-bar button{border:0;border-radius:6px;padding:5px 12px;font-size:12px;cursor:pointer;}' +
        '#vdl-clip-bar .cb-dl{background:#4f46e5;color:#fff;font-weight:600;}' +
        '#vdl-clip-bar .cb-dl:disabled{opacity:.6;cursor:default;}' +
        '#vdl-clip-bar .cb-parse{background:#2a3444;color:#cfd8e3;}' +
        '#vdl-clip-bar .cb-x{background:transparent;color:#8f9fb2;font-size:14px;padding:2px 6px;}' +
        '#vdl-clip-bar .cb-dl{min-height:0;height:auto;}';
      document.head.appendChild(css);

      bar = document.createElement('div');
      bar.id = 'vdl-clip-bar';
      bar.hidden = true;
      bar.innerHTML =
        '<span class="cb-ico">🔗</span>' +
        '<div class="cb-txt"><div class="cb-t1">检测到视频链接</div><div class="cb-t2"></div></div>' +
        '<select class="cb-q" title="「下载」用这个清晰度"></select>' +
        '<button type="button" class="cb-dl">下载</button>' +
        '<button type="button" class="cb-parse">解析并选择</button>' +
        '<button type="button" class="cb-x" title="忽略这条">✕</button>';
      const sel = bar.querySelector('.cb-q');
      QUALITY_CHOICES.forEach((q) => {
        const o = document.createElement('option');
        o.value = q.v; o.textContent = q.t;
        sel.appendChild(o);
      });
      document.body.appendChild(bar);

      sel.addEventListener('change', () => rememberQuality(sel.value));

      bar.querySelector('.cb-dl').addEventListener('click', async () => {
        if (!currentUrl) return;
        const url = currentUrl;
        const quality = sel.value;
        const btn = bar.querySelector('.cb-dl');
        rememberQuality(quality);
        btn.disabled = true; btn.textContent = '创建中…';
        const ok = await startDownload(url, quality);
        btn.disabled = false; btn.textContent = '下载';
        lastHandled = url;
        if (ok) hideBar();
      });
      bar.querySelector('.cb-parse').addEventListener('click', () => {
        if (!currentUrl) return;
        const url = currentUrl;
        lastHandled = url;
        parseInstead(url);
        hideBar();
      });
      bar.querySelector('.cb-x').addEventListener('click', () => {
        if (currentUrl) lastHandled = currentUrl;
        hideBar();
      });
      return bar;
    };

    const showBar = (url) => {
      currentUrl = url;
      const b = ensureBar();
      b.querySelector('.cb-t2').textContent = url.length > 90 ? url.slice(0, 90) + '…' : url;
      const sel = b.querySelector('.cb-q');
      const want = lastQuality();
      sel.value = want;
      if (sel.value !== want) sel.value = 'best';   // 记忆值不在选项内时兜底
      b.hidden = false;
    };

    const hideBar = () => { if (bar) bar.hidden = true; currentUrl = ''; };

    // 「下载」：直接建任务（与嗅探面板 downloadItem 同一后端入口）
    const startDownload = async (url, quality) => {
      const { request, createTaskCard, trackTask, showError, switchView } = window.VDL;
      try {
        const data = await request('/api/download', {
          method: 'POST',
          body: JSON.stringify({
            url, quality, title: '', cookie: '', proxy: '', extract_script: '',
            format_id: '', concurrent_fragments: 0, downloader: 'native',
            play_url: '', watch_options: [], is_hls: false, referer: '',
          }),
        });
        const refs = createTaskCard(data.task_id, { title: '(剪贴板链接)', platform: '剪贴板' });
        trackTask(data.task_id, refs, '');
        if (typeof switchView === 'function') switchView('download');
        return true;
      } catch (err) {
        if (typeof showError === 'function') {
          showError('下载失败', (err && err.message) || '未知错误');
        }
        return false;
      }
    };

    // 「解析并选择」：填进首页输入框并提交，走原来的解析 → 挑清晰度 → 下载
    const parseInstead = (url) => {
      const { switchView } = window.VDL;
      if (typeof switchView === 'function') switchView('download');
      const form = document.getElementById('resolveForm');
      const input = document.getElementById('urlInput');
      if (!form || !input) return;
      input.value = url;
      input.dispatchEvent(new Event('input', { bubbles: true }));
      try {
        if (form.requestSubmit) form.requestSubmit();
        else form.dispatchEvent(new Event('submit', { cancelable: true, bubbles: true }));
      } catch (e) { /* 提交失败时输入框已填好，用户可手动点「解析链接」 */ }
    };

    const check = async () => {
      if (busy || currentUrl) return;   // 提示条挂着时先不打扰，等用户处理完
      const api = window.pywebview && window.pywebview.api;
      if (!(api && typeof api.read_clipboard === 'function')) return;
      busy = true;
      let text = '';
      try { text = await Promise.resolve(api.read_clipboard()); } catch (e) { text = ''; }
      busy = false;
      const url = asVideoUrl(text);
      if (!url || url === lastHandled) return;
      if (isSelfCopied(url)) { lastHandled = url; return; }   // App 自己写的，别自弹
      showBar(url);
    };

    const arm = () => {
      setInterval(check, POLL_MS);
      window.addEventListener('focus', () => { check(); }, false);
      document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') check();
      });
      setTimeout(check, 800);   // 启动后先查一次：App 刚开就把剪贴板里已有的链接接住
    };

    const api = window.pywebview && window.pywebview.api;
    if (api && typeof api.read_clipboard === 'function') {
      arm();
    } else {
      document.addEventListener('pywebviewready', () => {
        const a = window.pywebview && window.pywebview.api;
        if (a && typeof a.read_clipboard === 'function') arm();
      }, { once: true });
    }
  })();
})();
