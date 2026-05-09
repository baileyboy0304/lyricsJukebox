import { useEffect, useMemo, useRef, useState } from 'react';
import type { ArtistNode } from '../api/apiTypes';
import type { GraphPhase } from '../types';
import { EXPLOSION_SPEED, FLY_IN_SPEED } from '../utils/animationConstants';
import { pickOffscreen } from '../utils/graphLayout';

type WrappedText = { lines: string[]; fontSize: number; lineHeight: number; radius: number };

type SimNode = ArtistNode & {
  x: number; y: number;
  vx: number; vy: number;
  tx: number; ty: number;
  r: number;
  lines: string[];
  fontSize: number;
  lineHeight: number;
  fill: string;
  stroke: string;
  opacity: number;
  scale: number;
  selected?: boolean;
  driftPhase: number;
  norm: number;
};

const clamp01 = (v: number) => Math.max(0, Math.min(1, v));

// Layout parameters for a given viewport + bubble count.
// Picks a ring radius that fits in the viewport, then sizes bubbles so they
// fit at uniform angular spacing without overflowing the canvas.
const computeLayout = (w: number, h: number, n: number) => {
  const halfMin = Math.max(80, Math.min(w, h) / 2);
  const margin = 16;
  const padding = 12;
  const safeN = Math.max(1, n);
  const angleStep = (Math.PI * 2) / safeN;
  const sinHalf = Math.sin(angleStep / 2);
  const usable = halfMin - margin;
  // Solve: ring + maxBubbleR = usable; maxBubbleR = ring*sinHalf - padding/2
  let ring = Math.max(80, (usable + padding / 2) / (1 + sinHalf));
  let maxBubbleR = Math.max(28, ring * sinHalf - padding / 2);
  // Cap the max bubble radius so a single big bubble never dominates
  maxBubbleR = Math.min(maxBubbleR, halfMin * 0.22);
  // Keep bubbles fairly uniform in size — colour does the heavy lifting on
  // signalling similarity; size is a secondary signal.
  const minBubbleR = Math.max(38, Math.round(maxBubbleR * 0.78));
  const activeR = Math.min(Math.round(ring * 0.42), Math.max(72, Math.round(halfMin * 0.18)));
  return { ring, maxBubbleR, minBubbleR, activeR, padding, angleStep };
};

const MIN_FONT_PX = 12;

const greedyWrap = (text: string, maxChars: number): string[] => {
  const words = (text || '').split(/\s+/).filter(Boolean);
  if (words.length === 0) return [''];
  const lines: string[] = [];
  let cur = '';
  for (const w of words) {
    if (!cur) { cur = w; continue; }
    if (cur.length + 1 + w.length <= maxChars) cur += ' ' + w;
    else { lines.push(cur); cur = w; }
  }
  if (cur) lines.push(cur);
  return lines;
};

