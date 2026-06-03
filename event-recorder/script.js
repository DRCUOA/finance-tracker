(() => {
  "use strict";

  const canvas = document.getElementById("stage");
  const ctx = canvas.getContext("2d");
  const logEl = document.getElementById("log");
  const counterEl = document.getElementById("counter");
  const autoscrollToggle = document.getElementById("autoscrollToggle");
  const clearLogBtn = document.getElementById("clearLogBtn");
  const clearCanvasBtn = document.getElementById("clearCanvasBtn");
  const filtersEl = document.getElementById("filters");

  const MAX_LOG_ITEMS = 500;
  let eventCount = 0;
  const enabledGroups = new Set(["mouse", "keyboard", "wheel", "pointer", "touch", "focus"]);
  const keyDownTimes = new Map();

  function resizeCanvas() {
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const prev = ctx.getImageData(0, 0, canvas.width || 1, canvas.height || 1);
    canvas.width = Math.max(1, Math.floor(rect.width * dpr));
    canvas.height = Math.max(1, Math.floor(rect.height * dpr));
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    try {
      ctx.putImageData(prev, 0, 0);
    } catch (_) {
      // ignore re-paint failures on first sizing
    }
  }

  function getCanvasPos(e) {
    const rect = canvas.getBoundingClientRect();
    return {
      x: Math.round(e.clientX - rect.left),
      y: Math.round(e.clientY - rect.top),
    };
  }

  function drawDot(x, y, color = "#6aa9ff", radius = 3) {
    ctx.beginPath();
    ctx.fillStyle = color;
    ctx.arc(x, y, radius, 0, Math.PI * 2);
    ctx.fill();
  }

  function drawLine(x1, y1, x2, y2, color = "#7ee787") {
    ctx.beginPath();
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.moveTo(x1, y1);
    ctx.lineTo(x2, y2);
    ctx.stroke();
  }

  function fmtTime(d = new Date()) {
    const pad = (n, w = 2) => String(n).padStart(w, "0");
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(
      d.getMilliseconds(),
      3
    )}`;
  }

  function formatDetails(obj) {
    return Object.entries(obj)
      .filter(([, v]) => v !== undefined && v !== null && v !== "")
      .map(([k, v]) => `<span class="k">${k}:</span> ${v}`)
      .join("  ");
  }

  function logEvent(group, name, details) {
    if (!enabledGroups.has(group)) return;

    const li = document.createElement("li");
    li.className = "log-item";
    li.innerHTML = `
      <span class="log-time">${fmtTime()}</span>
      <span class="log-name ${group}">${name}</span>
      <span class="log-details">${formatDetails(details)}</span>
    `;
    logEl.appendChild(li);

    while (logEl.childElementCount > MAX_LOG_ITEMS) {
      logEl.removeChild(logEl.firstElementChild);
    }

    eventCount += 1;
    counterEl.textContent = `${eventCount} event${eventCount === 1 ? "" : "s"}`;

    if (autoscrollToggle.checked) {
      logEl.scrollTop = logEl.scrollHeight;
    }
  }

  function clearCanvas() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
  }

  function clearLog() {
    logEl.innerHTML = "";
    eventCount = 0;
    counterEl.textContent = "0 events";
  }

  let lastMove = null;
  let isPointerDown = false;

  canvas.addEventListener("mousedown", (e) => {
    const { x, y } = getCanvasPos(e);
    isPointerDown = true;
    drawDot(x, y, "#6aa9ff", 5);
    logEvent("mouse", "mousedown", { button: e.button, x, y });
  });

  canvas.addEventListener("mouseup", (e) => {
    const { x, y } = getCanvasPos(e);
    isPointerDown = false;
    lastMove = null;
    logEvent("mouse", "mouseup", { button: e.button, x, y });
  });

  canvas.addEventListener("click", (e) => {
    const { x, y } = getCanvasPos(e);
    logEvent("mouse", "click", { button: e.button, x, y });
  });

  canvas.addEventListener("dblclick", (e) => {
    const { x, y } = getCanvasPos(e);
    drawDot(x, y, "#ffd166", 7);
    logEvent("mouse", "dblclick", { x, y });
  });

  canvas.addEventListener("contextmenu", (e) => {
    e.preventDefault();
    const { x, y } = getCanvasPos(e);
    logEvent("mouse", "contextmenu", { x, y });
  });

  canvas.addEventListener("mousemove", (e) => {
    const { x, y } = getCanvasPos(e);
    if (isPointerDown) {
      if (lastMove) drawLine(lastMove.x, lastMove.y, x, y, "#7ee787");
      lastMove = { x, y };
    }
    logEvent("mouse", "mousemove", {
      x,
      y,
      dx: e.movementX,
      dy: e.movementY,
      buttons: e.buttons,
    });
  });

  canvas.addEventListener("mouseenter", (e) => {
    const { x, y } = getCanvasPos(e);
    logEvent("mouse", "mouseenter", { x, y });
  });

  canvas.addEventListener("mouseleave", (e) => {
    const { x, y } = getCanvasPos(e);
    isPointerDown = false;
    lastMove = null;
    logEvent("mouse", "mouseleave", { x, y });
  });

  canvas.addEventListener("wheel", (e) => {
    e.preventDefault();
    const { x, y } = getCanvasPos(e);
    logEvent("wheel", "wheel", {
      x,
      y,
      deltaX: e.deltaX.toFixed(2),
      deltaY: e.deltaY.toFixed(2),
      mode: ["pixel", "line", "page"][e.deltaMode] || e.deltaMode,
    });
  }, { passive: false });

  canvas.addEventListener("keydown", (e) => {
    if (e.repeat) {
      logEvent("keyboard", "keydown (repeat)", {
        key: JSON.stringify(e.key),
        code: e.code,
      });
      return;
    }
    keyDownTimes.set(e.code, performance.now());
    logEvent("keyboard", "keydown", {
      key: JSON.stringify(e.key),
      code: e.code,
      ctrl: e.ctrlKey || undefined,
      shift: e.shiftKey || undefined,
      alt: e.altKey || undefined,
      meta: e.metaKey || undefined,
    });
  });

  canvas.addEventListener("keyup", (e) => {
    const start = keyDownTimes.get(e.code);
    const duration = start ? `${Math.round(performance.now() - start)}ms` : undefined;
    keyDownTimes.delete(e.code);
    logEvent("keyboard", "keyup", {
      key: JSON.stringify(e.key),
      code: e.code,
      duration,
    });
  });

  canvas.addEventListener("focus", () => logEvent("focus", "focus", {}));
  canvas.addEventListener("blur", () => {
    keyDownTimes.clear();
    logEvent("focus", "blur", {});
  });

  canvas.addEventListener("pointerdown", (e) => {
    const { x, y } = getCanvasPos(e);
    logEvent("pointer", "pointerdown", {
      type: e.pointerType,
      id: e.pointerId,
      x,
      y,
      pressure: e.pressure.toFixed(2),
    });
  });

  canvas.addEventListener("pointerup", (e) => {
    const { x, y } = getCanvasPos(e);
    logEvent("pointer", "pointerup", {
      type: e.pointerType,
      id: e.pointerId,
      x,
      y,
    });
  });

  canvas.addEventListener("touchstart", (e) => {
    e.preventDefault();
    const t = e.changedTouches[0];
    const { x, y } = getCanvasPos(t);
    logEvent("touch", "touchstart", {
      touches: e.touches.length,
      x,
      y,
    });
  }, { passive: false });

  canvas.addEventListener("touchmove", (e) => {
    e.preventDefault();
    const t = e.changedTouches[0];
    const { x, y } = getCanvasPos(t);
    logEvent("touch", "touchmove", {
      touches: e.touches.length,
      x,
      y,
    });
  }, { passive: false });

  canvas.addEventListener("touchend", (e) => {
    const t = e.changedTouches[0];
    const { x, y } = getCanvasPos(t);
    logEvent("touch", "touchend", {
      remaining: e.touches.length,
      x,
      y,
    });
  });

  filtersEl.addEventListener("change", (e) => {
    const target = e.target;
    if (!(target instanceof HTMLInputElement)) return;
    const group = target.dataset.group;
    if (!group) return;
    if (target.checked) enabledGroups.add(group);
    else enabledGroups.delete(group);
  });

  clearLogBtn.addEventListener("click", clearLog);
  clearCanvasBtn.addEventListener("click", clearCanvas);

  window.addEventListener("resize", resizeCanvas);
  resizeCanvas();
  canvas.focus();
})();
