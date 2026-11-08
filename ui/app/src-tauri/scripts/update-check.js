// meiva:note
// The injected prompt asks the native check once per session, publishes an available build on window.__MEIVA_APP__.update (notifying the console via 'meiva-app-update'), and falls back to the download page when an install fails. Each case runs the checked-in script in a Node VM with canned command results.
// meiva:link ui/app/docs/android-updates.meiva.okf.md
(function () {
  var cfg = window.__MEIVA_APP__;
  if (!cfg || cfg.platform !== "android" || !cfg.version) return;
  if (window.top !== window) return;
  if (location.protocol === "tauri:" || location.hostname === "tauri.localhost") return;
  var MARKER = "meiva-app-update-checked";
  if (!window.sessionStorage || window.sessionStorage.getItem(MARKER)) return;
  window.sessionStorage.setItem(MARKER, "1");

  function invoke(command, args) {
    var target = cfg && typeof cfg.invoke === "function" ? cfg : window.__TAURI_INTERNALS__;
    return target.invoke(command, args);
  }

  function emit(update) {
    cfg.update = update;
    if (typeof window.dispatchEvent === "function" && typeof Event === "function") {
      window.dispatchEvent(new Event("meiva-app-update"));
    }
  }

  function label(check) {
    return "Meiva " + check.latestVersionName + " (build " + check.latestVersionCode + ") is available";
  }

  function publishUpdate(check) {
    var downloadPage = check.downloadPage || null;
    function dismiss() {
      emit(null);
    }
    function openDownloadPage() {
      if (!downloadPage) return;
      invoke("app_open_external", { url: downloadPage }).catch(function (error) {
        console.error("[meiva-app] update download page failed", error);
      });
    }
    function install() {
      var current = cfg.update;
      if (!current || current.status === "installing") return;
      if (current.status === "download-fallback") {
        openDownloadPage();
        return;
      }
      emit({
        status: "installing",
        versionName: check.latestVersionName,
        versionCode: check.latestVersionCode,
        text: label(check),
        actionLabel: "Updating\u2026",
        downloadPage: downloadPage,
        install: install,
        dismiss: dismiss,
      });
      invoke("app_install_update")
        .then(function (outcome) {
          if (outcome && outcome.status !== "failed") {
            emit(null);
            return;
          }
          downloadPage = (outcome && outcome.downloadPage) || downloadPage;
          if (!downloadPage) {
            emit(null);
            return;
          }
          emit({
            status: "download-fallback",
            versionName: check.latestVersionName,
            versionCode: check.latestVersionCode,
            text: label(check),
            actionLabel: "Open download page",
            downloadPage: downloadPage,
            install: install,
            dismiss: dismiss,
          });
        })
        .catch(function (error) {
          console.error("[meiva-app] update install failed", error);
          emit(null);
        });
    }

    emit({
      status: "available",
      versionName: check.latestVersionName,
      versionCode: check.latestVersionCode,
      text: label(check),
      actionLabel: "Update",
      downloadPage: downloadPage,
      install: install,
      dismiss: dismiss,
    });
  }

  invoke("app_check_update")
    .then(function (check) {
      if (!check || check.status !== "available") return;
      if (cfg.notifications) {
        invoke("app_notify", {
          title: label(check),
          body: "Tap Update in the app to install it.",
        }).catch(function (error) {
          console.error("[meiva-app] update notification failed", error);
        });
      }
      publishUpdate(check);
    })
    .catch(function (error) {
      console.error("[meiva-app] update check failed", error);
    });
})();
