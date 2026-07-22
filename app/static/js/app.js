(() => {
  const shell = document.querySelector("#app-shell");
  const toggle = document.querySelector("[data-sidebar-toggle]");
  const refreshButton = document.querySelector("[data-refresh-button]");
  const toast = document.querySelector("[data-toast]");
  const initialDashboardRevision = document.body.dataset.dashboardRevision || "";
  const drawingViewerImage = document.querySelector("[data-drawing-viewer-image]");
  const drawingViewerZoomIn = document.querySelector("[data-drawing-viewer-zoom-in]");
  const drawingViewerZoomOut = document.querySelector("[data-drawing-viewer-zoom-out]");
  const drawingViewerZoomReset = document.querySelector("[data-drawing-viewer-zoom-reset]");
  const printSubmitForms = document.querySelectorAll("[data-print-submit-form]");
  let drawingViewerZoom = 100;
  let userOperationInProgress = false;
  let reloadPending = false;

  const requestPageReload = () => {
    if (userOperationInProgress) {
      reloadPending = true;
      return;
    }
    window.location.reload();
  };

  const setSidebarState = (collapsed) => {
    if (!shell || !toggle) return;
    shell.classList.toggle("sidebar-collapsed", collapsed);
    const label = collapsed ? "サイドバーを開く" : "サイドバーを折りたたむ";
    toggle.setAttribute("aria-label", label);
    toggle.setAttribute("title", label);
    localStorage.setItem("machine-portal-sidebar", collapsed ? "collapsed" : "open");
  };

  if (toggle) {
    setSidebarState(localStorage.getItem("machine-portal-sidebar") === "collapsed");
    toggle.addEventListener("click", () => {
      setSidebarState(!shell.classList.contains("sidebar-collapsed"));
    });
  }

  const showToast = (message, isError = false) => {
    if (!toast) return;
    toast.textContent = message;
    toast.classList.toggle("toast-error", isError);
    toast.hidden = false;
    window.setTimeout(() => { toast.hidden = true; }, 4000);
  };

  const refreshDashboard = async () => {
    if (!refreshButton || refreshButton.disabled) return;
    userOperationInProgress = true;
    refreshButton.disabled = true;
    refreshButton.classList.add("is-loading");
    try {
      const response = await fetch("/api/refresh", { method: "POST" });
      if (!response.ok) throw new Error("refresh failed");
      const result = await response.json();
      if (!result.ok) {
        showToast(result.message, true);
        return;
      }
      showToast(result.message);
      window.setTimeout(requestPageReload, 500);
    } catch (_error) {
      showToast("更新できませんでした。しばらくしてから再度お試しください。", true);
    } finally {
      userOperationInProgress = false;
      refreshButton.disabled = false;
      refreshButton.classList.remove("is-loading");
      if (reloadPending) requestPageReload();
    }
  };

  if (refreshButton) {
    refreshButton.addEventListener("click", refreshDashboard);
  }

  printSubmitForms.forEach((form) => {
    form.addEventListener("submit", () => {
      userOperationInProgress = true;
      const button = form.querySelector("[data-print-submit]");
      if (!button) return;
      button.disabled = true;
      button.textContent = "印刷しています…";
    });
  });

  const refreshPrintAttention = async () => {
    try {
      const response = await fetch("/api/printing/attention", {
        headers: { "Accept": "application/json" },
        cache: "no-store",
      });
      if (!response.ok) return;
      const result = await response.json();
      const currentRequired = document.body.dataset.hasPrintAttention === "true";
      const currentCount = Number.parseInt(document.body.dataset.printAttentionCount || "0", 10);
      if (Boolean(result.required) !== currentRequired || Number(result.count) !== currentCount) {
        requestPageReload();
      }
    } catch (_error) {
      // Printing continues on the server; a temporary status-check failure needs no user alert.
    }
  };
  window.setInterval(refreshPrintAttention, 60000);

  const refreshDashboardRevision = async () => {
    try {
      const response = await fetch("/api/dashboard/revision", {
        headers: { "Accept": "application/json" },
        cache: "no-store",
      });
      if (!response.ok) return;
      const result = await response.json();
      const latestRevision = result.updated_at || "";
      if (latestRevision !== initialDashboardRevision) requestPageReload();
    } catch (_error) {
      // A later poll will detect the completed update; users need no error message.
    }
  };
  window.setInterval(refreshDashboardRevision, 10000);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refreshDashboardRevision();
  });

  const setDrawingViewerZoom = (nextZoom) => {
    if (!drawingViewerImage) return;
    drawingViewerZoom = Math.min(300, Math.max(50, nextZoom));
    drawingViewerImage.style.width = `${drawingViewerZoom}%`;
    if (drawingViewerZoomReset) drawingViewerZoomReset.textContent = `${drawingViewerZoom}%`;
  };
  drawingViewerZoomIn?.addEventListener("click", () => setDrawingViewerZoom(drawingViewerZoom + 25));
  drawingViewerZoomOut?.addEventListener("click", () => setDrawingViewerZoom(drawingViewerZoom - 25));
  drawingViewerZoomReset?.addEventListener("click", () => setDrawingViewerZoom(100));
})();
