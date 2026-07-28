// Shared front-end helpers. socket/reconnect logic lands in Phase 2; for now this is
// just CSRF-aware fetch for the dashboard's JSON calls (DRF SessionAuthentication
// requires the X-CSRFToken header on unsafe methods).
(function () {
  function getCookie(name) {
    const m = document.cookie.match("(^|;)\\s*" + name + "\\s*=\\s*([^;]+)");
    return m ? decodeURIComponent(m.pop()) : "";
  }

  async function apiFetch(url, method, body) {
    const opts = { method: method, headers: { "X-CSRFToken": getCookie("csrftoken") } };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(url, opts);
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    return { ok: res.ok, status: res.status, data: data };
  }

  function uuid() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    // Fallback for non-secure contexts.
    var b = crypto.getRandomValues(new Uint8Array(16));
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    var h = [];
    for (var i = 0; i < 16; i++) h.push((b[i] + 0x100).toString(16).slice(1));
    return (
      h[0] + h[1] + h[2] + h[3] + "-" + h[4] + h[5] + "-" + h[6] + h[7] + "-" +
      h[8] + h[9] + "-" + h[10] + h[11] + h[12] + h[13] + h[14] + h[15]
    );
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  window.api = { getCookie: getCookie, apiFetch: apiFetch, uuid: uuid, escapeHtml: escapeHtml };
})();
