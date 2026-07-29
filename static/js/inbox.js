// Agent inbox: subscribes to socket.js events, renders the filtered list + thread,
// and sends over REST with optimistic UI. Rendering is deduped by client_msg_id so
// the optimistic bubble, the POST-response ack, and the WS echo collapse to one
// message in any order.
//
// Phase 5 adds: filter bar (status/channel/assignee/search), thread-header action
// bar (assign / snooze / resolve), and keyboard nav on the list.
(function () {
  var convListEl = document.getElementById("conv-list");
  var threadEl = document.getElementById("thread");
  var titleEl = document.getElementById("thread-title");
  var presenceEl = document.getElementById("peer-presence");
  var actionsEl = document.getElementById("thread-actions");
  var composer = document.getElementById("composer");
  var input = document.getElementById("composer-input");
  var typingEl = document.getElementById("typing-indicator");
  var banner = document.getElementById("offline-banner");
  var searchInput = document.getElementById("search-input");
  var assigneeSelect = document.getElementById("assignee-select");
  var assignMenu = document.getElementById("assign-menu");
  var snoozeBtn = document.getElementById("snooze-btn");
  var snoozeMenu = document.getElementById("snooze-menu");
  var snoozeCustom = document.getElementById("snooze-custom");
  var resolveBtn = document.getElementById("resolve-btn");
  var summaryCard = document.getElementById("summary-card");
  var summaryBody = document.getElementById("summary-body");
  var summaryMeta = document.getElementById("summary-meta");
  var summaryRefresh = document.getElementById("summary-refresh");
  if (!convListEl) return;

  var api = window.api;
  var currentConv = null;
  var currentConvMeta = null;
  var byClient = {};
  var typingTimer = null;
  var lastTypingSent = 0;

  // Filter state — every fetch reflects this.
  var filters = { status: "open", channel: "", assignee_id: "", q: "" };
  var members = [];                 // {id, email, name}[]
  var listRows = [];                // last-rendered rows, kept in DOM order
  var kbIndex = -1;                 // keyboard-highlighted row index
  var searchDebounce = null;
  var firstLoad = true;

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
        .then(function (r) {
          var data = r.data || { results: [] };
          // Piggy-back: the messages endpoint returns the summary too; render it
          // on the open-thread fetch (afterSeq=0) so agents see it immediately.
          if (afterSeq === 0 && data.summary) renderSummary(data.summary);
          return data;
        });
    },
    onEvent: onEvent,
  });
  socket.connect();

  // --- init: filters, members, keyboard, first fetch ------------------------

  initFilterBar();
  loadMembers().then(function () { loadConversations(); });

  function onEvent(type, data) {
    if (type === "message.created") renderMessage(data);
    else if (type === "conversation.updated") loadConversations();
    else if (type === "typing") showTyping(data);
    else if (type === "presence.updated") showPresence(data);
    else if (type === "summary.ready") renderSummary(data);
  }

  function showPresence(data) {
    if (!data || String(data.actor || "").indexOf("contact:") !== 0) return;
    var label = data.online ? "online" : "offline";
    presenceEl.innerHTML = '<span class="presence-dot"></span>' + label;
    presenceEl.className = "peer-presence " + (data.online ? "online" : "offline");
  }

  // --- filter bar -----------------------------------------------------------

  function initFilterBar() {
    // Status pills.
    document.querySelectorAll(".filter-pill[data-status]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        filters.status = btn.dataset.status;
        paintFilterPills();
        loadConversations();
      });
    });
    // Channel pills.
    document.querySelectorAll(".filter-pill[data-channel]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        filters.channel = btn.dataset.channel;
        paintFilterPills();
        loadConversations();
      });
    });
    paintFilterPills();

    assigneeSelect.addEventListener("change", function () {
      filters.assignee_id = assigneeSelect.value;
      loadConversations();
    });

    searchInput.addEventListener("input", function () {
      if (searchDebounce) clearTimeout(searchDebounce);
      searchDebounce = setTimeout(function () {
        filters.q = searchInput.value.trim();
        loadConversations();
      }, 250);
    });
  }

  function paintFilterPills() {
    document.querySelectorAll(".filter-pill[data-status]").forEach(function (b) {
      b.classList.toggle("active", b.dataset.status === filters.status);
    });
    document.querySelectorAll(".filter-pill[data-channel]").forEach(function (b) {
      b.classList.toggle("active", b.dataset.channel === filters.channel);
    });
  }

  function loadMembers() {
    return api.apiFetch("/api/members", "GET").then(function (r) {
      if (!r.ok || !Array.isArray(r.data)) return;
      members = r.data;
      var frag1 = document.createDocumentFragment();
      var frag2 = document.createDocumentFragment();
      members.forEach(function (m) {
        var label = (m.name && m.name.trim()) || m.email;
        // Both filter + assign use User.id — Conversation.assignee_id is the User FK.
        var o1 = document.createElement("option");
        o1.value = m.user_id;
        o1.textContent = label;
        frag1.appendChild(o1);
        var o2 = document.createElement("option");
        o2.value = m.user_id;
        o2.textContent = label;
        frag2.appendChild(o2);
      });
      assigneeSelect.appendChild(frag1);
      assignMenu.appendChild(frag2);
    });
  }

  // --- conversation list ----------------------------------------------------

  function loadConversations() {
    if (firstLoad) {
      convListEl.innerHTML = '<div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>';
    }
    var qs = new URLSearchParams();
    if (filters.status) qs.set("status", filters.status);
    if (filters.channel) qs.set("channel", filters.channel);
    if (filters.assignee_id) qs.set("assignee_id", filters.assignee_id);
    if (filters.q) qs.set("q", filters.q);
    api.apiFetch("/api/conversations?" + qs.toString(), "GET").then(function (r) {
      firstLoad = false;
      if (!r.ok || !r.data) return;
      renderList(r.data.results || []);
    });
  }

  function renderList(rows) {
    listRows = rows;
    convListEl.innerHTML = "";
    if (rows.length === 0) {
      var empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "No conversations match";
      convListEl.appendChild(empty);
      kbIndex = -1;
      return;
    }
    rows.forEach(function (c, i) {
      var el = document.createElement("div");
      el.className = "conv-item" + (c.id === currentConv ? " active" : "") +
                     (i === kbIndex ? " kb-focus" : "") +
                     (c.status === "snoozed" ? " snoozed" : "");
      el.dataset.convId = c.id;
      var badge = '<span class="channel-badge channel-' + api.escapeHtml(c.channel) + '">' + api.escapeHtml(c.channel) + "</span>";
      var subj = c.channel === "email" && c.subject
        ? '<div class="conv-subject">' + api.escapeHtml(c.subject) + "</div>"
        : "";
      var meta = [];
      if (c.assignee_id) {
        var m = members.find(function (mm) { return mm.user_id === c.assignee_id; });
        if (m) meta.push('<span class="conv-assignee">→ ' + api.escapeHtml((m.name || m.email).split("@")[0]) + "</span>");
      }
      if (c.status === "snoozed" && c.snoozed_until) {
        meta.push('<span class="conv-snoozed">💤 ' + api.escapeHtml(shortTime(c.snoozed_until)) + "</span>");
      }
      if (c.unread) meta.push('<span class="unread">' + c.unread + "</span>");
      el.innerHTML =
        '<div class="conv-name">' + api.escapeHtml(c.contact_name) + " " + badge + "</div>" +
        subj +
        '<div class="conv-meta">' + meta.join(" · ") + "</div>";
      el.onclick = function () { openConversation(c); };
      convListEl.appendChild(el);
    });
    // Reflect selection when the list re-renders after a WS update.
    if (kbIndex >= rows.length) kbIndex = rows.length - 1;
  }

  function shortTime(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    // "Tue 3:15 PM" if today/this week, else month-day.
    var now = new Date();
    var sameDay = d.toDateString() === now.toDateString();
    if (sameDay) {
      return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    }
    return d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
  }

  // --- open + hydrate a thread ---------------------------------------------

  function openConversation(c) {
    currentConv = c.id;
    currentConvMeta = c;
    byClient = {};
    threadEl.innerHTML = "";
    // Hide any previous summary; backfill (afterSeq=0) will re-populate.
    summaryCard.hidden = true;
    summaryBody.innerHTML = "";
    summaryMeta.textContent = "";
    summaryMeta.className = "summary-meta";
    var title = c.contact_name || "Conversation";
    if (c.channel === "email") {
      var subj = c.subject ? " — " + c.subject : "";
      var email = c.contact_email ? " <" + c.contact_email + ">" : "";
      titleEl.textContent = title + email + subj;
      input.placeholder = "Reply by email…";
    } else {
      titleEl.textContent = title;
      input.placeholder = "Type a reply…";
    }
    presenceEl.textContent = "";
    presenceEl.className = "peer-presence";
    composer.style.display = "flex";
    actionsEl.style.display = "inline-flex";
    assignMenu.value = c.assignee_id || "";
    updateResolveBtn(c.status);
    socket.subscribe(c.id);
    loadConversations();
  }

  function updateResolveBtn(status) {
    if (status === "resolved") {
      resolveBtn.textContent = "Reopen";
      resolveBtn.classList.add("reopen");
    } else {
      resolveBtn.textContent = "Resolve";
      resolveBtn.classList.remove("reopen");
    }
  }

  // --- thread actions (assign / snooze / resolve) ---------------------------

  assignMenu.addEventListener("change", function () {
    if (!currentConv) return;
    var body = { assignee_id: assignMenu.value || null };
    api.apiFetch("/api/conversations/" + currentConv + "/assign", "POST", body);
    // The conversation.updated broadcast will refresh the list.
  });

  snoozeBtn.addEventListener("click", function (e) {
    e.stopPropagation();
    snoozeMenu.hidden = !snoozeMenu.hidden;
  });
  document.addEventListener("click", function (e) {
    if (!snoozeMenu.hidden && !snoozeMenu.contains(e.target) && e.target !== snoozeBtn) {
      snoozeMenu.hidden = true;
    }
  });

  snoozeMenu.addEventListener("click", function (e) {
    var btn = e.target.closest("button[data-snooze]");
    if (!btn || !currentConv) return;
    var choice = btn.dataset.snooze;
    var when = computeSnoozeUntil(choice);
    if (!when) return;
    api
      .apiFetch("/api/conversations/" + currentConv + "/status", "POST", {
        status: "snoozed",
        snoozed_until: when.toISOString(),
      })
      .then(function (r) {
        if (r.ok) snoozeMenu.hidden = true;
        else alert((r.data && r.data.detail) || "Could not snooze");
      });
  });

  function computeSnoozeUntil(choice) {
    var now = new Date();
    if (choice === "1h") return new Date(now.getTime() + 60 * 60 * 1000);
    if (choice === "4h") return new Date(now.getTime() + 4 * 60 * 60 * 1000);
    if (choice === "tomorrow") {
      var t = new Date(now); t.setDate(t.getDate() + 1); t.setHours(9, 0, 0, 0); return t;
    }
    if (choice === "monday") {
      var d = new Date(now);
      var daysUntilMon = (8 - d.getDay()) % 7; if (daysUntilMon === 0) daysUntilMon = 7;
      d.setDate(d.getDate() + daysUntilMon); d.setHours(9, 0, 0, 0); return d;
    }
    if (choice === "custom") {
      var v = snoozeCustom.value;
      if (!v) return null;
      var custom = new Date(v);
      if (isNaN(custom.getTime()) || custom <= now) { alert("Pick a future time."); return null; }
      return custom;
    }
    return null;
  }

  resolveBtn.addEventListener("click", function () {
    if (!currentConv) return;
    var target = resolveBtn.classList.contains("reopen") ? "open" : "resolved";
    api.apiFetch("/api/conversations/" + currentConv + "/status", "POST", { status: target });
  });

  // --- keyboard navigation on the list --------------------------------------

  document.addEventListener("keydown", function (e) {
    // Never intercept when the user is typing.
    var tag = (e.target.tagName || "").toLowerCase();
    var isTyping = tag === "input" || tag === "textarea" || tag === "select";

    if (e.key === "/" && !isTyping) {
      e.preventDefault();
      searchInput.focus();
      return;
    }
    if (e.key === "Escape" && isTyping) {
      e.target.blur();
      return;
    }
    if (isTyping) return;

    var isDown = e.key === "ArrowDown" || e.key === "j";
    var isUp = e.key === "ArrowUp" || e.key === "k";
    if (isDown || isUp) {
      if (!listRows.length) return;
      e.preventDefault();
      if (isDown) kbIndex = Math.min(listRows.length - 1, (kbIndex < 0 ? 0 : kbIndex + 1));
      else kbIndex = Math.max(0, (kbIndex < 0 ? 0 : kbIndex - 1));
      paintKbFocus();
      return;
    }
    if (e.key === "Enter" && kbIndex >= 0 && kbIndex < listRows.length) {
      e.preventDefault();
      openConversation(listRows[kbIndex]);
    }
  });

  function paintKbFocus() {
    var items = convListEl.querySelectorAll(".conv-item");
    items.forEach(function (el, i) { el.classList.toggle("kb-focus", i === kbIndex); });
    var focused = items[kbIndex];
    if (focused) focused.scrollIntoView({ block: "nearest" });
  }

  // --- messages -------------------------------------------------------------

  function renderMessage(data) {
    var cid = data.client_msg_id;
    var existing = cid && byClient[cid];
    if (existing) {
      existing.classList.remove("pending");
      existing.classList.add("confirmed");
      existing.dataset.seq = data.seq || "";
      applyDeliveryState(existing, data);
      return;
    }
    var el = document.createElement("div");
    el.className = "msg msg-" + data.sender_type + " confirmed";
    el.dataset.seq = data.seq || "";
    var bodyHtml = '<span class="msg-body">' + api.escapeHtml(data.body_text) + "</span>";
    el.innerHTML = bodyHtml;
    applyDeliveryState(el, data);
    threadEl.appendChild(el);
    if (cid) byClient[cid] = el;
    threadEl.scrollTop = threadEl.scrollHeight;
    if (data.seq) socket.sendControl({ action: "read", seq: data.seq });
  }

  function applyDeliveryState(el, data) {
    if (data.delivery_state === "failed" && !el.querySelector(".msg-failed")) {
      var tag = document.createElement("span");
      tag.className = "msg-failed";
      tag.textContent = " ⚠ failed to send";
      el.appendChild(tag);
    }
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
          applyDeliveryState(el, r.data);
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

  // --- summary card ---------------------------------------------------------
  //
  // Data shape (from either the /messages open fetch or a summary.ready WS event):
  //   { summary: "<json string>", upto_seq: int, generated_at: iso|null,
  //     degraded: bool, stale?: bool }
  // The `summary` field is JSON with the schema locked in prompts.py.

  function renderSummary(data) {
    if (!data || !data.summary) {
      // No summary yet — hide the card. The worker will fill it in shortly.
      summaryCard.hidden = true;
      return;
    }
    var parsed = null;
    try { parsed = JSON.parse(data.summary); } catch (e) { parsed = null; }
    if (!parsed) { summaryCard.hidden = true; return; }

    var sections = [
      { label: "What they want", value: parsed.what_they_want || "" },
      { label: "What's been tried", value: parsed.whats_been_tried || "" },
      { label: "Current status", value: parsed.current_status || "" },
    ];
    var html = "";
    for (var i = 0; i < sections.length; i++) {
      if (!sections[i].value) continue;
      html += '<div class="summary-section">' +
              '<div class="summary-label">' + api.escapeHtml(sections[i].label) + "</div>" +
              '<div class="summary-value">' + api.escapeHtml(sections[i].value) + "</div>" +
              "</div>";
    }
    if (parsed.key_details && parsed.key_details.length) {
      html += '<div class="summary-section">' +
              '<div class="summary-label">Key details</div><ul class="summary-list">';
      for (var j = 0; j < parsed.key_details.length; j++) {
        html += '<li>' + api.escapeHtml(String(parsed.key_details[j])) + '</li>';
      }
      html += '</ul></div>';
    }
    if (!html) { summaryCard.hidden = true; return; }
    summaryBody.innerHTML = html;

    // Meta / badges. Precedence: degraded > stale > fresh.
    summaryMeta.className = "summary-meta";
    var when = data.generated_at ? shortTime(data.generated_at) : "";
    if (data.degraded) {
      summaryMeta.classList.add("badge-degraded");
      summaryMeta.textContent = "Basic summary — AI unavailable" + (when ? " · " + when : "");
    } else if (data.stale) {
      summaryMeta.classList.add("badge-stale");
      summaryMeta.textContent = "Refreshing…" + (when ? " · last " + when : "");
    } else {
      summaryMeta.textContent = when ? "Generated " + when : "";
    }
    summaryCard.hidden = false;
  }

  summaryRefresh.addEventListener("click", function () {
    if (!currentConv) return;
    summaryMeta.className = "summary-meta badge-refreshing";
    summaryMeta.textContent = "Refreshing…";
    api.apiFetch("/api/conversations/" + currentConv + "/summary/refresh", "POST");
    // Worker will broadcast summary.ready when it's done.
  });
})();
