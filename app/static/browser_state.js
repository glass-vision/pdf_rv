(function () {
  const CLIENT_ID_KEY = "pdf-rv:client-id";
  const memory = new Map();

  function makeMemoryStorage() {
    return {
      getItem(key) {
        return memory.has(key) ? memory.get(key) : null;
      },
      setItem(key, value) {
        memory.set(key, String(value));
      },
      removeItem(key) {
        memory.delete(key);
      },
    };
  }

  function getSessionStorage() {
    try {
      const probeKey = "__pdf_rv_probe__";
      window.sessionStorage.setItem(probeKey, "1");
      window.sessionStorage.removeItem(probeKey);
      return window.sessionStorage;
    } catch {
      return makeMemoryStorage();
    }
  }

  function getIdentityStorage() {
    try {
      const probeKey = "__pdf_rv_probe__";
      window.localStorage.setItem(probeKey, "1");
      window.localStorage.removeItem(probeKey);
      return window.localStorage;
    } catch {
      return makeMemoryStorage();
    }
  }

  const identityStorage = getIdentityStorage();
  const stateStorage = getSessionStorage();

  function getClientId() {
    let clientId = identityStorage.getItem(CLIENT_ID_KEY);
    if (!clientId) {
      clientId = (window.crypto && window.crypto.randomUUID)
        ? window.crypto.randomUUID()
        : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
      identityStorage.setItem(CLIENT_ID_KEY, clientId);
    }
    return clientId;
  }

  function key(scope, suffix = "") {
    return suffix
      ? `pdf-rv:${getClientId()}:${scope}:${suffix}`
      : `pdf-rv:${getClientId()}:${scope}`;
  }

  function getJobClientId(job) {
    return String(job?.client_id || job?.provenance?.client_id || "").trim();
  }

  function getJobLabel(job, fallback = "Unknown file") {
    return String(
      job?.filename ||
      job?.provenance?.source_file_name ||
      job?.name ||
      fallback
    ).trim() || fallback;
  }

  const STATUS_PREFIX = {
    ready: "พร้อมนำเข้า / Ready",
    preparing: "กำลังเตรียมไฟล์ / Preparing file",
    processing: "กำลังทำงาน / Processing",
    done: "สำเร็จ / Done",
    failed: "ไม่สำเร็จ / Failed",
    cancelled: "ยกเลิกแล้ว / Cancelled",
    unknown: "ไม่ทราบสถานะ / Unknown",
  };

  function getStatusPrefix(state) {
    return STATUS_PREFIX[state] || STATUS_PREFIX.unknown;
  }

  function formatLabeledStatus(state, label, detail = "") {
    const suffix = detail ? `${detail}` : "";
    return `${getStatusPrefix(state)} — ${label}${suffix}`;
  }

  function isForeignJob(job) {
    const jobClientId = getJobClientId(job);
    return Boolean(jobClientId) && jobClientId !== getClientId();
  }

  function createOriginBadge(job, label = "เบราว์เซอร์อื่น / Other browser") {
    if (!isForeignJob(job)) return null;
    const badge = document.createElement("span");
    badge.className = "job-origin-badge";
    badge.textContent = label;
    return badge;
  }

  function createPdfLink(url, label = "เปิด PDF") {
    const link = document.createElement("a");
    link.href = `${url}#page=1&zoom=100`;
    link.target = "_blank";
    link.rel = "noopener";
    link.className = "pdf-action-link view-btn";
    link.textContent = label;
    return link;
  }

  function createUploadViewSync({
    getPageActive,
    getHistoryOpen,
    refreshActiveUploads,
    refreshHistory,
    intervalMs = 3000,
  }) {
    let inFlight = false;

    function sync() {
      if (!getPageActive || !getPageActive()) return;
      if (document.visibilityState === "hidden") return;
      if (inFlight) return;

      inFlight = true;
      Promise.resolve(refreshActiveUploads())
        .then(() => {
          if (getHistoryOpen && getHistoryOpen() && refreshHistory) {
            return refreshHistory();
          }
          return undefined;
        })
        .finally(() => {
          inFlight = false;
        });
    }

    document.addEventListener("visibilitychange", sync);
    const timerId = window.setInterval(sync, intervalMs);

    return {
      sync,
      stop() {
        document.removeEventListener("visibilitychange", sync);
        window.clearInterval(timerId);
      },
    };
  }

  window.PdfRvState = {
    storage: stateStorage,
    identityStorage,
    stateStorage,
    getClientId,
    key,
    getJobClientId,
    getJobLabel,
    getStatusPrefix,
    formatLabeledStatus,
    isForeignJob,
    createOriginBadge,
    createPdfLink,
    createUploadViewSync,
  };
})();
