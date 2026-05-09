/**
 * musicConnect.js — Music Connect UX panel
 *
 * Slide-in panel showing a similar-artist bubble graph + albums/tracks for
 * whatever the SyncUDP metadata orchestrator currently has playing. The
 * active artist is sourced from /current-track; no separate player picker.
 *
 * Behaviour ported from music-connect-ux/src/components/BubbleGraph.tsx +
 * MusicNeighbourhoodPage.tsx + MediaPanel.tsx (drift, drag, fly-in,
 * pop-in, imploding-on-select).
 */

const PANEL_ID = 'music-connect-panel';
const SVG_NS = 'http://www.w3.org/2000/svg';
const POLL_MS = 5000;

const FLY_IN_SPEED = 0.16;
const EXPLOSION_SPEED = 22;

// ---------------- Module state ----------------

let panel = null;
let svgEl = null;
let titleEl = null;
let statusEl = null;
let toggleBtn = null;
let albumsListEl = null;
let tracksListEl = null;
let searchInputEl = null;
let exploreBtnEl = null;
let showLinesCheckboxEl = null;

let isOpen = false;
let isLoadingMedia = false;
let showLines = (() => {
    try {
        const v = localStorage.getItem('music-connect:show-lines');
        return v === null ? true : v === '1';
    } catch (e) { return true; }
})();

let currentArtist = '';
let lastNormalizedArtist = '';
let lastPlayerArtistKey = '';
let pollTimer = null;
let resizeObserver = null;
let panelWidth = 0;
let panelHeight = 0;
let cx = 0;
let cy = 0;
let layout = null;

let nodes = [];
let activeFit = null;
let activeScale = 1;
let activeOpacity = 1;
let activePopMs = 0;
let phase = 'idle';
let pendingNode = null;
let frameCount = 0;
let rafId = 0;
let drag = null;
let artistLoadSeq = 0;

// ---------------- Helpers ----------------

const clamp01 = (v) => Math.max(0, Math.min(1, v));

