// Agent inbox: subscribes to socket.js events, renders the list + thread, and sends over
// REST with optimistic UI. Rendering is deduped by client_msg_id so the optimistic bubble,
// the POST-response ack, and the WS echo collapse to one message in any order.
(function () {
  var convListEl = document.getElementById("conv-list");
  var threadEl = document.getElementById("thread");
  var headerEl = document.getElementById("thread-header");
  var composer = document.getElementById("composer");
  var input = document.getElementById("composer-input");
  var typingEl = document.getElementById("typing-indicator");
  var banner = document.getElementById("offline-banner");
  if (!convListEl) return;

  var api = window.api;
  var currentConv = null;
  var byClient = {};
  var typingTimer = null;
  var lastTypingSent = 0;

  var socket = window.createSocket({
    wsUrl: "/ws/agent/",
    mode: "agent",
    onStatus: function (state) {
      banner.style.display = state === "offline" ? "block" : "none";
    },
    backfill: function (afterSeq) {
      if (!currentConv) return Promise.resolve({ results: [] });
      return api
        .apiFetch("/api/conversations/" + currentConv + "/messages?after_seq=" + afterSeq, "GET")
        .then(function (r) { return r.data || { results: [] }; });
    },
    onEvent: onEvent,
  });
  socket.connect();
  loadConversations();

  function onEvent(type, data) {
    if (type === "message.created") renderMessage(data);
    else if (type === "conversation.updated") loadConversations();
    else if (type === "typing") showTyping(data);
  }

  function loadConversations() {
    api.apiFetch("/api/conversations", "GET").then(function (r) {
      if (!r.ok || !r.data) return;
      convListEl.innerHTML = "";
      (r.data.results || []).forEach(function (c) {
        var el = document.createElement("div");
        el.className = "conv-item" + (c.id === currentConv ? " active" : "");
        el.innerHTML =
          '<div class="conv-name">' + api.escapeHtml(c.contact_name) + "</div>" +
          '<div class="conv-meta">' + api.escapeHtml(c.channel) +
          (c.unread ? ' · <span class="unread">' + c.unread + "</span>" : "") + "</div>";
        el.onclick = function () { openConversation(c.id, c.contact_name); };
        convListEl.appendChild(el);
      });
    });
  }

  function openConversation(id, name) {
    currentConv = id;
    byClient = {};
    threadEl.innerHTML = "";
    headerEl.textContent = name || "Conversation";
    composer.style.display = "flex";
    socket.subscribe(id); // resets seq tracking + backfills history from 0
    loadConversations();
  }

  function renderMessage(data) {
    var cid = data.client_msg_id;
    var existing = cid && byClient[cid];
    if (existing) {
      existing.classList.remove("pending");
      existing.classList.add("confirmed");
      existing.dataset.seq = data.seq || "";
      return;
    }
    var el = document.createElement("div");
    el.className = "msg msg-" + data.sender_type + " confirmed";
    el.dataset.seq = data.seq || "";
    el.innerHTML = '<span class="msg-body">' + api.escapeHtml(data.body_text) + "</span>";
    threadEl.appendChild(el);
    if (cid) byClient[cid] = el;
    threadEl.scrollTop = threadEl.scrollHeight;
    if (data.seq) socket.sendControl({ action: "read", seq: data.seq });
  }

  composer.addEventListener("submit", function (e) {
    e.preventDefault();
    var body = input.value.trim();
    if (!body || !currentConv) return;
    var cid = api.uuid();
    var el = document.createElement("div");
    el.className = "msg msg-agent pending";
    el.dataset.cid = cid;
    el.innerHTML = '<span class="msg-body">' + api.escapeHtml(body) + "</span>";
    threadEl.appendChild(el);
    byClient[cid] = el;
    threadEl.scrollTop = threadEl.scrollHeight;
    input.value = "";
    api
      .apiFetch("/api/conversations/" + currentConv + "/messages", "POST", {
        client_msg_id: cid,
        body_text: body,
      })
      .then(function (r) {
        if (r.ok && r.data) {
          el.classList.remove("pending");
          el.classList.add("confirmed");
          el.dataset.seq = r.data.seq;
        } else {
          el.classList.add("failed");
        }
      })
      .catch(function () { el.classList.add("failed"); });
  });

  input.addEventListener("input", function () {
    var now = Date.now();
    if (now - lastTypingSent > 1500) {
      socket.sendControl({ action: "typing" });
      lastTypingSent = now;
    }
  });

  function showTyping(data) {
    if (!data || !data.is_typing) { typingEl.textContent = ""; return; }
    typingEl.textContent = (data.name || "Someone") + " is typing…";
    if (typingTimer) clearTimeout(typingTimer);
    typingTimer = setTimeout(function () { typingEl.textContent = ""; }, 3000);
  }
})();
