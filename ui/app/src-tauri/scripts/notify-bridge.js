// meiva:note
// Renders the gateway's delivered `notification` events on Android and macOS. The
// delivery decision and the category switches live in the notification spine; this bridge
// deduplicates by `notification_id` inside one webview session, folds a catch-up gap down to
// the final state of each notification `tag`, keeps an agent the user is looking at quiet, and
// turns a native tap into a deep link to the agent.
// meiva:link ui/app/docs/app.md
(function () {
  if (window.top !== window) return;
  if (location.protocol === "tauri:" || location.hostname === "tauri.localhost") return;
  if (window.__MEIVA_APP_NOTIFY__) return;

  var SEEN_LIMIT = 200;
  var CURSOR_KEY = "meiva.notify.cursor";
  var TARGET_KEY = "meiva.notify.target";
  var CATCHUP_LIMIT = 100;
  var UNREAD_CATEGORY = "core.unread";
  var CATCHUP_DEBOUNCE_MS = 400;

  function start() {
    var cfg = window.__MEIVA_APP__;
    if (!cfg || !cfg.notifications || !cfg.gatewayUrl) return false;
    window.__MEIVA_APP_NOTIFY__ = true;

    var sseOpen = false;
    var seen = [];
    var seenSet = Object.create(null);
    var raised = Object.create(null);
    var catchUpTimer = null;

    function invoke(command, args) {
      var target = cfg && typeof cfg.invoke === "function" ? cfg : window.__TAURI_INTERNALS__;
      return target.invoke(command, args);
    }

    function notify(mode, tag, title, body) {
      invoke("app_notify", { mode: mode, tag: tag, title: title, body: body }).catch(function (error) {
        console.error("[meiva-app] notification failed", error);
      });
    }

    // meiva:note
    // The user is looking at an agent when the window is visible and focused and the console has
    // that agent open (`?agent_id=` on the console route, the state the sidebar's reading marker
    // follows). The server cannot know this, so the unread notification for that agent is
    // dropped here; the cursor advance the console sends while it is open retracts it anyway.
    // meiva:link ui/app/docs/notifications.md
    function isViewing(agentId) {
      if (document.visibilityState !== "visible" || !document.hasFocus()) return false;
      var open = new URLSearchParams(window.location.search).get("agent_id");
      return open !== null && open === String(agentId);
    }

    // meiva:note
    // One batch of new events (one live event, or the whole catch-up gap) folded to the final
    // state of each `tag`, as the operations to run, in the order the tags last changed. Only the
    // final event of a tag matters: a gap of alert, replace, replace is one alert carrying the
    // newest content, and alert, retract shows nothing but still sends the retract, so a
    // notification raised before the webview restarted is taken down. A replace is silent only
    // when this session already raised the tag; otherwise it alerts once, because the user has
    // never been told about it.
    // meiva:link ui/app/docs/notifications.md
    function foldEvents(events) {
      var finalByTag = Object.create(null);
      var order = [];
      events.forEach(function (event) {
        order = order.filter(function (tag) {
          return tag !== event.tag;
        });
        order.push(event.tag);
        finalByTag[event.tag] = event;
      });
      var ops = [];
      order.forEach(function (tag) {
        var event = finalByTag[tag];
        if (event.action === "retract") {
          ops.push({ mode: "retract", tag: tag, event: event });
          return;
        }
        if (event.category === UNREAD_CATEGORY && isViewing(event.agent_id)) return;
        var silent = event.action === "replace" && raised[tag] === true;
        ops.push({ mode: silent ? "replace" : "alert", tag: tag, event: event });
      });
      return ops;
    }

    function present(events) {
      foldEvents(events).forEach(function (op) {
        var event = op.event;
        if (op.mode === "retract") {
          delete raised[op.tag];
          notify("retract", op.tag, "", "");
          return;
        }
        if (typeof event.subject !== "string" || !event.subject) return;
        raised[op.tag] = true;
        notify(op.mode, op.tag, event.subject, typeof event.body === "string" ? event.body : "");
        rememberTarget(event.href);
      });
    }

    // meiva:note
    // A legal tap target is one same-origin PATH the delivery named (`href`, e.g.
    // `/?agent_id=7`). Everything else — a scheme, a protocol-relative
    // `//host`, a backslash the URL parser folds into a slash — would take the
    // webview off the console, and the value comes off the wire.
    // meiva:link ui/app/docs/notifications.md
    function legalTarget(href) {
      return (
        typeof href === "string" &&
        href.length > 1 &&
        href.charAt(0) === "/" &&
        href.charAt(1) !== "/" &&
        href.indexOf("\\") === -1 &&
        !/\s/.test(href)
      );
    }

    function rememberTarget(href) {
      if (!legalTarget(href)) return;
      try {
        window.localStorage.setItem(TARGET_KEY, href);
      } catch (error) {
        console.error("[meiva-app] notification target not persisted", error);
      }
    }

    function loadTarget() {
      try {
        var stored = window.localStorage.getItem(TARGET_KEY);
        return legalTarget(stored) ? stored : null;
      } catch (error) {
        return null;
      }
    }

    function consumeDeeplink() {
      if (!sseOpen || !cfg.entryUrl) return;
      var target;
      try {
        // meiva:note
        // The agent page the newest delivery named; the console root otherwise, so an
        // install whose first tap precedes any delivery still lands on the console.
        // meiva:link ui/app/docs/notifications.md
        target = new URL(loadTarget() || "/", cfg.entryUrl);
        if (target.origin !== new URL(cfg.entryUrl).origin) return;
      } catch (error) {
        console.error("[meiva-app] could not resolve the notification deep link", error);
        return;
      }
      invoke("app_take_pending_click")
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
    // meiva:link ui/app/docs/notifications.md
    function onNotification(event) {
      if (typeof event.notification_id !== "number" || !event.tag) return;
      // meiva:note
      // `present` remembers where a delivery points BEFORE the user can tap it: the tap is
      // consumed by the native side, which then only asks the bridge for a target.
      // meiva:link ui/app/docs/notifications.md
      if (remember(event.notification_id)) present([event]);
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
    // meiva:link ui/app/docs/notifications.md
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
          var fresh = [];
          rows.forEach(function (item) {
            if (typeof item.id !== "number" || !item.tag || !remember(item.id)) return;
            fresh.push(item);
          });
          present(fresh);
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