const normalize = (s) => (s || '').toLowerCase().replace(/[‘’“”'`]/g, '').replace(/\s+/g, ' ').trim();

const toPrimaryArtist = (raw) => {
    const v = (raw || '').trim();
    if (!v) return '';
    const lead = v.split(/\s+(?:feat\.?|ft\.?)\s+/i)[0]?.trim() || v;
    if (lead.includes('/')) return lead.split('/').map((x) => x.trim()).find(Boolean) || lead;
    return lead;
};

const colorForNorm = (norm) => {
    const v = clamp01(norm);
    const hue = 6 + v * 124;
    const sat = 60 + v * 12;
    const light = 46 + v * 10;
    return {
        fill: `hsl(${hue.toFixed(0)}, ${sat.toFixed(0)}%, ${light.toFixed(0)}%)`,
        stroke: `hsl(${hue.toFixed(0)}, 80%, 72%)`,
    };
};

const greedyWrap = (text, maxChars) => {
    const words = (text || '').split(/\s+/).filter(Boolean);
    if (!words.length) return [''];
    const lines = [];
    let cur = '';
    for (const w of words) {
        if (!cur) { cur = w; continue; }
        if (cur.length + 1 + w.length <= maxChars) cur += ' ' + w;
        else { lines.push(cur); cur = w; }
    }
    if (cur) lines.push(cur);
    return lines;
};

const fitText = (name, baseR, maxR, fontSizes, maxLines) => {
    for (let r = baseR; r <= maxR; r += 4) {
        const widthBudget = r * 1.7;
        const heightBudget = r * 1.55;
        for (const fs of fontSizes) {
            const charW = fs * 0.55;
            const lineH = fs * 1.18;
            const maxChars = Math.max(3, Math.floor(widthBudget / charW));
            const lines = greedyWrap(name, maxChars);
            if (lines.length > maxLines) continue;
            const longest = lines.reduce((m, l) => Math.max(m, l.length), 0);
            if (longest * charW > widthBudget) continue;
            if (lines.length * lineH > heightBudget) continue;
            return { lines, fontSize: fs, lineHeight: lineH, radius: r };
        }
    }
    const fs = fontSizes[fontSizes.length - 1];
    const lineH = fs * 1.18;
    const maxChars = Math.max(4, Math.floor((maxR * 1.7) / (fs * 0.55)));
    return { lines: greedyWrap(name, maxChars).slice(0, maxLines), fontSize: fs, lineHeight: lineH, radius: maxR };
};

const computeLayout = (w, h, n) => {
    const halfMin = Math.max(80, Math.min(w, h) / 2);
    const margin = 16;
    const padding = 12;
    const safeN = Math.max(1, n);
    const angleStep = (Math.PI * 2) / safeN;
    const sinHalf = Math.sin(angleStep / 2);
    const usable = halfMin - margin;
    let ring = Math.max(70, (usable + padding / 2) / (1 + sinHalf));
    let maxBubbleR = Math.max(22, ring * sinHalf - padding / 2);
    maxBubbleR = Math.min(maxBubbleR, halfMin * 0.22);
    const minBubbleR = Math.max(28, Math.round(maxBubbleR * 0.78));
    const activeR = Math.min(Math.round(ring * 0.42), Math.max(54, Math.round(halfMin * 0.18)));
    return { ring, maxBubbleR, minBubbleR, activeR, padding, angleStep };
};

const pickOffscreen = (w, h) => {
    const side = Math.floor(Math.random() * 4);
    const margin = 200;
    if (side === 0) return { x: -margin, y: Math.random() * h };
    if (side === 1) return { x: w + margin, y: Math.random() * h };
    if (side === 2) return { x: Math.random() * w, y: -margin };
    return { x: Math.random() * w, y: h + margin };
};

const placeTargets = (list) => {
    if (!list.length || !layout) return;
    const { ring, angleStep } = layout;
    for (let i = 0; i < list.length; i++) {
        const angle = -Math.PI / 2 + i * angleStep;
        list[i].tx = cx + Math.cos(angle) * ring;
        list[i].ty = cy + Math.sin(angle) * ring;
    }
};

const measurePanel = () => {
    if (!panel) return;
    const graph = panel.querySelector('.music-connect-graph');
    const rect = graph?.getBoundingClientRect();
    if (!rect) return;
    panelWidth = Math.max(240, Math.round(rect.width));
    panelHeight = Math.max(220, Math.round(rect.height));
    cx = panelWidth / 2;
    cy = panelHeight / 2;
};

const setStatus = (text, isError = false) => {
    if (!statusEl) return;
    statusEl.textContent = text || '';
    statusEl.classList.toggle('error', !!isError);
    statusEl.style.display = text ? 'block' : 'none';
};

// ---------------- Bubble graph rendering ----------------

const ensureSvg = () => {
    if (!svgEl) return;
    while (svgEl.firstChild) svgEl.removeChild(svgEl.firstChild);
    svgEl.setAttribute('viewBox', `0 0 ${panelWidth} ${panelHeight}`);

    const defs = document.createElementNS(SVG_NS, 'defs');
    const grad = document.createElementNS(SVG_NS, 'radialGradient');
    grad.setAttribute('id', 'mc-active-grad');
    grad.setAttribute('cx', '50%');
    grad.setAttribute('cy', '40%');
    grad.setAttribute('r', '60%');
    [
        { offset: '0%', color: '#a78bff' },
        { offset: '60%', color: '#7d8dff' },
        { offset: '100%', color: '#3f48a8' },
    ].forEach(({ offset, color }) => {
        const stop = document.createElementNS(SVG_NS, 'stop');
        stop.setAttribute('offset', offset);
        stop.setAttribute('stop-color', color);
        grad.appendChild(stop);
    });
    defs.appendChild(grad);
    svgEl.appendChild(defs);
};

const renderTextRuns = (lines, fontSize, lineHeight, className) => {
    const totalH = lines.length * lineHeight;
    const startDy = -totalH / 2 + fontSize * 0.85;
    const text = document.createElementNS(SVG_NS, 'text');
    text.setAttribute('text-anchor', 'middle');
    text.setAttribute('class', className);
    text.setAttribute('font-size', String(fontSize));
    lines.forEach((line, i) => {
        const tspan = document.createElementNS(SVG_NS, 'tspan');
        tspan.setAttribute('x', '0');
        tspan.setAttribute('dy', i === 0 ? String(startDy) : String(lineHeight));
        tspan.textContent = line;
        text.appendChild(tspan);
    });
    return text;
};

const drawFrame = () => {
    if (!svgEl) return;
    ensureSvg();

    if (showLines && nodes.length && activeFit) {
        const linesG = document.createElementNS(SVG_NS, 'g');
        nodes.forEach((n) => {
            if (n.selected) return;
            const line = document.createElementNS(SVG_NS, 'line');
            line.setAttribute('class', 'mc-connector');
            line.setAttribute('x1', String(Math.round(cx)));
            line.setAttribute('y1', String(Math.round(cy)));
            line.setAttribute('x2', String(Math.round(n.x)));
            line.setAttribute('y2', String(Math.round(n.y)));
            line.setAttribute('stroke', n.stroke);
            line.setAttribute('stroke-opacity', String((n.opacity || 0) * 0.35));
            line.setAttribute('stroke-width', '1.2');
            linesG.appendChild(line);
        });
        svgEl.appendChild(linesG);
    }

    if (activeFit) {
        const g = document.createElementNS(SVG_NS, 'g');
        g.setAttribute('transform', `translate(${Math.round(cx)},${Math.round(cy)}) scale(${activeScale.toFixed(3)})`);
        g.setAttribute('opacity', String(activeOpacity));
        const c = document.createElementNS(SVG_NS, 'circle');
        c.setAttribute('class', 'mc-active-bubble');
        c.setAttribute('r', String(activeFit.radius));
        g.appendChild(c);
        g.appendChild(renderTextRuns(activeFit.lines, activeFit.fontSize, activeFit.lineHeight, 'mc-active-label'));
        svgEl.appendChild(g);
    }

    nodes.forEach((n) => {
        const g = document.createElementNS(SVG_NS, 'g');
        g.setAttribute('class', 'mc-bubble');
        g.setAttribute('transform', `translate(${Math.round(n.x)},${Math.round(n.y)}) scale(${(n.scale || 1).toFixed(3)})`);
        g.setAttribute('opacity', String(n.opacity ?? 1));
        g.setAttribute('data-node-id', n.id);

        const hit = document.createElementNS(SVG_NS, 'circle');
        hit.setAttribute('r', String(n.r + 10));
        hit.setAttribute('fill', 'transparent');
        g.appendChild(hit);

        const c = document.createElementNS(SVG_NS, 'circle');
        c.setAttribute('class', 'mc-related-bubble');
        c.setAttribute('r', String(n.r));
        c.setAttribute('fill', n.fill);
        c.setAttribute('stroke', n.stroke);
        g.appendChild(c);

        g.appendChild(renderTextRuns(n.lines, n.fontSize, n.lineHeight, 'mc-related-label'));

        const title = document.createElementNS(SVG_NS, 'title');
        title.textContent = `${n.name} (${(n.similarity * 100).toFixed(0)}% match) — click to explore`;
        g.appendChild(title);

        g.addEventListener('pointerdown', (e) => onPointerDownNode(e, n));
        g.addEventListener('pointermove', onPointerMoveNode);
        g.addEventListener('pointerup', onPointerUpNode);
        g.addEventListener('pointercancel', onPointerUpNode);

        svgEl.appendChild(g);
    });
};

// ---------------- Animation loop ----------------

const stepIdle = () => {
    const arr = nodes;
    const t = frameCount * 0.03;
    for (const n of arr) {
        const angleFromCenter = Math.atan2(n.ty - cy, n.tx - cx);
        const radial = Math.sin(t + n.driftPhase) * 4;
        const tangential = Math.cos(t * 0.7 + n.driftPhase * 1.3) * 6;
        const targetX = n.tx + Math.cos(angleFromCenter) * radial + Math.cos(angleFromCenter + Math.PI / 2) * tangential;
        const targetY = n.ty + Math.sin(angleFromCenter) * radial + Math.sin(angleFromCenter + Math.PI / 2) * tangential;
        const dx = targetX - n.x;
        const dy = targetY - n.y;
        n.vx += dx * 0.012;
        n.vy += dy * 0.012;
        n.vx += (Math.random() - 0.5) * 0.05;
        n.vy += (Math.random() - 0.5) * 0.05;
        n.vx *= 0.90;
        n.vy *= 0.90;
    }

    for (let i = 0; i < arr.length; i++) {
        for (let j = i + 1; j < arr.length; j++) {
            const a = arr[i]; const b = arr[j];
            const dx = b.x - a.x; const dy = b.y - a.y;
            const dist = Math.hypot(dx, dy) || 0.0001;
            const minDist = a.r + b.r + 4;
            if (dist < minDist) {
                const overlap = minDist - dist;
                const nx = dx / dist; const ny = dy / dist;
                a.x -= nx * overlap * 0.5; a.y -= ny * overlap * 0.5;
                b.x += nx * overlap * 0.5; b.y += ny * overlap * 0.5;
                const av = a.vx * nx + a.vy * ny;
                const bv = b.vx * nx + b.vy * ny;
                const exch = (av - bv) * 0.6;
                a.vx -= exch * nx; a.vy -= exch * ny;
                b.vx += exch * nx; b.vy += exch * ny;
            }
        }
    }

    if (activeFit) {
        for (const n of arr) {
            const dx = n.x - cx; const dy = n.y - cy;
            const dist = Math.hypot(dx, dy) || 0.0001;
            const minDist = activeFit.radius + n.r + 6;
            if (dist < minDist) {
                const overlap = minDist - dist;
                const nx = dx / dist; const ny = dy / dist;
                n.x += nx * overlap; n.y += ny * overlap;
                const v = n.vx * nx + n.vy * ny;
                if (v < 0) { n.vx -= 2 * v * nx * 0.8; n.vy -= 2 * v * ny * 0.8; }
            }
        }
    }

    for (const n of arr) {
        if (drag && drag.dragging && drag.node === n) continue;
        n.x += n.vx;
        n.y += n.vy;
    }
};

const tickAnimation = () => {
    frameCount++;

    if (phase === 'fly-in-new-neighbours') {
        if (nodes.length) {
            let done = true;
            for (const n of nodes) {
                const dx = n.tx - n.x; const dy = n.ty - n.y;
                n.x += dx * FLY_IN_SPEED;
                n.y += dy * FLY_IN_SPEED;
                n.opacity = Math.min(1, (n.opacity || 0) + 0.04);
                if (Math.hypot(dx, dy) > 3) done = false;
            }
            if (done) {
                phase = 'collision-pulse';
                activeScale = 0.92;
                setTimeout(() => { activeScale = 1.05; }, 120);
                setTimeout(() => { activeScale = 1; phase = 'settle'; }, 300);
            }
        } else {
            phase = 'idle';
        }
    } else if (phase === 'pop-in') {
        activePopMs += 16;
        const t = activePopMs;
        if (t < 180) {
            const k = t / 180;
            activeScale += (1.18 * k - activeScale) * 0.35;
            activeOpacity = Math.min(1, activeOpacity + 0.12);
        } else if (t < 320) {
            activeScale += (1.0 - activeScale) * 0.25;
            activeOpacity = 1;
        } else {
            activeScale = 1;
            activeOpacity = 1;
            phase = 'fly-in-new-neighbours';
        }
    } else if (phase === 'imploding') {
        let allOut = true;
        for (const n of nodes) {
            if (n.selected) {
                n.x += (cx - n.x) * 0.25;
                n.y += (cy - n.y) * 0.25;
                n.scale = Math.max(0, (n.scale ?? 1) - 0.06);
                n.opacity = Math.max(0, (n.opacity ?? 1) - 0.05);
                if (n.scale > 0.05) allOut = false;
                continue;
            }
            const dx = n.x - cx; const dy = n.y - cy;
            const mag = Math.max(0.01, Math.hypot(dx, dy));
            n.x += (dx / mag) * EXPLOSION_SPEED;
            n.y += (dy / mag) * EXPLOSION_SPEED;
            n.opacity = Math.max(0, (n.opacity ?? 1) - 0.04);
            if (n.x > -240 && n.x < panelWidth + 240 && n.y > -240 && n.y < panelHeight + 240 && n.opacity > 0) allOut = false;
        }
        activeOpacity = Math.max(0, activeOpacity - 0.06);
        activeScale = Math.max(0, activeScale - 0.05);
        if (allOut && pendingNode) {
            const sel = pendingNode;
            pendingNode = null;
            loadArtist(sel.name, true);
        }
    } else {
        stepIdle();
        if (phase === 'settle') phase = 'idle';
    }

    drawFrame();
    rafId = requestAnimationFrame(tickAnimation);
};

const startAnimation = () => {
    if (rafId) return;
    rafId = requestAnimationFrame(tickAnimation);
};

const stopAnimation = () => {
    if (rafId) cancelAnimationFrame(rafId);
    rafId = 0;
};

// ---------------- Pointer drag/click ----------------

const toSvg = (clientX, clientY) => {
    const rect = svgEl?.getBoundingClientRect();
    if (!rect) return { x: 0, y: 0 };
    return {
        x: ((clientX - rect.left) / rect.width) * panelWidth,
        y: ((clientY - rect.top) / rect.height) * panelHeight,
    };
};

function onPointerDownNode(e, n) {
    if (phase !== 'idle' && phase !== 'settle') return;
    try { e.currentTarget.setPointerCapture(e.pointerId); } catch (err) { /* ignore */ }
    const { x, y } = toSvg(e.clientX, e.clientY);
    drag = {
        pointerId: e.pointerId,
        node: n,
        startClientX: e.clientX,
        startClientY: e.clientY,
        lastSx: x,
        lastSy: y,
        lastT: performance.now(),
        dragging: false,
    };
}

function onPointerMoveNode(e) {
    if (!drag || drag.pointerId !== e.pointerId) return;
    const dx = e.clientX - drag.startClientX;
    const dy = e.clientY - drag.startClientY;
    if (!drag.dragging && Math.hypot(dx, dy) > 6) drag.dragging = true;
    if (!drag.dragging) return;
    const { x, y } = toSvg(e.clientX, e.clientY);
    const now = performance.now();
    const dt = Math.max(8, now - drag.lastT);
    drag.node.vx = (x - drag.lastSx) * (16 / dt) * 1.4;
    drag.node.vy = (y - drag.lastSy) * (16 / dt) * 1.4;
    drag.node.x = x;
    drag.node.y = y;
    drag.lastSx = x; drag.lastSy = y; drag.lastT = now;
}

function onPointerUpNode(e) {
    if (!drag || drag.pointerId !== e.pointerId) { drag = null; return; }
    try { e.currentTarget.releasePointerCapture(e.pointerId); } catch (err) { /* ignore */ }
    if (!drag.dragging) {
        const sel = drag.node;
        drag = null;
        selectNode(sel);
    } else {
        const speed = Math.hypot(drag.node.vx, drag.node.vy);
        const max = 18;
        if (speed > max) {
            drag.node.vx = (drag.node.vx / speed) * max;
            drag.node.vy = (drag.node.vy / speed) * max;
        }
        drag = null;
    }
}

const selectNode = (n) => {
    if (phase !== 'idle' && phase !== 'settle') return;
    pendingNode = n;
    for (const node of nodes) node.selected = node.id === n.id;
    n.scale = 1;
    n.opacity = 1;
    phase = 'imploding';
};

// ---------------- Building / refreshing nodes ----------------

const buildNodes = (similar) => {
    if (!similar.length) return [];
    layout = computeLayout(panelWidth, panelHeight, similar.length);
    const sims = similar.map((a) => Number(a.match) || 0);
    const minSim = Math.min(...sims);
    const maxSim = Math.max(...sims);
    const range = Math.max(0.001, maxSim - minSim);
    const fontStack = [16, 14, 13, 12];

    const list = similar.map((a, i) => {
        const norm = ((Number(a.match) || 0) - minSim) / range;
        const baseR = layout.minBubbleR + norm * (layout.maxBubbleR - layout.minBubbleR);
        const fit = fitText(a.name, baseR, layout.maxBubbleR, fontStack, 3);
        const c = colorForNorm(norm);
        return {
            id: normalize(a.name).replace(/\s+/g, '-') || `n-${i}`,
            name: a.name,
            similarity: Number(a.match) || 0,
            r: fit.radius,
            lines: fit.lines,
            fontSize: fit.fontSize,
            lineHeight: fit.lineHeight,
            fill: c.fill,
            stroke: c.stroke,
            x: 0, y: 0, vx: 0, vy: 0, tx: 0, ty: 0,
            opacity: 0, scale: 1,
            driftPhase: i * 0.7 + Math.random() * Math.PI * 2,
            norm,
        };
    });
    for (let i = list.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [list[i], list[j]] = [list[j], list[i]];
    }
    placeTargets(list);
    return list.map((n) => ({ ...n, ...pickOffscreen(panelWidth, panelHeight) }));
};

const recomputeLayoutForResize = () => {
    measurePanel();
    if (!nodes.length) return;
    layout = computeLayout(panelWidth, panelHeight, nodes.length);
    const fontStack = [16, 14, 13, 12];
    for (const n of nodes) {
        const baseR = layout.minBubbleR + n.norm * (layout.maxBubbleR - layout.minBubbleR);
        const fit = fitText(n.name, baseR, layout.maxBubbleR, fontStack, 3);
        n.r = fit.radius;
        n.lines = fit.lines;
        n.fontSize = fit.fontSize;
        n.lineHeight = fit.lineHeight;
    }
    placeTargets(nodes);
    if (currentArtist) {
        activeFit = fitText(currentArtist, layout.activeR, layout.activeR + 24, [22, 20, 18, 16, 14], 3);
    }
};

// ---------------- Media panel (albums / tracks) ----------------

const escapeHtml = (s) => String(s ?? '').replace(/[&<>"']/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));

const extractArtists = (item) => {
    if (!item) return [];
    const out = [];
    const push = (v) => {
        if (!v) return;
        if (Array.isArray(v)) v.forEach(push);
        else if (typeof v === 'string') out.push(v);
        else if (typeof v === 'object' && v.name) out.push(v.name);
    };
    push(item.artists);
    push(item.artist);
    return out.filter(Boolean);
};

const artistListIncludes = (list, target) => {
    const t = normalize(toPrimaryArtist(target));
    return list.some((a) => normalize(toPrimaryArtist(a)) === t);
};

const renderEmpty = (el, msg) => {
    if (!el) return;
    el.innerHTML = `<div class="mc-empty">${escapeHtml(msg)}</div>`;
};

const renderAlbums = (albums) => {
    if (!albumsListEl) return;
    if (!albums.length) { renderEmpty(albumsListEl, 'No albums'); return; }
    albumsListEl.innerHTML = '';
    for (const a of albums) {
        const card = document.createElement('div');
        card.className = 'mc-card';
        card.innerHTML = `
            <div class="mc-card-body">
                <div class="mc-card-title">${escapeHtml(a.title)}</div>
                <div class="mc-card-sub">${escapeHtml(a.artistName)}${a.year ? ` · ${escapeHtml(a.year)}` : ''}</div>
            </div>
            <button type="button" class="mc-play-btn" title="Play album" aria-label="Play album">▶</button>
        `;
        const playBtn = card.querySelector('.mc-play-btn');
        playBtn?.addEventListener('click', (e) => {
            e.stopPropagation();
            playMedia(a.uri);
        });
        albumsListEl.appendChild(card);
    }
};

const renderTracks = (tracks) => {
    if (!tracksListEl) return;
    if (!tracks.length) { renderEmpty(tracksListEl, 'No tracks'); return; }
    tracksListEl.innerHTML = '';
    for (const t of tracks) {
        const card = document.createElement('div');
        card.className = 'mc-card';
        card.innerHTML = `
            <div class="mc-card-body">
                <div class="mc-card-title">${escapeHtml(t.title)}</div>
                <div class="mc-card-sub">${escapeHtml(t.artistName)}${t.album ? ` · ${escapeHtml(t.album)}` : ''}</div>
            </div>
            <button type="button" class="mc-play-btn" title="Play track" aria-label="Play track">▶</button>
        `;
        const playBtn = card.querySelector('.mc-play-btn');
        playBtn?.addEventListener('click', (e) => {
            e.stopPropagation();
            playMedia(t.uri);
        });
        tracksListEl.appendChild(card);
    }
};

const playMedia = async (uri) => {
    if (!uri) return;
    try {
        const res = await fetch('/api/music-connect/play-media', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ uri }),
        });
        if (!res.ok) {
            let msg = `play failed (${res.status})`;
            try { const j = await res.json(); if (j && j.error) msg = j.error; } catch (err) { /* ignore */ }
            setStatus(msg, true);
        } else {
            setStatus('Playback request sent', false);
            setTimeout(() => { if (statusEl?.textContent === 'Playback request sent') setStatus(''); }, 2000);
        }
    } catch (err) {
        setStatus(err.message || 'play failed', true);
    }
};

const fetchMaSearch = async (artist) => {
    const params = new URLSearchParams({
        query: artist,
        media_types: 'artist,album,track',
        limit: '80',
    });
    const res = await fetch(`/api/music-connect/search?${params.toString()}`);
    if (!res.ok) {
        let msg = `MA search failed (${res.status})`;
        try { const j = await res.json(); if (j && j.error) msg = j.error; } catch (err) { /* ignore */ }
        throw new Error(msg);
    }
    return res.json();
};

const loadMediaForArtist = async (artist, requestSeq) => {
    if (!albumsListEl || !tracksListEl) return;
    isLoadingMedia = true;
    renderEmpty(albumsListEl, 'Loading…');
    renderEmpty(tracksListEl, 'Loading…');
    try {
        const search = await fetchMaSearch(artist);
        if (requestSeq !== artistLoadSeq) return;
        const root = search?.result ?? search ?? {};
        const albumsRaw = root.album ?? root.albums ?? root?.results?.album ?? [];
        const tracksRaw = root.track ?? root.tracks ?? root?.results?.track ?? [];

        const allAlbums = (Array.isArray(albumsRaw) ? albumsRaw : []).filter((a) => artistListIncludes(extractArtists(a), artist));
        const albumItems = allAlbums.map((a) => ({
            id: a.item_id || a.uri || a.name,
            uri: a.uri || '',
            title: a.name || 'Unknown',
            artistName: extractArtists(a)[0] || artist,
            year: a.year,
        }));
        renderAlbums(albumItems.slice(0, 30));

        const allTracks = (Array.isArray(tracksRaw) ? tracksRaw : []).filter((t) => artistListIncludes(extractArtists(t), artist));
        const dedup = new Map();
        for (const t of allTracks) {
            const item = {
                id: t.item_id || t.uri || t.name,
                uri: t.uri || '',
                title: t.name || 'Unknown',
                artistName: extractArtists(t)[0] || artist,
                album: t.album?.name,
                popularity: t.metadata?.popularity ?? 0,
            };
            const key = normalize(item.title);
            const existing = dedup.get(key);
            if (!existing || (item.popularity ?? 0) > (existing.popularity ?? 0)) dedup.set(key, item);
        }
        const trackItems = Array.from(dedup.values()).sort((a, b) => (b.popularity ?? 0) - (a.popularity ?? 0)).slice(0, 30);
        renderTracks(trackItems);
    } catch (err) {
        console.warn('[MusicConnect] media load failed', err);
        renderEmpty(albumsListEl, err.message || 'Could not load albums');
        renderEmpty(tracksListEl, err.message || 'Could not load tracks');
    } finally {
        isLoadingMedia = false;
    }
};

// ---------------- Last.fm + main load flow ----------------

const fetchSimilar = async (artist) => {
    const params = new URLSearchParams({ artist, limit: '20', autocorrect: '0' });
    const res = await fetch(`/api/lastfm/similar?${params.toString()}`);
    if (!res.ok) {
        let msg = `Last.fm request failed (${res.status})`;
        try { const j = await res.json(); if (j && j.error) msg = j.error; } catch (err) { /* ignore */ }
        throw new Error(msg);
    }
    return res.json();
};

const triggerActivePopIn = () => {
    activeScale = 0.05;
    activeOpacity = 0;
    activePopMs = 0;
    phase = 'pop-in';
};

const loadArtist = async (rawArtist, force = false) => {
    const artist = toPrimaryArtist(rawArtist);
    if (!artist) {
        currentArtist = '';
        lastNormalizedArtist = '';
        nodes = [];
        activeFit = null;
        if (titleEl) titleEl.textContent = 'No artist playing';
        setStatus('Start playback to see related artists.', false);
        renderAlbums([]);
        renderTracks([]);
        return;
    }
    const norm = normalize(artist);
    if (!force && norm === lastNormalizedArtist && nodes.length) return;
    lastNormalizedArtist = norm;
    currentArtist = artist;
    if (titleEl) titleEl.textContent = artist;
    if (searchInputEl) searchInputEl.placeholder = artist;
    setStatus('');

    const requestSeq = ++artistLoadSeq;

    measurePanel();
    layout = computeLayout(panelWidth, panelHeight, 1);
    activeFit = fitText(artist, layout.activeR, layout.activeR + 24, [22, 20, 18, 16, 14], 3);
    nodes = [];
    triggerActivePopIn();

    // Kick off media + similar in parallel
    loadMediaForArtist(artist, requestSeq);

    try {
        const data = await fetchSimilar(artist);
        if (requestSeq !== artistLoadSeq) return;
        const raw = data?.similarartists?.artist;
        const list = Array.isArray(raw) ? raw : raw ? [raw] : [];
        if (!list.length) {
            setStatus('No similar artists from Last.fm.', false);
            return;
        }
        nodes = buildNodes(list.slice(0, 20));
    } catch (err) {
        console.warn('[MusicConnect] failed to load similar artists', err);
        setStatus(err.message || 'Could not load similar artists.', true);
    }
};

const fetchCurrentArtist = async () => {
    try {
        const res = await fetch('/current-track');
        if (!res.ok) return null;
        const data = await res.json();
        return data?.artist || data?.artist_name || null;
    } catch (err) {
        console.warn('[MusicConnect] failed to fetch current track', err);
        return null;
    }
};

const tickPolling = async () => {
    const artist = await fetchCurrentArtist();
    if (!artist) return;
    const primary = toPrimaryArtist(artist);
    const norm = normalize(primary);
    // Only follow the player when it actually moves — never override manual nav.
    if (!lastPlayerArtistKey) {
        lastPlayerArtistKey = norm;
        await loadArtist(primary, false);
        return;
    }
    if (norm !== lastPlayerArtistKey) {
        lastPlayerArtistKey = norm;
        await loadArtist(primary, false);
    }
};

const startPolling = async () => {
    if (pollTimer) return;
    await tickPolling();
    pollTimer = setInterval(tickPolling, POLL_MS);
};

const stopPolling = () => {
    if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
    }
};

