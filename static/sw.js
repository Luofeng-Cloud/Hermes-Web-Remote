/* Hermes 远程访问 - 最小 Service Worker
 * 仅为满足 PWA 安装条件并在断网时给出友好提示；
 * 不缓存任何接口/会话数据（内容需要登录且实时变化）。 */
const OFFLINE_HTML = '<!doctype html><meta charset="utf-8">' +
  '<meta name="viewport" content="width=device-width,initial-scale=1">' +
  '<title>Hermes</title><style>body{font-family:sans-serif;background:#0f1115;color:#e8eaf0;' +
  'display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center}' +
  '@media(prefers-color-scheme:light){body{background:#f4f6fa;color:#1e293b}}</style>' +
  '<body><div><div style="font-size:42px">📡</div><p>无法连接到 Hermes<br>请确认手机已连接 Tailscale 后重试</p>' +
  '<button onclick="location.reload()" style="padding:8px 20px;border-radius:10px;border:0;' +
  'background:#5b8cff;color:#fff;font-size:15px">重新加载</button></div></body>';

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("fetch", (event) => {
  const req = event.request;
  // 只接管页面导航；其余请求（接口、脚本、视频 Range 等）交还浏览器默认处理
  if (req.mode !== "navigate") return;
  event.respondWith(
    fetch(req).catch(() => new Response(OFFLINE_HTML, {
      status: 503, headers: {"Content-Type": "text/html; charset=utf-8"},
    }))
  );
});
