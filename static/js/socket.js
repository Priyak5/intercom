// socket.js — the ONLY module that touches the WebSocket. It owns connection lifecycle,
// reconnection, heartbeat, and per-conversation seq gap-detection + backfill (architecture
// §5 R1/R2). Pages subscribe via opts.onEvent and never open a socket themselves.
//
// Division of labour: socket.js tracks lastSeen by seq (gap detection / backfill). The
// page dedups RENDERING by client_msg_id, so an optimistic bubble, its POST-response ack,
// and its WS echo all collapse to one message regardless of arrival order.
(function () {
  function fullJitter(base) {
    return Math.random() * base;
  }

  function createSocket(opts) {
    // opts: { wsUrl, mode: "agent"|"widget", onEvent(type,data,envelope),
    //         onStatus(state), backfill(afterSeq) -> Promise<{results,last_seq}> }
    var ws = null;
    var attempt = 0;
    var failCount = 0;
    var heartbeat = null;
    var currentConv = null;
    var lastSeen = 0;
    var buffer = {};
    var backfillTimer = null;
    var manualClose = false;

    function setStatus(s) {
      if (opts.onStatus) opts.onStatus(s);
    }

    function connect() {
      manualClose = false;
      setStatus(attempt === 0 ? "connecting" : "reconnecting");
      var proto = location.protocol === "https:" ? "wss:" : "ws:";
      ws = new WebSocket(proto + "//" + location.host + opts.wsUrl);
      ws.onopen = onOpen;
      ws.onmessage = onMessage;
      ws.onclose = onClose;
      ws.onerror = function () {};
    }

    function onOpen() {
      attempt = 0;
      failCount = 0;
      setStatus("online");
      startHeartbeat();
      if (opts.mode === "agent" && currentConv) {
        ws.send(JSON.stringify({ action: "subscribe", conversation_id: currentConv }));
      }
      if (currentConv) doBackfill(); // heal anything missed while disconnected
    }

    function onClose() {
      stopHeartbeat();
      if (manualClose) return;
      failCount++;
      if (failCount >= 2) setStatus("offline");
      attempt++;
      var base = Math.min(30000, 1000 * Math.pow(2, attempt));
      setTimeout(connect, fullJitter(base));
    }

    function onMessage(ev) {
      var msg;
      try {
        msg = JSON.parse(ev.data);
      } catch (e) {
        return;
      }
      var type = msg.type;
      if (type === "pong") return;
      if (type === "subscribed" || type === "error") {
        if (opts.onEvent) opts.onEvent(type, msg.data, msg);
        return;
      }
      if (type === "message.created" && String(msg.conversation_id) === String(currentConv)) {
        handleSeqEvent(msg);
      } else if (opts.onEvent) {
        opts.onEvent(type, msg.data, msg); // typing/presence/read/conversation.updated
      }
    }

    function handleSeqEvent(msg) {
      var seq = msg.seq;
      if (seq <= lastSeen) return; // duplicate (incl. our own echo)
      if (seq === lastSeen + 1) {
        apply(msg.data);
        lastSeen = seq;
        drainBuffer();
      } else {
        buffer[seq] = msg; // gap → buffer + debounce a backfill
        scheduleBackfill();
      }
    }

    function drainBuffer() {
      while (buffer[lastSeen + 1]) {
        var m = buffer[lastSeen + 1];
        delete buffer[lastSeen + 1];
        apply(m.data);
        lastSeen += 1;
      }
    }

    function apply(data) {
      if (opts.onEvent) opts.onEvent("message.created", data, null);
    }

    function scheduleBackfill() {
      if (backfillTimer) return;
      backfillTimer = setTimeout(function () {
        backfillTimer = null;
        doBackfill();
      }, 500);
    }

    function doBackfill() {
      if (!opts.backfill || !currentConv) return;
      var conv = currentConv;
      opts
        .backfill(lastSeen)
        .then(function (data) {
          if (String(conv) !== String(currentConv)) return; // switched away
          (data.results || []).forEach(function (m) {
            if (m.seq > lastSeen) {
              apply(m);
              lastSeen = m.seq;
            }
          });
          drainBuffer();
        })
        .catch(function () {}); // retried on next event / reconnect
    }

    function startHeartbeat() {
      stopHeartbeat();
      heartbeat = setInterval(function () {
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ action: "ping" }));
        }
      }, 20000);
    }

    function stopHeartbeat() {
      if (heartbeat) {
        clearInterval(heartbeat);
        heartbeat = null;
      }
    }

    return {
      connect: connect,
      // Agent: switch to a conversation (resets seq tracking, subscribes, loads history).
      subscribe: function (convId) {
        currentConv = convId;
        lastSeen = 0;
        buffer = {};
        if (ws && ws.readyState === WebSocket.OPEN) {
          if (opts.mode === "agent") {
            ws.send(JSON.stringify({ action: "subscribe", conversation_id: convId }));
          }
          doBackfill();
        }
      },
      // Widget: fix the (single) conversation before connect().
      setConversation: function (convId) {
        currentConv = convId;
        lastSeen = 0;
        buffer = {};
      },
      sendControl: function (obj) {
        if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
      },
      lastSeen: function () {
        return lastSeen;
      },
      close: function () {
        manualClose = true;
        stopHeartbeat();
        if (ws) ws.close();
      },
    };
  }

  window.createSocket = createSocket;
})();
