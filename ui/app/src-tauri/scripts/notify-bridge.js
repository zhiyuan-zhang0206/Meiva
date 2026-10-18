// meiva:note
// Renders the gateway's delivered `notification` events on Android and macOS. The
// delivery decision, the category switches and deduplication live in the
// notification spine; this bridge only deduplicates by `notification_id` inside
// one webview session and turns a native tap into the Inbox deep link.
// meiva:link ui/app/docs/app.meiva.okf.md
(function () {
  if (window.top !== window) return;
  if (window.__MEIVA_APP_NOTIFY__) return;

  var SEEN_LIMIT = 200;
  var CURSOR_KEY = "meiva.notify.cursor";
  var CATCHUP_LIMIT = 10;
  var CATCHUP_DEBOUNCE_MS = 400;

  function start() {
    var cfg = window.__MEIVA_APP__;
    if (!cfg || !cfg.notifications || !cfg.gatewayUrl) return false;
    window.__MEIVA_APP_NOTIFY__ = true;

    var sseOpen = false;
    var seen = [];
    var seenSet = Object.create(null);
    var catchUpTimer = null;

    function notify(title, body) {
      window.__TAURI_INTERNALS__
        .invoke("app_notify", { title: title, body: body })
        .catch(function (error) {
          console.error("[meiva-app] notification failed", error);
        });
    }

    function consumeDeeplink() {
      if (!sseOpen || !cfg.entryUrl) return;
      var target;
      try {
        target = new URL("fleet#inbox", cfg.entryUrl);
      } catch (error) {
        console.error("[meiva-app] could not resolve Inbox deep link", error);
        return;
      }
      window.__TAURI_INTERNALS__
        .invoke("app_take_pending_click")
        .then(function (pending) {
          if (!pending) return;
          window.location.href = target.href;
        })
        .catch(function (error) {
          console.error("[meiva-app] notification click check failed", error);
        });
    }

    function loadCursor() {
      try {
        var stored = window.localStorage.getItem(CURSOR_KEY);
        var value = stored === null ? 0 : parseInt(stored, 10);
        return isNaN(value) || value < 0 ? 0 : value;
      } catch (error) {
        return 0;
      }
    }

    function saveCursor(id) {
      if (typeof id !== "number" || id <= loadCursor()) return;
      try {
        window.localStorage.setItem(CURSOR_KEY, String(id));
      } catch (error) {
        console.error("[meiva-app] notification cursor not persisted", error);
      }
    }

    function remember(id) {
      if (seenSet[id]) return false;
      seenSet[id] = true;
      seen.push(id);
      if (seen.length > SEEN_LIMIT) delete seenSet[seen.shift()];
      return true;
    }

    // meiva:note
    // A live event is shown at once but does NOT move the cursor: an event proves
    // that its own delivery exists, not that nothing before it was missed. Events
    // can arrive out of order, and an earlier delivery can have been published
    // while this webview was away. The delivery log is the only authority for the
    // cursor, so a live event asks for a catch-up and that read moves it.
    // meiva:link ui/app/docs/notifications.meiva.okf.md
    function onNotification(event) {
      if (typeof event.notification_id !== "number") return;
      if (remember(event.notification_id)) {
        var body = typeof event.subject === "string" ? event.subject : "";
        if (body) notify("Meiva", body);
      }
      scheduleCatchUp();
    }

    function scheduleCatchUp() {
      if (catchUpTimer !== null) return;
      catchUpTimer = window.setTimeout(function () {
        catchUpTimer = null;
        catchUp();
      }, CATCHUP_DEBOUNCE_MS);
    }

    // meiva:note
    // The live event rides Redis, so a webview that was offline missed it with no
    // second chance. The cursor closes that gap: it is persisted across launches
    // and a reconnect asks the gateway for the deliveries after it. It advances
    // only over rows this webview actually read, so a delivery committed between
    // the read and the next look is never skipped: `latest_id` is the table's
    // newest id, not a claim about what was processed, and advancing to it here
    // would jump the cursor over rows the client never saw. A first run (no
    // cursor yet) is the one deliberate exception: it adopts `latest_id` instead
    // of replaying history at the user.
    // meiva:link ui/app/docs/notifications.meiva.okf.md
    function catchUp() {
      var after = loadCursor();
      var url =
        cfg.gatewayUrl +
        "/api/notifications?after_id=" +
        after +
        "&limit=" +
        CATCHUP_LIMIT;
      fetch(url, { credentials: "include" })
        .then(function (response) {
          return response.ok ? response.json() : null;
        })
        .then(function (payload) {
          if (!payload) return;
          if (after === 0) {
            saveCursor(payload.latest_id);
            return;
          }
          var rows = payload.notifications || [];
          rows.forEach(function (item) {
            if (typeof item.id !== "number" || !remember(item.id)) return;
            if (typeof item.subject !== "string" || !item.subject) return;
            notify("Meiva", item.subject);
          });
          if (rows.length) saveCursor(rows[rows.length - 1].id);
        })
        .catch(function (error) {
          console.error("[meiva-app] notification catch-up failed", error);
        });
    }

    function onEvent(event) {
      if (!event || typeof event !== "object") return;
      if (event.role === "notification") onNotification(event);
    }

    function connect() {
      var source = new EventSource(cfg.gatewayUrl + "/api/system", { withCredentials: true });
      source.onopen = function () {
        sseOpen = true;
        consumeDeeplink();
        catchUp();
      };
      source.onmessage = function (message) {
        var parsed;
        try {
          parsed = JSON.parse(message.data);
        } catch (err) {
          return;
        }
        // meiva:note
        // `/api/system` sends one event per frame; tolerating the batched array
        // shape of the sibling `/api/system/all` stream costs one line.
        if (Array.isArray(parsed)) parsed.forEach(onEvent);
        else onEvent(parsed);
      };
      source.onerror = function () {
        sseOpen = false;
        // meiva:note
        // EventSource retries a dropped connection itself; only a CLOSED socket is
        // a dead end that needs a fresh one.
        if (source.readyState !== EventSource.CLOSED) return;
        source.close();
        window.setTimeout(connect, 5000);
      };
    }

    connect();
    window.addEventListener("focus", consumeDeeplink);
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) return;
      consumeDeeplink();
      catchUp();
    });
    return true;
  }

  if (start()) return;
  window.addEventListener("meiva-app-config", function onConfig() {
    window.removeEventListener("meiva-app-config", onConfig);
    start();
  });
})();
