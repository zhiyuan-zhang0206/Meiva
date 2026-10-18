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

  function start() {
    var cfg = window.__MEIVA_APP__;
    if (!cfg || !cfg.notifications || !cfg.gatewayUrl) return false;
    window.__MEIVA_APP_NOTIFY__ = true;

    var sseOpen = false;
    var seen = [];
    var seenSet = Object.create(null);

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

    function remember(id) {
      if (seenSet[id]) return false;
      seenSet[id] = true;
      seen.push(id);
      if (seen.length > SEEN_LIMIT) delete seenSet[seen.shift()];
      return true;
    }

    function onNotification(event) {
      if (typeof event.notification_id !== "number") return;
      if (!remember(event.notification_id)) return;
      var body = typeof event.subject === "string" ? event.subject : "";
      if (!body) return;
      notify("Meiva", body);
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
      if (!document.hidden) consumeDeeplink();
    });
    return true;
  }

  if (start()) return;
  window.addEventListener("meiva-app-config", function onConfig() {
    window.removeEventListener("meiva-app-config", onConfig);
    start();
  });
})();
