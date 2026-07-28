// loader.js — the single <script> tag customers embed. Injects a floating bubble and,
// on first click, lazy-loads the iframe pointed at /widget/frame/. Cross-origin safe:
// loader lives on the customer's page, iframe on our origin; they communicate via
// postMessage with an origin lock.
//
// Usage:
//   <script src="https://app.example.com/static/widget/loader.js"
//           data-key="pk_..."         (required)
//           data-endpoint="https://app.example.com"   (optional; defaults to script src)
//           async></script>
(function () {
  if (window.__supportWidgetLoaded) return;   // idempotent — multiple includes = one bubble
  window.__supportWidgetLoaded = true;

  var script = document.currentScript || (function () {
    var scripts = document.getElementsByTagName("script");
    for (var i = scripts.length - 1; i >= 0; i--) {
      if (scripts[i].src && scripts[i].src.indexOf("loader.js") !== -1) return scripts[i];
    }
    return null;
  })();
  if (!script) return;

  var PUBLIC_KEY = script.getAttribute("data-key") || "";
  if (!PUBLIC_KEY) { console.warn("[widget] missing data-key"); return; }

  // Endpoint defaults to the script's own origin — so ordinarily customers only supply
  // data-key.
  var ENDPOINT = script.getAttribute("data-endpoint") || (function () {
    try { return new URL(script.src).origin; } catch (e) { return ""; }
  })();
  if (!ENDPOINT) { console.warn("[widget] cannot derive endpoint"); return; }

  var FRAME_ORIGIN = ENDPOINT.replace(/\/+$/, "");

  var bubble, iframe, panel, dot;
  var open = false, unread = 0;

  function h(tag, attrs, style) {
    var el = document.createElement(tag);
    if (attrs) for (var k in attrs) el.setAttribute(k, attrs[k]);
    if (style) for (var s in style) el.style[s] = style[s];
    return el;
  }

  function mountBubble(color) {
    bubble = h("div", { "aria-label": "Open chat", "role": "button", "tabindex": "0" }, {
      position: "fixed", right: "20px", bottom: "20px", zIndex: "2147483000",
      width: "56px", height: "56px", borderRadius: "50%",
      background: color || "#2563eb", color: "#fff",
      display: "flex", alignItems: "center", justifyContent: "center",
      cursor: "pointer", boxShadow: "0 6px 18px rgba(0,0,0,0.18)",
      fontFamily: "-apple-system, system-ui, sans-serif", fontSize: "26px",
      transition: "transform 120ms ease",
    });
    bubble.innerHTML = "\u{1F4AC}"; // 💬
    bubble.addEventListener("mouseenter", function () { bubble.style.transform = "scale(1.05)"; });
    bubble.addEventListener("mouseleave", function () { bubble.style.transform = "scale(1)"; });
    bubble.addEventListener("click", toggle);
    bubble.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") toggle(); });

    dot = h("span", null, {
      position: "absolute", top: "6px", right: "6px",
      width: "12px", height: "12px", borderRadius: "50%",
      background: "#ef4444", border: "2px solid #fff", display: "none",
    });
    bubble.appendChild(dot);
    document.body.appendChild(bubble);
  }

  function mountPanel() {
    if (panel) return;
    panel = h("div", null, {
      position: "fixed", right: "20px", bottom: "88px", zIndex: "2147483000",
      width: "380px", maxWidth: "calc(100vw - 24px)",
      height: "560px", maxHeight: "calc(100vh - 108px)",
      background: "#fff", borderRadius: "14px", overflow: "hidden",
      boxShadow: "0 20px 60px rgba(0,0,0,0.22)",
      display: "none",
    });

    // Mobile: fullscreen. Match by viewport width at mount time — the browser will just
    // re-evaluate on resize because we re-apply these on open().
    iframe = h("iframe", {
      src: FRAME_ORIGIN + "/widget/frame/?key=" + encodeURIComponent(PUBLIC_KEY),
      title: "Support chat",
      allow: "clipboard-write",
    }, {
      width: "100%", height: "100%", border: "0", display: "block", background: "#fff",
    });
    panel.appendChild(iframe);
    document.body.appendChild(panel);
    applyResponsiveLayout();
  }

  function applyResponsiveLayout() {
    if (!panel) return;
    var isMobile = window.innerWidth < 480;
    if (isMobile) {
      panel.style.right = "0"; panel.style.bottom = "0";
      panel.style.width = "100vw"; panel.style.height = "100vh";
      panel.style.maxWidth = "100vw"; panel.style.maxHeight = "100vh";
      panel.style.borderRadius = "0";
    } else {
      panel.style.right = "20px"; panel.style.bottom = "88px";
      panel.style.width = "380px"; panel.style.height = "560px";
      panel.style.maxWidth = "calc(100vw - 24px)";
      panel.style.maxHeight = "calc(100vh - 108px)";
      panel.style.borderRadius = "14px";
    }
  }

  function toggle() { open ? hide() : show(); }

  function show() {
    mountPanel();
    applyResponsiveLayout();
    panel.style.display = "block";
    open = true;
    unread = 0; renderDot();
    // Tell the frame it's visible so it can reset its unread counter.
    try { iframe.contentWindow.postMessage({ type: "widget:opened" }, FRAME_ORIGIN); } catch (e) {}
  }

  function hide() {
    if (panel) panel.style.display = "none";
    open = false;
  }

  function renderDot() {
    if (!dot) return;
    dot.style.display = unread > 0 ? "block" : "none";
  }

  window.addEventListener("message", function (ev) {
    // Origin lock: only trust messages from our iframe.
    if (ev.origin !== FRAME_ORIGIN) return;
    var d = ev.data || {};
    if (d.type === "widget:close") hide();
    else if (d.type === "widget:unread_changed") {
      unread = Math.max(0, parseInt(d.count, 10) || 0);
      renderDot();
    }
  });

  window.addEventListener("resize", applyResponsiveLayout);

  // Fetch config for brand color BEFORE mounting the bubble so it appears on-brand.
  // Falls back to the default color if config is unreachable — the bubble still shows.
  function fetchConfig() {
    var url = FRAME_ORIGIN + "/api/widget/config?key=" + encodeURIComponent(PUBLIC_KEY);
    return fetch(url).then(function (r) { return r.ok ? r.json() : null; }).catch(function () { return null; });
  }

  function boot() {
    fetchConfig().then(function (cfg) {
      mountBubble(cfg && cfg.brand_color);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
