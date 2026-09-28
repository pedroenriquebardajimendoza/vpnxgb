// Gráficas sin librerías: velocidad en vivo (líneas) y consumo por día (barras apiladas).
// Colores por rol: --series-1 = bajada, --series-2 = subida (definidos en style.css).
(function () {
  const NS = "http://www.w3.org/2000/svg";

  function el(tag, attrs, parent) {
    const n = document.createElementNS(NS, tag);
    for (const k in attrs) n.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(n);
    return n;
  }

  function fmtBytes(n) {
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    n = Number(n) || 0;
    while (Math.abs(n) >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i >= 3 ? n.toFixed(2) : n.toFixed(0)) + " " + u[i];
  }

  function fmtRate(bytesPerSec) {
    const bits = (Number(bytesPerSec) || 0) * 8;
    if (bits >= 1e6) return (bits / 1e6).toFixed(bits >= 1e7 ? 1 : 2) + " Mbps";
    if (bits >= 1e3) return (bits / 1e3).toFixed(0) + " kbps";
    return bits.toFixed(0) + " bps";
  }

  // Escala "bonita" para el eje Y: 1, 2, 5 × 10^n
  function niceMax(v) {
    if (v <= 0) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    for (const m of [1, 2, 4, 5, 10]) if (v <= m * p) return m * p;
    return 10 * p;
  }

  // Cuántas divisiones usar para que las marcas sean números redondos.
  function ticksFor(max) {
    const m = Math.round(max / Math.pow(10, Math.floor(Math.log10(max))));
    return m === 5 ? 5 : 4;
  }

  function tooltip(host) {
    let t = host.querySelector(".viz-tip");
    if (!t) { t = document.createElement("div"); t.className = "viz-tip"; host.appendChild(t); }
    return t;
  }

  function placeTip(tip, host, x, y) {
    const w = tip.offsetWidth, hw = host.clientWidth;
    let left = x + 12;
    if (left + w > hw) left = x - w - 12;
    tip.style.left = Math.max(0, left) + "px";
    tip.style.top = Math.max(0, y - 10) + "px";
  }

  // ---------------------------------------------------------------- líneas en vivo
  // points: [{t: "HH:MM:SS", down: bytes/s, up: bytes/s}]
  function liveChart(host, points) {
    const W = host.clientWidth || 600, H = 180;
    const m = { l: 64, r: 10, t: 10, b: 22 };
    host.querySelector("svg")?.remove();
    const svg = el("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img",
      "aria-label": "Velocidad en vivo: bajada y subida" });
    host.prepend(svg);
    const iw = W - m.l - m.r, ih = H - m.t - m.b;
    const max = niceMax(Math.max(1, ...points.map(p => Math.max(p.down, p.up))) * 1.1);
    const n = Math.max(points.length, 2);
    const X = i => m.l + (iw * i) / (n - 1);
    const Y = v => m.t + ih - (ih * v) / max;

    const nt = ticksFor(max);
    for (let k = 0; k <= nt; k++) {
      const v = (max * k) / nt, y = Y(v);
      el("line", { x1: m.l, x2: W - m.r, y1: y, y2: y, class: k ? "viz-grid" : "viz-base" }, svg);
      const lbl = el("text", { x: m.l - 6, y: y + 4, "text-anchor": "end", class: "viz-axis" }, svg);
      lbl.textContent = fmtRate(v);
    }
    if (points.length) {
      const first = el("text", { x: m.l, y: H - 4, class: "viz-axis" }, svg);
      first.textContent = points[0].t;
      const last = el("text", { x: W - m.r, y: H - 4, "text-anchor": "end", class: "viz-axis" }, svg);
      last.textContent = points[points.length - 1].t;
    }
    for (const [key, cls] of [["up", "viz-s2"], ["down", "viz-s1"]]) {
      if (points.length < 2) break;
      const d = points.map((p, i) => (i ? "L" : "M") + X(i).toFixed(1) + "," + Y(p[key]).toFixed(1)).join("");
      el("path", { d, class: "viz-line " + cls }, svg);
    }

    // Cruceta + tooltip
    const cross = el("line", { y1: m.t, y2: m.t + ih, class: "viz-cross", visibility: "hidden" }, svg);
    const dots = [el("circle", { r: 4, class: "viz-dot viz-s1f", visibility: "hidden" }, svg),
                  el("circle", { r: 4, class: "viz-dot viz-s2f", visibility: "hidden" }, svg)];
    const hit = el("rect", { x: m.l, y: m.t, width: iw, height: ih, fill: "transparent" }, svg);
    const tip = tooltip(host);
    function hide() { cross.setAttribute("visibility", "hidden"); dots.forEach(d => d.setAttribute("visibility", "hidden")); tip.style.display = "none"; }
    hit.addEventListener("pointermove", e => {
      if (!points.length) return;
      const r = svg.getBoundingClientRect();
      const i = Math.max(0, Math.min(points.length - 1, Math.round(((e.clientX - r.left - m.l) / iw) * (n - 1))));
      const p = points[i], x = X(i);
      cross.setAttribute("x1", x); cross.setAttribute("x2", x); cross.setAttribute("visibility", "visible");
      dots[0].setAttribute("cx", x); dots[0].setAttribute("cy", Y(p.down)); dots[0].setAttribute("visibility", "visible");
      dots[1].setAttribute("cx", x); dots[1].setAttribute("cy", Y(p.up)); dots[1].setAttribute("visibility", "visible");
      tip.innerHTML = `<b>${p.t}</b><div><i class="sw s1"></i>Bajada <b>${fmtRate(p.down)}</b></div>` +
                      `<div><i class="sw s2"></i>Subida <b>${fmtRate(p.up)}</b></div>`;
      tip.style.display = "block";
      placeTip(tip, host, x, Y(Math.max(p.down, p.up)));
    });
    hit.addEventListener("pointerleave", hide);
  }

  // ---------------------------------------------------------------- barras por día
  // days: [{day: "YYYY-MM-DD", down: bytes, up: bytes}]
  function dailyChart(host, days) {
    const W = host.clientWidth || 600, H = 200;
    const m = { l: 56, r: 8, t: 10, b: 22 };
    host.querySelector("svg")?.remove();
    const svg = el("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img",
      "aria-label": "Consumo por día: bajada, subida y Outline" });
    host.prepend(svg);
    const iw = W - m.l - m.r, ih = H - m.t - m.b;
    const GBy = 1024 ** 3;
    const total = d => d.down + d.up + (d.ol || 0);
    const max = niceMax(Math.max(0.001, ...days.map(d => total(d) / GBy)) * 1.05);
    const Y = v => m.t + ih - (ih * v) / max;
    const nt = ticksFor(max);
    for (let k = 0; k <= nt; k++) {
      const v = (max * k) / nt, y = Y(v);
      el("line", { x1: m.l, x2: W - m.r, y1: y, y2: y, class: k ? "viz-grid" : "viz-base" }, svg);
      const lbl = el("text", { x: m.l - 6, y: y + 4, "text-anchor": "end", class: "viz-axis" }, svg);
      lbl.textContent = (v >= 10 ? v.toFixed(0) : v >= 1 ? v.toFixed(1) : v.toFixed(2)) + " GB";
    }
    const slot = iw / days.length;
    const bw = Math.max(2, Math.min(24, slot - 3));
    const tip = tooltip(host);
    const hasOutline = days.some(d => d.ol > 0);
    days.forEach((d, i) => {
      const x = m.l + slot * i + (slot - bw) / 2;
      // Segmentos de abajo hacia arriba, con 2px de separación entre ellos.
      const segs = [[d.down, "viz-s1f"], [d.up, "viz-s2f"], [d.ol || 0, "viz-s3f"]].filter(s => s[0] > 0);
      let base = 0;
      const g = el("g", {}, svg);
      segs.forEach(([bytes, cls], k) => {
        const yBottom = Y(base / GBy), yTop = Y((base + bytes) / GBy);
        const h = Math.max(0, yBottom - yTop - (k > 0 ? 2 : 0));
        el("path", { d: roundedTop(x, yTop, bw, h, k === segs.length - 1 ? 3 : 0), class: cls }, g);
        base += bytes;
      });
      const yTop = Y(total(d) / GBy);
      const hit = el("rect", { x: m.l + slot * i, y: m.t, width: slot, height: ih, fill: "transparent" }, svg);
      hit.addEventListener("pointermove", () => {
        const [yy, mm, dd] = d.day.split("-");
        tip.innerHTML = `<b>${dd}/${mm}/${yy}</b><div><i class="sw s1"></i>Bajada <b>${fmtBytes(d.down)}</b></div>` +
                        `<div><i class="sw s2"></i>Subida <b>${fmtBytes(d.up)}</b></div>` +
                        (hasOutline ? `<div><i class="sw s3"></i>Outline <b>${fmtBytes(d.ol || 0)}</b></div>` : "") +
                        `<div class="muted">Total ${fmtBytes(total(d))}</div>`;
        tip.style.display = "block";
        placeTip(tip, host, x + bw / 2, yTop);
      });
      hit.addEventListener("pointerleave", () => { tip.style.display = "none"; });
      if (i === 0 || i === days.length - 1 || i === Math.floor(days.length / 2)) {
        const t = el("text", { x: x + bw / 2, y: H - 4, "text-anchor": "middle", class: "viz-axis" }, svg);
        t.textContent = d.day.slice(8, 10) + "/" + d.day.slice(5, 7);
      }
    });
  }

  function roundedTop(x, y, w, h, r) {
    if (h <= 0) return "";
    r = Math.min(r, w / 2, h);
    return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
  }

  window.VizCharts = { liveChart, dailyChart, fmtBytes, fmtRate };
})();
