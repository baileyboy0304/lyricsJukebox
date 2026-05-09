/**
 * musicConnect.js — Music Connect UX panel
 *
 * Slide-in panel that shows a bubble graph of artists similar to whatever
 * the SyncUDP metadata orchestrator currently has playing. The panel is
 * triggered from the transport-control lozenge and consumes 1/3 of the
 * canvas, with the lyrics container shifted to leave room.
 *
 * The active artist is sourced from /current-track (already populated by
 * the addon's HA / Music Assistant connection) so this module never has
 * to pick a player.
 */

const PANEL_ID = 'music-connect-panel';
const SVG_NS = 'http://www.w3.org/2000/svg';
const POLL_MS = 5000;

let panel = null;
let svgEl = null;
let titleEl = null;
let statusEl = null;
let toggleBtn = null;

let isOpen = false;
let isLoading = false;
let currentArtist = '';
let lastNormalizedArtist = '';
let pollTimer = null;
let resizeRaf = 0;
let nodes = [];
let activeFit = null;
let panelWidth = 0;
let panelHeight = 0;

const normalize = (s) => (s || '').toLowerCase().replace(/[‘’“”'`]/g, '').replace(/\s+/g, ' ').trim();

const toPrimaryArtist = (raw) => {
    const v = (raw || '').trim();
    if (!v) return '';
    // Keep "Mumford & Sons" intact, but split off feat./ft. and "/" collaborators.
    const lead = v.split(/\s+(?:feat\.?|ft\.?)\s+/i)[0]?.trim() || v;
    if (lead.includes('/')) return lead.split('/').map((x) => x.trim()).find(Boolean) || lead;
    return lead;
};

const colorForNorm = (norm) => {
    const v = Math.max(0, Math.min(1, norm));
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

const buildNodes = (similar) => {
    if (!similar.length) return [];
    const sims = similar.map((a) => Number(a.match) || 0);
    const minSim = Math.min(...sims);
    const maxSim = Math.max(...sims);
    const range = Math.max(0.001, maxSim - minSim);
    const fontStack = [16, 14, 13, 12];
    const layout = computeLayout(panelWidth, panelHeight, similar.length);
    const cx = panelWidth / 2;
    const cy = panelHeight / 2;

    const list = similar.map((a, i) => {
        const norm = ((Number(a.match) || 0) - minSim) / range;
        const baseR = layout.minBubbleR + norm * (layout.maxBubbleR - layout.minBubbleR);
        const fit = fitText(a.name, baseR, layout.maxBubbleR, fontStack, 3);
        const c = colorForNorm(norm);
        return {
            name: a.name,
            r: fit.radius,
            lines: fit.lines,
            fontSize: fit.fontSize,
            lineHeight: fit.lineHeight,
            fill: c.fill,
            stroke: c.stroke,
            tx: 0,
            ty: 0,
            similarity: Number(a.match) || 0,
        };
    });
    for (let i = list.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [list[i], list[j]] = [list[j], list[i]];
    }
    for (let i = 0; i < list.length; i++) {
        const angle = -Math.PI / 2 + i * layout.angleStep;
        list[i].tx = cx + Math.cos(angle) * layout.ring;
        list[i].ty = cy + Math.sin(angle) * layout.ring;
    }
    activeFit = fitText(currentArtist || 'Now Playing', layout.activeR, layout.activeR + 24, [22, 20, 18, 16, 14], 3);
    return list;
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

const renderGraph = () => {
    if (!svgEl) return;
    svgEl.setAttribute('viewBox', `0 0 ${panelWidth} ${panelHeight}`);
    while (svgEl.firstChild) svgEl.removeChild(svgEl.firstChild);

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

    const cx = panelWidth / 2;
    const cy = panelHeight / 2;

    if (nodes.length && activeFit) {
        const linesG = document.createElementNS(SVG_NS, 'g');
        nodes.forEach((n) => {
            const line = document.createElementNS(SVG_NS, 'line');
            line.setAttribute('class', 'mc-connector');
            line.setAttribute('x1', String(cx));
            line.setAttribute('y1', String(cy));
            line.setAttribute('x2', String(Math.round(n.tx)));
            line.setAttribute('y2', String(Math.round(n.ty)));
            line.setAttribute('stroke', n.stroke);
            line.setAttribute('stroke-opacity', '0.35');
            line.setAttribute('stroke-width', '1.2');
            linesG.appendChild(line);
        });
        svgEl.appendChild(linesG);
    }

    if (activeFit) {
        const g = document.createElementNS(SVG_NS, 'g');
        g.setAttribute('transform', `translate(${cx},${cy})`);
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
        g.setAttribute('transform', `translate(${Math.round(n.tx)},${Math.round(n.ty)})`);
        const c = document.createElementNS(SVG_NS, 'circle');
        c.setAttribute('class', 'mc-related-bubble');
        c.setAttribute('r', String(n.r));
        c.setAttribute('fill', n.fill);
        c.setAttribute('stroke', n.stroke);
        g.appendChild(c);
        g.appendChild(renderTextRuns(n.lines, n.fontSize, n.lineHeight, 'mc-related-label'));
        const title = document.createElementNS(SVG_NS, 'title');
        title.textContent = `${n.name} (${(n.similarity * 100).toFixed(0)}% match)`;
        g.appendChild(title);
        svgEl.appendChild(g);
    });
};

const setStatus = (text, isError = false) => {
    if (!statusEl) return;
    statusEl.textContent = text || '';
    statusEl.classList.toggle('error', !!isError);
    statusEl.style.display = text ? 'block' : 'none';
};

const measurePanel = () => {
    if (!panel) return;
    const rect = panel.getBoundingClientRect();
    panelWidth = Math.max(240, Math.round(rect.width - 32));
    panelHeight = Math.max(280, Math.round(rect.height - 80));
};

const refreshLayout = () => {
    measurePanel();
    if (!nodes.length) {
        if (svgEl) {
            svgEl.setAttribute('viewBox', `0 0 ${panelWidth} ${panelHeight}`);
            while (svgEl.firstChild) svgEl.removeChild(svgEl.firstChild);
        }
        return;
    }
    nodes = buildNodes(nodes.map((n) => ({ name: n.name, match: n.similarity })));
    renderGraph();
};

const fetchSimilar = async (artist) => {
    const params = new URLSearchParams({ artist, limit: '20', autocorrect: '0' });
    const res = await fetch(`/api/lastfm/similar?${params.toString()}`);
    if (!res.ok) {
        let msg = `Last.fm request failed (${res.status})`;
        try {
            const j = await res.json();
            if (j && j.error) msg = j.error;
        } catch (e) { /* ignore */ }
        throw new Error(msg);
    }
    return res.json();
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
        renderGraph();
        return;
    }
    const norm = normalize(artist);
    if (!force && norm === lastNormalizedArtist && nodes.length) return;
    lastNormalizedArtist = norm;
    currentArtist = artist;
    if (titleEl) titleEl.textContent = artist;
    setStatus('');

    if (isLoading) return;
    isLoading = true;
    try {
        measurePanel();
        const data = await fetchSimilar(artist);
        const raw = data?.similarartists?.artist;
        const list = Array.isArray(raw) ? raw : raw ? [raw] : [];
        nodes = buildNodes(list.slice(0, 20));
        if (!nodes.length) setStatus('No similar artists from Last.fm.', false);
        renderGraph();
    } catch (err) {
        console.warn('[MusicConnect] failed to load similar artists', err);
        setStatus(err.message || 'Could not load similar artists.', true);
        nodes = [];
        activeFit = fitText(artist, 64, 90, [22, 20, 18, 16, 14], 3);
        renderGraph();
    } finally {
        isLoading = false;
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
    await loadArtist(artist, false);
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
        <div class="music-connect-graph">
            <svg id="music-connect-svg" preserveAspectRatio="xMidYMid meet"></svg>
        </div>
        <div class="music-connect-status" id="music-connect-status"></div>
    `;
    document.body.appendChild(panel);

    titleEl = panel.querySelector('#music-connect-title');
    svgEl = panel.querySelector('#music-connect-svg');
    statusEl = panel.querySelector('#music-connect-status');

    panel.querySelector('#music-connect-close')?.addEventListener('click', () => closePanel());

    window.addEventListener('resize', () => {
        if (resizeRaf) cancelAnimationFrame(resizeRaf);
        resizeRaf = requestAnimationFrame(() => {
            resizeRaf = 0;
            if (isOpen) refreshLayout();
        });
    });
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
    // Allow CSS transition / reflow before measuring.
    requestAnimationFrame(() => {
        measurePanel();
        if (currentArtist) renderGraph();
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
};

const toggle = () => {
    if (isOpen) closePanel();
    else openPanel();
};

/**
 * Wire up the Music Connect toggle button (added to the transport lozenge)
 * and lazily create the panel on first interaction.
 */
export function setupMusicConnect() {
    toggleBtn = document.getElementById('btn-music-connect');
    if (!toggleBtn) return;
    toggleBtn.addEventListener('click', toggle);
    // Build the panel up-front so the slide-in transition feels instant on first click.
    ensurePanel();
}

export function isMusicConnectOpen() {
    return isOpen;
}
