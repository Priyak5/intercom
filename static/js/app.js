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

  window.api = { getCookie: getCookie, apiFetch: apiFetch };
})();