// ---------------- Panel lifecycle ----------------

const ensurePanel = () => {
    if (panel) return;

    panel = document.createElement('aside');
    panel.id = PANEL_ID;
    panel.className = 'music-connect-panel';
    panel.setAttribute('aria-hidden', 'true');
    panel.innerHTML = `
        <div class="music-connect-header">
            <div class="music-connect-title-wrap">
                <span class="music-connect-eyebrow">Music Connect</span>
                <h3 class="music-connect-title" id="music-connect-title">No artist playing</h3>
            </div>
            <button type="button" class="music-connect-close" id="music-connect-close" title="Hide Music Connect">×</button>
        </div>
        <div class="music-connect-toolbar">
            <input type="text" id="music-connect-search" class="music-connect-search-input" placeholder="Find artist" autocomplete="off">
            <button type="button" class="music-connect-explore-btn" id="music-connect-explore">Explore</button>
            <label class="music-connect-toggle">
                <input type="checkbox" id="music-connect-show-lines">
                <span>Show connections</span>
            </label>
        </div>
        <div class="music-connect-graph">
            <svg id="music-connect-svg" preserveAspectRatio="xMidYMid meet"></svg>
        </div>
        <div class="music-connect-status" id="music-connect-status"></div>
        <div class="music-connect-media">
            <div class="mc-media-column">
                <h4>Albums</h4>
                <div class="mc-media-list" id="music-connect-albums"></div>
            </div>
            <div class="mc-media-column">
                <h4>Tracks</h4>
                <div class="mc-media-list" id="music-connect-tracks"></div>
            </div>
        </div>
    `;
    document.body.appendChild(panel);

    titleEl = panel.querySelector('#music-connect-title');
    svgEl = panel.querySelector('#music-connect-svg');
    statusEl = panel.querySelector('#music-connect-status');
    albumsListEl = panel.querySelector('#music-connect-albums');
    tracksListEl = panel.querySelector('#music-connect-tracks');
    searchInputEl = panel.querySelector('#music-connect-search');
    exploreBtnEl = panel.querySelector('#music-connect-explore');
    showLinesCheckboxEl = panel.querySelector('#music-connect-show-lines');
    showLinesCheckboxEl.checked = showLines;

    panel.querySelector('#music-connect-close')?.addEventListener('click', () => closePanel());

    showLinesCheckboxEl.addEventListener('change', (e) => {
        showLines = !!e.target.checked;
        try { localStorage.setItem('music-connect:show-lines', showLines ? '1' : '0'); } catch (err) { /* ignore */ }
    });

    const submitSearch = () => {
        const v = (searchInputEl?.value || '').trim();
        if (!v) return;
        loadArtist(v, true);
    };
    exploreBtnEl?.addEventListener('click', submitSearch);
    searchInputEl?.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); submitSearch(); }
    });

    if (typeof ResizeObserver !== 'undefined') {
        resizeObserver = new ResizeObserver(() => {
            if (!isOpen) return;
            recomputeLayoutForResize();
        });
        resizeObserver.observe(panel);
    } else {
        window.addEventListener('resize', () => { if (isOpen) recomputeLayoutForResize(); });
    }
};

const setBodyOpenState = (open) => {
    document.body.classList.toggle('music-connect-open', !!open);
};

const openPanel = async () => {
    ensurePanel();
    if (isOpen) return;
    isOpen = true;
    panel.classList.add('open');
    panel.setAttribute('aria-hidden', 'false');
    setBodyOpenState(true);
    if (toggleBtn) toggleBtn.classList.add('active');
    requestAnimationFrame(() => {
        measurePanel();
        recomputeLayoutForResize();
        startAnimation();
        startPolling();
    });
};

const closePanel = () => {
    if (!isOpen) return;
    isOpen = false;
    if (panel) {
        panel.classList.remove('open');
        panel.setAttribute('aria-hidden', 'true');
    }
    setBodyOpenState(false);
    if (toggleBtn) toggleBtn.classList.remove('active');
    stopPolling();
    stopAnimation();
};

const toggle = () => {
    if (isOpen) closePanel();
    else openPanel();
};

export function setupMusicConnect() {
    toggleBtn = document.getElementById('btn-music-connect');
    if (!toggleBtn) return;
    toggleBtn.addEventListener('click', toggle);
    ensurePanel();
}

export function isMusicConnectOpen() {
    return isOpen;
}