// Find a (radius, fontSize, lineCount) combo that fits the name inside the bubble.
const fitText = (
  name: string,
  baseRadius: number,
  maxRadius: number,
  fontSizes: number[],
  maxLines = 3
): WrappedText => {
  for (let r = baseRadius; r <= maxRadius; r += 4) {
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
  // Last-resort: shrink to fit by character truncation
  const fs = fontSizes[fontSizes.length - 1];
  const lineH = fs * 1.18;
  const maxChars = Math.max(4, Math.floor((maxRadius * 1.7) / (fs * 0.55)));
  const lines = greedyWrap(name, maxChars).slice(0, maxLines);
  return { lines, fontSize: fs, lineHeight: lineH, radius: maxRadius };
};

const colorForNorm = (norm: number) => {
  const v = clamp01(norm);
  const hue = 6 + v * 124;
  const sat = 60 + v * 12;
  const light = 46 + v * 10;
  return {
    fill: `hsl(${hue.toFixed(0)}, ${sat.toFixed(0)}%, ${light.toFixed(0)}%)`,
    stroke: `hsl(${hue.toFixed(0)}, 80%, 72%)`
  };
};

type DragState = {
  pointerId: number;
  node: SimNode;
  startClientX: number;
  startClientY: number;
  lastSx: number;
  lastSy: number;
  lastT: number;
  dragging: boolean;
};

export function BubbleGraph({
  activeArtist, similarArtists, onSelectArtist, phase, setPhase, showLines
}: {
  activeArtist: ArtistNode;
  similarArtists: ArtistNode[];
  onSelectArtist: (a: ArtistNode) => boolean;
  phase: GraphPhase;
  setPhase: (p: GraphPhase) => void;
  showLines: boolean;
}) {
  const wrap = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 900, h: 620 });
  const [, setTick] = useState(0);
  const center = useMemo(() => ({ x: size.w / 2, y: size.h / 2 }), [size]);
  const nodes = useRef<SimNode[]>([]);
  const activeScale = useRef(1);
  const activeOpacity = useRef(1);
  const activeLabel = useRef(activeArtist.name);
  const activePos = useRef({ x: center.x, y: center.y, vx: 0, vy: 0, exploding: false });
  const pending = useRef<SimNode | null>(null);
  const popTimer = useRef(0);
  const drag = useRef<DragState | null>(null);

  const layout = useMemo(
    () => computeLayout(size.w, size.h, Math.max(1, similarArtists.length)),
    [size.w, size.h, similarArtists.length]
  );

  const activeFit = useMemo(
    () => fitText(activeArtist.name, layout.activeR, layout.activeR + 30, [24, 22, 20, 18, 16, 14, MIN_FONT_PX], 4),
    [activeArtist.name, layout.activeR]
  );

  const placeTargets = (list: SimNode[]) => {
    if (list.length === 0) return;
    const { ring, angleStep } = layout;
    for (let i = 0; i < list.length; i++) {
      const angle = -Math.PI / 2 + i * angleStep;
      list[i].tx = center.x + Math.cos(angle) * ring;
      list[i].ty = center.y + Math.sin(angle) * ring;
    }
  };

  const initNodes = () => {
    if (similarArtists.length === 0) {
      nodes.current = [];
      return;
    }
    const sims = similarArtists.map((a) => a.similarity ?? 0);
    const minSim = Math.min(...sims);
    const maxSim = Math.max(...sims);
    const range = Math.max(0.001, maxSim - minSim);

    const fontStack = [16, 15, 14, 13, MIN_FONT_PX];

    const list: SimNode[] = similarArtists.map((n, i) => {
      const norm = ((n.similarity ?? 0) - minSim) / range;
      // Size variance is small — colour drives the similarity signal.
      const baseR = layout.minBubbleR + norm * (layout.maxBubbleR - layout.minBubbleR);
      const fit = fitText(n.name, baseR, layout.maxBubbleR, fontStack, 3);
      const c = colorForNorm(norm);
      return {
        ...n,
        r: fit.radius,
        lines: fit.lines,
        fontSize: fit.fontSize,
        lineHeight: fit.lineHeight,
        fill: c.fill,
        stroke: c.stroke,
        x: 0, y: 0, vx: 0, vy: 0, tx: 0, ty: 0,
        opacity: 0, scale: 1,
        driftPhase: i * 0.7 + Math.random() * Math.PI * 2,
        norm
      };
    });
    // Shuffle before placement so colour gradient doesn't run clockwise around the ring.
    for (let i = list.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [list[i], list[j]] = [list[j], list[i]];
    }
    placeTargets(list);
    nodes.current = list.map((n) => ({ ...n, ...pickOffscreen(size.w, size.h) }));
  };

  // Resize observer
  useEffect(() => {
    const ro = new ResizeObserver(() => {
      const r = wrap.current?.getBoundingClientRect();
      if (r) setSize({ w: Math.max(200, r.width), h: Math.max(200, r.height) });
    });
    if (wrap.current) ro.observe(wrap.current);
    return () => ro.disconnect();
  }, []);

  // Active artist change: reset position/label and run the pop-in animation
  useEffect(() => {
    activeLabel.current = activeArtist.name;
    activePos.current = { x: center.x, y: center.y, vx: 0, vy: 0, exploding: false };
    activeScale.current = 0.05;
    activeOpacity.current = 0;
    popTimer.current = 0;
    setPhase('pop-in');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeArtist.id, activeArtist.name]);

  // Similar list change: rebuild ring of related bubbles (offscreen, awaiting fly-in)
  useEffect(() => {
    initNodes();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [similarArtists]);

  // On viewport resize: rescale bubbles and re-place ring targets without flying them in again
  useEffect(() => {
    if (nodes.current.length > 0) {
      const fontStack = [16, 15, 14, 13, MIN_FONT_PX];
      for (const n of nodes.current) {
        const baseR = layout.minBubbleR + n.norm * (layout.maxBubbleR - layout.minBubbleR);
        const fit = fitText(n.name, baseR, layout.maxBubbleR, fontStack, 3);
        n.r = fit.radius;
        n.lines = fit.lines;
        n.fontSize = fit.fontSize;
        n.lineHeight = fit.lineHeight;
      }
      placeTargets(nodes.current);
    }
    if (!activePos.current.exploding) {
      activePos.current.x = center.x;
      activePos.current.y = center.y;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [center.x, center.y, layout.ring, layout.minBubbleR, layout.maxBubbleR]);

  // Animation loop
  useEffect(() => {
    let raf = 0;
    let frameCount = 0;

    const stepIdle = () => {
      const arr = nodes.current;
      const t = frameCount * 0.03;

      for (const n of arr) {
        const angleFromCenter = Math.atan2(n.ty - center.y, n.tx - center.x);
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

      // Sibling-vs-sibling
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
      // Sibling-vs-active centre
      for (const n of arr) {
        const dx = n.x - center.x; const dy = n.y - center.y;
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
      // Apply velocity (skip bubbles being dragged)
      for (const n of arr) {
        if (drag.current && drag.current.dragging && drag.current.node === n) continue;
        n.x += n.vx;
        n.y += n.vy;
      }
    };

    const frame = () => {
      frameCount++;

      if (phase === 'fly-in-new-neighbours') {
        if (nodes.current.length > 0) {
          let done = true;
          for (const n of nodes.current) {
            const dx = n.tx - n.x; const dy = n.ty - n.y;
            n.x += dx * FLY_IN_SPEED;
            n.y += dy * FLY_IN_SPEED;
            n.opacity = Math.min(1, n.opacity + 0.04);
            if (Math.hypot(dx, dy) > 3) done = false;
          }
          if (done) {
            setPhase('collision-pulse');
            activeScale.current = 0.92;
            setTimeout(() => (activeScale.current = 1.05), 120);
            setTimeout(() => { activeScale.current = 1; setPhase('settle'); }, 300);
          }
        }
      } else if (phase === 'pop-in') {
        popTimer.current += 16;
        const t = popTimer.current;
        if (t < 180) {
          const k = t / 180;
          activeScale.current += (1.18 * k - activeScale.current) * 0.35;
          activeOpacity.current = Math.min(1, activeOpacity.current + 0.12);
        } else if (t < 320) {
          activeScale.current += (1.0 - activeScale.current) * 0.25;
          activeOpacity.current = 1;
        } else {
          activeScale.current = 1;
          activeOpacity.current = 1;
          setPhase('fly-in-new-neighbours');
        }
      } else if (phase === 'imploding') {
        // Selected: shrink to centre. Others: explode out. Active: fade.
        let allOut = true;
        for (const n of nodes.current) {
          if (n.selected) {
            n.x += (center.x - n.x) * 0.25;
            n.y += (center.y - n.y) * 0.25;
            n.scale = Math.max(0, n.scale - 0.06);
            n.opacity = Math.max(0, n.opacity - 0.05);
            if (n.scale > 0.05) allOut = false;
            continue;
          }
          const dx = n.x - center.x; const dy = n.y - center.y;
          const mag = Math.max(0.01, Math.hypot(dx, dy));
          n.x += (dx / mag) * EXPLOSION_SPEED;
          n.y += (dy / mag) * EXPLOSION_SPEED;
          n.opacity = Math.max(0, n.opacity - 0.04);
          if (n.x > -240 && n.x < size.w + 240 && n.y > -240 && n.y < size.h + 240 && n.opacity > 0) allOut = false;
        }
        activeOpacity.current = Math.max(0, activeOpacity.current - 0.06);
        activeScale.current = Math.max(0, activeScale.current - 0.05);
        if (allOut && pending.current) {
          const sel = pending.current;
          pending.current = null;
          onSelectArtist(sel); // triggers parent → activeArtist change → effect resets to pop-in
        }
      } else if (phase === 'idle' || phase === 'settle' || phase === 'collision-pulse') {
        stepIdle();
        if (phase === 'settle') setPhase('idle');
      }

      setTick((t) => (t + 1) % 100000);
      raf = requestAnimationFrame(frame);
    };
    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase, center.x, center.y, onSelectArtist, setPhase, size.h, size.w, activeFit.radius]);

  const select = (n: SimNode) => {
    if (phase !== 'idle' && phase !== 'settle') return;
    pending.current = n;
    for (const node of nodes.current) node.selected = node.id === n.id;
    n.scale = 1;
    n.opacity = 1;
    setPhase('imploding');
  };

  // Convert client coords to SVG coords using current size
  const toSvg = (clientX: number, clientY: number) => {
    const rect = wrap.current?.getBoundingClientRect();
    if (!rect) return { x: 0, y: 0 };
    return {
      x: ((clientX - rect.left) / rect.width) * size.w,
      y: ((clientY - rect.top) / rect.height) * size.h
    };
  };

  const onPointerDownNode = (e: React.PointerEvent<SVGGElement>, n: SimNode) => {
    if (phase !== 'idle' && phase !== 'settle') return;
    (e.currentTarget as Element).setPointerCapture(e.pointerId);
    const { x, y } = toSvg(e.clientX, e.clientY);
    drag.current = {
      pointerId: e.pointerId,
      node: n,
      startClientX: e.clientX,
      startClientY: e.clientY,
      lastSx: x,
      lastSy: y,
      lastT: performance.now(),
      dragging: false
    };
  };

  const onPointerMoveNode = (e: React.PointerEvent<SVGGElement>) => {
    const d = drag.current;
    if (!d || d.pointerId !== e.pointerId) return;
    const dx = e.clientX - d.startClientX;
    const dy = e.clientY - d.startClientY;
    if (!d.dragging && Math.hypot(dx, dy) > 6) d.dragging = true;
    if (!d.dragging) return;
    const { x, y } = toSvg(e.clientX, e.clientY);
    const now = performance.now();
    const dt = Math.max(8, now - d.lastT);
    // Velocity in svg units per frame (approx); multiplier amplifies push energy
    d.node.vx = (x - d.lastSx) * (16 / dt) * 1.4;
    d.node.vy = (y - d.lastSy) * (16 / dt) * 1.4;
    d.node.x = x;
    d.node.y = y;
    d.lastSx = x; d.lastSy = y; d.lastT = now;
  };

  const onPointerUpNode = (e: React.PointerEvent<SVGGElement>) => {
    const d = drag.current;
    if (!d || d.pointerId !== e.pointerId) { drag.current = null; return; }
    try { (e.currentTarget as Element).releasePointerCapture(e.pointerId); } catch { /* ignore */ }
    if (!d.dragging) {
      drag.current = null;
      select(d.node);
    } else {
      // Cap velocity so we don't fling offscreen
      const speed = Math.hypot(d.node.vx, d.node.vy);
      const max = 18;
      if (speed > max) {
        d.node.vx = (d.node.vx / speed) * max;
        d.node.vy = (d.node.vy / speed) * max;
      }
      drag.current = null;
    }
  };

  const renderText = (lines: string[], fontSize: number, lineHeight: number, className: string) => {
    const totalH = lines.length * lineHeight;
    const startDy = -totalH / 2 + fontSize * 0.85;
    return (
      <text textAnchor="middle" className={className} fontSize={fontSize}>
        {lines.map((line, i) => (
          <tspan key={i} x={0} dy={i === 0 ? startDy : lineHeight}>{line}</tspan>
        ))}
      </text>
    );
  };

  const ax = Math.round(activePos.current.x);
  const ay = Math.round(activePos.current.y);

  return (
    <div className="graph" ref={wrap}>
      <svg viewBox={`0 0 ${size.w} ${size.h}`}>
        <defs>
          <radialGradient id="activeGradient" cx="50%" cy="40%" r="60%">
            <stop offset="0%" stopColor="#a78bff" />
            <stop offset="60%" stopColor="#7d8dff" />
            <stop offset="100%" stopColor="#3f48a8" />
          </radialGradient>
        </defs>

        {showLines && (
          <g>
            {nodes.current.map((n) => {
              if (n.selected) return null;
              return (
                <line
                  key={`line-${n.id}`}
                  className="connectorLine"
                  x1={ax}
                  y1={ay}
                  x2={Math.round(n.x)}
                  y2={Math.round(n.y)}
                  stroke={n.stroke}
                  strokeOpacity={n.opacity * 0.35}
                  strokeWidth={1.2}
                />
              );
            })}
          </g>
        )}

        <g
          transform={`translate(${ax},${ay}) scale(${activeScale.current.toFixed(3)})`}
          style={{ opacity: activeOpacity.current }}
        >
          <circle r={activeFit.radius} className="activeBubble" />
          {renderText(activeFit.lines, activeFit.fontSize, activeFit.lineHeight, 'activeLabel')}
        </g>

        {nodes.current.map((n) => {
          const x = Math.round(n.x);
          const y = Math.round(n.y);
          return (
            <g
              key={n.id}
              className="bubbleGroup"
              transform={`translate(${x},${y}) scale(${n.scale.toFixed(3)})`}
              onPointerDown={(e) => onPointerDownNode(e, n)}
              onPointerMove={onPointerMoveNode}
              onPointerUp={onPointerUpNode}
              onPointerCancel={onPointerUpNode}
              style={{ opacity: n.opacity, cursor: 'pointer', touchAction: 'none' }}
            >
              <circle r={n.r + 10} fill="transparent" />
              <circle r={n.r} className="relatedBubble" fill={n.fill} stroke={n.stroke} />
              {renderText(n.lines, n.fontSize, n.lineHeight, 'relatedLabel')}
            </g>
          );
        })}
      </svg>
    </div>
  );
}
