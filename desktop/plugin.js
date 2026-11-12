// z-midi-console — desktop pane v1 (display: Z's MIDI Console)
// Plain ESM + jsx() factories (no JSX syntax). Theme via var(--ui-*). REST via ctx.rest.
// Laws: pane is a dumb renderer of the profile; all state truth is the backend Bus.
import { ROUTES_AREA, SIDEBAR_NAV_AREA, PALETTE_AREA, host } from '@hermes/plugin-sdk'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

let api = null
const PAGE = '/midi-console'
const CELL = 88 // grid cell px (control slot)
// Effective grid width (review 2026-11-09): analyzers default to 4 cells even
// when the stored doc omits w (imported/legacy). Every span computation —
// drop footprint, occupant push, group advance, vacated placeholder — must
// use the SAME effective width or wide mates swallow their neighbours' slots.
const effW = (c) => Math.max(1, (c && c.w) || (c && c.widget === 'analyzer' ? 4 : 1))
// CHAIN-PUSH law (owner ruling 2026-11-10 night, after ~30 patch attempts):
// a drop moves ONLY the occupants its footprint actually touches. Sort by x,
// walk right from the landing span [x0, x0+w-1]: an occupant moves only if it
// overlaps the span or a mate already pushed into it (chain); the first
// occupant with a gap before it — and everything right of it — stays put.
// This replaces the list-insertion tail-shift (move everything at/after the
// drop point) that made untouched neighbours flee from EMPTY-slot drops.
// ONE function feeds BOTH grids: controls pass real widths, categories pass
// w=1 columns — the two laws can never drift apart again. Pure + module
// scope so the test_ask node harness runs the REAL shipped code.
function chainPushRow(mates, x0, w) {
  const out = []
  let cursor = x0 + w
  for (const m of [...mates].sort((a, b) => a.x - b.x)) {
    let x = m.x
    if (x + m.w - 1 >= x0 && x < cursor) x = cursor
    cursor = Math.max(cursor, x + m.w)
    out.push({ id: m.id, x })
  }
  return out
}
let CH = 0 // active profile's MIDI channel — law: every wire send carries it explicitly

// Shared button style (review: was duplicated per-render in Surface AND
// renderGrid under two names — one module-scope copy, no per-render alloc).
const tinyS = { border: '1px solid var(--ui-stroke-secondary)', background: 'transparent', color: 'var(--ui-text-secondary)', fontSize: 10, padding: '1px 5px', borderRadius: 4, cursor: 'pointer' }
// Toolbar dropdown law (owner ruling 2026-11-09): EDIT and SETTINGS are the
// SAME size bold caps buttons; their options pop DOWNWARD as stacked equal
// boxes — like a right-click context menu.
const bigBtnS = { background: 'transparent', padding: '6px 14px', borderRadius: 6, cursor: 'pointer', fontSize: 15, fontWeight: 800, letterSpacing: '0.06em' }
// HIDE/SHOW (owner ask 2026-11-09): tiny bold corner box that tucks the whole
// PRESET/MIDI/PROFILE bar away; SHOW snaps the rest of the toolbar snug left.
const hideBtnS = { fontSize: 9, fontWeight: 800, letterSpacing: '0.08em', textTransform: 'uppercase', border: '1px solid var(--ui-stroke-secondary)', color: 'var(--ui-text-secondary)', background: 'transparent', padding: '3px 7px', borderRadius: 5, cursor: 'pointer', alignSelf: 'flex-start' }
// Dropdown law (owner ruling 2026-11-09, transparency 2nd ruling): pop-down
// panels are OPAQUE solid — knobs and surface text must never phase through
// them (same solid palette as the tooltip law).
const menuPanelS = { position: 'absolute', top: '100%', left: 0, marginTop: 6, zIndex: 60, display: 'flex', flexDirection: 'column', gap: 6, padding: 8, background: '#242c36', border: '1px solid #3d444d', borderRadius: 8, boxShadow: '0 8px 20px rgba(0,0,0,.55)', minWidth: 170 }
const menuItemS = { border: '1px solid var(--ui-stroke-secondary)', background: 'transparent', color: 'var(--ui-text-primary)', fontSize: 13, padding: '6px 12px', borderRadius: 6, cursor: 'pointer', textAlign: 'left', width: '100%' }
// Shared input style (deck 2026-11-09: hoisted from Surface-local — DeckSurface
// needs it too; one module-scope copy, no per-render alloc).
const inpS = { padding: 4, background: 'var(--ui-bg-secondary)', color: 'var(--ui-text-primary)', border: '1px solid var(--ui-stroke-secondary)', borderRadius: 6 }

// Drag law (review): every window-drag gesture registers pointermove +
// pointerup + pointercancel and unhooks ALL THREE on either terminal event.
// This was copy-pasted six times and pointercancel was missing in three of
// the copies (phantom drops). One helper, one law.
function startPointerDrag({ onMove, onUp, onCancel }) {
  // Frame law (owner polish ask 2026-11-10): raw pointermove fires 100-1000
  // events/s on Windows; every onMove ran a rect scan AND a full Surface
  // re-render — read/write/read thrash at pointer rate, which is exactly the
  // "janky boxes". Coalesce to ONE onMove per animation frame (latest event
  // wins). onUp/onCancel stay immediate; the un-flushed move is irrelevant
  // because every onUp re-computes from the up event's own coordinates.
  let last = null, raf = 0
  const flush = () => { raf = 0; if (last && onMove) { const ev = last; last = null; onMove(ev) } }
  const mv = (ev) => { last = ev; if (!raf) raf = requestAnimationFrame(flush) }
  const up = (ev) => {
    window.removeEventListener('pointermove', mv); window.removeEventListener('pointerup', up)
    window.removeEventListener('pointercancel', cancel)
    if (raf) { cancelAnimationFrame(raf); raf = 0 } last = null
    if (onUp) onUp(ev)
  }
  const cancel = () => {
    window.removeEventListener('pointermove', mv); window.removeEventListener('pointerup', up)
    window.removeEventListener('pointercancel', cancel)
    if (raf) { cancelAnimationFrame(raf); raf = 0 } last = null
    if (onCancel) onCancel()
  }
  window.addEventListener('pointermove', mv); window.addEventListener('pointerup', up)
  window.addEventListener('pointercancel', cancel)
}

const clamp = (v, a, b) => Math.max(a, Math.min(b, v))
const clampT = (v) => clamp(v, 0, 127)
// Deep rinse: the backend writes profiles with sort_keys=True while every
// pane-side insertion appends keys in insertion order — a raw stringify
// compare kept the "Save to profile ●" lit (and the Discard prompt firing)
// after a SUCCESSFUL save. Compare canonically (keys sorted recursively).
const canon = (v) => Array.isArray(v) ? v.map(canon)
  : v && typeof v === 'object' ? Object.fromEntries(Object.keys(v).sort().map(k => [k, canon(v[k])]))
  : v

// #13: send failures must surface — widgets have no status line of their own,
// so the page installs a sink and every fire-and-forget POST reports through it.
// Deck law (2026-11-09): every send carries its SURFACE context {chan, bus} —
// one screen can show several devices; the channel global CH is only the
// default for the active (first) surface.
let onSendError = () => {}
function postCc(cc, value, ctx) {
  const body = { cc, value, channel: ctx && ctx.chan != null ? ctx.chan : CH,
                 bus: (ctx && ctx.bus) || 'primary', source: 'ui' }
  return api.rest('/cc', { method: 'POST', body })
    .then(r => { if (r && r.error) onSendError(`send failed: ${r.error}`) })
    .catch(() => onSendError('send failed: backend unreachable'))
}

// Hover-help law (owner): every action explains itself — hover ~0.7s and a
// themed tooltip appears. Elements carry data-tip; registered ONCE per window
// (the pane module is re-evaluated on reload, listeners must not stack).
if (!window.__zmidiTips) {
  window.__zmidiTips = true
  let tipEl = null
  let tipTimer = null
  const killTip = () => { clearTimeout(tipTimer); if (tipEl) { tipEl.remove(); tipEl = null } }
  const showTip = (t) => {
    if (!t.isConnected) return  // review: poll re-render can unmount the hovered node before the 700 ms timer fires
    const text = t.getAttribute('data-tip')
    if (!text) return
    tipEl = document.createElement('div')
    tipEl.textContent = text
    // Opaque by law (owner ruling 2026-11-09): a translucent themed bg let the
    // text underneath bleed through. Solid panel + border; hide is already
    // instant on pointerout/pointerdown/scroll (killTip).
    tipEl.style.cssText = 'position:fixed;z-index:9999;max-width:300px;padding:6px 9px;border-radius:6px;font-size:12px;line-height:1.4;background:#242c36;color:#e6edf3;border:1px solid #3d444d;box-shadow:0 4px 14px rgba(0,0,0,.55);pointer-events:none;opacity:1'
    document.body.appendChild(tipEl)
    const r = t.getBoundingClientRect()
    const tr = tipEl.getBoundingClientRect()
    const left = Math.max(6, Math.min(r.left + r.width / 2 - tr.width / 2, window.innerWidth - tr.width - 6))
    let top = r.top - tr.height - 6
    if (top < 6) top = r.bottom + 6
    tipEl.style.left = left + 'px'
    tipEl.style.top = top + 'px'
  }
  document.addEventListener('pointerover', (e) => {
    const t = e.target && e.target.closest && e.target.closest('[data-tip]')
    if (!t) return
    clearTimeout(tipTimer)
    tipTimer = setTimeout(() => showTip(t), 700)
  })
  // Review 2026-11-09: (a) a 600 ms poll can unmount the hovered node before
  // the 700 ms timer fires — a detached rect parks the tip at the screen
  // corner; (b) moving between children of the SAME [data-tip] ancestor fired
  // pointerout and killed+re-armed the tip (flicker). Keep it alive in both.
  document.addEventListener('pointerout', (e) => {
    const t = e.target && e.target.closest && e.target.closest('[data-tip]')
    const rt = e.relatedTarget && e.relatedTarget.closest && e.relatedTarget.closest('[data-tip]')
    if (t && rt === t) return
    killTip()
  })
  document.addEventListener('pointerdown', killTip)
  window.addEventListener('scroll', killTip, true)
}

// ---------------------------------------------------------------------------
// widgets — every one maps pointer interaction -> CC 0..127 and renders the
// live shadow value from the backend (source-tagged). Send cadence: coalesced
// to ~30ms so drags don't flood winmm.
// ---------------------------------------------------------------------------
function useThrottledSend(ctx) {
  const last = useRef(0)
  const send = useCallback((cc, value) => {
    const now = Date.now()
    if (now - last.current < 30) return
    last.current = now
    postCc(cc, value, ctx)
  }, [ctx && ctx.bus, ctx && ctx.chan])
  // Trailing-flush law: throttling must never drop a drag's FINAL value —
  // pointer-up always sends the last value straight through (regression review).
  send.now = (cc, value) => postCc(cc, value, ctx)
  return send
}

// #13 (): widgets must act on IMMEDIATE interaction state, not
// 600ms-poll-old values — two fast clicks computed from the same stale poll
// emitted the same CC twice. Local value wins until the backend confirms it
// (poll matches) or a 2s grace expires.
// Settle-ease law (regression review report option D, owner polish 2026-11-10): values
// arriving from the status poll (hardware echo, matrix modulation) GLIDE to
// their new position (~140 ms cubic-out) instead of teleporting at the poll
// clock. The eye reads continuous motion as live even though the data clock
// is slow. Local interaction values bypass this entirely (optimistic paint
// stays instant) — see useOptimistic.
function useEased(value) {
  const [shown, setShown] = useState(value)
  const from = useRef(value), t0 = useRef(0), raf = useRef(0), target = useRef(value)
  useEffect(() => {
    if (value === target.current) return
    from.current = shown; target.current = value; t0.current = performance.now()
    if (raf.current) return
    const step = () => {
      const u = Math.min(1, (performance.now() - t0.current) / 140)
      const e = 1 - Math.pow(1 - u, 3)
      setShown(Math.round(from.current + (target.current - from.current) * e))
      if (u < 1) raf.current = requestAnimationFrame(step); else raf.current = 0
    }
    raf.current = requestAnimationFrame(step)
  }, [value])
  useEffect(() => () => { if (raf.current) cancelAnimationFrame(raf.current) }, [])
  return shown
}

function useOptimistic(value) {
  const [local, setLocal] = useState(null)
  const lastSet = useRef({ v: null, t: 0 })
  const confirmed = useRef(false)
  const poll = useEased(value)  // backend values ease; local interaction below never does
  // Deep rinse (blocker): the early-return used to hand callers the RAW
  // setLocal — after the first backend confirm, lastSet never updated again,
  // so every later interaction rendered the stale poll value until the next
  // 600 ms round trip (and on non-echoing devices looked frozen). The
  // wrapper must be returned in BOTH branches: calling it re-arms the grace.
  const set = (v) => { lastSet.current = { v, t: Date.now() }; confirmed.current = false; setLocal(v) }
  // regression review bug 4 (2026-11-10): once the backend CONFIRMS our value,
  // local's job is DONE — release it at once. The old code kept local alive
  // for the whole 2 s grace, so a HARDWARE move during that window hid behind
  // our stale local value (panel ignored real twists for up to two seconds).
  if (local != null && value === lastSet.current.v) confirmed.current = true
  if (local == null || confirmed.current ||
      Date.now() - lastSet.current.t > 2000) return [poll, set]
  const eff = local != null ? local : poll
  return [eff, set]
}

function Knob({ ctrl, value, edit, color, part, ctx }) {
  const send = useThrottledSend(ctx)
  const [eff, setLocal] = useOptimistic(value)  // #13: drag from what I see, not the last poll
  const vv = eff == null ? 0 : eff
  const t = vv / 127
  const R = 26, C = 34
  const a0 = -225, a1 = 45 // arc sweep degrees
  const ang = a0 + t * (a1 - a0)
  const arc = (from, to) => {
    const p = (deg) => [C + R * Math.cos(deg * Math.PI / 180), C + R * Math.sin(deg * Math.PI / 180)]
    const [x0, y0] = p(from), [x1, y1] = p(to)
    return `M ${x0} ${y0} A ${R} ${R} 0 ${to - from > 180 ? 1 : 0} 1 ${x1} ${y1}`
  }
  const down = (e) => {
    if (edit) return
    e.preventDefault()
    const y0 = e.clientY, v0 = vv
    let lastV = null
    startPointerDrag({
      onMove: (ev) => {
        const v = clampT(Math.round(v0 + (y0 - ev.clientY) * (127 / 150)))
        lastV = v
        setLocal(v)
        send(ctrl.cc, v)
      },
      // Trailing-flush law: up AND cancel both flush the final value.
      onUp: () => { if (lastV != null) send.now(ctrl.cc, lastV) },
      onCancel: () => { if (lastV != null) send.now(ctrl.cc, lastV) },
    })
  }
  const shown = eff == null ? '—'
    : ctrl.labels && ctrl.labels[eff] != null ? ctrl.labels[eff]
    : ctrl.display && ctrl.display.domain === 'raw' ? String(eff)
    : `${Math.round(t * 100)}%`
  return jsxs('div', {
    onPointerDown: down,
    style: { width: CELL, textAlign: 'center', cursor: edit ? 'grab' : 'ns-resize', userSelect: 'none' },
    children: [
      jsx('div', { onClick: partTap(part, 'w'), style: { display: 'inline-block', ...partRing(part, 'w') }, children:
      jsx('svg', { width: 68, height: 68, viewBox: '0 0 68 68', children: jsxs('g', { children: [
        // colored control: thicker track in the quiet hue, value arc in the
        // strong neon version with a glow (owner: FL-mixer-style, not pastel)
        jsx('path', { d: arc(a0, a1), stroke: color ? dim(color) : 'var(--ui-stroke-secondary)', 'stroke-width': color ? 5 : 3, fill: 'none' }),
        jsx('path', { d: arc(a0, ang), stroke: color ? neon(color) : 'var(--ui-accent)', 'stroke-width': color ? 5 : 3, fill: 'none', strokeLinecap: 'round', style: color ? { filter: `drop-shadow(0 0 3px ${neon(color)})` } : undefined }),
        jsx('circle', { cx: C, cy: C, r: 16, fill: 'var(--ui-bg-secondary)', stroke: color ? dim(color) : 'var(--ui-stroke-secondary)' }),
        jsx('line', { x1: C, y1: C, x2: C + 13 * Math.cos(ang * Math.PI / 180), y2: C + 13 * Math.sin(ang * Math.PI / 180), stroke: color ? neon(color) : 'var(--ui-text-primary)', 'stroke-width': 2 }),
      ] }) }) }),
      nameDiv(part, ctrl),
      jsx('div', { style: { fontSize: 10, color: 'var(--ui-text-secondary)' }, children: shown }),
    ] })
}

function Slider({ ctrl, value, edit, color, part, ctx }) {
  const send = useThrottledSend(ctx)
  const [eff, setLocal] = useOptimistic(value)
  const vv = eff == null ? 0 : eff
  const H = 96, W = 34
  const down = (e) => {
    if (edit) return
    e.preventDefault()
    const rect = e.currentTarget.getBoundingClientRect()
    let lastV = null
    const set = (ev) => {
      lastV = clampT(Math.round((1 - (ev.clientY - rect.top) / rect.height) * 127))
      setLocal(lastV)
      send(ctrl.cc, lastV)
    }
    set(e)
    // Listener-hygiene fix (regression review) + review batch: one drag helper owns
    // move/up/cancel; up AND cancel both flush the final value.
    startPointerDrag({
      onMove: set,
      onUp: () => { if (lastV != null) send.now(ctrl.cc, lastV) },
      onCancel: () => { if (lastV != null) send.now(ctrl.cc, lastV) },
    })
  }
  return jsxs('div', {
    onPointerDown: down,
    style: { width: CELL, textAlign: 'center', cursor: edit ? 'grab' : 'pointer', userSelect: 'none' },
    children: [
      jsx('div', { onClick: partTap(part, 'w'), style: { display: 'inline-block', ...partRing(part, 'w') }, children:
      jsxs('svg', { width: W + 12, height: H + 8, viewBox: `0 0 ${W + 12} ${H + 8}`, children: [
        jsx('rect', { x: 12, y: 4, width: 10, height: H, rx: 4, fill: color ? dim(color) + '55' : 'var(--ui-bg-secondary)', stroke: color ? dim(color) : 'var(--ui-stroke-secondary)' }),
        jsx('rect', { x: 12, y: 4 + (H * (1 - vv / 127)), width: 10, height: H * (vv / 127), rx: 4, fill: color ? neon(color) : 'var(--ui-accent)', style: color ? { filter: `drop-shadow(0 0 3px ${neon(color)})` } : undefined }),
        jsx('rect', { x: 6, y: Math.max(0, 4 + H * (1 - vv / 127) - 5), width: 22, height: 10, rx: 3, fill: color ? neon(color) : 'var(--ui-text-secondary)' }),
      ] }) }),
      nameDiv(part, ctrl),
      jsx('div', { style: { fontSize: 10, color: 'var(--ui-text-secondary)' }, children: eff == null ? '—' : `${Math.round(vv / 127 * 100)}%` }),
    ] })
}

function Toggle({ ctrl, value, edit, color, part, ctx }) {
  const [eff, setLocal] = useOptimistic(value)  // #13: second click must not read the stale poll
  const on = (eff || 0) >= 64
  return jsxs('div', {
    onClick: () => { if (!edit) { setLocal(on ? 0 : 127); postCc(ctrl.cc, on ? 0 : 127, ctx) } },
    style: { width: CELL, textAlign: 'center', cursor: 'pointer', userSelect: 'none' },
    children: [
      jsx('div', { onClick: partTap(part, 'w'), style: { ...partRing(part, 'w') }, children:
      jsx('div', { style: {
        width: 52, height: 28, margin: '18px auto 8px', borderRadius: 14,
        border: `1px solid ${on ? (color ? neon(color) : 'var(--ui-accent)') : (color ? dim(color) : 'var(--ui-stroke-secondary)')}`,
        background: on ? (color ? neon(color) : 'var(--ui-accent)') : (color ? dim(color) + '33' : 'var(--ui-bg-secondary)'),
        boxShadow: on && color ? `0 0 6px ${neon(color)}` : 'none', position: 'relative',
      }, children: jsx('div', { style: {
        position: 'absolute', top: 3, left: on ? 27 : 3, width: 20, height: 20, borderRadius: '50%',
        background: 'var(--ui-text-primary)', transition: 'left 80ms',
      } }) }) }),
      nameDiv(part, ctrl),
      jsx('div', { style: { fontSize: 10, color: 'var(--ui-text-secondary)' }, children: on ? 'on' : 'off' }),
    ] })
}

function Momentary({ ctrl, value, edit, color, part, ctx }) {
  const pressed = useRef(false)  // law: release only if WE pressed — hovering must not send 0
  const [eff, setLocal] = useOptimistic(value)
  const send = (v) => { if (!edit) { setLocal(v); postCc(ctrl.cc, v, ctx) } }
  return jsxs('div', {
    onPointerDown: () => { if (edit) return; pressed.current = true; send(127) },
    onPointerUp: () => { if (pressed.current) { pressed.current = false; send(0) } },
    onPointerLeave: () => { if (pressed.current) { pressed.current = false; send(0) } },
    onPointerCancel: () => { if (pressed.current) { pressed.current = false; send(0) } },  // review: touch/pen steal used to latch the button at 127 with no release
    style: { width: CELL, textAlign: 'center', cursor: 'pointer', userSelect: 'none' },
    children: [
      jsx('div', { onClick: partTap(part, 'w'), style: { ...partRing(part, 'w') }, children:
      jsx('div', { style: {
        width: 52, height: 52, margin: '6px auto 8px', borderRadius: 10,
        border: `1px solid ${eff >= 64 ? (color ? neon(color) : 'var(--ui-accent)') : (color ? dim(color) : 'var(--ui-stroke-secondary)')}`,
        background: eff >= 64 ? (color ? neon(color) : 'var(--ui-accent)') : (color ? dim(color) + '33' : 'var(--ui-bg-secondary)'),
        boxShadow: eff >= 64 && color ? `0 0 6px ${neon(color)}` : 'none',
      } }) }),
      nameDiv(part, ctrl),
    ] })
}

function Stepped({ ctrl, value, edit, color, part, ctx }) {
  const [eff, setLocal] = useOptimistic(value)  // #13: rapid +/- must chain, not repeat
  const vv = eff == null ? 0 : eff
  const keys = ctrl.labels ? Object.keys(ctrl.labels).map(Number).sort((a, b) => a - b) : null
  // Deep rinse (#12 law): the invented 12-step fallback was unreachable dead
  // code — renderCtrl already routes a label-less 'stepped' control to a raw
  // Slider (no fake detents). Keep that law in ONE place.
  const steps = keys || []
  const idx = steps.reduce((best, s, i) => Math.abs(s - vv) < Math.abs(steps[best] - vv) ? i : best, 0)
  const bump = (d) => {
    if (edit || !steps.length) return
    const ni = clamp(idx + d, 0, steps.length - 1)
    setLocal(steps[ni])
    postCc(ctrl.cc, steps[ni], ctx)
  }
  const shown = eff == null ? '—' : ctrl.labels ? ctrl.labels[steps[idx]] : String(vv)
  return jsxs('div', {
    style: { width: CELL, textAlign: 'center', userSelect: 'none', cursor: edit ? 'grab' : 'default' },
    children: [
      jsx('div', { onClick: partTap(part, 'w'), style: { ...partRing(part, 'w') }, children:
      jsxs('div', { style: { display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 6, margin: '10px 0 4px' }, children: [
        jsx('button', { onClick: () => bump(-1), style: { border: `1px solid ${color ? dim(color) : 'var(--ui-stroke-secondary)'}`, background: 'transparent', color: 'var(--ui-text-secondary)', width: 22, height: 22, borderRadius: 5, cursor: 'pointer' }, children: '−' }),
        jsx('div', { style: { minWidth: 44, fontSize: 11, color: color ? neon(color) : 'var(--ui-text-primary)', textShadow: color ? `0 0 4px ${neon(color)}` : 'none' }, children: shown }),
        jsx('button', { onClick: () => bump(1), style: { border: `1px solid ${color ? dim(color) : 'var(--ui-stroke-secondary)'}`, background: 'transparent', color: 'var(--ui-text-secondary)', width: 22, height: 22, borderRadius: 5, cursor: 'pointer' }, children: '+' }),
      ] }) }),
      nameDiv(part, ctrl),
    ] })
}

function LabelWidget({ ctrl, color, part }) {
  // B11 (2026-11-10): a w>1 label card reserved the whole
  // span but the text stayed 88px at the left edge — the box now uses its
  // full width and wraps long text inside it.
  const w = Math.max(1, ctrl.w || 1)
  return jsx('div', { style: { width: w > 1 ? w * CELL + (w - 1) * 6 : CELL, textAlign: 'center', paddingTop: 24 },
    children: jsx('div', { onClick: partTap(part, 'w'), style: { display: 'inline-block', fontSize: 11, maxWidth: '100%', whiteSpace: 'normal', color: color ? neon(color) : 'var(--ui-text-secondary)', ...partRing(part, 'w') }, children: ctrl.label }) })
}

// ---------------------------------------------------------------------------
// page
// ---------------------------------------------------------------------------
// Spectrum analyzer v3 (owner batch 2026-11-09, part 2): the visual styles are
// the PORTED '10 horizontal concepts' from the audio-strip chat widget
// (widgets/audio-strip-concepts.html) — bars+peak caps, smooth curve, scrolling
// spectrogram, oscilloscope, center-mirror, LED meters, energy capsule, dot
// matrix, frequency-hue, dual-lane — plus radial/ring bonuses. Ballistics are
// the mf/strip law: instant rise, 0.2 s linear fall, peak-hold 1.8/s.
// Lane A = primary (desktop out, or the picked device's MIDI, or mic when
// source=mic); lane B = microphone — dual styles (led/capsule/dual) show both.
// HONESTY law: audio payload must be FRESH (t within 2.5 s) — a stale
// audio-strip state.json shows 'STALE', never frozen bars pretending to live.
const SPEC_STYLES = ['bars', 'curve', 'spectro', 'oscilloscope', 'mirror', 'led', 'capsule', 'dots', 'hue', 'radial', 'ring']
// regression review debt sweep: /audio is a DISK read on the gateway — per-instance 60 ms
// polling meant 4 analyzers = ~66 redundant file reads/s. One shared poller
// broadcasts to every mounted analyzer; subscribers come and go with cards.
const _audioBus = { subs: new Set(), timer: null, last: null }
function _audioSub(api, fn) {
  _audioBus.subs.add(fn)
  if (!_audioBus.timer) {
    // 33 ms (was 60): publish is now 50 Hz and FFT frames ~47 Hz — polling at
    // 16.7 Hz kept the picture stepping at the poll clock (owner polish ask).
    // One shared poller only; the payload is a ~2 KB cached JSON read.
    _audioBus.timer = setInterval(() => {
      if (_audioBus.busy) return
      _audioBus.busy = true
      api.rest('/audio').then(r => {
        _audioBus.last = (r && !r.error) ? r : null
        // BROADCAST law (owner bug 2026-11-10): updating .last without pushing
        // to subs left every card on its mount-time snapshot — audioFresh died
        // after 2.5 s and every analyzer read 'STALE' forever (MIDI kept
        // moving because /viz still polls per-instance).
        for (const f of Array.from(_audioBus.subs)) { try { f(_audioBus.last) } catch (e) {} }
      }).catch(() => {
        _audioBus.last = null
        for (const f of Array.from(_audioBus.subs)) { try { f(null) } catch (e) {} }
      }).finally(() => { _audioBus.busy = false })
    }, 33)
  }
  if (_audioBus.last) fn(_audioBus.last)  // no cold 60 ms gap for a fresh card
  return () => {
    _audioBus.subs.delete(fn)
    if (!_audioBus.subs.size && _audioBus.timer) { clearInterval(_audioBus.timer); _audioBus.timer = null }
  }
}
// 'dual' retired (owner ruling 2026-11-09): two lanes in one box is redundant —
// just add a second analyzer from SETTINGS and place them side by side.
function SpectrumBox({ api, spec, sources, onPatch, onRemove }) {
  const cvRef = useRef(null)
  const specRef = useRef(spec); specRef.current = spec
  const srcRef = useRef(sources); srcRef.current = sources
  // Resize law: canvas width follows the control's w (grid cells) — pull
  // the card corner and the picture itself grows.
  const CW = Math.max(120, ((spec.w || 4) * CELL + ((spec.w || 4) - 1) * 6) - 2)
  useEffect(() => {
    let alive = true, raf = 0, last = performance.now(), lastAge = Infinity, tcur = 0, lastMotion = -9
    let midi = { sends: [] }, audio = null, audioFresh = false
    let aBusy = false, mBusy = false
    const NB = 64
    const dispA = new Float64Array(NB), dispB = new Float64Array(NB)
    const tgtA = new Float64Array(NB), tgtB = new Float64Array(NB)
    const pkA = new Float64Array(NB)
    let lvlA = 0, lvlB = 0, pkL = 0, ph = Array.from({ length: 8 }, () => Math.random() * 7)
    const specBuf = new Float64Array(256 * NB); let specHead = 0
    const specOff = document.createElement('canvas'); specOff.width = 256; specOff.height = NB
    const specCtx = specOff.getContext('2d'); const specImg = specCtx.createImageData(256, NB)
    const ACC0 = { r: 124, g: 140, b: 255 }, MU = { r: 154, g: 154, b: 176 }, BD = { r: 51, g: 51, b: 51 }, FG = { r: 230, g: 237, b: 243 }
    // Color-mode tint (owner ask 2026-11-09): a picked analyzer paints its
    // bars/curve/meters/lines in the control's color. frequency-hue and the
    // spectrogram keep their own schemes (they ARE a color scheme).
    let ACC = ACC0
    const C = (c) => `rgb(${c.r},${c.g},${c.b})`, CA = (c, a) => `rgba(${c.r},${c.g},${c.b},${a})`
    const STOPS = [[0, [10, 12, 20]], [0.3, [90, 110, 220]], [0.6, [240, 180, 60]], [0.85, [255, 95, 86]], [1, [255, 255, 255]]]
    const heat = (v) => { v = Math.max(0, Math.min(1, v))
      for (let k = 1; k < STOPS.length; k++) if (v <= STOPS[k][0]) { const [a0, ca] = STOPS[k - 1], [b0, cb] = STOPS[k], u = (v - a0) / (b0 - a0)
        return [ca[0] + (cb[0] - ca[0]) * u, ca[1] + (cb[1] - ca[1]) * u, ca[2] + (cb[2] - ca[2]) * u] }
      return STOPS[4][1] }
    // Smoothness law (owner ask 2026-11-09): paint runs on rAF = display
    // refresh (60 fps; 100+ only if the monitor does). Choppiness was the
    // DATA rate, so audio polls at 60 ms and every frame interpolates toward
    // the newest payload — buttery motion even when the publisher is slower.
    const ivA = setInterval(() => {
      if (aBusy) return // in-flight guard: never pile REST calls behind a slow host
      const src = specRef.current.source
      if (src === 'pc' || src === 'mic' || src === 'all') {
        aBusy = true  // shared-poller flag kept for the corner-status law
      } else { audio = null; audioFresh = false }  // source switched off audio: drop the frozen snapshot, or bands() keeps painting the stale frame forever
    }, 60)
    const ivM = setInterval(() => {
      if (mBusy) return
      const src = specRef.current.source
      if (src !== 'pc' && src !== 'mic') {
        mBusy = true
        const bus = (src === 'midi' || src === 'all') ? '*' : ((srcRef.current.find(s => s.value === src) || {}).bus || '')
        api.rest('/viz' + (bus ? '?bus=' + encodeURIComponent(bus) : '')).then(r => { if (alive && r && r.sends) midi = r }).catch(() => {}).finally(() => { mBusy = false })
      }
    }, 100)
    const bands = (k) => (audioFresh && audio && audio[k] && audio[k].live && Array.isArray(audio[k].bands)) ? audio[k].bands : null
    // MIDI loudness law (owner bug 2026-11-09): linear val/127 made a real
    // keybed tap (vel 4-18 on the MicroFreak) a 3% blip — perceived loudness
    // is logarithmic, so use sqrt with a 0.3 floor: ANY played note reads at
    // least 30% of full height, hard hits still reach 100%.
    const amp = (v) => v <= 0 ? 0 : Math.max(0.3, Math.sqrt(v / 127))
    const prevCC = new Map() // CC motion = delta from last seen value, not absolute position
    const paint = (now) => {
      ACC = specRef.current.color ? hexRgb(specRef.current.color) : ACC0
      if (!alive) return
      const dt = Math.min(0.1, (now - last) / 1000); last = now; tcur += dt
      const src = specRef.current.source, style = specRef.current.type === 'dual' ? 'bars' : specRef.current.type
      for (const s of midi.sends) {
        if (s.age >= lastAge || s.age > 1.5) break // newest-first: skip stale history (mount flash)
        if (src !== 'pc' && src !== 'mic') {
          const b = Math.min(NB - 1, (s.num | 0) >> 1)
          const isCC = s.kind === 'cc' || s.kind === 'cc-in'
          let a = 0
          if (isCC) { const p = prevCC.get(s.kind + ':' + s.ch + ':' + s.num); prevCC.set(s.kind + ':' + s.ch + ':' + s.num, s.val | 0); const d = p == null ? (s.val | 0) : Math.abs((s.val | 0) - p); a = d > 0 ? amp(d) : 0 }
          else if ((s.val | 0) > 0) a = amp(s.val | 0) // note-off (val 0) never pulses; sustain decays on its own
          if (a > 0) { dispA[b] = Math.max(dispA[b], a); lastMotion = tcur }
        }
      }
      if (midi.sends.length) lastAge = midi.sends[0].age
      // 'everything' means MERGED (owner video 2026-11-10): the mic lane rode
      // only the aB second channel, which every style except 'led' never
      // paints — analyzers on 'all' silently ignored the mic/MicroFreak even
      // while the lane was live. Fold mic into the main channel by band-max.
      const _o = bands('out'), _m = bands('mic')
      const _mg = () => { if (!_o) return _m; if (!_m) return _o
        const r = new Array(NB); for (let i = 0; i < NB; i++) r[i] = Math.max(_o[i] || 0, _m[i] || 0); return r }
      const aA = src === 'mic' ? _m : src === 'all' ? _mg() : _o
      const aB = src === 'all' ? _m : null
      for (let i = 0; i < NB; i++) { tgtA[i] = aA ? Math.min(1, aA[i] || 0) : 0; tgtB[i] = aB ? Math.min(1, aB[i] || 0) : 0 }
      // Audio keeps the strip law (0.2 s linear fall) but RISES smoothed
      // (~40 ms time constant) so 60 fps frames glide between payloads
      // instead of stepping; MIDI pulses stay instant-rise with ~0.9 s
      // sustain so a played note is watchable, not a blink.
      const rise = 1 - Math.exp(-dt * 24)
      const fall = dt / (aA ? 0.2 : 0.9)
      for (let i = 0; i < NB; i++) {
        dispA[i] = tgtA[i] > dispA[i] ? dispA[i] + (tgtA[i] - dispA[i]) * rise : Math.max(tgtA[i], dispA[i] - fall)
        dispB[i] = tgtB[i] > dispB[i] ? dispB[i] + (tgtB[i] - dispB[i]) * rise : Math.max(tgtB[i], dispB[i] - fall)
        pkA[i] = Math.max(pkA[i] - 1.8 * dt, dispA[i])
      }
      let sa = 0, sb = 0; for (let i = 0; i < NB; i++) { sa += dispA[i] * dispA[i]; sb += dispB[i] * dispB[i] }
      lvlA = Math.min(1, Math.sqrt(sa / NB) * 2.4); lvlB = Math.min(1, Math.sqrt(sb / NB) * 3.2)
      pkL = Math.max(pkL - 0.5 * dt, lvlA)
      const cv = cvRef.current
      if (cv) {
        const g = cv.getContext('2d'), W = cv.width, H = cv.height, bw = W / NB, mid = H / 2
        g.clearRect(0, 0, W, H)
        const rr = (x, y, w2, h2, r) => { g.beginPath(); g.moveTo(x + r, y); g.arcTo(x + w2, y, x + w2, y + h2, r); g.arcTo(x + w2, y + h2, x, y + h2, r); g.arcTo(x, y + h2, x, y, r); g.arcTo(x, y, x + w2, y, r); g.closePath() }
        const ledRow = (y, hh, lv, pklv, warm) => { const n = 42, sw = W / n
          for (let i = 0; i < n; i++) { const f = i / n, on = lv >= f
            g.fillStyle = on ? (f < 0.6 ? (warm ? '#f0b429' : C(ACC)) : '#ff5f56') : CA(BD, 0.4)
            g.fillRect(i * sw + 1, y, sw - 2, hh)
            if (pklv >= f && pklv < f + 1 / n) { g.fillStyle = C(FG); g.fillRect(i * sw + 1, y, sw - 2, hh) } } }
        if (style === 'bars') { // 1 · bars + peak caps
          for (let i = 0; i < NB; i++) { const v = dispA[i] * (H - 6); g.fillStyle = C(ACC); g.fillRect(i * bw, H - v, bw * 0.72, v); g.fillStyle = C(MU); g.fillRect(i * bw, H - Math.max(pkA[i] * (H - 6), 2) - 1.5, bw * 0.72, 1.5) }
        } else if (style === 'curve') { // 2 · smooth mountain + gradient fill
          const pts = []; for (let i = 0; i < NB; i++) pts.push([i / (NB - 1) * W, H - 4 - dispA[i] * (H - 10)])
          const grad = g.createLinearGradient(0, 0, 0, H); grad.addColorStop(0, CA(ACC, 0.8)); grad.addColorStop(1, CA(ACC, 0.06))
          g.beginPath(); g.moveTo(0, H); g.lineTo(pts[0][0], pts[0][1])
          for (let i = 1; i < pts.length - 1; i++) { const mx = (pts[i][0] + pts[i + 1][0]) / 2, my = (pts[i][1] + pts[i + 1][1]) / 2; g.quadraticCurveTo(pts[i][0], pts[i][1], mx, my) }
          g.lineTo(W, pts[NB - 1][1]); g.lineTo(W, H); g.closePath(); g.fillStyle = grad; g.fill()
          g.beginPath(); g.moveTo(pts[0][0], pts[0][1])
          for (let i = 1; i < pts.length - 1; i++) { const mx = (pts[i][0] + pts[i + 1][0]) / 2, my = (pts[i][1] + pts[i + 1][1]) / 2; g.quadraticCurveTo(pts[i][0], pts[i][1], mx, my) }
          g.lineTo(W, pts[NB - 1][1]); g.strokeStyle = C(ACC); g.lineWidth = 1.5; g.stroke()
        } else if (style === 'spectro') { // 3 · scrolling spectrogram, newest right
          for (let i = 0; i < NB; i++) specBuf[specHead * NB + i] = dispA[i]
          specHead = (specHead + 1) % 256
          for (let x = 0; x < 256; x++) { const col = (specHead + x) % 256
            for (let b = 0; b < NB; b++) { const hx = heat(specBuf[col * NB + b]); const o = ((NB - 1 - b) * 256 + x) * 4
              specImg.data[o] = hx[0]; specImg.data[o + 1] = hx[1]; specImg.data[o + 2] = hx[2]; specImg.data[o + 3] = 255 } }
          specCtx.putImageData(specImg, 0, 0); g.imageSmoothingEnabled = false; g.drawImage(specOff, 0, 0, 256, NB, 0, 0, W, H)
        } else if (style === 'oscilloscope') { // 4 · time-domain line with glow (synth from bands)
          g.strokeStyle = C(BD); g.beginPath(); g.moveTo(0, mid); g.lineTo(W, mid); g.stroke()
          const fs = [60, 120, 240, 480, 720, 960, 1200, 1500]
          const amps = fs.map((f) => { const bi = Math.round(NB * Math.log(f / 24) / Math.log(20000 / 24)); return dispA[Math.min(NB - 1, bi)] })
          g.beginPath()
          for (let x = 0; x <= W; x += 2) { const tt = x / W * 0.06; let y = 0
            for (let j = 0; j < fs.length; j++) y += amps[j] * Math.sin(2 * Math.PI * fs[j] * tt + ph[j] + tcur * fs[j] * 0.02)
            const yy = mid - y * (mid - 4) * 0.5; x === 0 ? g.moveTo(x, yy) : g.lineTo(x, yy) }
          g.strokeStyle = C(ACC); g.lineWidth = 1.4; g.shadowColor = C(ACC); g.shadowBlur = 4; g.stroke(); g.shadowBlur = 0
        } else if (style === 'mirror') { // 5 · center-mirror bars
          for (let i = 0; i < NB; i++) { const v = dispA[i] * (mid - 2); g.fillStyle = C(ACC); g.fillRect(i * bw, mid - v, bw * 0.72, v); g.fillRect(i * bw, mid, bw * 0.72, v) }
        } else if (style === 'led') { // 6 · LED meters: A row (+ B row when mic lane exists)
          if (aB) { ledRow(4, mid - 7, lvlA, pkL, false); ledRow(mid + 3, mid - 7, lvlB, lvlB, true) }
          else ledRow(4, H - 8, lvlA, pkL, false)
        } else if (style === 'capsule') { // 7 · energy capsule (A fill, B dashed ghost)
          const hh = Math.min(22, H - 8), y = (H - hh) / 2
          g.fillStyle = CA(BD, 0.33); rr(2, y, W - 4, hh, hh / 2); g.fill()
          const grad = g.createLinearGradient(0, 0, W, 0); grad.addColorStop(0, CA(ACC, 1)); grad.addColorStop(0.7, '#f0b429'); grad.addColorStop(1, '#ff5f56')
          g.fillStyle = grad; rr(2, y, 14 + lvlA * (W - 20), hh, hh / 2); g.fill()
          if (aB) { g.strokeStyle = C(MU); g.setLineDash([4, 4]); rr(2, y - 4, 14 + lvlB * (W - 20), hh + 8, (hh + 8) / 2); g.stroke(); g.setLineDash([]) }
        } else if (style === 'dots') { // 8 · dot-matrix bars
          const cell = 6, cols = Math.floor(W / cell), rowsN = Math.floor((H - 4) / cell)
          for (let x = 0; x < cols; x++) { const bi = Math.min(NB - 1, Math.floor(x / cols * NB)), lit = Math.round(dispA[bi] * rowsN)
            for (let r = 0; r < rowsN; r++) { g.fillStyle = r < lit ? (r > rowsN * 0.8 ? '#ff5f56' : r > rowsN * 0.6 ? '#f0b429' : C(ACC)) : CA(BD, 0.27)
              g.beginPath(); g.arc(x * cell + cell / 2, H - 2 - (r + 0.5) * cell, 1.7, 0, 7); g.fill() } }
        } else if (style === 'hue') { // 9 · frequency-hue bars (bass→treble)
          for (let i = 0; i < NB; i++) { const v = dispA[i] * (H - 4); g.fillStyle = `hsl(${(i / NB) * 270} 82% ${38 + 27 * dispA[i]}%)`; g.fillRect(i * bw, H - v, bw * 0.8, v) }
        } else if (style === 'radial') {
          const r0 = Math.min(W, H) * 0.18
          for (let i = 0; i < NB; i++) { const a = (i / NB) * Math.PI * 2, r = r0 + dispA[i] * (mid - r0 - 2)
            g.strokeStyle = C(ACC); g.lineWidth = 1.5; g.beginPath(); g.moveTo(W / 2 + Math.cos(a) * r0, H / 2 + Math.sin(a) * r0); g.lineTo(W / 2 + Math.cos(a) * r, H / 2 + Math.sin(a) * r); g.stroke() }
        } else { // ring
          g.strokeStyle = C(ACC); g.lineWidth = 3
          g.beginPath(); g.arc(W / 2, H / 2, (Math.min(W, H) / 2 - 4) * Math.max(0.05, lvlA), 0, 7); g.stroke()
        }
        // honest corner status — never silently frozen
        const wantAudio = src === 'pc' || src === 'mic' || src === 'all'
        const wantMidi = src !== 'pc' && src !== 'mic'
        const st = wantAudio && !audioFresh ? (src === 'mic' ? 'mic: no feed' : 'STALE — audio-strip off?')
          : wantMidi && !aA && tcur - lastMotion > 2 ? 'silent' : 'live'
        g.font = '8px sans-serif'; g.fillStyle = CA(FG, 0.55); g.textAlign = 'right'; g.fillText(st, W - 4, H - 3); g.textAlign = 'left'
      }
      raf = requestAnimationFrame(paint)
    }
    raf = requestAnimationFrame(paint)
    const unsubAudio = _audioSub(api, (r) => {
      if (!alive) return
      const src = specRef.current.source
      if (src !== 'pc' && src !== 'mic' && src !== 'all') return
      if (r) { audio = r; audioFresh = (Date.now() / 1000 - (r.t || 0)) < 2.5 } else { audio = null; audioFresh = false }
    })
    return () => { alive = false; unsubAudio(); clearInterval(ivA); clearInterval(ivM); cancelAnimationFrame(raf) }
  }, [api])
  // Settings wheel in the top-right corner of the box (owner ask 2026-11-09):
  // source, style and remove live in an OPAQUE popup (pop-down law) — the box
  // itself stays clean and draggable.
  const [gear, setGear] = useState(false)
  const gearWrapRef = useRef(null)
  useEffect(() => {
    if (!gear) return
    const off = (e) => { if (gearWrapRef.current && !gearWrapRef.current.contains(e.target)) setGear(false) }
    window.addEventListener('pointerdown', off)
    return () => window.removeEventListener('pointerdown', off)
  }, [gear])
  return jsx('span', { ref: gearWrapRef, style: { position: 'relative', display: 'inline-block', alignSelf: 'flex-start' }, children: [
    jsx('canvas', { ref: cvRef, width: CW, height: 64, 'data-tip': 'Live spectrum — ' + (((sources || []).find(s => s.value === spec.source) || {}).label || spec.source), style: { display: 'block', border: '1px solid var(--ui-stroke-secondary)', borderRadius: 8, background: 'rgba(0,0,0,.25)' } }),
    jsx('button', { onPointerDown: (e) => e.stopPropagation(), onClick: (e) => { e.stopPropagation(); setGear(!gear) }, 'data-tip': 'Analyzer settings — source, style, remove', style: { position: 'absolute', top: 4, right: 4, width: 18, height: 18, padding: 0, borderRadius: 5, fontSize: 11, lineHeight: '16px', background: 'rgba(18,22,28,.8)', color: 'var(--ui-text-primary)', border: '1px solid var(--ui-stroke-secondary)', cursor: 'pointer' }, children: '⚙' }),
    gear ? jsxs('span', { onPointerDown: (e) => e.stopPropagation(), // popup lives inside the drag wrapper — without this, startCardDrag's preventDefault kills the native <select> dropdowns (Chromium suppresses the compat mousedown)
      style: { position: 'absolute', zIndex: 60, display: 'flex', flexDirection: 'column', gap: 5, padding: 8, borderRadius: 8, background: '#242c36', border: '1px solid #3d444d', boxShadow: '0 8px 20px rgba(0,0,0,.55)', ...(gearWrapRef.current && window.innerWidth - gearWrapRef.current.getBoundingClientRect().right > 230 ? { left: CW + 8, top: 0 } : { top: 70, right: 0 }) }, children: [
      jsx('select', { value: spec.source, onChange: (e) => onPatch(spec.id, { source: e.target.value }), 'data-tip': 'What this analyzer listens to — a device on this screen, all MIDI, desktop audio, microphone, or everything', style: { ...tinyS, fontSize: 10, padding: '3px 5px' }, children: sources.map(s => jsx('option', { value: s.value, children: s.label }, s.value)) }),
      jsx('select', { value: spec.type, onChange: (e) => onPatch(spec.id, { type: e.target.value }), 'data-tip': 'Visual style — the 10 ported strip concepts + radial/ring', style: { ...tinyS, fontSize: 10, padding: '3px 5px' }, children: SPEC_STYLES.map(t => jsx('option', { value: t, children: t }, t)) }),
      jsx('button', { onClick: () => { setGear(false); onRemove(spec.id) }, 'data-tip': 'Remove this analyzer', style: { ...tinyS, fontSize: 10, padding: '3px 5px', color: '#ff5f56' }, children: '× remove' }),
    ] }, 'gearpop') : null,
  ] })
}

// MIDI piano (owner ask 2026-11-09): keybed sized to the REGISTERED synth —
// the profile's 'keyboard' key decides the key count and lowest note, so the
// MicroFreak preset shows its exact 25 keys (C3 up), geometry nipped from the
// mf-visualizer keybed (blacks drawn LAST so they win the click). Keys light
// on live notes (the real synth lighting them lights these), and clicking a
// key plays it on the active profile's channel.
const KEY_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
const isBlackKey = (n) => [1, 3, 6, 8, 10].includes(((n % 12) + 12) % 12)
function PianoBox({ first, keys, notes, onNote }) {
const [held, setHeld] = useState({})
// Typing-keyboard law (LMMS "MIDI or typing keyboard", ours): the piano is
// playable WITHOUT a synth in the loop — demo mode. Tracker layout anchored
// C4-ish inside the profile's keyboard block; − / = shift octave. Keys never
// fire while a text input/select has focus, and every handled key down is
// paired with its own key up (held-per-code, refcounted with the mouse).
const onNoteRef = useRef(onNote)
onNoteRef.current = onNote
const octRef = useRef(0)
const lit = new Set(notes.map(n => n.note))
for (const k of Object.keys(held)) lit.add(+k)
const W = 640, H = 110
const all = []
for (let n = first; n < first + keys; n++) all.push(n)
const whites = all.filter(n => !isBlackKey(n))
const ww = W / whites.length
// regression guard UI3: the mouse path read `held` from the RENDER CLOSURE (a fast
// tap whose repaint had not landed answered "not held" and never sent the
// note-off — stuck notes) and DELETED the whole refcount (a key held by both
// the typing keyboard and the mouse died when the mouse let go). ONE
// synchronous refcount ref feeds BOTH paths now; pointer-cancel releases too.
const heldRef = useRef({})
// A5 (): the note-OFF followed whatever onNote was
// CURRENT at release time — if the profile/channel/bus changed while a key
// was held, the note-on's route was lost and the old synth kept a stuck
// note. Press captures the exact closure that sent the note-on; release
// goes back down that same wire. Window blur releases everything held.
const offFnsRef = useRef({})
const press = (n) => { heldRef.current[n] = (heldRef.current[n] || 0) + 1; if (heldRef.current[n] === 1) offFnsRef.current[n] = onNoteRef.current; setHeld({ ...heldRef.current }); onNoteRef.current(n, 100) }
const release = (n) => { const c = heldRef.current[n] || 0; if (!c) return; if (c > 1) heldRef.current[n] = c - 1; else { delete heldRef.current[n]; const off = offFnsRef.current[n] || onNoteRef.current; delete offFnsRef.current[n]; off(n, 0) } setHeld({ ...heldRef.current }) }  // drag-across phantom note-offs stay refused (count 0 -> no-op)
const keyEv = (n) => ({ onPointerDown: (e) => { e.preventDefault(); press(n) }, onPointerUp: () => release(n), onPointerLeave: () => release(n), onPointerCancel: () => release(n), style: { cursor: 'pointer' } })
useEffect(() => {
  const TYPE = { KeyZ: 0, KeyX: 2, KeyC: 4, KeyV: 5, KeyB: 7, KeyN: 9, KeyM: 11, Comma: 12, Period: 14, Slash: 16, KeyS: 1, KeyD: 3, KeyG: 6, KeyH: 8, KeyJ: 10, KeyL: 13, Semicolon: 15 }
  const typeHeld = {}
  const down = (ev) => {
    if (ev.repeat || ev.ctrlKey || ev.metaKey || ev.altKey) return
    const el = document.activeElement
    if (el && (el.tagName === 'INPUT' || el.tagName === 'SELECT' || el.tagName === 'TEXTAREA' || el.isContentEditable)) return
    if (ev.code === 'Minus' || ev.code === 'Equal') { octRef.current = Math.max(-3, Math.min(3, octRef.current + (ev.code === 'Equal' ? 1 : -1))); ev.preventDefault(); return }
    const off = TYPE[ev.code]
    if (off == null || typeHeld[ev.code] !== undefined) return
    // regression guard UI2: the anchor sat on the TOP key (first+24), so on the
    // 25-key MicroFreak range EVERY positive offset clamped onto one note —
    // the whole typing piano was a single-key machine (all whites = 72).
    // Anchor at the block BOTTOM: the z../row spans first..first+16, inside
    // any 17+ key instrument, every key distinct; octaves shift within range.
    const anchor = first + octRef.current * 12
    const n = Math.max(first, Math.min(first + keys - 1, anchor + off))
    typeHeld[ev.code] = n
    press(n)
    ev.preventDefault()
  }
  const up = (ev) => {
    const n = typeHeld[ev.code]
    if (n === undefined) return
    delete typeHeld[ev.code]
    release(n)
  }
  window.addEventListener('keydown', down)
  window.addEventListener('keyup', up)
  // A5 (): alt-tab / window focus steal used to strand every held
  // note (no keyup ever arrives) — blur releases everything held, both paths.
  const blurRelease = () => { for (const k of Object.keys(heldRef.current)) release(+k) }
  window.addEventListener('blur', blurRelease)
  return () => { window.removeEventListener('keydown', down); window.removeEventListener('keyup', up); window.removeEventListener('blur', blurRelease); for (const code of Object.keys(typeHeld)) { const n = typeHeld[code]; delete typeHeld[code]; release(n) } }
}, [first, keys])
// React jsx-runtime law (piano bug 2026-11-09): children go INSIDE props —
// jsx(type, props, key) ignores a 4th argument, so the old 4-arg calls
// rendered every <g> empty (invisible piano).
const whitesEls = whites.map((n, i) => jsxs('g', { ...keyEv(n), children: [
  jsx('rect', { x: i * ww, y: 0, width: ww - 1, height: H, rx: 2, fill: lit.has(n) ? '#0091ff' : '#e8e8ec', stroke: '#9a9aa2', strokeWidth: 0.5 }),
  n % 12 === 0 ? jsx('text', { x: i * ww + ww / 2, y: H - 8, textAnchor: 'middle', fontSize: 8, fill: lit.has(n) ? '#fff' : '#777', children: 'C' + (Math.floor(n / 12) - 1) }) : null,
] }, 'w' + n))
const blackEls = all.filter(isBlackKey).map(n => {
  const wi = whites.filter(w => w < n).length
  const bw = ww * 0.62, bh = H * 0.62
  return jsxs('g', { ...keyEv(n), children: [
    jsx('rect', { x: wi * ww - bw / 2, y: 0, width: bw, height: bh, rx: 2, fill: lit.has(n) ? '#0091ff' : '#17171b', stroke: '#000' }),
  ] }, 'b' + n)
})
return jsx('svg', { width: W, height: H, 'data-tip': "Typing keyboard: Z X C V B N M , . = whites · S D G H J = blacks · − / = octave. Plays even with no synth connected", style: { touchAction: 'none', display: 'block', marginTop: 8 }, children: [...whitesEls, ...blackEls] })
}


function Surface() {
  const [st, setSt] = useState(null)          // /state
  const [profLive, setProfLive] = useState(null) // active profile as saved on the backend
  // Lost-update law (review 2026-11-09): back-to-back non-edit mutates each
  // re-based off a profLive that only refreshes AFTER the save round trip —
  // the second save resurrected pre-first-save state. profLiveRef carries the
  // last SAVED doc immediately, so consecutive edits stack instead of clobber.
  const profLiveRef = useRef(null)
  // Draft law (owner ruling): edit mode works on a LOCAL draft. Nothing hits
  // the backend until "Save to profile"; leaving edit mode with unsaved edits
  // prompts Save / Discard. Undo/Redo are pure local draft history.
  const [draft, setDraft] = useState(null)
  // Deep rinse: deferred callbacks (the 350 ms wheel debounce) must never
  // commit against the STALE draft captured at scheduling time — every
  // setDraft goes through putDraft so draftRef always holds the live doc.
  const draftRef = useRef(null)
  const putDraft = (d) => { draftRef.current = d; setDraft(d) }
  const prof = draft || profLive
  // Review: dirty deep-compares both docs — memoize so it runs only when a
  // doc actually changes, not on every 600 ms poll re-render.
  const dirty = useMemo(
    () => draft != null && profLive != null && JSON.stringify(canon(draft)) !== JSON.stringify(canon(profLive)),
    [draft, profLive])
  const [exitAsk, setExitAsk] = useState(false)   // unsaved-exit prompt open
  const [colorMode, setColorMode] = useState(false) // color-selection sub-mode of edit
  const [hexIn, setHexIn] = useState('')            // hex color box (color mode)
  const [presetMode, setPresetMode] = useState('decks') // PRESET box: 'decks' | 'add'
  const [createMenu, setCreateMenu] = useState(false) // edit-mode Create dropdown
  // Review 2026-11-09: pane prefs belong in the plugin-scoped storage (api.
  // storage), not raw localStorage — an un-scoped key can collide with any
  // other plugin's name.
  const [barHidden, setBarHidden] = useState(() => { try { return api.storage.get('bar') === '1' } catch (e) { return false } }) // PRESET/MIDI/PROFILE bar tucked away
  // Spectrum analyzers v2 (owner batch 2026-11-09): a LIST of instances, each
  // {id, source, type}; persisted. Toggle in SETTINGS adds/removes them.
  const [specN, setSpecN] = useState(1)                 // SETTINGS number box: how many analyzers to insert
  // Analyzer-as-control (owner ask 2026-11-09): spectrum boxes are profile
  // controls (widget 'analyzer', w=4 grid cells) — draggable like knobs,
  // assignable into a category or between knobs; no section = toolbar home.
  const specCtrls = prof ? Object.entries(prof.controls || {}).filter(([, c]) => c.widget === 'analyzer') : []
  const addAnalyzers = (n, src) => {
    const have = Object.values((prof && prof.controls) || {}).filter(c => c.widget === 'analyzer').length
    const k = Math.min(n, 4 - have)
    if (k <= 0) { setStatus('four analyzers is the cap — remove one before adding more'); return }
    mutate(p => {
      for (let i = 0; i < k; i++) {
        if (Object.values(p.controls).filter(c => c.widget === 'analyzer').length >= 4) break
        p.controls['spec-' + Date.now().toString(36) + '-' + i] = { label: 'spectrum', widget: 'analyzer', source: src || (i === 0 ? 'midi' : 'pc'), type: 'bars', w: 4 }
      }
    }, `added ${k} analyzer(s)${k < n ? ' — cap is 4' : ''}`)  // review: said "added N" even when the cap ate some
  }
  const rmAnalyzers = () => mutate(p => { Object.keys(p.controls).forEach(cid => { if (p.controls[cid].widget === 'analyzer') delete p.controls[cid] }) }, 'removed analyzer(s)')
  const patchSpecCtrl = (cid, q) => mutate(p => { const t = p.controls[cid]; if (t) Object.assign(t, q) }, 'analyzer setting')
  const migratedRef = useRef(false)
  useEffect(() => {
    // One-time migration: old localStorage toolbar specs become real controls
    // in the preset (survive export, per-device, draggable).
    if (migratedRef.current || !profLive) return
    migratedRef.current = true
    let legacy = []
    try { legacy = JSON.parse(localStorage.getItem('zmidi_specs') || '[]') } catch { /* junk */ }
    localStorage.removeItem('zmidi_specs')
    legacy = legacy.filter(s => s && s.source && s.type)
    if (legacy.length && !Object.values(profLive.controls || {}).some(c => c.widget === 'analyzer'))
      mutate(p => { legacy.slice(0, 4).forEach((s, i) => { p.controls['spec-mig-' + i] = { label: 'spectrum', widget: 'analyzer', source: s.source, type: s.type === 'dual' ? 'bars' : s.type, w: 4 } }) }, 'migrated toolbar analyzers into the preset')
  }, [profLive])
  const [ignoredOpen, setIgnoredOpen] = useState(false) // SETTINGS ignored-inputs flyout
  // Measured category boxes (owner bug 2026-11-09): the story packer used
  // ESTIMATED heights, so categories "didn't fit" slots that clearly had
  // room and snapped elsewhere. Measure the real DOM boxes after each render
  // and pack with truth; re-render only when a measurement actually moves.
  const secH = useRef({})
  const [, setSecV] = useState(0)
  useEffect(() => {
    if (!edit) return  // B12 (): this measured EVERY category after EVERY render — the 4/s polls included. Category geometry only matters for category drags, which only exist in edit mode.
    const next = {}
    ;(rootRef.current || document).querySelectorAll('[data-secblock]').forEach(b => {
      const r = b.getBoundingClientRect()
      next[b.getAttribute('data-secblock')] = [r.height, r.width]
    })
    let changed = false
    for (const k in next) { const p = secH.current[k]; if (!p || Math.abs(p[0] - next[k][0]) > 2 || Math.abs(p[1] - next[k][1]) > 2) { changed = true; break } }
    if (changed) { secH.current = { ...secH.current, ...next }; setSecV(v => v + 1) }
  })
  const [audDev, setAudDev] = useState(null)            // audio-strip lane device names {out, mic} — real labels in the source menu
  const doDiscard = () => {
    // Discard law (owner ruling 2026-11-09): throw the draft back to the saved
    // doc — but the pre-discard state goes onto the UNDO stack, so Undo
    // brings the edits straight back. Pending wheel paints die first.
    clearTimeout(saveTimerRef.current)
    if (!prof || draftRef.current == null) return
    undoRef.current.push(JSON.parse(JSON.stringify(draftRef.current)))
    if (undoRef.current.length > 50) undoRef.current.shift()
    redoRef.current = []
    putDraft(JSON.parse(JSON.stringify(profLive)))  // discard = back to the SAVED doc (prof aliases the draft in edit mode — the old line discarded nothing)
    setStatus('edits discarded — Undo brings them back')
  }
  // audio-strip lane device names for the source menu (5 s file-read poll is
  // nothing; while audio-strip is disabled the labels simply stay plain).
  useEffect(() => {
    let alive = true
    const poll = () => api.rest('/audio').then(r => { if (alive && r && !r.error) setAudDev({ out: (r.out || {}).src || '', mic: (r.mic || {}).src || '' }) }).catch(() => {})
    poll(); const iv = setInterval(poll, 5000)
    return () => { alive = false; clearInterval(iv) }
  }, [])
  const [menu, setMenu] = useState(null)          // open dropdown: 'edit' | 'settings' | null
  const [manage, setManage] = useState(null)      // SETTINGS manage card: 'profiles' | 'devices' | null
  // Chat box (owner feature 2026-11-09): natural-language preset editing. The
  // model only proposes ops (chat_engine whitelists them); this half holds all
  // the power — labels resolve against the live doc and every op runs through
  // mutate's choke point, so draft/undo/hot-apply laws apply.
  //
  // Catalog review (PR 153102): the box no longer lists model lanes or holds a
  // model pick. It calls its own Python half over a plugin RPC; Hermes keeps the
  // provider credentials and routes through the `midi_chat` auxiliary slot,
  // which follows the app's current model until the user pins one under
  // Settings → Models → Auxiliary. The proposal comes back as a session event
  // keyed by job id, so chatJobs maps the pending asks.
  const [chat, setChat] = useState(false)
  const [chatLog, setChatLog] = useState([])      // {role:'user'|'say'|'done'|'err', text}
  const [chatIn, setChatIn] = useState('')
  const [chatBusy, setChatBusy] = useState(false)
  const [chatEffort, setChatEffort] = useState('auto')  // 'auto'|'fast'|'balanced'|'thoughtful' — persisted 'chat_effort'
  const chatJobs = useRef({})                     // job id -> resolve (filled by the chat.answered session event)
  const [mEdit, setMEdit] = useState(null)        // {kind:'rename'|'copy'|'dev', pid, val} inline input
  const [mArm, setMArm] = useState(null)          // preset id armed for delete (second click deletes)
  const menuWrapRef = useRef(null)                // wraps EDIT + SETTINGS so outside clicks close them
// Context-menu law (owner ruling 2026-11-09): dropdowns pop DOWNWARD under
  // their button and die on any click outside the menu group.
  useEffect(() => {
    if (!menu && !createMenu) return
    const h = (e) => { if (menuWrapRef.current && !menuWrapRef.current.contains(e.target)) { setMenu(null); setCreateMenu(false); setIgnoredOpen(false) } }
    window.addEventListener('pointerdown', h)
    return () => window.removeEventListener('pointerdown', h)
  }, [menu, createMenu])
  const [csel, setCsel] = useState({})            // colorable part keys -> true
  const [catSel, setCatSel] = useState({})     // sid -> true: category select-all is ON (honest dot)
  const [allSel, setAllSel] = useState(false)  // global select-all is ON
  const [secSel, setSecSel] = useState({})     // sid -> true: category picked in MOVE mode (multi-select; drag moves the group)
  const [profiles, setProfiles] = useState([])
  const [devices, setDevices] = useState([])
  const [devPick, setDevPick] = useState('')
  const [edit, setEdit] = useState(false)
  const [dismissed, setDismissed] = useState(() => { try { return api.storage.get('ignored_ccs') || {} } catch (e) { return {} } }) // channel:cc -> true; persistent (owner ruling): ignores survive reconnects+reloads until restored from the Ignored list
  const [rebind, setRebind] = useState(null)     // {cid, seq} — armed: the next panel CC with seq > rebind.seq rebinds cid (deep rinse: comment said ts — the #2 fix compares the EVENT SEQ cursor, not a timestamp)
  const [mx, setMx] = useState({ src: 'lfo', tgt: '', depth: 0.5, wave: 'sine', rate: 2 })
  const [takes, setTakes] = useState(null)      // saved .mid takes (recorder library)
  const [recName, setRecName] = useState('')
  const [recBpm, setRecBpm] = useState('120')  // review: raw STRING while typing — parseFloat||120 snapped the box back mid-edit (couldn't blank/retype)
  const [status, setStatus] = useState('')
  const [busy, setBusy] = useState(false)
  // Deck (2026-11-09): profile docs for the surfaces AFTER the active one.
  // Fetched once per id (the 600 ms poll must not re-read every doc); a
  // forced refresh invalidates the cache so saves show up immediately.
  const [deckDocs, setDeckDocs] = useState({})
  const deckDocsRef = useRef({})
  const deckGenRef = useRef(0)  // A10 (): deck docs are invalidated by SAVES/ACTIVATES, not by every 250 ms poll — the shared genRef made any fetch slower than one poll interval discard its own result forever (starvation: refetch, discard, refetch…)
  // Deck-write law (review 2026-11-09): /deck is a full-list replace built
  // from the 600 ms-poll closure — two quick deck actions raced and the
  // second dropped the first's entry. deckRef carries the last acknowledged
  // list; every deck action rebases off it. genRef drops stale merges from
  // refreshes that were in flight when a force-clear happened.
  const deckRef = useRef([])
  const genRef = useRef(0)
  // Electron renders NO window.prompt (it silently returns null — that killed
  // rename/+Control/rebind-CC/Import for the pane's whole life). Everything
  // that needs typed input is inline UI now.
  const [renaming, setRenaming] = useState(null)  // {cid, val} — inline rename
  const [newCtl, setNewCtl] = useState(null)      // {cc, name} — inline +Control form
  const [newProf, setNewProf] = useState(null)    // string — inline +Profile name
  const fileRef = useRef(null)                    // hidden file input for Import
  const rootRef = useRef(null)                    // pane root — geometry scans scope to this (review)
  // Edit-mode move law (owner ruling): rubber-band multi-select, group drag
  // with a live drop outline before release; order-based placement (the flex
  // grid IS the order — x/y get normalized to display order on commit).
  const [sel, setSel] = useState({})              // cid -> true
  const [band, setBand] = useState(null)          // rubber band {x0,y0,x1,y1}
  const [drag, setDrag] = useState(null)          // {cids, px, py, over:{sid,idx}|null}
  // Drag-freshness law (owner polish ask 2026-11-10): during a drag the live
  // pointer position + target cell live in REFS — the fixed overlays are
  // moved imperatively every frame, and a state update (full Surface
  // re-render) fires ONLY when the drop cell actually changes. Renders read
  // the refs so any unrelated re-render (status poll) still paints the
  // overlay at the LIVE pointer, never a stale px/py jump.
  const dragPosRef = useRef(null)   // {px,py} while a card drag is in flight
  const dragCellRef = useRef('')    // last committed drop-cell key
  const grabRef = useRef(null)      // B5/B6 (owner ruling 2026-11-10): {dx,dy} grab offset inside the dragged group — the grabbed edge follows the finger
  const secPosRef = useRef(null)    // {px,py} while a category drag is in flight
  const secSnapRef = useRef(null)   // FROZEN grid geometry (bands+columns px) captured at pointerdown — mid-drag targeting never touches the DOM
  const secCellRef = useRef('')
  const bandPosRef = useRef(null)   // {x1,y1} while the rubber band is open
  // categories (sections) as first-class objects (owner ask): rename, label
  // size/bold, color, and whole-category drag-to-move (controls follow)
  const [secEdit, setSecEdit] = useState(null)    // {sid, val} — inline rename
  const [secDrag, setSecDrag] = useState(null)    // {sid, px, py, over}
  const [newSec, setNewSec] = useState(null)      // string — inline +Category name
  // #13: widget send failures reach the status line. Deep rinse: installed in
  // an effect (with unmount cleanup) instead of as a render-body side effect.
  useEffect(() => {
    onSendError = (m) => setStatus(m)
    // Review: a pending 350 ms wheel paint must die with the pane too.
    return () => { onSendError = () => {}; clearTimeout(saveTimerRef.current) }
  }, [])

  // Review: /state already carries active_profile — re-fetching the profile
  // doc every 600 ms was pure churn (backend file IO + a fresh object
  // identity per tick, which re-armed the dirty compare). Re-fetch only when
  // the active id CHANGES or a caller forces it (after any save/mutate).
  const lastActiveIdRef = useRef(undefined)
  const deckPendingRef = useRef(0)  // in-flight /deck POSTs — the 600 ms poll must not write its pre-write snapshot back over the last-ack deck
  const refresh = useCallback(async (force) => {
    try {
      const gen = ++genRef.current
      const s = await api.rest('/state')
      setSt(s)
      if (!deckPendingRef.current) deckRef.current = ((s && s.deck) || []).map(d => d.profile_id)  // last acknowledged deck — deck actions rebase off this
      if (force) { deckDocsRef.current = {}; deckGenRef.current++ }  // saves/activates invalidate deck docs
      const aid = (s && s.active_profile) || null
      if (force || aid !== lastActiveIdRef.current) {
        lastActiveIdRef.current = aid
        const pa = await api.rest('/profile/active')
        // A3 (): an out-of-order /profile/active used to
        // re-poison profLive with a SUPERSEDED profile — and lastActiveIdRef
        // already named the newer id, so no later poll repaired it (edits then
        // hit the wrong doc). Re-check after the await: the newest aid always
        // commits, so unlike a generation check this cannot starve.
        if (aid === lastActiveIdRef.current) {
          setProfLive(pa && pa.profile ? pa.profile : null)
          profLiveRef.current = (pa && pa.profile) || null
          CH = Number(pa && pa.profile && pa.profile.channel) || 0
        }
      }
      // Deck: secondary surfaces need their profile docs — fetch only ids
      // not already held. Invalid entries (valid:false from /state) are NOT
      // re-fetched every poll — they render a named card with a remove
      // button instead (review: endless 600 ms re-read of a dead id).
      const missing = ((s && s.deck) || []).slice(1).filter(d => d.valid !== false).map(d => d.profile_id)
        .filter(id => id && !deckDocsRef.current[id])
      if (missing.length) {
        const dgen = deckGenRef.current
        const got = {}
        for (const id of missing) {
          const r = await api.rest('/profile/get?id=' + encodeURIComponent(id)).catch(() => null)
          if (r && !r.error) got[id] = r
        }
        // Generation guard: a force-clear while this fetch was in flight must
        // not be re-poisoned by the older result merging back in. A10: this
        // uses the deck-docs invalidation counter, NOT the per-poll gen —
        // ordinary polls must not discard their own slow-but-valid results.
        if (Object.keys(got).length && dgen === deckGenRef.current) {
          deckDocsRef.current = { ...deckDocsRef.current, ...got }
          setDeckDocs(deckDocsRef.current)
        }
      }
    } catch (e) { setStatus('backend unreachable — is the plugin enabled + backend restarted?') }
  }, [])

  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 250)  // 4 Hz (was 600 ms): hardware echo -> panel latency is this clock; settle-ease smooths between ticks (regression review option D pair)
    return () => clearInterval(t)
  }, [refresh])

  // Seamlessness law: remember the last port, auto-connect on pane open.
  // Deck: the remembered port is keyed per bus (legacy key = primary).
  // Review 2026-11-09: (a) the bus id derives from profLive — reading it
  // before the first /profile/active landed always looked up the PRIMARY
  // key, so a secondary-bus device never auto-connected; (b) one consumed
  // attempt meant a cold-start 502 killed auto-connect for the whole
  // session. Gate on profLive, allow 3 tries (poll-paced, then give up).
  const autoTries = useRef(0)
  const autoKey = useRef('')
  useEffect(() => {
    if (!st || !profLive || st.connected) return
    const bus = String(profLive.bus || 'primary')
    // regression review bug 5 (2026-11-10): the try budget was GLOBAL — it was
    // consumed by ordinary polls that had no remembered port (three polls
    // with nothing saved permanently disabled auto-connect for ports
    // remembered later), and a profile/bus change inherited the old budget.
    // Budget is now per (bus|profile), and a poll with no saved port
    // consumes NOTHING — only a real connect attempt counts.
    const key = bus + '|' + (profLive.profile_id || '')
    if (key !== autoKey.current) { autoKey.current = key; autoTries.current = 0 }
    if (autoTries.current >= 3) return
    let saved = ''
    try { saved = api.storage.get('last_port:' + bus) || (bus === 'primary' ? api.storage.get('last_port') : '') || '' } catch (e) {}
    if (!saved) return
    autoTries.current += 1
    api.rest('/connect', { method: 'POST', body: { name: saved, bus } })
      .then(r => {
        if (!(r && r.ok)) setStatus(`auto-connect failed: ${(r && r.error) || 'unreachable'} — pick a port above`)
      })
      .catch(() => {})
  }, [st, profLive])

  const loadLists = useCallback(async () => {
    try {
      const [d, p] = await Promise.all([api.rest('/devices'), api.rest('/profile/list')])
      setDevices((d && d.devices) || [])
      setProfiles((p && p.profiles) || [])
      // Review: a remembered device pick that is no longer plugged in used
      // to survive re-enumeration — Connect then substring-matches the first
      // similar port name and opens the wrong one. Drop stale picks.
      setDevPick(prev => prev && ((d && d.devices) || []).some(x => x.name === prev) ? prev : '')
      return !!(d && d.devices && p && p.profiles)
    } catch (e) { return false }
  }, [])
  // Cold-start law (owner bug report 2026-11-09): the plugin host can 502
  // while it warms up — a one-shot load at mount left every dropdown empty
  // while /state polling quietly recovered. Retry until the lists arrive.
  useEffect(() => {
    let alive = true
    const attempt = async (n) => {
      const ok = await loadLists()
      if (alive && !ok && n < 8) setTimeout(() => attempt(n + 1), 1200)
      else if (alive && !ok) setStatus('backend still waking — device/preset lists empty; SETTINGS → refresh to retry')  // review: silent give-up left empty dropdowns with no hint
    }
    attempt(0)
    return () => { alive = false }
  }, [loadLists])

  const ccMap = {}
  for (const c of (st && st.cc) || []) ccMap[c.channel + ':' + c.cc] = c
  // Passive-learn hygiene (regression review): dedupe by channel:cc, reset dismissals
  // on (re)connect, and only prompt for the profile's own channel — a control
  // created from another channel's traffic would bind the wrong wire.
  const unmapped = ((st && st.unmapped_cc) || [])
    .filter(u => !dismissed[u.channel + ':' + u.cc])
    .filter(u => !prof || (u.channel || 0) === (Number(prof.channel) || 0))

  // Deck law: Connect/Disconnect act on the ACTIVE profile's bus — the
  // surface you are editing. Each bus owns one exclusive port pair; the
  // remembered port is keyed per bus (legacy 'last_port' = primary).
  const activeBusId = (profLive && profLive.bus) || 'primary'
  const connect = async () => {
    if (!devPick) return
    const r = await api.rest('/connect', { method: 'POST', body: { name: devPick, bus: activeBusId } }).catch(() => null)
    if (r && r.ok) { try { api.storage.set('last_port:' + activeBusId, devPick) } catch (e) {} }  // only remember ports that actually worked
    // ignores are PERSISTENT now (owner ruling) — reconnecting does not wipe them
    if (!(r && r.ok)) setStatus(`connect failed: ${(r && r.error) || 'unreachable'}`)   // success shows in the green dot, not the status line
    refresh()
  }
  // regression guard UI9: the result used to be thrown away — a failed or
  // unreachable disconnect STILL cleared the remembered port and claimed
  // success while the device kept playing. Forget the port only when the
  // backend actually let go.
  const disconnect = async () => {
    const r = await api.rest('/disconnect', { method: 'POST', body: { bus: activeBusId } }).catch(() => null)
    if (r && r.ok) { try { api.storage.set('last_port:' + activeBusId, '') } catch (e) {}; setStatus('disconnected') }
    else setStatus(`disconnect refused: ${(r && r.error) || 'backend unreachable'} — the device may still be connected`)
    refresh()
  }
  const stopMatrix = async () => {
    let r = null
    try { r = await api.rest('/matrix/stop', { method: 'POST' }) } catch (e) {}
    // Lazy-arm law: targets modulated without an ever-observed base are NOT
    // restored (no fake value) — name them so the user knows what stayed put.
    const lbl = (c) => ((prof && prof.controls && prof.controls[c] || {}).label) || c
    setStatus(r && r.ok
      ? ((r.restored && r.restored.length ? 'matrix stopped, bases restored' : 'matrix stopped')
        + (r.not_restored && r.not_restored.length
          ? ` — left where the run ended (value never known before it): ${r.not_restored.map(lbl).join(', ')}` : ''))
      : `matrix stop: ${(r && r.error) || 'unreachable'}`)
    refresh()
  }
  const activate = async (id) => {
    if (edit && dirty && !window.confirm('Discard unsaved edits?')) return
    // Review: a wheel paint scheduled 350 ms ago must not fire against the
    // profile we are leaving (stale mutate closure resurrected discarded
    // drafts / POSTed them to the wrong doc).
    clearTimeout(saveTimerRef.current)
    setEdit(false); setColorMode(false); putDraft(null); setSel({}); setCsel({}); setSecSel({})
    // Deep rinse: switching profiles used to leave rename inputs, an armed
    // rebind and inline forms pointing at cids of the OLD profile — the rebind
    // effect or commitRen then indexed a nonexistent control and threw.
    setSecEdit(null); setRenaming(null); setRebind(null); setNewCtl(null); setNewSec(null)
    setMx({ src: 'lfo', tgt: '', depth: 0.5, wave: 'sine', rate: 2 })  // review: the add-lane form kept cids of the OLD profile — blank selects, then a cryptic validator refusal
    undoRef.current = []; redoRef.current = [] // history is per-profile — switching clears it
    const r = await api.rest('/profile/activate', { method: 'POST', body: { id } }).catch(() => null)
    // Review: a refused activation (INVALID preset picked, dead backend) was
    // silent — the select just snapped back a poll later with no explanation.
    if (r && r.matrix_warning) setStatus(`matrix stopped for profile change: ${r.matrix_warning}`)
    else if (!r || !r.ok) setStatus(`activate refused: ${(r && r.error) || 'backend unreachable'}`)
    refresh(true)
  }

  // ---- deck (multi-profile surface, 2026-11-09) --------------------------
  // Deck law: the deck is the on-screen profile list; deck[0] is active
  // (edit/matrix/record/learn follow it). Each profile binds one bus; two
  // profiles may share a bus (two layouts, one device) or split across
  // buses (one port pair each — winmm exclusivity floor).
  const deckIds = ((st && st.deck) || []).map(d => d.profile_id)
  const deckPresets = (st && st.deck_presets) || []   // saved whole-screen layouts
  // Analyzer source menu (owner batch 2026-11-09): every deck device by name,
  // plus desktop audio, microphone, all-MIDI, and everything. Audio entries
  // carry audio-strip's ACTUAL device names (Voicemeeter etc.) — no fiction.
  const devShort = (s) => (s && s.length > 26 ? s.slice(0, 24) + '…' : s || '')
  const specSources = [
    { value: 'all', label: 'everything', bus: '*' },
    { value: 'midi', label: 'all MIDI', bus: '*' },
    { value: 'pc', label: 'desktop audio' + (audDev && audDev.out ? ' · ' + devShort(audDev.out) : ''), bus: '' },
    { value: 'mic', label: 'microphone' + (audDev && audDev.mic ? ' · ' + devShort(audDev.mic) : ''), bus: '' },
  // regression guard UI6: deckDocs caches only SECONDARY docs — the ACTIVE profile
  // on a custom bus resolved to 'primary', so its device analyzer listened
  // to the wrong wire. profLive is the active doc; check it first.
  ].concat(deckIds.map(pid => ({ value: 'midi:' + pid, label: `${pid} (MIDI)`,
    bus: (((profLive && profLive.profile_id === pid ? profLive : null) || deckDocs[pid] || {}).bus) || 'primary' })))
  const deckSet = async (ids) => {
    // Full-list replace: rebase off the LAST ACKNOWLEDGED deck (deckRef),
    // not the 600 ms-poll closure — two quick actions (add + add, remove +
    // make-active) used to race and the second silently dropped the first.
    // regression guard bug 5: that fix only closed the READ side. deckRef used to
    // move after the await, so two fast clicks both derived from the same
    // pre-write deck and the second POST dropped the first addition. The
    // ref now advances OPTIMISTICALLY before the request; a failed write
    // re-syncs from the backend truth via the refresh below (the poll cannot
    // clobber it while deckPendingRef is raised).
    deckRef.current = ids
    deckPendingRef.current++
    const r = await api.rest('/deck', { method: 'POST', body: { ids } }).catch(() => null)
    deckPendingRef.current = Math.max(0, deckPendingRef.current - 1)
    if (r && r.error) setStatus(`deck: ${r.error}`)
    else if (r && r.ok) setStatus(ids.length > 1 ? `deck: ${ids.join(' + ')} — one screen, separate surfaces` : ids.length === 1 ? `deck: ${ids[0]} (the active surface stays on screen)` : 'deck: nothing left to show — activate a preset below')
    else setStatus('deck: backend unreachable')
    refresh(true)
    return r  // regression guard UI10: callers must know whether the write landed — the 'remove from screen' button used to print its success line over its OWN failure status
  }
  const [busDraft, setBusDraft] = useState(null) // {pid, val} — inline new-bus slug
  const setDeckBus = async (doc, bus) => {
    if (bus === '__new') { setBusDraft({ pid: doc.profile_id, val: '' }); return }
    const p = { ...doc, bus }
    // /profile/save takes the doc under key 'doc' — 'profile' is the Hermes
    // auth-gate's profile-selector key (a doc there 403s before we see it).
    const r = await api.rest('/profile/save', { method: 'POST', body: { doc: p } }).catch(() => null)
    // Review: r === null (thrown transport / 502) used to fall into the
    // success branch — the pane announced a bus change that never landed.
    if (r && r.ok) {
      const bundled = profiles.some(x => x.profile_id === doc.profile_id && x.tier !== 'user')
      setStatus(`${doc.profile_id} now rides bus "${bus}"${bundled ? ' — saved as a USER OVERRIDE of the factory preset (copy it for a private edit)' : ''} — connect it while it is the active surface`)
    } else setStatus(`bus change refused: ${(r && r.error) || 'backend unreachable'}`)
    refresh(true)
  }
  // passive learn: create a control for an unmapped incoming CC
  const createControl = async (u) => {
    if (!prof) {
      // bootstrap a custom profile from the connected device name
      // Review: a port name with no ASCII alphanumerics slugified to 'custom-'
      // with a dangling dash (the sibling slugs in createCategory/createProfile
      // both carry fallbacks — mirror them).
      const devName = (st && st.connected && st.connected.out) || 'My Device'
      const slug = devName.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40) || 'device'
      const pid = 'custom-' + slug
      // regression guard bug 3: this id is DETERMINISTIC, and /profile/save
      // overwrites. Deactivating a custom profile and learning one CC used to
      // replace the whole layout with a one-control document. If the id
      // already exists in the store, MERGE the learned control into that doc
      // and re-activate it instead of clobbering.
      const ex = await api.rest('/profile/get?id=' + encodeURIComponent(pid)).catch(() => null)
      if (ex && ex.profile_id === pid) {
        if (ex.controls && ex.controls['cc' + u.cc]) {
          const ra = await api.rest('/profile/activate', { method: 'POST', body: { id: pid } }).catch(() => null)
          setStatus(ra && ra.ok ? `${pid} reactivated — CC ${u.cc} was already mapped to "${ex.controls['cc' + u.cc].label}"` : `activate refused: ${(ra && ra.error) || 'unreachable'}`)
          refresh(true)
          return
        }
        const tk = new Set(Object.values(ex.controls || {}).filter(c => !c.section).map(c => (c.y || 0) + ':' + (c.x || 0)))
        let x = 0, y = 0
        while (tk.has(y + ':' + x)) { x += 1; if (x > 7) { x = 0; y += 1 } }
        ex.controls = ex.controls || {}
        ex.controls['cc' + u.cc] = { label: u.name || 'CC ' + u.cc, cc: u.cc, x, y }
        const r = await api.rest('/profile/save', { method: 'POST', body: { doc: ex } }).catch(() => null)
        if (r && r.ok) { await api.rest('/profile/activate', { method: 'POST', body: { id: pid } }).catch(() => null); setStatus(`added ${u.name || 'CC ' + u.cc} to existing ${pid} (not overwritten)`) }
        else setStatus(`profile rejected: ${(r && r.error) || 'unreachable'}`)
        refresh(true)
        return
      }
      const p = {
        schema_version: 1,
        profile_id: pid,
        device: devName,
        match: { name_contains: devName },
        channel: u.channel || 0,
        controls: {}, sections: {},
      }
      p.controls['cc' + u.cc] = { label: u.name || 'CC ' + u.cc, cc: u.cc, x: 0, y: 0 }
      const r = await api.rest('/profile/save', { method: 'POST', body: { doc: p } }).catch(() => null)
      if (r && r.ok) { await api.rest('/profile/activate', { method: 'POST', body: { id: p.profile_id } }); setStatus(`created profile ${p.profile_id}`) }
      else setStatus(`profile rejected: ${(r && r.error) || 'unreachable'}`)
      refresh(true)
      return
    }
    const p = JSON.parse(JSON.stringify(prof))
    // Deep rinse: +Control with an already-bound CC used to silently REPLACE
    // the old control (its label/position/colors vanished, no confirm). The
    // passive-learn path can't hit this (backend filters bound CCs out of
    // unmapped_cc) — the manual add can. Refuse by name, like createCategory.
    if (p.controls['cc' + u.cc]) { setStatus(`CC ${u.cc} is already control "${p.controls['cc' + u.cc].label}" — rebind it instead of adding`); return }
    // first FREE grid cell (gaps are real now — don't pack, fill holes).
    // Review: the scan must look at the UNSECTIONED grid only — cells busy
    // inside categories live in their own grids and must not block placement.
    const taken = new Set(Object.values(p.controls).filter(c => !c.section).map(c => (c.y || 0) + ':' + (c.x || 0)))
    let x = 0, y = 0
    while (taken.has(y + ':' + x)) { x += 1; if (x > 7) { x = 0; y += 1 } }
    if (edit && draft) { // draft law: learn-created controls join the unsaved draft
      mutate(pp => { pp.controls['cc' + u.cc] = { label: u.name || 'CC ' + u.cc, cc: u.cc, x, y } }, `added ${u.name || 'CC ' + u.cc} (unsaved)`)
      return
    }
    p.controls['cc' + u.cc] = { label: u.name || 'CC ' + u.cc, cc: u.cc, x, y }
    const r = await api.rest('/profile/save', { method: 'POST', body: { doc: p } }).catch(() => null)
    setStatus(r && r.ok ? `added ${p.controls['cc' + u.cc].label}` : `rejected: ${(r && r.error) || 'unreachable'}`)
    refresh(true)
  }

  // edit ops — DRAFT LAW: while edit mode is open every op mutates the local
  // draft and enters local history; the backend sees nothing until saveDraft.
  // Outside edit mode (learn prompts, matrix panel) mutate posts immediately.
  // Undo/Redo law: mutate is the single choke point for EVERY profile edit, so
  // the history lives here. Stacks hold full documents — profiles are small
  // JSON, simplicity beats diffs. Capped at 50.
  const undoRef = useRef([]), redoRef = useRef([])
  const mutate = async (fn, label) => {
    if (!prof) return
    // Review: gate on the LIVE draft ref, not the closure — a debounced wheel
    // callback scheduled before Discard used to fall back to the captured
    // (discarded) draft and resurrect it.
    if (edit && draftRef.current != null) {
      // Deep rinse: base off draftRef (live), not the closure `draft` — a
      // debounced wheel paint landing after a rename commit used to clone the
      // pre-rename doc and silently drop the rename.
      const base = draftRef.current
      const before = JSON.parse(JSON.stringify(base))
      const p = JSON.parse(JSON.stringify(base))
      try { fn(p) } catch (e) { setStatus(`${label} refused: ${e && e.message}`); return { ok: false, error: String(e && e.message || e) } }  // a throwing fn was an unhandled rejection — edit lost, no report
      undoRef.current.push(before)
      if (undoRef.current.length > 50) undoRef.current.shift()
      redoRef.current = []
      putDraft(p)
      setStatus(label + ' — unsaved')
      return { ok: true, local: true }
    }
    setBusy(true)
    // Review: base off the LIVE saved doc — a stale closure (old prof) POSTing
    // would write a superseded document back to disk. profLiveRef carries the
    // last save IMMEDIATELY (before the round trip completes), so back-to-back
    // non-edit edits stack instead of the second resurrecting pre-first state.
    const p = JSON.parse(JSON.stringify(profLiveRef.current != null ? profLiveRef.current : (profLive != null ? profLive : prof)))
    let r
    try {
      fn(p)
      profLiveRef.current = p
      r = await api.rest('/profile/save', { method: 'POST', body: { doc: p } }).catch(() => null)
    } catch (e) {
      profLiveRef.current = null
      setStatus(`${label} refused: ${e && e.message}`)
      return { ok: false, error: String(e && e.message || e) }
    } finally { setBusy(false) }  // a throw used to strand "saving…" forever
    if (!(r && r.ok)) profLiveRef.current = null  // refused save: re-base off the server next time, never off a doc it rejected
    // Deep rinse: the non-edit branch used to push onto the undo stack — but
    // undo() refuses without a draft and ENTERING edit clears the stack, so
    // those entries were unreachable dead data inflating the "N in history"
    // tooltip. History is draft-mode only, one place, honest.
    setStatus(r && r.ok ? label : `rejected: ${(r && r.error) || 'unreachable'}`)
    refresh(true)
    return r
  }
  const saveDraft = async () => {
    // commit the LIVE draft ref, not the closure doc —
    // a chat batch's save_as used to POST the click-time doc (missing its own
    // edits) and copy THAT stale file to the new preset.
    const doc = draftRef.current != null ? draftRef.current : draft
    if (!doc) return true
    setBusy(true)
    const r = await api.rest('/profile/save', { method: 'POST', body: { doc } }).catch(() => null)
    if (r && r.ok) profLiveRef.current = doc  // the draft IS the saved doc now — keep the lost-update base honest
    setBusy(false)
    if (r && r.ok) {
      setStatus('saved to profile')
      await refresh(true)
      // matrix hot-apply law: a saved edit to a running matrix lands at once.
      // regression guard UI5: EXCEPT when the save route itself already restarted
      // the engine from the new doc (matrix_restarted) — a second reload was
      // a double stop/start with a mid-relaunch base recapture.
      if (!(r && r.matrix_restarted)) api.rest('/matrix/reload', { method: 'POST' }).catch(() => null).then(() => refresh(true))
    } else setStatus(`save refused: ${(r && r.error) || 'unreachable'}`)
    return r && r.ok
  }
  const exitEdit = () => { clearTimeout(saveTimerRef.current); setEdit(false); setColorMode(false); setMenu(null); setCreateMenu(false); putDraft(null); setSel({}); setCsel({}); setSecSel({}); setSecEdit(null); setRenaming(null); refresh(true) }
  const undo = () => {
    if (!undoRef.current.length || !draft) return
    redoRef.current.push(JSON.parse(JSON.stringify(draftRef.current != null ? draftRef.current : draft)))
    putDraft(undoRef.current.pop())
    setStatus('undone')
  }
  const redo = () => {
    if (!redoRef.current.length || !draft) return
    undoRef.current.push(JSON.parse(JSON.stringify(draftRef.current != null ? draftRef.current : draft)))
    putDraft(redoRef.current.pop())
    setStatus('redone')
  }
  // Matrix edits: outside edit mode save + hot-apply immediately; inside edit
  // mode they join the draft and hot-apply at Save.
  // #13: always call /matrix/reload — the backend no-ops it when nothing runs,
  // so a stale local `running` flag can never skip a needed hot-swap.
  const mutateMx = async (fn, label) => {
    const r = await mutate(fn, label)
    // regression guard UI5: the active-save route already runs stop->save->restart
    // with the NEW doc on disk — reloading again right after stopped the
    // freshly armed engine a second time (audible gap + base recapture of
    // mid-relaunch values after every matrix edit). Skip when save restarted.
    if (r && r.ok && !r.local && !r.matrix_restarted) api.rest('/matrix/reload', { method: 'POST' }).catch(() => null).then(() => refresh(true))
    return r
  }
  const commitNum = (i, key, ev, min, max, dflt) => {
    const v = parseFloat(ev.target.value)
    const c = isNaN(v) ? dflt : Math.max(min, Math.min(max, v))
    ev.target.value = c
    const cur = prof.matrix[i][key] != null ? prof.matrix[i][key] : dflt
    if (c !== cur) mutateMx(p => { p.matrix[i][key] = c }, `${key} set`)
  }
  // Field captions: the matrix row is dense; every selector must say what it is.
  const fld = (t) => jsx('span', { style: { fontSize: 9, textTransform: 'uppercase', letterSpacing: '0.08em', color: 'var(--ui-text-secondary)', opacity: 0.75 }, children: t })
  // Edge-kind helpers (LMMS-borrow batch): integer commits + trigger `then`
  // editing + a kind switch that reshapes the edge so the validator is
  // satisfied the moment the lane leaves the row (refuse-closed upstream,
  // never guess-and-hope downstream).
  const commitInt = (i, key, ev, min, max, dflt) => {
    const v = parseFloat(ev.target.value)
    const c = isNaN(v) ? dflt : Math.round(Math.max(min, Math.min(max, v)))
    ev.target.value = c
    const cur = prof.matrix[i][key] != null ? prof.matrix[i][key] : dflt
    if (c !== cur) mutateMx(p => { p.matrix[i][key] = c }, `${key} set`)
  }
  // regression guard UI7: the then-row rate_hz used THIS integer committer despite
  // the 0.1-step input beside it — 0.5 became 1, 0.1 became 0 and died in the
  // validator. Float fields get a float committer; integers stay as they are.
  const commitThenNum = (i, j, key, ev, min, max, dflt) => {
    const v = parseFloat(ev.target.value)
    const c = isNaN(v) ? dflt : Math.max(min, Math.min(max, v))
    ev.target.value = c
    const t = ((prof.matrix[i] || {}).then || [])[j] || {}
    if (c !== (t[key] != null ? t[key] : dflt)) mutateMx(p => { p.matrix[i].then[j][key] = c }, `${key} set`)
  }
  const commitThenInt = (i, j, key, ev, min, max, dflt) => {
    const v = parseFloat(ev.target.value)
    const c = isNaN(v) ? dflt : Math.round(Math.max(min, Math.min(max, v)))
    ev.target.value = c
    const t = ((prof.matrix[i] || {}).then || [])[j] || {}
    if (c !== (t[key] != null ? t[key] : dflt)) mutateMx(p => { p.matrix[i].then[j][key] = c }, `${key} set`)
  }
  const thenSet = (i, j, key, val, label) => mutateMx(p => { p.matrix[i].then[j][key] = val }, label)
  const thenDel = (i, j) => mutateMx(p => { p.matrix[i].then.splice(j, 1) }, 'then entry removed')
  const thenAdd = (i, kind) => mutateMx(p => {
    const ed = p.matrix[i]
    ed.then = ed.then || []
    ed.then.push(kind === 'lfo'
      ? { kind: 'lfo', target: ctrlIds[0] || '', wave: 'tri', rate_hz: 0.5, center: 64, amp: 25 }
      : { kind: 'move', target: ctrlIds[0] || '', from: 0, to: 127, duration_ms: 3000 })
  }, 'then entry added')
  const setKind = (i, nv) => mutateMx(p => {
    const ed = p.matrix[i]
    ed.kind = nv
    if (nv === 'lfo') { if (!ed.source) ed.source = 'lfo'; if (!ed.target) ed.target = ctrlIds[0] || '' }
    if (nv === 'move') {
      delete ed.source
      if (!ed.target) ed.target = ctrlIds[0] || ''
      if (ed.from == null) { ed.from = 0; ed.to = 127; ed.duration_ms = 5000 }
    }
    if (nv === 'when' || nv === 'watch') {
      if (nv === 'watch' && (!ed.source || ed.source === 'lfo')) ed.source = ctrlIds[0] || ''
      if (nv === 'when' && !ed.source) ed.source = 'lfo'
      delete ed.target
      if (ed.level == null) ed.level = 90
      if (!ed.crosses) ed.crosses = 'up'
      if (!Array.isArray(ed.then) || !ed.then.length) ed.then = [{ kind: 'move', target: ctrlIds[0] || '', from: 0, to: 127, duration_ms: 3000 }]
    }
  }, 'lane kind set')
  const loadTakes = useCallback(() =>
    api.rest('/rec/takes').then(r => setTakes((r && r.takes) || [])).catch(() => {}), [])
  useEffect(() => { loadTakes() }, [loadTakes])
  const dlTake = (name) => api.rest('/rec/take/get?name=' + encodeURIComponent(name)).then(r => {
    if (!r || !r.mid_b64) { setStatus(`take: ${(r && r.error) || 'unreachable'}`); return }
    const bin = atob(r.mid_b64), bytes = new Uint8Array(bin.length)
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
    const a = document.createElement('a')
    a.href = URL.createObjectURL(new Blob([bytes], { type: 'audio/midi' }))
    a.download = name
    a.click()
    setTimeout(() => URL.revokeObjectURL(a.href), 10000)  // deep rinse: blob URLs were never revoked
  }).catch(() => setStatus('take download: backend unreachable'))
  // regression guard (destructive-consistency note): exported takes were one-click
  // gone while presets ask twice. Same gravity, same confirm.
  const delTake = (name) => {
    if (!window.confirm(`Delete exported take "${name}"? It is gone for good.`)) return
    api.rest('/rec/take/delete', { method: 'POST', body: { name } })
      .then(r => { setStatus(r && r.ok ? 'take deleted' : `take: ${(r && r.error) || 'unreachable'}`); loadTakes() }).catch(() => setStatus('take delete: backend unreachable'))
  }
  // (inpS is module-scope now — see top of file.)
  const ctrlIds = prof ? Object.keys(prof.controls || {}).filter(cid => !['label', 'analyzer'].includes(prof.controls[cid].widget || 'knob')) : []  // analyzers have no cc — they are not matrix lanes
  const rename = (cid) => setRenaming({ cid, val: prof.controls[cid].label })
  const commitRen = () => {
    if (!renaming) return
    const { cid } = renaming
    const name = renaming.val.trim()
    setRenaming(null)
    if (name && name !== prof.controls[cid].label) mutate(p => { p.controls[cid].label = name }, 'renamed')
  }
  const setWidget = (cid, w) => mutate(p => { p.controls[cid].widget = w }, 'widget set')
  const del = (cid) => {
    const _mx = ((prof && prof.matrix) || [])
    // regression guard bug 4: count nested references too — a trigger's then[] can
    // point at this control even when the top-level edge does not.
    const uses = _mx.filter(e => e.source === cid || e.target === cid).length +
      _mx.reduce((n, e) => n + ((e.then || []).filter(t => t.source === cid || t.target === cid).length), 0)
    const aliasN = Object.keys((prof && prof.aliases) || {}).filter(a => prof.aliases[a] === cid).length
    const extra = (uses || aliasN) ? ` Its ${uses} matrix edge-ref(s) and ${aliasN} alias(es) go too.` : ''
    if (window.confirm(`Delete ${prof.controls[cid].label}?${extra}`)) {
      // Review: an armed rebind pointing at a deleted control made the learn
      // effect dereference p.controls[undefined] — dangling-ref crash class.
      if (rebind && rebind.cid === cid) setRebind(null)
      mutate(p => {
        delete p.controls[cid]
        // refuse-closed validator rejects edges/aliases pointing at a deleted
        // control — deleting a control takes everything that referenced it,
        // in one save. (This is why deleting Cutoff "did nothing".)
        // regression guard bug 4: then[] children are references too. The old
        // top-level-only filter left a dead target inside watch/when children
        // — the validator then refused the WHOLE draft ("target ... is not a
        // control") and the user was trapped in edit mode. Scrub children;
        // a trigger whose last child dies is itself dead (an empty then is
        // invalid), so it goes too.
        if (p.matrix) p.matrix = p.matrix.filter(e => {
          if (e.source === cid || e.target === cid) return false
          if (Array.isArray(e.then)) e.then = e.then.filter(t => t.source !== cid && t.target !== cid)
          return !((e.kind === 'when' || e.kind === 'watch') && Array.isArray(e.then) && e.then.length === 0)
        })
        if (p.aliases) for (const a of Object.keys(p.aliases)) if (p.aliases[a] === cid) delete p.aliases[a]
      }, 'deleted')
    }
  }
  // Rebind law: same machinery as learn, with the TARGET pinned. Click arms;
  // the next hardware CC (source panel, profile channel, AFTER the arm cursor)
  // rebinds; click again to type a CC.
  // #2 (): the old filter compared Date.now() (ms) against
  // backend ts (seconds) — it could NEVER match, twist-rebind never worked.
  // The backend now stamps a monotonic seq on every event; arm at the current
  // cursor and take the FIRST qualifying event, on the profile's own channel.
  const rebindClick = (cid) => {
    if (rebind && rebind.cid === cid) { setRebind(null); setStatus('rebind cancelled') }
    else {
      setRebind({ cid, seq: (st && st.event_seq) || 0 })
      setStatus(`Rebinding ${prof.controls[cid].label} — twist a real knob, or type a CC in the box beside it`)
    }
  }
  const setCc = (cid, cc) => { setRebind(null); mutate(p => { p.controls[cid].cc = cc }, `rebound to CC ${cc}`) }
  useEffect(() => {
    if (!rebind || !st || !st.events) return
    const profCh = Number(prof && prof.channel) || 0
    const ev = st.events.find(e => e.kind === 'cc' && e.source === 'panel'
                                  && (e.channel || 0) === profCh
                                  && e.seq != null && e.seq > rebind.seq)
    if (ev) {
      // Review: the control may have been deleted while the rebind was armed
      // (del() now clears it too — this is the belt) — dereferencing
      // p.controls[rebind.cid] inside mutate threw a TypeError in an effect.
      const live = draftRef.current || profLive || prof
      if (!live || !live.controls || !live.controls[rebind.cid]) { setRebind(null); return }
      setRebind(null)
      mutate(p => { p.controls[rebind.cid].cc = ev.cc }, `rebound to CC ${ev.cc} (learned from hardware)`)
    }
  }, [st, rebind])
  // Manual control creation (output-only devices never echo — learn can't help them)
  const addManual = () => {
    if (!prof) { setStatus('+ Control: activate a profile first (or twist a knob to bootstrap one)'); return }
    setNewCtl({ cc: '', name: '' })
  }
  const addCtl = () => {
    const cc = parseInt(newCtl.cc, 10)
    if (!(cc >= 0 && cc <= 127)) { setStatus(`add refused: "${newCtl.cc}" is not a CC 0-127`); return }
    const name = (newCtl.name || '').trim()
    setNewCtl(null)
    createControl({ cc, value: 0, channel: Number(prof.channel) || 0, name })
  }
  // Manage law (owner feature 2026-11-09): rename/copy/delete presets and
  // rename/remove devices from the SETTINGS card. Electron has no
  // window.prompt — every input is inline; deletes are two-click armed.
  const doRename = async (from, to) => {
    if (!dropDraftIfPid(from)) return
    const r = await api.rest('/profile/rename', { method: 'POST', body: { from, to } }).catch(() => null)
    setStatus(r && r.ok ? `renamed ${from} → ${to}` : `rename refused: ${(r && r.error) || 'unreachable'}`)
    setMEdit(null); loadLists(); refresh(true)
  }
  const doCopy = async (from, to) => {
    const r = await api.rest('/profile/copy', { method: 'POST', body: { from, to } }).catch(() => null)
    setStatus(r && r.ok ? `copied ${from} → ${to}` : `copy refused: ${(r && r.error) || 'unreachable'}`)
    setMEdit(null); loadLists(); refresh(true)
    return r  // callers must know whether the copy actually happened
  }
  // Draft-vs-manage law (review 2026-11-09 blocker): delete/rename of the
  // preset an OPEN EDIT draft points at used to leave the draft rendering a
  // dead doc — and "SAVE EDIT" then resurrected the deleted file. Manage
  // actions on the draft's own pid must exit edit first (with a confirm if
  // there are unsaved edits). Returns false when the user backed out.
  const dropDraftIfPid = (pid) => {
    const d = draftRef.current
    if (edit && d && d.profile_id === pid) {
      if (dirty && !window.confirm(`Unsaved edits on "${pid}" will be lost — proceed?`)) return false
      clearTimeout(saveTimerRef.current)
      putDraft(null); setEdit(false); setColorMode(false)
      undoRef.current = []; redoRef.current = []
    }
    return true
  }
  const doDelete = async (pid) => {
    if (!dropDraftIfPid(pid)) return
    const r = await api.rest('/profile/delete', { method: 'POST', body: { id: pid } }).catch(() => null)
    setStatus(r && r.ok ? `deleted ${pid}` : `delete refused: ${(r && r.error) || 'unreachable'}`)
    setMArm(null); loadLists(); refresh(true)
  }
  const doDev = async (pid, name) => {
    const doc = await api.rest(`/profile/get?id=${encodeURIComponent(pid)}`).catch(() => null)
    if (!doc || doc.error || !doc.profile_id) { setStatus(`device rename refused: ${(doc && doc.error) || 'unreachable'}`); return }
    doc.device = name
    const r = await api.rest('/profile/save', { method: 'POST', body: { doc } }).catch(() => null)
    setStatus(r && r.ok ? `device renamed to ${name}` : `device rename refused: ${(r && r.error) || 'unreachable'}`)
    setMEdit(null); loadLists(); refresh(true)
  }
  // ---- chat box engine (owner feature 2026-11-09) --------------------------
  // chatLiveDoc is the SAME base mutate uses, so resolving labels "against
  // what the user sees" is exact in both modes (draft while editing, saved
  // doc outside). Resolvers take a gentle second pass (substring) but the
  // result line always names what was hit — never silently paints a lookalike.
  const chatLiveDoc = () => (draftRef.current != null ? draftRef.current : (profLiveRef.current != null ? profLiveRef.current : prof))
  const chatSecTier = (d, name) => {
    const n = String(name || '').trim().toLowerCase()
    if (!n) return { tier: 'none', hits: [] }
    const entries = Object.entries(d.sections || {})
    const exact = entries.filter(([sid, s]) => String(s.label || sid).trim().toLowerCase() === n || String(sid).toLowerCase() === n)
    if (exact.length) return { tier: 'exact', hits: exact }
    const fuzzy = entries.filter(([sid, s]) => { const lab = String(s.label || sid).trim().toLowerCase(); return lab.length > 2 && (lab.includes(n) || n.includes(lab)) })
    return { tier: 'fuzzy', hits: fuzzy }
  }
  const chatFindSec = (d, name) => {
    const t = chatSecTier(d, name)
    if (!t.hits.length || t.hits.length > 1) return null  // A11 (): no exact-tier exemption here either — duplicate category labels are ambiguous
    return t.hits[0][0]
  }
  const chatSecHint = (d, name) => {
    const t = chatSecTier(d, name)
    return (t.hits.length > 1)
      ? ` — ambiguous: ${t.hits.slice(0, 4).map(([sid, s]) => s.label || sid).join(' / ')} — say which one exactly`
      : ''
  }
  const chatCtlTier = (d, name) => {
    let n = String(name || '').trim().toLowerCase()
    n = n.replace(/^(the|this|that)\s+/, '')
    if (!n) return { tier: 'none', hits: [] }
    const entries = Object.entries(d.controls || {})
    const lab = (id, c) => String(c.label || id).trim().toLowerCase()
    const eq = (q) => entries.filter(([id, c]) => lab(id, c) === q || String(id).toLowerCase() === q)
    // Loose-reference law (owner demo ask 2026-11-10): people say "wave knob",
    // "the cutoff dial" — strip widget nouns and retry before substring.
    const n2 = n.replace(/\b(knob|dial|pot|potentiometer|slider|fader|button|switch|encoder|control)\b/g, ' ').replace(/\s+/g, ' ').trim()
    const fz = (q) => entries.filter(([id, c]) => { const l = lab(id, c); return l.length > 2 && (l.includes(q) || q.includes(l)) })
    let h = eq(n)
    if (h.length) return { tier: 'exact', hits: h }
    if (n2 && n2 !== n) { h = eq(n2); if (h.length) return { tier: 'exact', hits: h } }
    h = fz(n)
    if (!h.length && n2 && n2 !== n) h = fz(n2)
    return { tier: 'fuzzy', hits: h }
  }
  const chatFindCtl = (d, name) => {
    // the FIRST substring hit used to win by JSON insertion
    // order — "Attack" silently became "Filter Attack" OR "Amp Attack".
    // Apply only on an EXACT hit or a UNIQUELY matched loose hit; ambiguity
    // refuses (and chatCtlHint names the candidates so the model asks).
    // A11 (): EXACT hits were exempt — two controls
    // genuinely labelled "Attack" let insertion order pick one silently. The
    // law now has no tier exemption: more than one hit = ambiguous = refuse.
    const t = chatCtlTier(d, name)
    if (!t.hits.length) return null
    if (t.hits.length > 1) return null
    return t.hits[0][0]
  }
  const chatCtlHint = (d, name) => {
    const t = chatCtlTier(d, name)
    return (t.hits.length > 1)
      ? ` — ambiguous: ${t.hits.slice(0, 4).map(([, c]) => c.label).join(' / ')} — say which one exactly`
      : ''
  }
  const chatCtlWired = (d, cid) => { const w = ((d.controls || {})[cid] || {}).widget || 'knob'; return w !== 'label' && w !== 'analyzer' }
  const chatMatrixTargets = (d, cid) => {
    const hits = (edges) => (edges || []).some(e => e && (e.target === cid || hits(e.then)))
    return hits(d.matrix)
  }
  // color/clear_color target -> the full part-key unit (same law as rainbow:
  // a category is ONE unit: label + box + contents; a control is its parts).
  const chatUnitKeys = (d, target) => {
    const i = String(target || '').indexOf(':')
    if (i < 0) return null
    const kind = target.slice(0, i), rest = target.slice(i + 1)
    if (kind === 'section') {
      const sid = chatFindSec(d, rest)
      if (!sid) return null
      const out = { ['sec:' + sid]: true, ['sb:' + sid]: true }
      for (const [id, c] of Object.entries(d.controls || {})) if (c.section === sid) { out['w:' + id] = true; out['b:' + id] = true; if ((c.widget || 'knob') !== 'label') out['n:' + id] = true }
      return Object.keys(out)
    }
    if (kind === 'control') {
      const cid = chatFindCtl(d, rest)
      if (!cid) return null
      const w = (d.controls[cid] || {}).widget || 'knob'
      if (w === 'analyzer') return ['w:' + cid]  // the owner law: an analyzer owns its scheme — only its tint is paintable
      return (w === 'label' ? ['n:' + cid, 'b:' + cid] : ['w:' + cid, 'n:' + cid, 'b:' + cid])
    }
    return null
  }
  // Edge labels -> control ids (the validator/engine speak ids). Wire law is
  // enforced HERE too so the user reads "labels have no wire" instead of a
  // save refusal surfacing as a red status line.
  const chatMapEdge = (d, e) => {
    const o = JSON.parse(JSON.stringify(e || {}))
    if (!o.kind) o.kind = 'lfo'
    for (const key of ['source', 'target']) {
      if (!o[key]) continue
      const id = chatFindCtl(d, o[key])
      if (!id) return { err: `no control named "${o[key]}" in this preset` + chatCtlHint(d, o[key]) }
      if (!chatCtlWired(d, id)) return { err: `"${o[key]}" has no wire — labels/analyzers cannot modulate` }
      o[key] = id
    }
    if (Array.isArray(o.then)) {
      const mapped = []
      for (const t of o.then) { const r = chatMapEdge(d, t); if (r.err) return r; mapped.push(r.edge) }
      if (!mapped.length) return { err: 'trigger has no valid action left' }
      o.then = mapped
    }
    return { edge: o }
  }
  const chatSummary = () => {
    const d = chatLiveDoc()
    if (!d) return null
    const counts = {}
    for (const c of Object.values(d.controls || {})) { const s = c.section || '_'; counts[s] = (counts[s] || 0) + 1 }
    return {
      profile_id: d.profile_id, device: d.device, channel: d.channel,
      sections: Object.entries(d.sections || {}).map(([sid, s]) => ({ sid, label: s.label, row: s.row || 0, order: s.order || 0, controls: counts[sid] || 0 })),
      controls: Object.entries(d.controls || {}).map(([id, c]) => ({ id, label: c.label, cc: c.cc, widget: c.widget || 'knob', section: c.section || null, invert: !!c.invert })),
      matrix: (d.matrix || []).map(e => ({ kind: e.kind || 'lfo', source: e.source, target: e.target, wave: e.wave, rate_hz: e.rate_hz, depth: e.depth })),
      matrix_running: !!(st && st.matrix && st.matrix.running),
    }
  }
  // Chat transport (catalog review, PR 153102): ONE plugin RPC out, ONE session
  // event back. An RPC call holds this client's request lane until the handler
  // returns, so the handler answers with a job id at once and pushes the
  // proposal onto the session's event stream — awaited here, keyed by that job.
  // The job id is minted HERE and its waiter registered BEFORE the call goes
  // out: a backend that fails instantly can emit the answer before the {job}
  // reply reaches this pane, and an unmatched event is dropped — the box would
  // then sit out its whole timeout blaming a slow model for a failure that took
  // milliseconds. The old 25 s IPC ceiling is gone (nothing stays open), so the
  // wall clock is the model's own: 150 s before the box calls it stalled.
  useEffect(() => {
    const off = host.onEvent('plugin.z-midi-console.chat.answered', (ev) => {
      const p = (ev && ev.payload) || {}
      const wait = chatJobs.current[p.job]
      if (!wait) return
      delete chatJobs.current[p.job]
      clearTimeout(wait.timer)
      wait.resolve(p)
    })
    const jobs = chatJobs.current
    return () => {
      off()
      // Closing the console mid-think: park the timers and answer the pending
      // send honestly instead of leaving a 150 s timer firing into a dead pane.
      for (const k of Object.keys(jobs)) {
        clearTimeout(jobs[k].timer)
        jobs[k].resolve({ ok: false, error: 'the console closed while the model was thinking — nothing applied' })
      }
    }
  }, [])
  useEffect(() => {
    // effort is persisted; the reader used to live in the deleted loadLanes(),
    // which left the knob write-only and silently reset it to auto on reload.
    try { const ef = api.storage.get('chat_effort'); if (ef) setChatEffort(String(ef)) } catch (e) {}
  }, [])
  const chatAsk = async (payload) => {
    let sid = null
    try { sid = host.state.focusedSessionId.get() } catch (e) { sid = null }
    if (!sid) {
      return { ok: false, error: 'the chat box answers through the focused chat session — focus one and press Send again' }
    }
    const job = (typeof crypto !== 'undefined' && crypto.randomUUID ? crypto.randomUUID() : '').replace(/-/g, '')
    if (!/^[0-9a-f]{32}$/.test(job)) {
      return { ok: false, error: 'this webview has no crypto.randomUUID — the chat box cannot anchor its job' }
    }
    return await new Promise((resolve) => {
      const timer = setTimeout(() => {
        delete chatJobs.current[job]
        resolve({ ok: false, error: 'no answer in 150 s — the model may be cold-loading or queued behind another job, or the focused chat session closed while it thought; press Send again once it is up' })
      }, 150000)
      chatJobs.current[job] = { resolve, timer }
      const settle = (obj) => { clearTimeout(timer); delete chatJobs.current[job]; resolve(obj) }
      host.request('plugin.z-midi-console.chat.ask', Object.assign({ session_id: sid, job }, payload))
        .then((sent) => {
          if (!sent || sent.job !== job) settle({ ok: false, error: 'the console backend did not echo the job id' })
          // else: the answer arrives as the chat.answered session event
        })
        .catch((e) => {
          const m = (e && e.message) || 'internal error'
          // A host older than the manifest floor answers -32601; name that case
          // instead of showing a bare JSON-RPC code.
          settle({ ok: false, error: (/32601|not found|plugin_rpc_failed/i.test(m)
            ? 'this Hermes build has no plugin RPC (needs 0.23.1): '
            : 'the console backend refused the ask: ') + m })
        })
    })
  }
  const applyChatOps = async (ops, pidAtSend) => {
    clearTimeout(saveTimerRef.current)  // a pending wheel paint used to overwrite a chat color op 350 ms later
    const lines = []
    // A6 (): the model SAW the profile summarized at
    // send time. If the owner switched presets while it thought, its ops
    // (label + id based) must not edit the wrong doc — bind and refuse.
    const d0 = chatLiveDoc()
    if (pidAtSend && d0 && d0.profile_id !== pidAtSend) {
      return [`✗ profile switched (${pidAtSend} → ${d0.profile_id}) while the model was thinking — nothing applied`]
    }
    for (const op of ops || []) {
      const d = chatLiveDoc()
      if (!d) { lines.push('✗ no preset to edit'); break }
      if (op.op === 'color' || op.op === 'clear_color') {
        const keys = chatUnitKeys(d, op.target)
        if (!keys) {
          const i = String(op.target || '').indexOf(':')
          const hint = i > 0 && op.target.slice(0, i) === 'section' ? chatSecHint(d, op.target.slice(i + 1)) : ''
          lines.push(`✗ ${op.op}: "${op.target}" not found here` + hint); continue
        }
        const r = await mutate(p => paintKeys(p, keys, op.op === 'color' ? op.hex : null), op.op === 'color' ? `chat: painted ${op.target}` : `chat: cleared ${op.target}`)
        lines.push(r && r.ok ? `✓ ${op.op === 'color' ? `painted ${op.target} ${op.hex}` : `cleared color on ${op.target}`}` : `✗ ${(r && r.error) || 'save refused'}`)
      } else if (op.op === 'rename') {
        const to = op.to
        if (op.kind === 'section') {
          const sid = chatFindSec(d, op.from)
          if (!sid) { lines.push(`✗ rename: no category "${op.from}"` + chatSecHint(d, op.from)); continue }
          if (chatFindSec(d, to) && String(to).trim().toLowerCase() === String(((d.sections || {})[sid] || {}).label || '').trim().toLowerCase()) { lines.push(`✗ rename: "${to}" is already that category's name`); continue }
          const r = await mutate(p => { p.sections[sid].label = to }, `chat: category renamed to ${to}`)
          lines.push(r && r.ok ? `✓ category → "${to}"` : `✗ ${(r && r.error) || 'save refused'}`)
        } else {
          const cid = chatFindCtl(d, op.from)
          if (!cid) { lines.push(`✗ rename: no control "${op.from}"`); continue }
          const r = await mutate(p => { p.controls[cid].label = to }, `chat: control renamed to ${to}`)
          lines.push(r && r.ok ? `✓ control "${op.from}" → "${to}"` : `✗ ${(r && r.error) || 'save refused'}`)
        }
      } else if (op.op === 'move_section') {
        const sid = chatFindSec(d, op.name)
        if (!sid) { lines.push(`✗ move: no category "${op.name}"` + chatSecHint(d, op.name)); continue }
        const r = await mutate(p => { p.sections[sid].row = op.row; p.sections[sid].order = op.order }, `chat: moved ${op.name}`)
        lines.push(r && r.ok ? `✓ ${op.name} → band ${op.row}, slot ${op.order}` : `✗ ${(r && r.error) || 'save refused'}`)
      } else if (op.op === 'order_sections') {
        const counts = {}
        for (const c of Object.values(d.controls || {})) { const s = c.section || ''; counts[s] = (counts[s] || 0) + 1 }
        const sids = Object.keys(d.sections || {})
        if (op.by === 'name') sids.sort((a, b) => String((d.sections[a] || {}).label || a).localeCompare(String((d.sections[b] || {}).label || b)))
        else sids.sort((a, b) => (counts[b] || 0) - (counts[a] || 0))
        const r = await mutate(p => { sids.forEach((sid, i) => { if (p.sections[sid]) { p.sections[sid].row = Math.floor(i / 2); p.sections[sid].order = i % 2 } }) }, `chat: categories ordered by ${op.by}`)
        lines.push(r && r.ok ? `✓ re-laid ${sids.length} categories by ${op.by === 'name' ? 'name' : 'knob count'} — two per band` : `✗ ${(r && r.error) || 'save refused'}`)
      } else if (op.op === 'style') {
        const sid = chatFindSec(d, op.name)
        if (!sid) { lines.push(`✗ style: no category "${op.name}"` + chatSecHint(d, op.name)); continue }
        const r = await mutate(p => { if (op.bold != null) p.sections[sid].bold = op.bold; if (op.font_size != null) p.sections[sid].font_size = op.font_size }, `chat: styled ${op.name}`)
        lines.push(r && r.ok ? `✓ ${op.name} restyled` : `✗ ${(r && r.error) || 'save refused'}`)
      } else if (op.op === 'invert') {
        const cid = chatFindCtl(d, op.label)
        if (!cid) { lines.push(`✗ invert: no control "${op.label}"` + chatCtlHint(d, op.label)); continue }
        if (!chatCtlWired(d, cid)) { lines.push(`✗ invert: "${op.label}" has no wire`); continue }
        const r = await mutate(p => { if (op.on) p.controls[cid].invert = true; else delete p.controls[cid].invert }, `chat: ${op.label} ${op.on ? 'inverted' : 'un-inverted'}`)
        lines.push(r && r.ok ? `✓ ${op.label} ${op.on ? 'inverted on the wire (surface stays 0-127, the device now sees 127-value)' : 'back to normal'}` : `✗ ${(r && r.error) || 'save refused'}`)
      } else if (op.op === 'set') {
        const cid = chatFindCtl(d, op.label)
        if (!cid) { lines.push(`✗ set: no control "${op.label}"` + chatCtlHint(d, op.label)); continue }
        const c = d.controls[cid]
        if (!chatCtlWired(d, cid)) { lines.push(`✗ set: "${op.label}" has no wire`); continue }
        const r = await api.rest('/cc', { method: 'POST', body: { cc: c.cc, value: op.value, channel: Number(d.channel) || 0, bus: activeBusId, source: 'chat' } }).catch(() => null)
        const matrixOwns = !!(st && st.matrix && st.matrix.running) && chatMatrixTargets(d, cid)
        lines.push(r && !r.error
          ? `✓ ${c.label} → ${op.value} (live send — the device moved)` + (matrixOwns ? ' — a running matrix lane also targets it, so the next matrix tick may move it again' : '')
          : `✗ set: ${(r && r.error) || 'unreachable — is the device connected?'}`)
      } else if (op.op === 'matrix_add') {
        const m = chatMapEdge(d, op.edge)
        if (m.err) { lines.push(`✗ lane refused: ${m.err}`); continue }
        const e = m.edge
        const nameOf = (id) => ((d.controls || {})[id] || {}).label || id
        const r = await mutateMx(p => { p.matrix = [...(p.matrix || []), e] }, 'chat: matrix lane added')
        lines.push(r && r.ok ? `✓ added ${e.kind} lane${e.target ? ' → ' + nameOf(e.target) : ''}${e.source ? (e.kind === 'when' ? ` when ${nameOf(e.source)} crosses` : e.kind === 'watch' ? ` on twist of ${nameOf(e.source)}` : ` from ${nameOf(e.source)}`) : ''}` : `✗ ${(r && r.error) || 'save refused'}`)
      } else if (op.op === 'matrix') {
        const r = await api.rest('/matrix/' + op.action, { method: 'POST' }).catch(() => null)
        const nm = (c) => ((d.controls || {})[c] || {}).label || c
        lines.push(r && r.ok
          ? (op.action === 'start'
            ? `✓ matrix running — ${r.edges} lane(s)` + (r.pending && r.pending.length ? ` — waiting for a first value on: ${r.pending.map(nm).join(', ')}; twist those once and the lanes arm themselves` : '')
            : '✓ matrix stopped — every held value restored' + (r.not_restored && r.not_restored.length ? ` (left at last value, never known before the run: ${r.not_restored.map(nm).join(', ')})` : ''))
          : `✗ matrix ${op.action}: ${(r && r.error) || 'unreachable'}`)
        refresh()
      } else if (op.op === 'save_as') {
        const slug = String(op.name || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '')
        if (!slug) { lines.push('✗ save_as: that name has no usable characters'); continue }
        if (edit && dirty) { const ok = await saveDraft(); if (!ok) { lines.push('✗ save_as: the open draft refused to save first — fix that error, then ask again'); continue } }
        const cp = await doCopy(prof.profile_id, slug)
        // a refused copy used to print a green "✓ copied".
        if (!cp || !cp.ok) { lines.push(`✗ save_as: copy refused: ${(cp && cp.error) || 'unreachable'}`); continue }
        const a = await api.rest('/profile/activate', { method: 'POST', body: { id: slug } }).catch(() => null)
        lines.push(a && a.ok ? `✓ saved as "${slug}" and switched to it` : `✓ copied to "${slug}" (switch failed: ${(a && a.error) || 'unreachable'} — pick it in manage profiles)`)
      } else {
        lines.push(`✗ unknown op "${op.op}"`)
      }
    }
    return lines
  }
  const sendChat = async () => {
    const text = chatIn.trim()
    if (!text || chatBusy) return
    if (!prof) { setStatus('chat: activate a preset first'); return }
    const summary = chatSummary()
    const pidAtSend = (((chatLiveDoc && chatLiveDoc()) || prof) || {}).profile_id || null  // A6: ops bind to the doc the model actually saw
    // Follow-up turns need the console's truth, not only the model's promise:
    // feed compact ✓/✗ apply results back so “no, the other filter” can recover.
    const logForCtx = chatLog.filter(m => ['user', 'say', 'done', 'err'].includes(m.role)).slice(-8).map(m => ({
      role: m.role === 'user' ? 'user' : 'assistant',
      text: (m.role === 'done' || m.role === 'err' ? 'console result: ' : '') + m.text,
    }))
    setChatIn(''); setChatLog(l => [...l, { role: 'user', text }]); setChatBusy(true)
    // busy is cleared in `finally`: chatAsk can throw synchronously (an older
    // SDK without the focusedSessionId atom), and a busy flag left true freezes
    // the box until the pane reloads.
    try {
      const r = await chatAsk({ text, effort: chatEffort, summary, log: logForCtx })
      if (!r || !r.ok) { setChatLog(l => [...l, { role: 'err', text: (r && r.error) || 'refused' }]); return }
      if (r.say) setChatLog(l => [...l, { role: 'say', text: r.say + (r.model ? `  ·  ${r.model}` : '') }])
      if ((r.dropped || []).length) setChatLog(l => [...l, { role: 'err', text: `dropped ${r.dropped.length} unknown op(s): ${r.dropped.join(' · ').slice(0, 160)}` }])
      if ((r.ops || []).length) {
        // Abort guard: a throwing op must never leave the box stuck in busy —
        // catch per batch, keep the already-applied truth honest.
        let lines = []
        try { lines = await applyChatOps(r.ops, pidAtSend) } catch (e) { lines = ['✗ apply aborted: ' + ((e && e.message) || 'internal error')] }
        setChatLog(l => [...l, ...lines.map(t => ({ role: t.startsWith('✓') ? 'done' : 'err', text: t }))])
      }
    } finally {
      setChatBusy(false)
    }
  }
  // Ignore/restore law (owner ruling): ignores are persistent until explicitly
  // restored — the Ignored list below the learn prompts is the undo path.
  const persistDismissed = (d) => { setDismissed(d); try { api.storage.set('ignored_ccs', d) } catch (e) {} }
  const ignore = (key) => persistDismissed({ ...dismissed, [key]: true })
  const unignore = (key) => { const d = { ...dismissed }; delete d[key]; persistDismissed(d) }
  const unignoreAll = () => persistDismissed({})
  // ---- color mode (owner ruling): inside edit mode, "Color" opens a part
  // picker. Every colorable part is its own click target — the category label
  // (sec:), the category box (sb:), the control's value incl. its ring (w:),
  // its name (n:) and its card background (b:). One central wheel paints the
  // whole part selection. Select-alls: per category and whole profile.
  const togglePart = (k) => setCsel(s => {
    const d = { ...s }
    if (d[k]) delete d[k]
    else { d[k] = true; lastPickRef.current = { ...(lastPickRef.current || {}), [k]: true } } // re-add memory covers single picks too
    return d
  })
  // Type chips (owner ruling): after a select-all pick, un-pick a WHOLE part
  // type (e.g. every background) with one click — and add it back just as easily.
  const lastPickRef = useRef(null)
  // Cumulative picks (owner ruling): 'select all' ADDS a category's parts to
  // the pick (press Oscillator then Filter — both paint); press again to drop
  // it. The dot tracks the BUTTON (catSel/allSel), so it fills the moment you
  // press select all and stays filled — un-picking single parts inside doesn't
  // unfill it (that would feel broken).
  const toggleCat = (sid) => {
    const on = !!catSel[sid]
    setCatSel(s => { const d = { ...s }; if (on) delete d[sid]; else d[sid] = true; return d })
    setAllSel(false)
    setCsel(s => {
      const p = partsOfCat(sid); const d = { ...s }
      for (const k of Object.keys(p)) { if (on) delete d[k]; else d[k] = true }
      lastPickRef.current = d
      return d
    })
  }
  const toggleAll = () => {
    const on = allSel
    setAllSel(!on)
    setCatSel(on ? {} : Object.fromEntries(Object.keys(prof && prof.sections || {}).map(s => [s, true])))
    setCsel(s => {
      const p = allParts(); const d = { ...s }
      for (const k of Object.keys(p)) { if (on) delete d[k]; else d[k] = true }
      lastPickRef.current = d
      return d
    })
  }
  const clearPicks = () => { setCsel({}); setCatSel({}); setAllSel(false); lastPickRef.current = null }
  const toggleKind = (kind) => setCsel(s => {
    const d = { ...s }
    const has = Object.keys(d).some(k => k.startsWith(kind + ':'))
    if (has) { for (const k of Object.keys(d)) if (k.startsWith(kind + ':')) delete d[k] }
    else {
      // add back: from the remembered pick, or rebuild the kind from the
      // whole profile if the memory is empty (single-click picks only)
      let keys = Object.keys(lastPickRef.current || {}).filter(k => k.startsWith(kind + ':'))
      if (!keys.length) keys = Object.keys(allParts()).filter(k => k.startsWith(kind + ':'))
      for (const k of keys) d[k] = true
    }
    return d
  })
  const partsOfCat = (sid) => {
    const out = { ['sec:' + sid]: true, ['sb:' + sid]: true }
    for (const c of controls) if (c.section === sid) {
      out['w:' + c.__cid] = true; out['b:' + c.__cid] = true
      if ((c.widget || 'knob') !== 'label') out['n:' + c.__cid] = true
    }
    return out
  }
  const allParts = () => {
    let out = {}
    for (const [sid] of sections) out = { ...out, ...partsOfCat(sid) }
    for (const c of unsectioned) { out['w:' + c.__cid] = true; out['b:' + c.__cid] = true; if ((c.widget || 'knob') !== 'label') out['n:' + c.__cid] = true }
    return out
  }
  const partColor = (k) => {
    const i = k.indexOf(':'), kind = k.slice(0, i), id = k.slice(i + 1)
    if (!prof) return null
    if (kind === 'sec') return ((prof.sections || {})[id] || {}).color || null
    if (kind === 'sb') return ((prof.sections || {})[id] || {}).bg || null
    const c = (prof.controls || {})[id] || {}
    return c[kind === 'w' ? 'color' : kind === 'n' ? 'name_color' : 'box_color'] || null
  }
  // Debounced: the wheel fires on every pointer move — one draft edit + one
  // undo entry per drag, not per pixel. (Clearing is NOT here — see clearColor.)
  // Deep rinse: `let saveTimer` was re-declared EVERY render, so the chain
  // broke at each setDraft re-render — a long wheel drag produced an undo
  // entry per ~350 ms burst instead of one. A ref survives renders.
  const saveTimerRef = useRef(null)
  const debouncedMutate = (fn, label) => { clearTimeout(saveTimerRef.current); saveTimerRef.current = setTimeout(() => mutate(fn, label), 350) }
  // Shared paint loop for the color wheel, the hex box and the clear button —
  // one place maps a picked part key -> its color field. set === null deletes.
  const paintKeys = (p, keys, set) => {
    for (const k of keys) {
      const i = k.indexOf(':'), kind = k.slice(0, i), id = k.slice(i + 1)
      const tgt = kind === 'sec' || kind === 'sb' ? (p.sections || {})[id] : (p.controls || {})[id]
      if (!tgt) continue
      const field = kind === 'sec' ? 'color' : kind === 'sb' ? 'bg' : kind === 'w' ? 'color' : kind === 'n' ? 'name_color' : 'box_color'
      if (set === null) delete tgt[field]; else tgt[field] = set
    }
  }
  const paintParts = (p, set) => paintKeys(p, Object.keys(csel), set)
  // "rainbow mode" (owner rename 2026-11-09): rainbow-ify the picks. Each
  // picked unit (a whole CATEGORY is one unit; individually picked parts are
  // units of one) gets its own SLICE of the spectrum, and the parts inside a
  // unit spread across that slice — one category painted = a full rainbow
  // across its label/box/knobs/names; two categories = each owns half.
  const rainbowify = () => {
    const cats = Object.keys(catSel).filter(Boolean)
    const inCat = new Set()
    cats.forEach(sid => Object.keys(partsOfCat(sid)).forEach(k => inCat.add(k)))
    const units = cats.map(sid => {
      // Review: a category selected with ◯ then partially un-picked (a part
      // type toggled off) must rainbow only what is actually picked.
      const keys = Object.keys(partsOfCat(sid))
      const picked = keys.filter(k => csel[k])
      return picked.length ? picked : keys
    })
    for (const k of Object.keys(csel)) if (!inCat.has(k)) units.push([k])
    if (!units.length) { setStatus('rainbow needs picks — click parts or ◯ select category first'); return }
    clearTimeout(saveTimerRef.current)
    mutate(p => { units.forEach((keys, i) => keys.forEach((key, j) => paintKeys(p, [key], hslHex(Math.round(((i + j / keys.length) * 360 / units.length) % 360), 0.85, 0.55)))) }, `rainbowed ${units.length} unit(s)`)  // s/l are FRACTIONS — percent scale made garbage hex the backend refused
    setStatus(`rainbowed ${units.length} unit(s) — each unit owns a slice, its parts spread across it`)
  }
  const applyColor = (hex) => debouncedMutate(p => paintParts(p, hex), `colored ${Object.keys(csel).length} part(s)`)
  // Hex box (owner ask 2026-11-09): exact color by typing — the '#' sits in
  // the box as a label, you type the 6 digits, Enter paints. Discrete input,
  // so immediate (same law as clearColor): kill any pending wheel paint first.
  const applyHex = () => {
    if (!/^[0-9a-fA-F]{6}$/.test(hexIn)) { setStatus('hex needs exactly 6 digits — e.g. ff8800 (the # is already in the box)'); return }
    if (!Object.keys(csel).length) { setStatus('pick a part first, then type a hex code'); return }
    clearTimeout(saveTimerRef.current)
    mutate(p => paintParts(p, '#' + hexIn.toLowerCase()), `colored ${Object.keys(csel).length} part(s)`)
  }
  // Clear color (owner ruling 2026-11-09): the button must ACTUALLY strip the
  // colors — so it mutates IMMEDIATELY (a click is discrete, no debounce
  // needed) and first kills any pending 350 ms wheel paint, which otherwise
  // lands after the clear and re-paints the color the button just removed.
  const clearColor = () => {
    clearTimeout(saveTimerRef.current)
    mutate(p => paintParts(p, null), `color cleared on ${Object.keys(csel).length} part(s)`)
  }

  // ---- category engine (owner ask): categories are editable objects — name,
  // label size/bold, color — and dragging a category header moves the whole
  // group (controls follow automatically: they reference the section id).
  const startSecDrag = (e, sid, sec) => {
    if (!edit || e.button !== 0) return
    if (colorMode) { e.stopPropagation(); togglePart('sec:' + sid); return } // color mode: label click picks the part
    e.preventDefault()
    // Group move (owner ask): categories picked with 'select category' all
    // travel together when you drag any one of them. B9 (// 2026-11-10): the clicked header leads ONLY when it is itself part of
    // the group — dragging an unselected category no longer hauls every
    // previously-selected one along (control cards behave the same way).
    const dragged = secSel[sid] ? [sid, ...Object.keys(secSel).filter(s => secSel[s] && s !== sid)] : [sid]
    const x0 = e.clientX, y0 = e.clientY
    let moved = false
    secPosRef.current = { px: x0, py: y0 }
    secCellRef.current = ''
    secSnapRef.current = snapSecGrid()
    startPointerDrag({
      onMove: (ev) => {
        if (!moved && Math.abs(ev.clientX - x0) + Math.abs(ev.clientY - y0) < 6) return
        moved = true
        secPosRef.current = { px: ev.clientX, py: ev.clientY }
        const g = (rootRef.current || document).querySelector('[data-ov="secghost"]')
        if (g) { g.style.left = (ev.clientX + 12) + 'px'; g.style.top = (ev.clientY + 12) + 'px' }
        const over = computeSecOver(ev.clientX, ev.clientY, dragged)
        const key = over ? over.row + ':' + over.col : '-'
        // Re-render ONLY when the target cell changes — pointer travel inside
        // one cell is ghost-move work, not layout work (drag-freshness law).
        if (key !== secCellRef.current) { secCellRef.current = key; setSecDrag({ sid, sids: dragged, px: ev.clientX, py: ev.clientY, over }) }
      },
      onUp: (ev) => {
        if (!moved) { setSecEdit({ sid, val: sec.label || sid }); secPosRef.current = null; secCellRef.current = ''; return } // plain click = rename
        setSecDrag(null)
        secPosRef.current = null; secCellRef.current = ''
        const over = computeSecOver(ev.clientX, ev.clientY, dragged)
        if (over != null) commitSecMoveAll(dragged, over.row, over.col)
        else setStatus('category snap-back — released outside the category workspace')
      },
      // Deep rinse: a cancelled gesture (touch steal, alt-focus) must ABORT —
      // never commit a phantom drop on the next unrelated pointerup.
      onCancel: () => { setSecDrag(null); secPosRef.current = null; secCellRef.current = '' },
    })
  }
  const snapSecGrid = () => {
    // FROZEN GEOMETRY (night rebuild) + PER-BAND COLUMNS (22:39 rebuild):
    // at pointerdown capture every band's px extent AND each band's OWN
    // column extents (DOM child position == order; slots fill the gaps).
    // Columns are no longer shared across bands — a lower band's content can
    // never resize an upper band's track ("hallucinating being in the way").
    // The whole drag resolves the pointer against THOSE NUMBERS ONLY: the DOM
    // is never re-measured mid-drag and never re-laid-out by the preview.
    const root = rootRef.current || document
    const grid = root.querySelector('[data-secgrid]')
    if (!grid) return null
    const gr = grid.getBoundingClientRect()
    const bands = []
    root.querySelectorAll('[data-secband]').forEach(bEl => {
      const row = parseInt(bEl.getAttribute('data-secband'), 10) || 0
      const br = bEl.getBoundingClientRect()
      const cols = []
      let i = 0
      for (const el of bEl.children) {
        const r = el.getBoundingClientRect()
        cols.push({ col: i, left: r.left, right: r.right })
        i += 1
      }
      bands.push({ row, top: br.top, bottom: br.bottom, cols })
    })
    bands.sort((a, b) => a.row - b.row)
    return { bands, left: gr.left, right: gr.right, top: gr.top, bottom: gr.bottom }
  }
  const computeSecOver = (px, py, sids) => {
    // Category law (pass 4 + containment-first) on PER-BAND columns
    // (22:39 rebuild): a pointer inside a BAND's px span resolves to that
    // storey; the column is resolved against THAT BAND's own frozen column
    // extents — arithmetic against the pointerdown snapshot, no DOM scan,
    // no layout feedback, and no cross-band track coupling at all. Gutters:
    // outside a column span -> nearest column centre in that band; left
    // margin = slot 0; right of the band's last cell past 8 px -> new column
    // IN THAT BAND; above/below all bands past 24 px -> new band (commit
    // normalizes). B7 cancel region unchanged: over toolbar/deck/chat or
    // >220 px outside the grid = null = snap back. Lifted blocks stay greyed
    // in the DOM so releasing back onto your own cell is the true no-op.
    const root = rootRef.current || document
    const snap = secSnapRef.current || (secSnapRef.current = snapSecGrid())
    if (!snap || !snap.bands.length || !snap.bands[0].cols.length) return { row: 0, col: 0 }
    const inZone = (zsel) => { const z = root.querySelector(zsel); if (!z) return false; const zb = z.getBoundingClientRect(); return px >= zb.left - 8 && px <= zb.right + 8 && py >= zb.top - 8 && py <= zb.bottom + 8 }
    if (inZone('[data-tbdrop]') || inZone('[data-deck]') || inZone('[data-chatcard]')) return null
    if (py > snap.bottom + 220 || py < snap.top - 220 || px > snap.right + 220 || px < snap.left - 220) return null
    const colAt = (b) => {
      for (const c of b.cols) { if (px >= c.left && px < c.right) return c.col }
      if (px < b.cols[0].left - 8) return 0
      if (px > b.cols[b.cols.length - 1].right + 8) return b.cols.length
      let bc = b.cols[0].col, bdd = 1e18
      for (const c of b.cols) { const cx = (c.left + c.right) / 2; const d = Math.abs(cx - px); if (d < bdd) { bdd = d; bc = c.col } }
      return bc
    }
    const bandAt = () => {
      for (const b of snap.bands) { if (py >= b.top && py < b.bottom) return b }
      let best = snap.bands[0], bd = 1e18
      for (const b of snap.bands) { const cy = (b.top + b.bottom) / 2; const d = Math.abs(cy - py); if (d < bd) { bd = d; best = b } }
      return best
    }
    const first = snap.bands[0], last = snap.bands[snap.bands.length - 1]
    if (py > last.bottom + 24) return { row: last.row + 1, col: colAt(bandAt()) }
    if (py < first.top - 24) return { row: first.row - 1, col: colAt(bandAt()) }
    const b = bandAt()
    return { row: b.row, col: colAt(b) }
  }

  const commitSecMoveAll = (sids, row, col) => {
    // Review: releasing a nudged category back inside its OWN footprint
    // resolved to a neighbour cell and pushed every mate right forever
    // (columns inflated with each wiggle). Same cell = true no-op — no
    // mutate, no undo entry, no dirty dot.
    {
      // the guard used to cover single drags only —
      // releasing a GROUP onto its own cells re-pushed every mate right, one
      // column per wiggle. Groups are no-ops when each member already sits
      // at col+i in the target band.
      const base = draftRef.current || profLive || {}
      const lift = sids.map(id => (base.sections || {})[id]).filter(Boolean)
        .sort((a, b) => ((a.row || 0) - (b.row || 0)) || ((a.order || 0) - (b.order || 0)))
      if (lift.length === sids.length && lift.every((s, i) => (s.row || 0) === row && (s.order || 0) === col + i)) return
    }
    mutate(p => {
    const entries = Object.entries(p.sections || {})
    const set = new Set(sids)
    const disp = (a, b) => ((a[1].row || 0) - (b[1].row || 0)) || ((a[1].order || 0) - (b[1].order || 0))
    const lifted = entries.filter(([s]) => set.has(s)).sort(disp)
    if (!lifted.length) return
    const inBand = entries.filter(([s, sec]) => !set.has(s) && (sec.row || 0) === row)
    // CHAIN-PUSH (module law, replaces the old shift-everything-at-or-after
    // splice): the landing span [col, col+n-1] pushes ONLY band occupants it
    // actually touches (chain while touching). A section with a gap before it
    // NEVER moves — dropping into a visible empty slot used to make every
    // section right of it flee one column, the owner's "everything around it
    // moves away despite not being touched".
    for (const m of chainPushRow(inBand.map(([sid2, sec]) => ({ id: sid2, x: sec.order || 0, w: 1 })), col, lifted.length)) p.sections[m.id].order = m.x
    lifted.forEach(([sid, sec], i) => { sec.row = row; sec.order = col + i })
    // rows must stay >= 0 (backend law int 0-99): a drop above the top band
    // lands at -1 locally — shift every band down to normalize.
    const mn = Math.min(...entries.map(([, sec]) => sec.row || 0))
    if (mn < 0) for (const [, sec] of entries) sec.row = (sec.row || 0) - mn
    for (const [sid, sec] of entries) p.sections[sid] = sec
    }, sids.length > 1 ? `moved ${sids.length} categories together — controls came along` : 'category moved — its controls came along')
  }
  const commitSecRen = () => {
    if (!secEdit) return
    const { sid, val } = secEdit
    setSecEdit(null)
    if (val.trim()) mutate(p => { p.sections[sid].label = val.trim() }, 'category renamed')
  }
  const createCategory = () => {
    const name = (newSec || '').trim()
    setNewSec(null)
    if (!name || !prof) return
    const slug = name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'cat'
    if (prof.sections && prof.sections[slug]) { setStatus(`category '${slug}' already exists`); return }
    mutate(p => {
      // B10 (2026-11-10): max-order-across-ALL bands + row
      // omitted (defaults to band 0) landed new categories in a distant column
      // when lower bands used high columns. First EMPTY cell, bands top-down.
      const secs = p.sections || {}
      const used = new Set(Object.values(secs).map(s => (s.row || 0) + ':' + (s.order || 0)))
      let row = 0, order = 0, found = false
      for (row = 0; row < 100 && !found; row++) { for (order = 0; order < 100; order++) { if (!used.has(row + ':' + order)) { found = true; break } } }
      p.sections = { ...secs, [slug]: { label: name, row, order } }
    }, `category '${name}' created — drag controls into it`)
  }

  // ---- edit-mode move engine (owner ruling): rubber-band multi-select, group
  // drag with a live drop outline, order-based placement. The flex grid IS the
  // order — x/y are normalized to display order on commit, so a drop lands
  // exactly where the outline showed.
  const startBand = (e) => {
    if (!edit || e.button !== 0) return
    const x0 = e.clientX, y0 = e.clientY
    setBand({ x0, y0, x1: x0, y1: y0 })
    bandPosRef.current = { x1: x0, y1: y0 }
    startPointerDrag({
      onMove: (ev) => {
        bandPosRef.current = { x1: ev.clientX, y1: ev.clientY }
        const el = (rootRef.current || document).querySelector('[data-ov="band"]')
        if (el) { const l = Math.min(x0, ev.clientX), t = Math.min(y0, ev.clientY)
          el.style.left = l + 'px'; el.style.top = t + 'px'
          el.style.width = Math.abs(ev.clientX - x0) + 'px'; el.style.height = Math.abs(ev.clientY - y0) + 'px' }
      },
      onUp: (ev) => {
        setBand(null)
        bandPosRef.current = null
        const l = Math.min(x0, ev.clientX), r = Math.max(x0, ev.clientX)
        const t = Math.min(y0, ev.clientY), b = Math.max(y0, ev.clientY)
        if (r - l < 4 && b - t < 4) return
        const hit = {}
        // Review: scan only THIS pane's root — a second mounted surface (or
        // another plugin reusing data-card) must not cross-contaminate
        // rubber-band hit-testing.
        ;(rootRef.current || document).querySelectorAll('[data-card]').forEach(el => {
          if (el.closest('[data-deck]')) return  // deck cards belong to other profiles — never rubber-band targets
          const k = el.getBoundingClientRect()
          if (k.left < r && k.right > l && k.top < b && k.bottom > t) hit[el.getAttribute('data-card')] = true
        })
        setSel(hit)
        setStatus(Object.keys(hit).length ? `${Object.keys(hit).length} selected — drag any selected card to move them together` : 'selection cleared')
      },
      onCancel: () => setBand(null), // cancel just clears the rubber band
    })
  }
  const computeOver = (px, py, cids) => {
    // Grid law (owner ruling): drops snap to GRID CELLS, not to the packed
    // order. A control lifted out of a cell leaves that cell EMPTY; a drop
    // lands in the cell under the pointer. Cohesive grid, free placement.
    let grid = null, gd = 1e9
    ;(rootRef.current || document).querySelectorAll('[data-grid]').forEach(g => {
      if (g.closest('[data-deck]')) return  // deck grids are not drop targets for the active profile
      const b = g.getBoundingClientRect()
      const dx = Math.max(b.left - px, 0, px - b.right)
      const dy = Math.max(b.top - py, 0, py - b.bottom)
      const d = dx * dx + dy * dy
      if (d < gd) { gd = d; grid = g }
    })
    if (!grid || gd > 90000) return null // pointer far from every grid — no drop
    const sid = grid.getAttribute('data-grid')
    // B5+B6 (owner ruling 2026-11-10): the pointer keeps its GRAB OFFSET —
    // grab an analyzer by its left edge and that edge follows your finger.
    // The old centre-the-span law teleported wide boxes ~2 cells the moment
    // a drag started. grabRef is set at pointerdown; the centre math stays
    // as the fallback for any caller without grab info.
    const gb = grabRef.current
    const offX = gb ? gb.dx : Math.floor((Math.max(1, ...cids.map(id => effW((prof && prof.controls[id]) || {}))) - 1) / 2)
    const offY = gb ? gb.dy : 0
    const rowEls = [...grid.querySelectorAll('[data-row]')]
    if (!rowEls.length) return { sid, col: 0, row: 0 }
    const lastEl = rowEls[rowEls.length - 1]
    const lb = lastEl.getBoundingClientRect()
    const colAt = (b) => Math.max(0, Math.round((px - b.left) / (CELL + 6)) - offX)
    // pointer clearly below the last row -> brand-new row underneath
    if (py > lb.bottom + 24) return { sid, col: colAt(lb), row: Math.max(0, parseInt(lastEl.getAttribute('data-row'), 10) + 1 - offY) }
    let rowEl = rowEls.find(el => { const b = el.getBoundingClientRect(); return py >= b.top && py <= b.bottom })
    if (!rowEl) {
      let best = 1e9
      rowEls.forEach(el => { const b = el.getBoundingClientRect(); const d = Math.max(b.top - py, py - b.bottom); if (d < best) { best = d; rowEl = el } })
    }
    return { sid, col: colAt(rowEl.getBoundingClientRect()), row: Math.max(0, parseInt(rowEl.getAttribute('data-row'), 10) - offY) }
  }
  // Resize law (owner ask 2026-11-09): pull the corner handle of an analyzer
  // card — width snaps to grid cells (1-8), released on up, one undo entry.
  // Live resize preview (owner ask 2026-11-09): while pulling the corner,
  // the card + canvas show the size they WILL be — no blind resizing.
  const [rz, setRz] = useState(null) // {cid, w} while a corner-resize is in flight
  const resizeAnalyzer = (e, c) => {
    if (!edit || e.button !== 0) return
    e.preventDefault(); e.stopPropagation()
    const el = (rootRef.current || document).querySelector('[data-card="' + c.__cid + '"]')
    const left = el ? el.getBoundingClientRect().left : e.clientX
    const wAt = (cx) => Math.max(1, Math.min(8, Math.round((cx - left + (CELL + 6) / 2) / (CELL + 6))))
    let lastW = c.w || 4
    startPointerDrag({
      onMove: (ev) => { const w = wAt(ev.clientX); if (w !== lastW) { lastW = w; setRz({ cid: c.__cid, w }); setStatus('resize: ' + w + ' cell' + (w > 1 ? 's' : '') + ' — release to apply') } },
      onUp: (ev) => { const w = wAt(ev.clientX); setRz(null); if (w !== (c.w || 4)) mutate(p => { const t = p.controls[c.__cid]; if (!t) return; t.w = w
            // B3 (): growing over a row-mate used to
            // SWALLOW it — the cover-set then skipped the consumed control
            // forever. Push, never eat (span-law parity with commitMove).
            if (t.x == null && t.y == null) return  // toolbar home — it owns no row
            const y = t.y || 0, sec = t.section || ''
            const mates = Object.keys(p.controls).filter(id => id !== c.__cid && (p.controls[id].section || '') === sec && (p.controls[id].y || 0) === y)
              .sort((a, b) => (p.controls[a].x || 0) - (p.controls[b].x || 0))
            let cursor = (t.x || 0) + w
            for (const id of mates) { const o = p.controls[id]
              if ((o.x || 0) + effW(o) - 1 < (t.x || 0)) continue  // fully clear to the left — stays put
              if ((o.x || 0) < cursor) o.x = cursor  // consumed or crowded — slide right, chaining
              cursor = (o.x || 0) + effW(o) }
          }, 'analyzer resized to ' + w + ' cells'); setStatus('analyzer is ' + w + ' cells wide') },
      onCancel: () => { setRz(null); setStatus('resize cancelled') },
    })
  }
  // CHAIN-PUSH law (owner ruling 2026-11-10, after ~30 source-pinned patch
  // attempts still read as "utterly broken"): a drop moves ONLY the occupants
  // its footprint ACTUALLY TOUCHES. The old law was list-insertion semantics
  // (shift everything at/after the drop point) pasted onto a spatial grid —
  // dropping into a visible empty cell made untouched neighbours flee, and
  // releasing onto a mate looked like a replacement. Walk right from the
  // landing span: an occupant moves only if it overlaps the span or a mate
  // already pushed into it; the first occupant with a gap before it — and
  // everything right of it — stays put. ONE function feeds BOTH grids
  // (controls: real widths; categories: w=1 columns) so they can never
  // drift apart again. Behaviour-tested in test_ask (node harness).
  const commitMove = (cids, sid, col, row) => {
    // a >=6px lift released over its OWN cell used to
    // push a dead undo entry + a false "unsaved" dot. Same true-no-op law the
    // category path implements: compare the group against its landing cells
    // BEFORE mutating.
    // B5 (owner ruling 2026-11-10): a multi-selection moves as a SHAPE — the
    // old sort-then-lay-into-one-row law flattened a 2×2 block into a 1×4
    // strip. Each item now lands at the group anchor + its own offset.
    {
      const base = draftRef.current || profLiveRef.current || profLive || {}
      const all0 = base.controls || {}
      if (cids.length) {
        const ax0 = Math.min(...cids.map(id => (all0[id] ? all0[id].x || 0 : 1e9)))
        const ay0 = Math.min(...cids.map(id => (all0[id] ? all0[id].y || 0 : 1e9)))
        const same = ax0 !== 1e9 && cids.every(cid => { const c0 = all0[cid]; return c0 && (c0.section || '') === (sid || '') }) && ax0 === col && ay0 === row
        if (same) return
      }
    }
    mutate(p => {
    const all = p.controls
    const movedSet = new Set(cids)
    const ax = Math.min(...cids.map(id => all[id].x || 0))
    const ay = Math.min(...cids.map(id => all[id].y || 0))
    // Insertion law (owner bugfix 2026-11-09): dropping onto an OCCUPIED cell
    // pushes that row's occupants right — the dropped group STICKS where you
    // released it. Span law (owner 2026-11-09): a w>1 footprint needs the WHOLE
    // span to fit; occupants inside it (and everything right of the drop)
    // shift until the leftmost pushed mate clears. Applied per landing row
    // of the group's shape.
    const rowsT = {}
    for (const cid of cids) {
      const t = { cid, x: col + ((all[cid].x || 0) - ax), y: row + ((all[cid].y || 0) - ay), w: effW(all[cid]) }
      ;(rowsT[t.y] = rowsT[t.y] || []).push(t)
    }
    const matesAt = (y) => Object.keys(all).filter(cid => !movedSet.has(cid) && (all[cid].section || '') === sid && (all[cid].y || 0) === y)
    for (const ys of Object.keys(rowsT).map(Number).sort((a, b) => a - b)) {
      const trow = rowsT[ys].sort((a, b) => a.x - b.x)
      for (const t of trow) {
        // CHAIN-PUSH (module law): only occupants the footprint (or a pushed
        // mate) actually touches slide right; a mate with a gap before it
        // NEVER moves. The old shift-everything-at-or-after-xmin law made a
        // whole row flee from one drop — the owner's "everything around it
        // moves away despite not being touched".
        for (const m of chainPushRow(matesAt(ys).map(cid2 => ({ id: cid2, x: all[cid2].x || 0, w: effW(all[cid2]) })), t.x, t.w)) all[m.id].x = m.x
      }
      for (const t of trow) { all[t.cid].x = t.x; all[t.cid].y = t.y; if (sid) all[t.cid].section = sid; else delete all[t.cid].section }
    }
    }, `moved ${cids.length} control(s)`)
  }
  const startCardDrag = (e, c) => {
    if (!edit || e.button !== 0) return
    if (colorMode) return // color mode: the card wrapper handles clicks (box part)
    e.preventDefault()
    const cids = sel[c.__cid] ? Object.keys(sel).filter(k => prof.controls[k]) : [c.__cid]
    const x0 = e.clientX, y0 = e.clientY
    let moved = false
    // B5+B6 (owner ruling 2026-11-10): capture WHERE the finger grabbed,
    // relative to the group's top-left cell. The landing anchor = pointer
    // cell - this offset, so the grabbed edge stays under the finger and
    // multi-row groups keep their shape.
    {
      const bs = prof.controls
      const axG = Math.min(...cids.map(id => (bs[id] || {}).x || 0))
      const ayG = Math.min(...cids.map(id => (bs[id] || {}).y || 0))
      const el0 = (rootRef.current || document).querySelector('[data-card="' + c.__cid + '"]')
      const r0 = el0 && el0.getBoundingClientRect()
      grabRef.current = { dx: ((bs[c.__cid] || {}).x || 0) - axG + (r0 ? Math.max(0, Math.round((x0 - r0.left) / (CELL + 6))) : 0),
                          dy: ((bs[c.__cid] || {}).y || 0) - ayG }
    }
    dragPosRef.current = { px: x0, py: y0 }
    dragCellRef.current = ''
    startPointerDrag({
      onMove: (ev) => {
        if (!moved && Math.abs(ev.clientX - x0) + Math.abs(ev.clientY - y0) < 6) return
        moved = true
        dragPosRef.current = { px: ev.clientX, py: ev.clientY }
        const g = (rootRef.current || document).querySelector('[data-ov="ghost"]')
        if (g) { g.style.left = (ev.clientX + 12) + 'px'; g.style.top = (ev.clientY + 12) + 'px' }
        // Toolbar drop law (owner bug 2026-11-10): the toolbar sits inside the
        // grid's 300 px snap radius, so hovering an analyzer over it still
        // painted the placeholder down in the workspace. Over the toolbar the
        // TOOLBAR gets the dashed highlight and the grid placeholder is off.
        const canTb = cids.length === 1 && ((prof.controls[cids[0]] || {}).widget === 'analyzer')
        const tbEl = (rootRef.current || document).querySelector('[data-tbdrop]')
        const tbRc = tbEl && tbEl.getBoundingClientRect()
        const overTb = canTb && tbRc && ev.clientY >= tbRc.top - 10 && ev.clientY <= tbRc.bottom + 10 && ev.clientX >= tbRc.left && ev.clientX <= tbRc.right
        if (tbEl) tbEl.style.outline = overTb ? '2px dashed var(--ui-accent)' : ''
        const ov = overTb ? null : computeOver(ev.clientX, ev.clientY, cids)
        const key = ov ? ov.sid + ':' + ov.col + ':' + ov.row : (overTb ? 'tb' : '-')
        if (key !== dragCellRef.current) { dragCellRef.current = key; setDrag({ cids, px: ev.clientX, py: ev.clientY, over: ov }) }
      },
      onUp: (ev) => {
        dragPosRef.current = null; dragCellRef.current = ''; grabRef.current = null
        if (!moved) { // plain click on a card toggles its selection
          const s = { ...sel }; if (s[c.__cid]) delete s[c.__cid]; else s[c.__cid] = true
          setSel(s)
          return
        }
        setDrag(null)
        // Toolbar first (owner bug 2026-11-09): an analyzer released over the
        // toolbar row goes BACK to the toolbar — the old code only checked
        // 'far from every grid', and the toolbar sits close enough to the
        // grids that the box always snapped straight back onto the surface.
        const tb = (rootRef.current || document).querySelector('[data-tbdrop]')
        if (tb) tb.style.outline = ''
        const tbB = tb && tb.getBoundingClientRect()
        const overTb = tbB && ev.clientY >= tbB.top - 10 && ev.clientY <= tbB.bottom + 10 && ev.clientX >= tbB.left && ev.clientX <= tbB.right
        if (overTb && cids.length === 1 && (prof.controls[cids[0]] || {}).widget === 'analyzer')
          mutate(p => { const t = p.controls[cids[0]]; if (t) { delete t.section; delete t.x; delete t.y } }, 'analyzer back to the toolbar')
        else {
          const over = computeOver(ev.clientX, ev.clientY, cids)
          if (over) commitMove(cids, over.sid, over.col, over.row)
          else if (cids.length === 1 && (prof.controls[cids[0]] || {}).widget === 'analyzer')
            // Analyzer law: released off any grid = back to the toolbar home.
            mutate(p => { const t = p.controls[cids[0]]; if (t) { delete t.section; delete t.x; delete t.y } }, 'analyzer back to the toolbar')
          else setStatus('drop cancelled — release over the surface to place')
        }
      },
      // Deep rinse: a cancelled drag must ABORT, never commit a phantom drop.
      onCancel: () => { setDrag(null); grabRef.current = null; const t = (rootRef.current || document).querySelector('[data-tbdrop]'); if (t) t.style.outline = '' },
    })
  }

  const exportProfile = async () => {
    if (!prof) return
    // Review: the only REST handler without a catch — backend-down threw an
    // unhandled rejection and the click died silently (#13 law).
    const r = await api.rest('/profile/export?id=' + encodeURIComponent(prof.profile_id)).catch(() => null)
    if (r && r.json) {
      const a = document.createElement('a')
      a.href = URL.createObjectURL(new Blob([r.json], { type: 'application/json' }))
      a.download = prof.profile_id + '.json'
      a.click()
      setTimeout(() => URL.revokeObjectURL(a.href), 10000)  // deep rinse: blob URLs were never revoked
    } else setStatus(`export: ${(r && r.error) || 'backend unreachable'}`)
  }
  const importProfile = () => { if (fileRef.current) fileRef.current.click() }
  const onPickFile = (e) => {
    const f = e.target.files && e.target.files[0]
    e.target.value = ''
    if (!f) return
    const rd = new FileReader()
    rd.onload = async () => {
      setMenu(null)  // review: the menu closes only once a file was actually chosen
      const r = await api.rest('/profile/import', { method: 'POST', body: { json: String(rd.result) } }).catch(() => null)
      setStatus(r && r.ok ? 'imported' : `import rejected: ${(r && r.error) || 'unreachable'}`)
      loadLists(); refresh(true)
    }
    rd.readAsText(f)
  }
  const createProfile = async () => {
    const name = (newProf || '').trim()
    setNewProf(null)
    if (!name) return
    const id = name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '') || 'profile'
    // Review: save_profile overwrites without an existence check — naming a
    // new device "MicroFreak" silently replaced the live user preset with an
    // empty doc. Refuse by name.
    if (profiles.some(p => p.profile_id === id)) { setStatus(`a preset "${id}" already exists — pick another name (or edit that one)`); return }
    const p = { schema_version: 1, profile_id: id, device: name, channel: 0, controls: {}, sections: {} }
    const r = await api.rest('/profile/save', { method: 'POST', body: { doc: p } }).catch(() => null)
    if (r && r.ok) {
      await api.rest('/profile/activate', { method: 'POST', body: { id } }).catch(() => null)
      setStatus(`created profile "${name}" (${id}) — twist knobs or use + Control to fill it`)
    } else setStatus(`profile rejected: ${(r && r.error) || 'unreachable'}`)
    loadLists(); refresh(true)
  }

  // Analyzer span law: a hand-authored analyzer without w defaults to 4 cells EVERYWHERE (canvas + grid footprint) — the old split default (canvas 4, grid 1) desynced span math (review 2026-11-09).
  const controls = prof ? Object.entries(prof.controls).map(([cid, c]) => ({ ...c, __cid: cid, ...(c.widget === 'analyzer' && !c.w ? { w: 4 } : {}) })) : []
  const sections = prof ? Object.entries(prof.sections || {}).sort((a, b) => (a[1].order || 0) - (b[1].order || 0)) : []
  // category color inheritance: a control with no own color wears its category's
  const secColor = {}
  for (const [sid, sec] of sections) if (sec.color) secColor[sid] = sec.color
  const unsectioned = controls.filter(c => !c.section && !(c.widget === 'analyzer' && c.x == null && c.y == null)) // B4 (): an UNPLACED section-less analyzer lives on the toolbar; once a drop gives it grid coords it renders between the knobs — the drag law always promised, the renderer used to veto it (drops looked fake)
  const renderCtrl = (c) => {
    // Analyzer widget (owner ask 2026-11-09): the spectrum box lives in the
    // grid like any control — same drag, same category assignment, same delete.
    if (c.widget === 'analyzer') {
      const box = jsx(SpectrumBox, { api, spec: { id: c.__cid, source: c.source || 'pc', type: c.type || 'bars', w: rz && rz.cid === c.__cid ? rz.w : (c.w || 4), color: c.color || null }, sources: specSources, onPatch: patchSpecCtrl, onRemove: () => del(c.__cid) })
      // Color mode: clicking the analyzer picks its TINT part ('w:') — the
      // bars/curve/meters take the color, like a knob's ring does.
      return colorMode ? jsx('span', { onClick: (e) => { e.stopPropagation(); togglePart('w:' + c.__cid) }, 'data-tip': 'Pick the analyzer tint — wheel, hex box or rainbow mode paint its visuals', style: { cursor: 'copy', display: 'inline-flex' }, children: box }) : box
    }
    const ent = ccMap[(Number(prof && prof.channel) || 0) + ':' + c.cc]
    const v = ent ? ent.value : null
    const color = c.color || secColor[c.section || ''] || null
    // part: color-mode plumbing — this control's click-target identity for the
    // wheel (w: value+ring, n: name, b: card bg handled by the card wrapper)
    const part = { mode: colorMode, sel: csel, toggle: togglePart, cid: c.__cid, nameColor: c.name_color || null }
    // Deck law: every send carries this surface's bus + channel — the active
    // profile's bus (omitted == 'primary', the historical default).
    const ctx = { chan: Number(prof && prof.channel) || 0, bus: activeBusId }
    const args = { ctrl: c, value: v, edit, color, part, ctx } // drag is handled by the card wrapper (startCardDrag) — widgets no longer need a drag prop
    // stepped vs slider (owner ruling 2026-11-09): ONE control — measured
    // labels make a slider detent (stepped); without labels it is an honest
    // raw slider (#12 law kept: never invent detents a synth doesn't have).
    // 'stepped' stays accepted in saved presets as a legacy slider alias.
    if (c.widget === 'slider' || c.widget === 'stepped')
      return (c.labels && Object.keys(c.labels).length) ? jsx(Stepped, { ...args, key: c.__cid }) : jsx(Slider, { ...args, key: c.__cid })
    if (c.widget === 'toggle') return jsx(Toggle, { ctrl: c, value: v, edit, color, part, ctx, key: c.__cid })
    if (c.widget === 'momentary') return jsx(Momentary, { ctrl: c, value: v, edit, color, part, ctx, key: c.__cid })
    if (c.widget === 'label') return jsx(LabelWidget, { ctrl: c, color, part, key: c.__cid })
    return jsx(Knob, { ...args, key: c.__cid })
  }

  const connected = st && st.connected
  const secHead = (sid, sec) => jsxs('div', { style: { display: 'flex', gap: 6, alignItems: 'center', marginBottom: 6, flexWrap: 'wrap' }, children: [
    secEdit && secEdit.sid === sid && !colorMode
      ? jsx('input', { autoFocus: true, value: secEdit.val, onChange: (e) => setSecEdit({ sid, val: e.target.value }), onKeyDown: (e) => { if (e.key === 'Enter') commitSecRen(); else if (e.key === 'Escape') setSecEdit(null) }, onBlur: commitSecRen, style: { ...tinyS, width: 150, fontSize: 12, padding: '2px 6px', background: 'var(--ui-bg-secondary)', color: 'var(--ui-text-primary)' } })
      : jsx('span', { onPointerDown: (e) => startSecDrag(e, sid, sec),
          // Deep rinse: stopPropagation on POINTERDOWN does not stop the
          // CLICK — in color mode one label click used to pick BOTH 'sec:'
          // (from pointerdown) and 'sb:' (click bubbling to the section
          // block), silently over-painting. Swallow the click here instead.
          onClick: colorMode ? (e) => e.stopPropagation() : undefined,
          'data-tip': colorMode ? 'Click to pick this category label for the color wheel' : edit ? 'Click = rename · drag = move the whole category (all its controls go with it)' : undefined,
          style: { fontSize: sec.font_size || 11, fontWeight: sec.bold ? 700 : 600, textTransform: 'uppercase', letterSpacing: '0.12em',
            color: sec.color ? neon(sec.color) : 'var(--ui-text-secondary)', textShadow: sec.color ? `0 0 5px ${neon(sec.color)}` : 'none',
            cursor: colorMode ? 'copy' : edit ? 'move' : 'default', padding: edit ? '2px 8px' : 0,
            border: edit && !colorMode ? '1px dashed var(--ui-stroke-secondary)' : 'none', borderRadius: 6,
            ...(colorMode ? partRing({ mode: true, sel: csel, cid: sid }, 'sec') : {}) },
          children: sec.label || sid }),
    // Word-style label size: type the number (8-48), no stepper buttons.
    // Kept visible in color mode (disabled) so the header never changes size
    // between modes — layout stability (owner: transitions were disorienting).
    edit ? jsx('input', { type: 'number', min: 8, max: 48, disabled: colorMode,
      key: 'fs-' + sid + '-' + (sec.font_size != null ? sec.font_size : 11),
      defaultValue: sec.font_size != null ? sec.font_size : 11,
      onBlur: (ev) => { const v = Math.max(8, Math.min(48, parseInt(ev.target.value, 10) || 11)); ev.target.value = v; if (v !== (sec.font_size != null ? sec.font_size : 11)) mutate(p => { p.sections[sid].font_size = v }, 'category label size set') },
      onKeyDown: (ev) => { if (ev.key === 'Enter') ev.target.blur() },
      'data-tip': 'Category label size (8-48) — type a number, Enter applies',
      style: { ...tinyS, width: 44, background: 'var(--ui-bg-secondary)', color: 'var(--ui-text-primary)', ...(colorMode ? { opacity: 0.35, pointerEvents: 'none' } : {}) } }) : null,
    edit ? jsx('button', { onClick: () => mutate(p => { p.sections[sid].bold = !p.sections[sid].bold }, 'category bold toggled'), 'data-tip': 'Bold the category label', style: { ...tinyS, fontWeight: 700, ...(sec.bold ? { color: 'var(--ui-accent)', borderColor: 'var(--ui-accent)' } : {}), ...(colorMode ? { opacity: 0.35, pointerEvents: 'none' } : {}) }, children: 'B' }) : null,
    // 'select category' — same button in both modes (owner ruling): in move
    // mode it toggles the category into the GROUP selection (drag any selected
    // category and the whole group moves); in color mode it toggles its parts.
    edit && !colorMode ? jsx('button', { onClick: (e) => { e.stopPropagation(); const mine = controls.filter(c => c.section === sid); const on = !!secSel[sid]; setSecSel(s => { const d = { ...s }; if (on) delete d[sid]; else d[sid] = true; return d }); setSel(pv => { const d = { ...pv }; for (const c of mine) { if (on) delete d[c.__cid]; else d[c.__cid] = true } return d }) }, 'data-tip': 'Add/remove this category from the group selection — drag any selected category header to move the whole group', style: { ...tinyS, ...(secSel[sid] ? { color: 'var(--ui-accent)', borderColor: 'var(--ui-accent)' } : {}) }, children: secSel[sid] ? '⊞ selected' : '⊞ select category' }) : null,
    // delete category (its controls stay, just unsectioned)
    edit && !colorMode ? jsx('button', { onClick: (e) => { e.stopPropagation(); const n = controls.filter(c => c.section === sid).length; if (window.confirm(`Delete category '${sec.label || sid}'? Its ${n} control(s) stay — just unsectioned.`)) { mutate(p => { delete p.sections[sid]; for (const c of Object.values(p.controls)) if (c.section === sid) delete c.section }, `category '${sec.label || sid}' deleted — controls unsectioned`); setSecSel(s => { const d = { ...s }; delete d[sid]; return d }) } }, 'data-tip': 'Delete this category — its controls stay, just unsectioned', style: tinyS, children: '×' }) : null,
    edit && colorMode ? jsx('button', { onClick: (e) => { e.stopPropagation(); toggleCat(sid) }, 'data-tip': (catSel[sid] ? 'Remove this category from the color pick' : 'Add this category to the color pick — label, box, knobs, names; press more categories to add them too'), style: { ...tinyS, ...(catSel[sid] ? { color: 'var(--ui-accent)', borderColor: 'var(--ui-accent)' } : {}) }, children: catSel[sid] ? '◉ selected' : '◯ select category' }) : null,
  ] }, 'sechead-' + sid)
  // (dropLine retired 2026-11-09 pass 3: the category grid draws its own
  // proportional placeholder child — see the secflow renderer.)
  // Review: the renderGrid ops object was written out twice (sections IIFE +
  // unsectioned grid) — miss one and the unsectioned surface gets undefined
  // handlers. One copy; spread with the sid at each use site.
  const gops = { rename, setWidget, del, rebindClick, rebind, renaming, setRenaming, commitRen, setCc, setStatus, sel, drag, startCardDrag, startBand, colorMode, csel, togglePart, specSources, patchSpecCtrl, resizeAnalyzer, rz, ctrls: (draftRef.current || prof || {}).controls || {} }

  return jsxs('div', { ref: rootRef, style: { padding: 16, maxWidth: 1700 }, children: [
    // header bar (owner rework 2026-11-09): PRESET box = the whole rig
    // (active preset, deck, add device, MIDI output, connect/disconnect);
    // EDIT and SETTINGS are equal-size bold caps buttons whose options drop
    // DOWNWARD like a context menu.
    jsxs('div', { 'data-tbdrop': '1', style: { display: 'flex', flexWrap: 'wrap', gap: 10, alignItems: 'flex-start', marginBottom: 12 }, children: [
      // Two stacked boxes, same width and look (owner swap 2026-11-09):
      // PRESET on top (deck, select preset, active profile), MIDI below
      // (output pair + connect/disconnect) — presets first, ports second.
      // HIDE/SHOW (owner ask 2026-11-09): the tiny bold box lives in the
      // PRESET box's top-left corner; pressing it tucks the whole stacked
      // bar away — only SHOW remains and the rest of the toolbar snaps snug.
      barHidden
        ? jsx('button', { onClick: () => { setBarHidden(false); try { api.storage.set('bar', '') } catch (e) {} }, 'data-tip': 'Bring the PRESET / MIDI / PROFILE bar back', style: { ...hideBtnS, marginTop: 5 }, children: 'SHOW' })
        : jsxs('span', { style: { display: 'inline-flex', flexDirection: 'column', gap: 6 }, children: [
        jsxs('span', { style: { display: 'inline-flex', gap: 6, alignItems: 'center', flexWrap: 'wrap', border: '1px solid var(--ui-stroke-secondary)', borderRadius: 8, padding: '5px 9px' }, children: [
          jsx('button', { onClick: () => { setBarHidden(true); try { api.storage.set('bar', '1') } catch (e) {} }, 'data-tip': 'Tuck the whole PRESET / MIDI / PROFILE bar away — the console gets smaller. SHOW brings it back', style: hideBtnS, children: 'HIDE' }),
          jsx('span', { style: { fontSize: 10, fontWeight: 800, letterSpacing: '0.08em', color: 'var(--ui-text-secondary)' }, children: 'PRESET' }),
          // Deck: presets sharing THIS screen — each its own surface + bus.
          deckIds.length ? jsx('span', { 'data-tip': 'Presets on this screen (first = active/edit surface)', style: { fontSize: 10, color: 'var(--ui-text-secondary)', border: '1px solid var(--ui-stroke-secondary)', borderRadius: 6, padding: '4px 7px' }, children: 'deck: ' + deckIds.join(' + ') }) : null,
          // Select preset (owner ask 2026-11-09): ONE menu — first option adds
          // a device, then every saved deck layout (whole-screen setups the
          // backend remembers, named by their presets, e.g. 'microfreak +
          // cloudburst'). Picking one swaps the whole screen in one click.
          presetMode === 'decks'
            ? jsx('select', { value: '', onChange: (e) => { const v = e.target.value; if (v === 'add') setPresetMode('add'); else if (v.startsWith('d')) { const ids = deckPresets[+v.slice(1)]; if (ids) deckSet(ids.slice()) } }, 'data-tip': 'Swap this whole screen to a saved preset layout — or add a device to the current one', style: { padding: 6, background: 'var(--ui-bg-secondary)', color: 'var(--ui-text-secondary)', border: '1px solid var(--ui-stroke-secondary)', borderRadius: 6 },
                children: [jsx('option', { value: '', children: 'select preset…' }, 'ph'), jsx('option', { value: 'add', children: 'add device…' }, 'add')].concat(
                  deckPresets.map((ids, i) => jsx('option', { value: 'd' + i, children: ids.join(' + ') }, 'd' + i))) })
            : jsx('select', { value: '', onChange: (e) => { const v = e.target.value; if (v === 'back') setPresetMode('decks'); else if (v) { deckSet([...new Set([...deckRef.current, v])]); setPresetMode('decks') } }, 'data-tip': 'Put another device preset on this screen — separate surface, separate MIDI bus; twist its knobs live', style: { padding: 6, background: 'var(--ui-bg-secondary)', color: 'var(--ui-text-secondary)', border: '1px solid var(--ui-stroke-secondary)', borderRadius: 6 },
                children: [jsx('option', { value: '', children: 'add device…' }, 'ph'), jsx('option', { value: 'back', children: '← presets' }, 'back')].concat(
                  profiles.filter(p => p.valid && !deckIds.includes(p.profile_id)).map(p => jsx('option', { value: p.profile_id, children: `${p.device} (${p.profile_id})` }, p.profile_id))) }),
        ] }),
        jsxs('span', { style: { display: 'inline-flex', gap: 6, alignItems: 'center', flexWrap: 'wrap', border: '1px solid var(--ui-stroke-secondary)', borderRadius: 8, padding: '5px 9px' }, children: [
          jsx('span', { style: { fontSize: 10, fontWeight: 800, letterSpacing: '0.08em', color: 'var(--ui-text-secondary)' }, children: 'MIDI' }),
          jsx('select', { value: devPick, onChange: (e) => setDevPick(e.target.value), style: { padding: 6, background: 'var(--ui-bg-secondary)', color: 'var(--ui-text-primary)', border: '1px solid var(--ui-stroke-secondary)', borderRadius: 6 },
            children: [jsx('option', { value: '', children: 'pick a MIDI output…' }, 'ph')].concat(
              devices.filter(d => d.dir === 'out').map(d => jsx('option', { value: d.name, children: d.name }, d.name))) }),
          jsx('button', { onClick: connect, disabled: !devPick, 'data-tip': 'Open the chosen MIDI output + input pair. Windows ports are exclusive — close other apps (e.g. a DAW) first', style: { border: '1px solid var(--ui-accent)', color: 'var(--ui-accent)', background: 'transparent', padding: '6px 10px', borderRadius: 6, cursor: 'pointer' }, children: 'Connect' }),
          jsx('button', { onClick: disconnect, disabled: !connected, 'data-tip': 'Release the MIDI ports (releases any notes we held first)', style: { border: '1px solid var(--ui-stroke-secondary)', color: 'var(--ui-text-secondary)', background: 'transparent', padding: '6px 10px', borderRadius: 6, cursor: 'pointer' }, children: 'Disconnect' }),
          // PROFILE (owner move 2026-11-09): the active-preset picker shares
          // the MIDI box, right of Disconnect, labelled like PRESET/MIDI.
          jsx('span', { style: { width: 1, alignSelf: 'stretch', background: 'var(--ui-stroke-secondary)' } }),
          jsx('span', { style: { fontSize: 10, fontWeight: 800, letterSpacing: '0.08em', color: 'var(--ui-text-secondary)' }, children: 'PROFILE' }),
          jsx('select', { value: prof ? prof.profile_id : '', onChange: (e) => { if (!e.target.value) return; activate(e.target.value) }, 'data-tip': 'The active preset — its layout, controls and colors (first in the deck)', style: { padding: 6, background: 'var(--ui-bg-secondary)', color: 'var(--ui-text-primary)', border: '1px solid var(--ui-stroke-secondary)', borderRadius: 6 },
            children: [jsx('option', { value: '', children: 'no profile active' }, 'ph')].concat(
              profiles.map(p => jsx('option', { value: p.profile_id, children: `${p.device} (${p.controls} ctrl, ${p.tier})${p.valid ? '' : ' INVALID'}` }, p.profile_id))) }),
        ] }),
      ] }),
      // Connection law (owner ruling 2026-11-09): ONLY the dot is green/grey;
      // the synth name is bold and 3 sizes up. Vertically centered on the
      // EDIT/SETTINGS band (same marginTop + height — owner alignment fix).
      jsxs('span', { style: { fontSize: 15, fontWeight: 700, marginTop: 5, height: 32, display: 'inline-flex', alignItems: 'center', gap: 7 }, children: [
        jsx('span', { style: { color: connected ? '#3fb950' : 'var(--ui-text-secondary)', textShadow: connected ? '0 0 8px #3fb950' : 'none' }, children: connected ? '●' : '○' }),
        jsx('span', { style: { color: 'var(--ui-text-primary)' }, children: connected ? `${connected.out}${!connected.in ? ' (no mirror)' : ''}` : 'not connected' }),
      ] }),
      // EDIT | SETTINGS — one wrapper so an outside click closes either menu.
      jsxs('span', { ref: menuWrapRef, style: { display: 'inline-flex', gap: 10, alignItems: 'flex-start', marginTop: 5 }, children: [
        jsxs('span', { style: { position: 'relative', display: 'inline-flex' }, children: [
          // SAVE EDIT (owner ruling 2026-11-09, 2nd batch): one click SAVES and
          // leaves edit mode — pressing it already implies saving, no prompt.
          // While unsaved changes exist, DISCARD sits immediately to its right,
          // same size/font/style; it throws the draft away and is Undo-able.
          // kill the pending wheel paint — it used to land AFTER
          // the save (re-dirty) or be destroyed by exitEdit (lost edit).
          jsx('button', { onClick: () => { if (edit) { clearTimeout(saveTimerRef.current); if (dirty) saveDraft().then(ok => { if (ok) exitEdit() }); else exitEdit() } else setMenu(menu === 'edit' ? null : 'edit') }, 'data-tip': edit ? (dirty ? 'Save the draft to the preset and leave edit mode — one click' : 'Leave edit mode (nothing to save)') : 'Edit this preset — Layout (move, rename, rebind, categories) or Color (paint parts with the wheel)', style: { ...bigBtnS, border: `1px solid ${edit ? 'var(--ui-accent)' : 'var(--ui-stroke-secondary)'}`, color: edit ? 'var(--ui-accent)' : 'var(--ui-text-primary)' }, children: edit ? (dirty ? 'SAVE EDIT ●' : 'SAVE EDIT') : 'EDIT' }),
          edit && dirty ? jsx('button', { onClick: doDiscard, 'data-tip': 'Throw away every unsaved edit (Undo brings them back)', style: { ...bigBtnS, border: '1px solid #ff5f56', color: '#ff5f56' }, children: 'DISCARD' }) : null,
          menu === 'edit' ? jsxs('div', { style: menuPanelS, children: [
            jsx('button', { onClick: () => {
                setMenu(null)
                if (edit) { if (dirty) setExitAsk(true); else exitEdit() }
                else if (!prof) setStatus('nothing to edit — activate a preset first')
                else { setEdit(true); putDraft(JSON.parse(JSON.stringify(prof))); undoRef.current = []; redoRef.current = []; setSel({}); setCsel({}); setSecSel({}); setColorMode(false) }
              }, 'data-tip': 'Layout editing — rename, rebind, widget type, delete, move, categories. Press again to leave edit mode', style: { ...menuItemS, ...(edit && !colorMode ? { color: 'var(--ui-accent)', borderColor: 'var(--ui-accent)' } : {}) }, children: edit && !colorMode ? '✓ Layout' : 'Layout' }),
            jsx('button', { onClick: () => {
                setMenu(null)
                if (!edit) {
                  if (!prof) setStatus('nothing to edit — activate a preset first')
                  else { setEdit(true); putDraft(JSON.parse(JSON.stringify(prof))); undoRef.current = []; redoRef.current = []; setSel({}); setCsel({}); setSecSel({}); setColorMode(true) }
                } else if (colorMode) { if (dirty) setExitAsk(true); else exitEdit() }
                else setColorMode(true)
              }, 'data-tip': 'Color mode — pick parts and paint them with the wheel. Press again to leave edit mode', style: { ...menuItemS, border: 'none', background: 'transparent' }, children: edit && colorMode ? rainbow('✓ Color', 13) : rainbow('Color', 13) }),
          ] }) : null,
        ] }),
        jsxs('span', { style: { position: 'relative', display: 'inline-flex' }, children: [
          jsx('button', { onClick: () => { if (menu === 'settings') setIgnoredOpen(false); setMenu(menu === 'settings' ? null : 'settings') }, 'data-tip': 'Preset tools — create presets/controls, export/import, panic, refresh', style: { ...bigBtnS, border: `1px solid ${menu === 'settings' ? 'var(--ui-accent)' : 'var(--ui-stroke-secondary)'}`, color: menu === 'settings' ? 'var(--ui-accent)' : 'var(--ui-text-primary)' }, children: 'SETTINGS' }),
          menu === 'settings' ? jsxs('div', { style: { ...menuPanelS, minWidth: 230 }, children: [
            newProf != null
              ? jsxs('span', { style: { display: 'inline-flex', gap: 4, alignItems: 'center' }, children: [
                  jsx('input', { autoFocus: true, placeholder: 'new preset name…', value: newProf, onChange: (e) => setNewProf(e.target.value), onKeyDown: (e) => { if (e.key === 'Enter') createProfile(); if (e.key === 'Escape') setNewProf(null) }, style: { ...inpS, width: 150 } }),
                  jsx('button', { onClick: () => setNewProf(null), 'data-tip': 'Cancel — go back without creating a preset', style: tinyS, children: '×' }),
                ] })
              : jsx('button', { onClick: () => setNewProf(''), 'data-tip': 'Create an empty user preset — then fill it by twisting knobs (learn) or add control', style: menuItemS, children: 'add new device' }),
            newCtl
              ? jsxs('span', { style: { display: 'inline-flex', gap: 4, alignItems: 'center' }, children: [
                  jsx('input', { autoFocus: true, type: 'number', min: 0, max: 127, placeholder: 'CC 0-127', value: newCtl.cc, onChange: (e) => setNewCtl({ ...newCtl, cc: e.target.value }), onKeyDown: (e) => { if (e.key === 'Enter') addCtl(); if (e.key === 'Escape') setNewCtl(null) }, style: { ...inpS, width: 84 } }),
                  jsx('input', { placeholder: 'name (optional)', value: newCtl.name, onChange: (e) => setNewCtl({ ...newCtl, name: e.target.value }), onKeyDown: (e) => { if (e.key === 'Enter') addCtl(); if (e.key === 'Escape') setNewCtl(null) }, style: { ...inpS, width: 120 } }),
                  jsx('button', { onClick: addCtl, style: { border: '1px solid var(--ui-accent)', color: 'var(--ui-accent)', background: 'transparent', padding: '4px 10px', borderRadius: 6, cursor: 'pointer' }, children: 'Add' }),
                  jsx('button', { onClick: () => setNewCtl(null), style: tinyS, children: '×' }),
                ] })
              : jsx('button', { onClick: addManual, 'data-tip': 'Add a control by CC number — for devices that never send back; for echoing devices just twist a knob and accept the learn prompt', style: menuItemS, children: 'control' }),
            jsx('button', { onClick: () => { setMenu(null); exportProfile() }, disabled: !prof, 'data-tip': 'Download the active preset as a .json file (share it, diff it, import it elsewhere)', style: { ...menuItemS, opacity: prof ? 1 : 0.45 }, children: 'export' }),
            jsx('button', { onClick: importProfile, 'data-tip': 'Import a preset .json file', style: menuItemS, children: 'import' }),
            jsx('button', { onClick: () => { setMenu(null); api.rest('/panic', { method: 'POST' }).then(r => setStatus(r && r.ok ? `panic: all notes off sent${r.buses ? ' on ' + r.buses.length + ' bus(es)' : ''}` : `panic: ${(r && r.error) || 'unreachable'}`)).catch(() => setStatus('panic: backend unreachable')) }, disabled: !(((st && st.buses) || []).some(b => b.connected) || connected), 'data-tip': 'All notes off (CC 123) on every channel + release anything we held — sweeps EVERY bus on screen', style: { ...menuItemS, opacity: (((st && st.buses) || []).some(b => b.connected) || connected) ? 1 : 0.45 }, children: 'panic' }),
            jsx('button', { onClick: loadLists, 'data-tip': 'Re-enumerate MIDI devices and presets (menu stays open)', style: menuItemS, children: 'refresh' }),
            jsx('button', { onClick: () => { setMenu(null); setMEdit(null); setMArm(null); setManage(manage === 'profiles' ? null : 'profiles') }, 'data-tip': 'Rename, copy or delete presets (bundled presets are factory — copy to make your own)', style: menuItemS, children: 'manage profiles' }),
            jsx('button', { onClick: () => { setMenu(null); setMEdit(null); setMArm(null); setManage(manage === 'devices' ? null : 'devices') }, 'data-tip': 'Rename devices or remove them from this screen (the preset stays in the store)', style: menuItemS, children: 'manage devices' }),
            // Spectrum analyzer row (owner 2nd batch): NO 'add spectrum' item —
            // the number box says how many; clicking the button inserts that
            // many (or clears them all), Enter in the box appends more.
            jsxs('span', { style: { display: 'flex', gap: 4, alignItems: 'stretch' }, children: [
              jsx('button', { onClick: () => { if (specCtrls.length) rmAnalyzers(); else addAnalyzers(specN) }, 'data-tip': 'Live analyzers — off: inserts the number on the right (they land on the toolbar); on: removes them all. Drag them anywhere afterwards; each has its own ⚙ for source + style. Enter in the box appends more (max 4)', style: { ...menuItemS, flex: 1, ...(specCtrls.length ? { color: 'var(--ui-accent)', borderColor: 'var(--ui-accent)' } : {}) }, children: specCtrls.length ? `✓ spectrum (${specCtrls.length})` : 'spectrum analyzer' }),
              // controlled-numeric straggler — the box could never
              // be blanked mid-type (law already fixed for recBpm/wheel/hex).
              jsx('input', { type: 'number', min: 1, max: 4, defaultValue: specN, onBlur: (e) => setSpecN(Math.max(1, Math.min(4, parseInt(e.target.value, 10) || 1))), onKeyDown: (e) => { if (e.key === 'Enter') { const n = Math.max(1, Math.min(4, parseInt(e.target.value, 10) || 1)); setSpecN(n); addAnalyzers(n) } }, 'data-tip': 'How many spectrum analyzers to insert', style: { width: 42, fontSize: 12, textAlign: 'center', padding: '5px 2px', background: '#1b2129', color: '#e6edf3', border: '1px solid #3d444d', borderRadius: 6 } }),
            ] }),
            // Ignored inputs (owner 2nd batch): SETTINGS item opening a flyout
            // to the RIGHT of the menu — the standard context-submenu shape.
            jsx('button', { onClick: () => setIgnoredOpen(!ignoredOpen), 'data-tip': 'CCs you told the console to stop asking about — open the list and restore them here', style: { ...menuItemS, ...(ignoredOpen ? { color: 'var(--ui-accent)', borderColor: 'var(--ui-accent)' } : {}) }, children: 'ignored inputs' + (Object.keys(dismissed).length ? ' (' + Object.keys(dismissed).length + ')' : '') }),
            ignoredOpen ? jsxs('div', { style: { position: 'absolute', left: '100%', top: 0, marginLeft: 6, display: 'flex', flexDirection: 'column', gap: 6, padding: 8, background: '#242c36', border: '1px solid #3d444d', borderRadius: 8, boxShadow: '0 8px 20px rgba(0,0,0,.55)', minWidth: 150, zIndex: 61 }, children: Object.keys(dismissed).length
              ? [Object.keys(dismissed).map(k => jsx('button', { onClick: () => { unignore(k); if (Object.keys(dismissed).length === 1) setIgnoredOpen(false) }, 'data-tip': 'Un-ignore — CC ' + k.split(':')[1] + ' on channel ' + k.split(':')[0] + ' can offer "create a control?" again', style: menuItemS, children: 'restore CC ' + k.split(':')[1] + '  (ch ' + k.split(':')[0] + ')' }, 'rg-' + k)),
                 jsx('button', { onClick: unignoreAll, 'data-tip': 'Restore every ignored CC at once', style: { ...menuItemS, color: 'var(--ui-text-secondary)' }, children: 'restore all' })]
              : [jsx('span', { style: { fontSize: 12, color: 'var(--ui-text-secondary)', padding: '4px 6px' }, children: 'nothing ignored' }), jsx('button', { onClick: () => setIgnoredOpen(false), style: menuItemS, children: 'close' })] }, 'ig-flyout') : null,
          ] }) : null,
        ] }),
        // CHAT (owner feature 2026-11-09): SAME size bold caps, immediately
        // RIGHT of SETTINGS (owner placement ruling). Opens a card, not a
        // menu — outside-click close would eat text mid-typing.
        jsx('button', { onClick: () => setChat(!chat), 'data-tip': 'Ask — describe a change in plain English ("paint the filter knob gentle purple", "ramp wave 0→100% over 5s", "save this as a new preset called Freakytime"). The model only proposes; this console applies and reports every op', style: { ...bigBtnS, border: `1px solid ${chat ? 'var(--ui-accent)' : 'var(--ui-stroke-secondary)'}`, color: chat ? 'var(--ui-accent)' : 'var(--ui-text-primary)' }, children: 'CHAT' }),
        // Create (owner ask 2026-11-09; alignment ruling): lives INSIDE the
        // menu group, same size / bold caps style as EDIT and SETTINGS.
        edit ? (newSec != null
          ? jsxs('span', { style: { display: 'inline-flex', gap: 4, alignItems: 'center' }, children: [
              jsx('input', { autoFocus: true, placeholder: 'new category name…', value: newSec, onChange: (e) => setNewSec(e.target.value), onKeyDown: (e) => { if (e.key === 'Enter') createCategory(); if (e.key === 'Escape') setNewSec(null) }, style: { ...inpS, width: 150 } }),
              jsx('button', { onClick: () => setNewSec(null), 'data-tip': 'Cancel — go back without creating a category', style: tinyS, children: '×' }),
            ] })
          : jsxs('span', { style: { position: 'relative', display: 'inline-flex' }, children: [
              jsx('button', { onClick: () => setCreateMenu(!createMenu), 'data-tip': 'Create — a new category (name it, then drag knobs in) or a new knob (by CC number)', style: { ...bigBtnS, border: `1px solid ${createMenu ? 'var(--ui-accent)' : 'var(--ui-stroke-secondary)'}`, color: createMenu ? 'var(--ui-accent)' : 'var(--ui-text-primary)' }, children: 'CREATE' }),
              createMenu ? jsxs('div', { style: menuPanelS, children: [
                jsx('button', { onClick: () => { setCreateMenu(false); setNewSec('') }, 'data-tip': 'Name it, then drag controls into it; drag its label to move the whole group', style: menuItemS, children: 'new category' }),
                jsx('button', { onClick: () => { setCreateMenu(false); addManual() }, 'data-tip': 'Add a knob by CC number — for devices that never send back; for echoing devices just twist a knob and accept the learn prompt', style: menuItemS, children: 'new knob' }),
                jsx('button', { onClick: () => { setCreateMenu(false); addAnalyzers(1, 'pc') }, 'data-tip': 'Add a spectrum analyzer — drag it into a category, between knobs, or leave it on the toolbar', style: menuItemS, children: 'new spectrum analyzer' }),
              ] }) : null,
            ] })) : null,
      ] }),
      // Spectrum analyzers (owner batch 2026-11-09): RIGHT of EDIT/SETTINGS;
      // when the row runs out of room flex-wrap drops them to the next line.
      ...specCtrls.filter(([, c]) => !c.section && c.x == null && c.y == null).map(([cid, c]) => jsx('span', { onPointerDown: edit && !colorMode ? (e) => startCardDrag(e, { __cid: cid }) : undefined, onClick: colorMode ? (e) => { e.stopPropagation(); togglePart('w:' + cid) } : undefined, 'data-tip': colorMode ? 'Pick the analyzer tint — wheel, hex box or rainbow mode paint its visuals' : (edit ? 'Drag me onto the surface — into a category, between knobs, or drop me back on this toolbar row' : undefined), style: { display: 'inline-flex', cursor: colorMode ? 'copy' : (edit ? 'move' : 'default') }, children: jsx(SpectrumBox, { api, spec: { id: cid, source: c.source || 'pc', type: c.type || 'bars', w: c.w || 4, color: c.color || null }, sources: specSources, onPatch: patchSpecCtrl, onRemove: () => del(cid) }) }, 'tb-' + cid)),
    ] }),
    // Manage card (owner feature 2026-11-09): opens from SETTINGS.
    // profiles: rename · copy · delete. devices: rename label · remove from
    // screen · delete preset. All inputs inline (Electron has no prompt()).
    manage ? jsxs('div', { style: { border: '1px solid var(--ui-stroke-secondary)', borderRadius: 10, padding: '10px 12px', marginBottom: 12, maxWidth: 900 }, children: [
      jsxs('div', { style: { display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }, children: [
        jsx('span', { style: { fontSize: 13, fontWeight: 800, letterSpacing: '0.06em' }, children: manage === 'profiles' ? 'MANAGE PROFILES' : 'MANAGE DEVICES' }),
        jsx('span', { style: { fontSize: 11, color: 'var(--ui-text-secondary)' }, children: manage === 'profiles' ? 'rename · copy · delete — bundled presets are factory (copy to make your own)' : 'rename the device label · remove from this screen (the preset stays in the store)' }),
        jsx('span', { style: { flex: 1 } }),
        jsx('button', { onClick: () => { setManage(null); setMEdit(null); setMArm(null) }, 'data-tip': 'Close the manage card', style: tinyS, children: '× close' }),
      ] }),
      ...(manage === 'profiles'
        ? profiles.map(p => jsxs('div', { style: { display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', borderTop: '1px solid var(--ui-stroke-secondary)', padding: '5px 0' }, children: [
            jsx('span', { style: { fontWeight: 700, fontSize: 13, minWidth: 150 }, children: p.profile_id }),
            jsx('span', { style: { fontSize: 12, minWidth: 150 }, children: p.device }),
            jsx('span', { style: { fontSize: 10, color: 'var(--ui-text-secondary)' }, children: `${p.controls} ctrl · ${p.tier}${deckIds.includes(p.profile_id) ? ' · on screen' : ''}${!p.valid ? ' · INVALID' : ''}` }),
            p.tier === 'user'
              ? (mEdit && mEdit.kind === 'rename' && mEdit.pid === p.profile_id
                  ? jsxs('span', { style: { display: 'inline-flex', gap: 4, alignItems: 'center' }, children: [
                      jsx('input', { autoFocus: true, value: mEdit.val, placeholder: 'new id…', onChange: (e) => setMEdit({ ...mEdit, val: e.target.value }), onKeyDown: (e) => { if (e.key === 'Enter') doRename(p.profile_id, mEdit.val); if (e.key === 'Escape') setMEdit(null) }, style: { ...inpS, width: 150 } }),
                      jsx('button', { onClick: () => doRename(p.profile_id, mEdit.val), style: tinyS, children: 'ok' }),
                      jsx('button', { onClick: () => setMEdit(null), style: tinyS, children: '×' }),
                    ] })
                  : jsx('button', { onClick: () => setMEdit({ kind: 'rename', pid: p.profile_id, val: p.profile_id }), 'data-tip': 'Rename this preset — the deck and active marker follow automatically', style: tinyS, children: 'rename' }))
              : null,
            mEdit && mEdit.kind === 'copy' && mEdit.pid === p.profile_id
              ? jsxs('span', { style: { display: 'inline-flex', gap: 4, alignItems: 'center' }, children: [
                  jsx('input', { autoFocus: true, value: mEdit.val, placeholder: 'copy id…', onChange: (e) => setMEdit({ ...mEdit, val: e.target.value }), onKeyDown: (e) => { if (e.key === 'Enter') doCopy(p.profile_id, mEdit.val); if (e.key === 'Escape') setMEdit(null) }, style: { ...inpS, width: 150 } }),
                  jsx('button', { onClick: () => doCopy(p.profile_id, mEdit.val), style: tinyS, children: 'ok' }),
                  jsx('button', { onClick: () => setMEdit(null), style: tinyS, children: '×' }),
                ] })
              : jsx('button', { onClick: () => setMEdit({ kind: 'copy', pid: p.profile_id, val: p.profile_id + '-copy' }), 'data-tip': 'Duplicate this preset under a new id (works on bundled presets too)', style: tinyS, children: 'copy' }),
            p.tier === 'user' ? jsx('button', { onClick: () => { if (mArm !== p.profile_id) { setMArm(p.profile_id); setStatus(`delete preset ${p.profile_id}? press again to confirm (also removes it from the deck)`) } else doDelete(p.profile_id) }, 'data-tip': 'Delete this preset for good — two clicks. Also removes it from the deck', style: { ...tinyS, ...(mArm === p.profile_id ? { color: 'var(--ui-accent)', borderColor: 'var(--ui-accent)' } : {}) }, children: mArm === p.profile_id ? 'sure?' : 'delete' }) : null,
          ] }, p.profile_id))
        : deckIds.slice(1).map(pid => {  // review: deck[0] is the ACTIVE surface — it is managed under manage profiles; offering "remove from screen" on it blanked the whole console
            const p = profiles.find(x => x.profile_id === pid) || {}
            return jsxs('div', { style: { display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', borderTop: '1px solid var(--ui-stroke-secondary)', padding: '5px 0' }, children: [
              jsx('span', { style: { fontWeight: 700, fontSize: 13, minWidth: 190 }, children: p.device || pid }),
              jsx('span', { style: { fontSize: 10, color: 'var(--ui-text-secondary)' }, children: `preset: ${pid}${p.controls != null ? ` · ${p.controls} ctrl` : ''}` }),
              mEdit && mEdit.kind === 'dev' && mEdit.pid === pid
                ? jsxs('span', { style: { display: 'inline-flex', gap: 4, alignItems: 'center' }, children: [
                    jsx('input', { autoFocus: true, value: mEdit.val, placeholder: 'device name…', onChange: (e) => setMEdit({ ...mEdit, val: e.target.value }), onKeyDown: (e) => { if (e.key === 'Enter') doDev(pid, mEdit.val); if (e.key === 'Escape') setMEdit(null) }, style: { ...inpS, width: 180 } }),
                    jsx('button', { onClick: () => doDev(pid, mEdit.val), style: tinyS, children: 'ok' }),
                    jsx('button', { onClick: () => setMEdit(null), style: tinyS, children: '×' }),
                  ] })
                : jsx('button', { onClick: () => setMEdit({ kind: 'dev', pid, val: p.device || pid }), 'data-tip': "Rename this device's label (stored in its preset)", style: tinyS, children: 'rename device' }),
              // regression review debt sweep: the claim used to be written before deckSet resolved.
              jsx('button', { onClick: () => { deckSet(deckRef.current.filter(d => d !== pid)).then((r) => { if (r && r.ok) setStatus(`removed ${pid} from the screen — the preset stays in the store`) }) }, 'data-tip': 'Take this device off the screen (deck). The preset itself is NOT deleted', style: tinyS, children: 'remove from screen' }),
              jsx('button', { onClick: () => { if (mArm !== pid) { setMArm(pid); setStatus(`delete preset ${pid}? press again to confirm`) } else doDelete(pid) }, 'data-tip': 'Delete this device preset for good (also removes it from the deck). Two clicks', style: { ...tinyS, ...(mArm === pid ? { color: 'var(--ui-accent)', borderColor: 'var(--ui-accent)' } : {}) }, children: mArm === pid ? 'sure?' : 'delete preset' }),
            ] }, pid)
          })),
    ] }) : null,
    // chat box card (owner feature 2026-11-09): stays open until closed —
    // a dropdown that closes on outside click would eat text mid-typing.
    // OPAQUE panel (popup law): #242c36 solid, knobs never phase through.
    chat ? jsxs('div', { 'data-chatcard': '1', style: { border: '1px solid #3d444d', borderRadius: 10, padding: '10px 12px', marginBottom: 12, maxWidth: 900, background: '#242c36', color: '#e6edf3' }, children: [
      jsxs('div', { style: { display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap', marginBottom: 8 }, children: [
        jsx('span', { style: { fontSize: 13, fontWeight: 800, letterSpacing: '0.06em' }, children: rainbow('ASK', 13) }),
        jsx('span', { style: { fontSize: 11, color: '#9aa7b4' }, children: 'describe a change in plain English — the model proposes, the console applies' }),
        jsx('span', { style: { flex: 1 } }),
        // Model routing (catalog review, PR 153102): the box no longer picks a lane —
        // Hermes holds the credentials and routes the ask through its own auxiliary
        // slot, which follows the app's current model until the user pins another.
        jsx('span', { 'data-tip': "Which model answers: Hermes keeps the provider keys and runs this box through its own auxiliary slot, so it follows whatever model the app is using right now. Pin a dedicated one under Settings \u2192 Models \u2192 Auxiliary \u2192 'MIDI Console chat' (reasoning effort lives there too)", style: { fontSize: 11, color: '#9aa7b4', maxWidth: 360 }, children: 'model: your current Hermes model \u00b7 change it in Settings \u2192 Models \u2192 Auxiliary' }),
        // Effort knob (owner ask 2026-11-10): thinking models spend the reply
        // budget on hidden reasoning first — auto = lane default; fast pins it
        // low/off; thoughtful raises it (and the reply cap) and runs slower.
        jsx('select', { value: chatEffort, onChange: (e) => { setChatEffort(e.target.value); try { api.storage.set('chat_effort', e.target.value) } catch (er) {} }, 'data-tip': 'How much reply room the model gets for this box. auto = the slot\'s own default · fast = short budget (snappier, can truncate a long plan) · balanced = room for a multi-op answer · thoughtful = biggest budget. Reasoning effort itself belongs to the model now — pin it under Settings → Models → Auxiliary → MIDI Console chat. Too small a budget can make a thinking model return EMPTY', style: { fontSize: 12, padding: '4px 6px', background: '#1b2129', color: '#e6edf3', border: '1px solid #3d444d', borderRadius: 6 }, children: [['auto', 'effort: auto'], ['fast', 'effort: fast'], ['balanced', 'effort: balanced'], ['thoughtful', 'effort: thoughtful']].map(([v, l]) => jsx('option', { value: v, children: l }, 'ce-' + v)) }),
        jsx('button', { onClick: () => setChat(false), 'data-tip': 'Close the chat box', style: { fontSize: 11, fontWeight: 700, padding: '3px 8px', border: '1px solid #3d444d', background: 'transparent', color: '#e6edf3', borderRadius: 6, cursor: 'pointer' }, children: '\u00d7 close' }),
      ] }),
      jsxs('div', { ref: (el) => { if (el) el.scrollTop = el.scrollHeight }, style: { maxHeight: 260, overflowY: 'auto', display: 'flex', flexDirection: 'column', gap: 6, marginBottom: 8 }, children: [
        chatLog.length ? null : jsx('span', { style: { fontSize: 12, color: '#9aa7b4' }, children: 'Try: “make the filter knob gentle purple” · “ramp wave 0→100% over 5 seconds” · “once timbre passes 70%, swing resonance back and forth” · “organize my categories by amount of knobs” · “save this as a new preset called Freakytime”' }),
        ...chatLog.map((m, i) => jsx('div', { style: { fontSize: m.role === 'say' || m.role === 'user' ? 13 : 12, fontWeight: m.role === 'user' ? 700 : 400, color: m.role === 'user' ? '#e6edf3' : m.role === 'err' ? '#ff8080' : m.role === 'done' ? '#7ee787' : '#cfe3f5', whiteSpace: 'pre-wrap' }, children: (m.role === 'user' ? '» ' : '') + m.text }, 'cl-' + i)),
      ] }),
      jsxs('div', { style: { display: 'flex', gap: 6 }, children: [
        jsx('input', { autoFocus: true, placeholder: chatBusy ? 'thinking… (give it a few seconds)' : 'ask the console to change something…', value: chatIn, onChange: (e) => setChatIn(e.target.value), onKeyDown: (e) => { if (e.key === 'Enter') sendChat() }, disabled: chatBusy, style: { flex: 1, fontSize: 13, padding: '6px 8px', background: '#1b2129', color: '#e6edf3', border: '1px solid #3d444d', borderRadius: 6 } }),
        jsx('button', { onClick: () => sendChat(), disabled: chatBusy || !chatIn.trim(), 'data-tip': 'Send — Enter works too', style: { border: '1px solid var(--ui-accent)', color: '#7ee787', background: 'transparent', padding: '4px 12px', borderRadius: 6, cursor: chatBusy ? 'wait' : 'pointer', opacity: chatBusy || !chatIn.trim() ? 0.45 : 1 }, children: chatBusy ? '…' : 'Send' }),
      ] }),
    ] }) : null,
    // passive-learn prompts
    ...unmapped.slice(0, 3).map(u => jsxs('div', { style: { display: 'flex', gap: 10, alignItems: 'center', border: '1px solid var(--ui-accent)', borderRadius: 8, padding: '8px 12px', marginBottom: 8 }, children: [
      jsx('span', { style: { fontSize: 13, color: 'var(--ui-text-primary)' }, children: `New value detected — CC ${u.cc} (value ${u.value}). Create a control?` }),
      jsx('button', { onClick: () => createControl(u), style: { border: '1px solid var(--ui-accent)', color: 'var(--ui-accent)', background: 'transparent', padding: '4px 10px', borderRadius: 6, cursor: 'pointer' }, children: 'Yes' }),
      jsx('button', { onClick: () => ignore(u.channel + ':' + u.cc), 'data-tip': 'Stop prompting for this CC. It is remembered — restore it any time from SETTINGS → ignored inputs', style: { border: '1px solid var(--ui-stroke-secondary)', color: 'var(--ui-text-secondary)', background: 'transparent', padding: '4px 10px', borderRadius: 6, cursor: 'pointer' }, children: 'Ignore' }),
    ] }, 'prompt-' + u.channel + '-' + u.cc)),
    // rebind hint
    rebind ? jsx('div', { style: { fontSize: 12, color: 'var(--ui-accent)', marginBottom: 10 }, children: `Rebinding ${rebind.cid} — twist a knob, or type a CC in the box beside that control` }) : null,
    // status line
    status ? jsx('div', { style: { fontSize: 12, color: 'var(--ui-text-secondary)', marginBottom: 10 }, children: status }) : null,
    // unsaved-draft exit prompt (owner ruling): Save / Discard / Keep editing
    exitAsk ? jsxs('div', { style: { display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', border: '1px solid var(--ui-accent)', borderRadius: 8, padding: '8px 12px', marginBottom: 10 }, children: [
      jsx('span', { style: { fontSize: 13, color: 'var(--ui-text-primary)' }, children: 'You have unsaved edits — save them to the profile before leaving edit mode?' }),
      jsx('button', { onClick: () => saveDraft().then(ok => { if (ok) { setExitAsk(false); exitEdit() } }), 'data-tip': 'Write the edits to the profile, then leave edit mode', style: { border: '1px solid var(--ui-accent)', color: 'var(--ui-accent)', background: 'transparent', padding: '4px 10px', borderRadius: 6, cursor: 'pointer' }, children: 'Save & exit' }),
      jsx('button', { onClick: () => { setExitAsk(false); exitEdit() }, 'data-tip': 'Throw away everything changed in this edit session', style: { border: '1px solid var(--ui-stroke-secondary)', color: 'var(--ui-text-secondary)', background: 'transparent', padding: '4px 10px', borderRadius: 6, cursor: 'pointer' }, children: 'Discard' }),
      jsx('button', { onClick: () => setExitAsk(false), 'data-tip': 'Stay in edit mode, changes kept unsaved', style: tinyS, children: 'Keep editing' }),
    ] }, 'exit-ask') : null,
    // edit-mode toolbar (owner ruling): selection tools, Color mode, explicit
    // Save — nothing edits the real profile until Save.
    edit ? jsxs('div', { style: { display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', fontSize: 12, color: 'var(--ui-accent)', marginBottom: 10 }, children: [
      // Mode banner (owner ruling): it must be OBVIOUS which mode you are in.
      jsx('span', { style: colorMode ? {} : { fontSize: 18, fontWeight: 800, letterSpacing: '0.05em', color: 'var(--ui-accent)' }, children: colorMode ? rainbow('Coloring Mode') : 'Layout Editor' }),
      jsx('span', { children: colorMode
        ? 'Color mode — click any dashed part to pick it (category label, category box, knob incl. its ring, name, card background) · the wheel paints everything picked'
        : 'Edit mode — click a card to select · drag on empty space to select many · drag a card to move (dashed outline shows where it lands)' }),
      colorMode
        ? (Object.keys(csel).length ? jsx('button', { onClick: clearPicks, 'data-tip': 'Clear the part picks', style: { ...tinyS, color: 'var(--ui-accent)', borderColor: 'var(--ui-accent)' }, children: `Unpick (${Object.keys(csel).length})` }) : null)
        : (Object.keys(sel).length || Object.values(secSel).some(Boolean) ? jsx('button', { onClick: () => { setSel({}); setSecSel({}) }, 'data-tip': 'Clear the current selection — B9 (): control AND category selection, the old button cleared only controls and the stale category group kept moving boxes together', style: { ...tinyS, color: 'var(--ui-accent)', borderColor: 'var(--ui-accent)' }, children: `Deselect (${Object.keys(sel).length})` }) : null),
      !colorMode ? jsx('button', { onClick: () => { const s = {}; for (const c of controls) s[c.__cid] = true; setSel(s); setStatus(`all ${Object.keys(s).length} controls selected`) }, 'data-tip': 'Select every control in the whole profile (categories included)', style: { ...tinyS, ...(controls.length && controls.every(c => sel[c.__cid]) ? { color: 'var(--ui-accent)', borderColor: 'var(--ui-accent)' } : {}) }, children: controls.length && controls.every(c => sel[c.__cid]) ? '⊞ selected (global)' : '⊞ select all (global)' }) : null,
      colorMode ? jsx('button', { onClick: () => toggleAll(), 'data-tip': (allSel ? 'Remove every part from the color pick' : 'Add every colorable part in the whole profile to the pick'), style: { ...tinyS, ...(allSel ? { color: 'var(--ui-accent)', borderColor: 'var(--ui-accent)' } : {}) }, children: allSel ? '◉ selected (global)' : '◯ select all (global)' }) : null,
      jsx('button', { onClick: () => { setColorMode(!colorMode); clearPicks() }, 'data-tip': 'Color Selector — pick individual parts (labels, knobs, names, boxes, categories) and paint them with one wheel', style: { ...tinyS, fontSize: 11, border: 'none', background: 'transparent', cursor: 'pointer' }, children: rainbow(colorMode ? '◉ Coloring' : 'Color', 11) }),
      jsx('button', { onClick: () => { clearTimeout(saveTimerRef.current); saveDraft() }, disabled: !dirty, 'data-tip': dirty ? 'Write all edit-mode changes to the profile now' : 'Nothing to save — the draft matches the saved profile', style: { border: `1px solid ${dirty ? 'var(--ui-accent)' : 'var(--ui-stroke-secondary)'}`, color: dirty ? 'var(--ui-accent)' : 'var(--ui-text-secondary)', background: 'transparent', padding: '3px 10px', borderRadius: 6, cursor: 'pointer', opacity: dirty ? 1 : 0.45 }, children: dirty ? 'Save to profile ●' : 'Save to profile' }),
      jsx('button', { onClick: undo, disabled: !undoRef.current.length, 'data-tip': `Undo the last edit (${undoRef.current.length} in history)`, style: { ...tinyS, opacity: undoRef.current.length ? 1 : 0.4 }, children: 'Undo' }),
      jsx('button', { onClick: redo, disabled: !redoRef.current.length, 'data-tip': `Redo the last undone edit (${redoRef.current.length} in history)`, style: { ...tinyS, opacity: redoRef.current.length ? 1 : 0.4 }, children: 'Redo' }),
    ] }) : null,
    // Color panel (owner: transitions were disorienting): ONE fixed row that
    // exists the moment color mode is on — the wheel and type chips fill it in
    // place, so picking things never pushes the surface around.
    edit && colorMode ? jsxs('div', { style: { display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap', minHeight: 52, marginBottom: 10, fontSize: 12, color: 'var(--ui-text-secondary)' }, children: [
      // The wheel is ALWAYS here in color mode (owner: appearing/disappearing
      // was disorienting) — dimmed until something is picked.
      jsx('div', { style: Object.keys(csel).length ? {} : { opacity: 0.4, pointerEvents: 'none' }, children: jsx(HueWheel, { color: Object.keys(csel).map(partColor).find(Boolean) || '#0091ff', onChange: (hex) => { if (Object.keys(csel).length) applyColor(hex) }, key: 'cwheel' }) }),
      // Hex box (owner ask 2026-11-09): exact color by typing. The # is drawn
      // inside the box so its purpose is obvious; type 6 digits, Enter paints.
      // The rainbow-ifier (owner idea) sits directly above it.
      jsxs('span', { style: { display: 'inline-flex', flexDirection: 'column', gap: 4 }, children: [
        jsx('button', { onClick: rainbowify, 'data-tip': 'Rainbow mode — every picked unit gets its own slice of the spectrum and its parts spread across that slice. A whole category is one unit (one category = a full rainbow across its label, box, knobs and names; two categories = each owns half). One Undo', style: { ...tinyS, padding: '3px 8px' }, children: rainbow('rainbow mode', 10) }),
        jsxs('span', { style: { display: 'inline-flex', alignItems: 'center', gap: 2, border: '1px solid var(--ui-stroke-secondary)', borderRadius: 6, padding: '4px 8px', background: 'var(--ui-bg-secondary)', ...(Object.keys(csel).length ? {} : { opacity: 0.4 }) }, children: [
          jsx('span', { style: { color: 'var(--ui-text-secondary)', fontWeight: 800, fontSize: 13, fontFamily: 'monospace' }, children: '#' }),
          jsx('input', { value: hexIn, onChange: (e) => setHexIn(e.target.value.replace(/[^0-9a-fA-F]/g, '').slice(0, 6)), onKeyDown: (e) => { if (e.key === 'Enter') applyHex() }, placeholder: '0091ff', spellCheck: false, 'data-tip': 'Exact color by code — type 6 hex digits and press Enter to paint the picked parts instantly', style: { width: 64, background: 'transparent', border: 'none', color: 'var(--ui-text-primary)', fontSize: 13, fontFamily: 'monospace', outline: 'none' } }),
        ] }),
      ] }),
      Object.keys(csel).length
        ? jsxs('span', { style: { display: 'inline-flex', gap: 10, alignItems: 'center' }, children: [
            jsx('span', { children: `Color ${Object.keys(csel).length} picked part${Object.keys(csel).length > 1 ? 's' : ''}:` }),
            jsx('button', { onClick: clearColor, 'data-tip': 'Strip the color from every picked part (label, box, knob, name, background) — instant, one Undo', style: { ...tinyS, color: 'var(--ui-text-primary)' }, children: '✕ clear color' }),
          ] })
        : jsx('span', { children: 'Nothing picked — click any dashed part, or use ◯ select category / ◯ select all (global).' }),
      // type chips: click = drop/add a WHOLE part type; double-click = keep
      // ONLY this type ("backgrounds only")
      jsxs('span', { style: { display: 'inline-flex', gap: 4, alignItems: 'center', flexWrap: 'wrap' }, children:
        [['sec', 'cat labels'], ['sb', 'cat boxes'], ['w', 'knobs/values'], ['n', 'names'], ['b', 'backgrounds']].map(([k, lbl]) => {
          const has = Object.keys(csel).some(key => key.startsWith(k + ':'))
          return jsx('button', { onClick: () => toggleKind(k), onDoubleClick: () => setCsel(s => { const d = {}; for (const key of Object.keys(s)) if (key.startsWith(k + ':')) d[key] = true; return d }), 'data-tip': (has ? 'Un-pick every ' : 'Add back every ') + lbl + ' · double-click keeps ONLY this type', style: { ...tinyS, ...(has ? { color: 'var(--ui-accent)', borderColor: 'var(--ui-accent)' } : { opacity: 0.5 }) }, children: (has ? '✓ ' : '○ ') + lbl }, 'kind-' + k)
        }) }),
    ] }) : null,
    // surface
    !prof
      ? jsx('div', { style: { padding: 24, border: '1px dashed var(--ui-stroke-secondary)', borderRadius: 10, color: 'var(--ui-text-secondary)', fontSize: 13 },
          children: 'No profile active. Connect a device and twist a knob to create controls automatically, or pick a bundled profile above.' })
      : jsxs('div', { children: [
        ...(sections.length ? (() => {
          // Category GRID (owner bug pass 3, 2026-11-09): ONE grid container;
          // band = gridRow (sec.row), slot = gridColumn (sec.order, sparse).
          // The drag preview is a grid child in the exact cell commitSecMoveAll
          // will write — preview == result by construction. Story-packing is
          // GONE: it was the root of "won't lock where I hover / snapped
          // somewhere else / preview was wrong" — a first-fit repack could
          // never land a tall category where the 1D splice pointed.
          const lift = secDrag ? new Set(secDrag.sids || [secDrag.sid]) : null
          const over = secDrag && secDrag.over
          // Bands render CONTIGUOUS (0..maxRow, plus a fresh band above/below
          // while dragging) — a band number gap is a real visual gap, and
          // every cell index below must exist for the drag targeting.
          const maxRow = Math.max(0, ...sections.map(([, s]) => s.row || 0))
          // PER-BAND ROWS REBUILD (owner 2026-11-10 22:39, "moves the boxes
          // ABOVE it like its hallucinating being in the way"): the category
          // grid was ONE CSS grid with max-content columns SHARED across all
          // bands — dragging a control in the bottom band widened that band's
          // section, which widened the shared column, which slid every box in
          // that column in EVERY band. No data moved; the CSS moved them.
          // Bands are now INDEPENDENT stacked flex rows: a band's content can
          // only ever reflow its own row. Cross-band movement is structurally
          // impossible, exactly matching the owner's storey model. Static-
          // during-drag is preserved: band contents derive from SECTIONS ONLY.
          const allRows = [...Array(maxRow + 1).keys()]
          const bandH = (r) => { let h = 0; sections.forEach(([sid2, s2]) => { if ((s2.row || 0) === r) { const m = secH.current[sid2]; if (m && m[0] > h) h = m[0] } }); return h || 60 }
          const secBox = (sid, sec, r) => jsxs('div', {
            'data-secblock': sid, 'data-secrow': String(r), 'data-secx': String(sec.order || 0),
            onClick: colorMode ? (e) => { e.stopPropagation(); togglePart('sb:' + sid) } : undefined,
            // In a per-band row the box IS its cell — its rect is the target
            // rect (the dead-edge bug lived in SHARED tracks; there are none
            // now, so content-sizing is honest again). minWidth keeps a
            // near-empty category hittable.
            style: { borderRadius: 10, width: 'fit-content', boxSizing: 'border-box', minWidth: 160, padding: '2px 8px 4px', alignSelf: 'start',
              ...(lift && lift.has(sid) ? { opacity: 0.35 } : {}),
              background: sec.bg ? sec.bg + '33' : 'transparent',
              ...(colorMode ? { outline: csel['sb:' + sid] ? '2px solid var(--ui-accent)' : '1px dashed rgba(160,160,160,0.45)', outlineOffset: 4, cursor: 'copy' } : {}) },
            // display:contents wrappers = keys for the array children without disturbing the layout (React key warning fix).
            children: [jsx('span', { style: { display: 'contents' }, children: secHead(sid, sec) }, 'h'), jsx('span', { style: { display: 'contents' }, children: renderGrid(controls.filter(c => c.section === sid), renderCtrl, edit, { ...gops, sid }) }, 'g')],
          }, 'sec-' + sid)
          // EMPTY CELLS ARE REAL CELLS (pass 4 law lives): every unoccupied
          // slot in the band gets a transparent DOM cell between the boxes —
          // the pointer can hit what it sees. DOM position == order (slots
          // fill the gaps), so targeting indices and commit orders agree.
          const bandDivs = allRows.map(r => {
            const inB = sections.filter(([, s]) => (s.row || 0) === r).sort((a, b) => (a[1].order || 0) - (b[1].order || 0))
            const maxO = Math.max(0, ...inB.map(([, s]) => s.order || 0))
            const kids = []
            for (let c = 0; c <= maxO; c++) {
              const hit = inB.find(([, s]) => (s.order || 0) === c)
              if (hit) kids.push(secBox(hit[0], hit[1], r))
              else kids.push(jsx('div', { 'data-secslot': r + ':' + c, style: { minHeight: bandH(r), minWidth: 160, alignSelf: 'stretch' } }, 'slot-' + r + '-' + c))
            }
            return jsx('div', { 'data-secband': String(r), style: { display: 'flex', gap: 14, alignItems: 'stretch' }, children: kids }, 'band-' + r)
          })
          // INERT PREVIEW LAYER: fixed px boxes from the frozen snapshot —
          // blue = where the group lands (whole group footprint), green = where
          // each spliced neighbour ends up. pointerEvents:none, OUTSIDE the
          // flow: they can never move a cell under the finger. Columns are
          // PER BAND now (the snapshot stores each band's child extents).
          const sp = secSnapRef.current
          const ovBoxes = []
          if (over && lift && sp && sp.bands.length && sp.bands[0].cols.length) {
            const n = lift.size
            const bandCols = (r) => { const b = sp.bands.find(x => x.row === r); return b && b.cols.length ? b.cols : sp.bands[0].cols }
            const colX = (r, c) => bandCols(r).find(k => k.col === c)
            const colL = (r, c) => { const x = colX(r, c); if (x) return x.left; const bc = bandCols(r); return bc[bc.length - 1].right + 14 }
            const colW = (r, c) => { const x = colX(r, c); if (x) return x.right - x.left; const f = bandCols(r)[0]; return Math.max(160, f.right - f.left) }
            const bandPx = (r) => { const b = sp.bands.find(x => x.row === r)
              if (b) return { top: b.top, h: Math.max(60, b.bottom - b.top) }
              const m0 = secH.current[secDrag.sid]
              const h0 = Math.max(90, m0 ? m0[0] : 120)
              if (r < sp.bands[0].row) return { top: sp.top - 24 - h0, h: h0 }
              return { top: Math.max(sp.bottom, sp.bands[sp.bands.length - 1].bottom) + 24, h: h0 } }
            const box = (kind, r, c, w) => { const t = bandPx(r)
              ovBoxes.push(jsx('div', { style: { position: 'absolute', left: colL(r, c), top: t.top, width: w, height: t.h, boxSizing: 'border-box', borderRadius: 10, border: kind === 'push' ? '1px dashed rgba(126,231,135,0.7)' : '2px dashed var(--ui-accent)', background: kind === 'push' ? 'transparent' : 'rgba(80,160,255,0.10)', opacity: kind === 'push' ? 0.65 : 1 } }, kind + '-' + r + '-' + c)) }
            let wsum = 0; for (let k = 0; k < n; k++) wsum += colW(over.row, over.col + k) + (k ? 14 : 0)
            box('secland', over.row, over.col, wsum)
            // Honest preview = the SAME chainPushRow the commit runs: green
            // boxes appear ONLY on occupants the landing span actually
            // touches (chain). An empty-slot drop previews zero push.
            const pushed = chainPushRow(sections.filter(([sid2, s2]) => !lift.has(sid2) && (s2.row || 0) === over.row)
              .map(([, s2]) => ({ id: s2, x: s2.order || 0, w: 1 })), over.col, n)
            for (const m of pushed) if (m.x !== (m.id.order || 0)) box('push', over.row, m.x, colW(over.row, m.x))
          }
          return [jsx('div', { 'data-secgrid': '1', style: { display: 'flex', flexDirection: 'column', gap: 14, marginBottom: 14 }, children: bandDivs }, 'secflow'),
            ...(ovBoxes.length ? [jsx('div', { style: { position: 'fixed', left: 0, top: 0, width: '100vw', height: '100vh', pointerEvents: 'none', zIndex: 30, children: ovBoxes } }, 'secpreview')] : [])]
        })() : []),
        ...(unsectioned.length ? [jsxs('div', { style: { marginBottom: 14 }, children: [
          sections.length ? jsx('div', { style: { fontSize: 11, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.12em', color: 'var(--ui-text-secondary)', marginBottom: 6 }, children: 'Unsectioned' }) : null,
          renderGrid(unsectioned, renderCtrl, edit, { ...gops, sid: '' }),
        ] }, 'sec-none')] : []),
      ] }),
    // software matrix panel — modulation in OUR layer (see matrix_engine laws)
    // #10: visible whenever a matrix EXISTS or RUNS — losing the active
    // profile must never strand a running modulation out of view (Stop gone).
    (prof || (st && st.matrix && st.matrix.running)) ? jsxs('div', { style: { marginTop: 16, border: '1px solid var(--ui-stroke-secondary)', borderRadius: 10, padding: 12 }, children: [
      jsxs('div', { style: { display: 'flex', gap: 8, alignItems: 'center', marginBottom: 8 }, children: [
        jsx('div', { style: { fontSize: 11, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.12em', color: 'var(--ui-text-secondary)' }, children: 'Matrix (software)' }),
        st && st.matrix && st.matrix.running ? jsx('span', { style: { fontSize: 11, color: 'var(--ui-accent)' }, children: '● running' }) : null,
        jsx('span', { style: { flex: 1 } }),
        st && st.matrix && st.matrix.running
          ? jsx('button', { onClick: stopMatrix, 'data-tip': 'Stop modulation and restore each target to the value it had when the matrix started', style: { border: '1px solid var(--ui-accent)', color: 'var(--ui-accent)', background: 'transparent', padding: '4px 12px', borderRadius: 6, cursor: 'pointer' }, children: 'Stop' })
          : jsx('button', { onClick: () => api.rest('/matrix/start', { method: 'POST' }).then(r => { setStatus(r && r.ok ? `matrix running — ${r.edges} edge(s)` + (r.pending && r.pending.length ? ` · waiting for a first value on: ${r.pending.map((c) => ((prof && prof.controls && prof.controls[c] || {}).label) || c).join(', ')} — twist once, the lane arms itself` : '') : `matrix: ${(r && r.error) || 'unreachable'}`); refresh() }).catch(() => setStatus('matrix start: backend unreachable')), 'data-tip': 'Start all modulation lanes. A lane whose target has no known value yet WAITS instead of blocking the start: twist that knob once and the lane captures the real value and arms itself. Absolute move lanes (a ramp names its own start) run immediately.', style: { border: '1px solid var(--ui-accent)', color: 'var(--ui-accent)', background: 'transparent', padding: '4px 12px', borderRadius: 6, cursor: 'pointer' }, children: 'Start' }),
      ] }),
      // #4: a latched engine fault (refused send mid-run) is loud, not silent.
      st && st.matrix && st.matrix.fault
        ? jsx('div', { style: { fontSize: 12, color: '#e5484d', marginBottom: 6 }, children: `matrix fault: ${st.matrix.fault}` })
        : null,
      // Lane law (owner ask 2026-11-09): EVERY lane is always a full editable
      // row (source → target, depth, mode, curve, slew, wave, rate, ×) — the
      // old read-only text line outside edit mode made added lanes look like
      // nothing happened. mutateMx hot-applies to a running matrix either way.
      ...((prof && prof.matrix) || []).map((e, i) => jsxs('div', { style: { display: 'flex', flexWrap: 'wrap', gap: 4, alignItems: 'center', marginBottom: 5 }, children: [
            fld('Kind'),
            jsx('select', { value: e.kind || 'lfo', onChange: (ev) => setKind(i, ev.target.value), style: inpS, 'data-tip': 'lfo: continuous modulation · move: one-shot ramp (ramp X from a to b over Ns) · when: fire when a source crosses a level · watch: fire the moment a knob is twisted', children: ['lfo', 'move', 'when', 'watch'].map(k => jsx('option', { value: k, children: k }, k)) }),
            ...(function () {
              const k = e.kind || 'lfo'
              const isL = (e.source || 'lfo') === 'lfo'
              const WAV = ['sine', 'tri', 'saw', 'sh']
              const srcOpt = (ctrlOnly) => (ctrlOnly ? [] : [jsx('option', { value: 'lfo', children: 'LFO' }, 'lfo')]).concat(ctrlIds.map(cid => jsx('option', { value: cid, children: prof.controls[cid].label }, cid)))
              const tgtOpt = [jsx('option', { value: '', children: 'target…' }, 'ph')].concat(ctrlIds.map(cid => jsx('option', { value: cid, children: prof.controls[cid].label }, cid)))
              const els = []
              if (k === 'lfo' || k === 'when') {
                els.push(fld('Source'), jsx('select', { value: e.source || 'lfo', onChange: (ev) => mutateMx(p => { p.matrix[i].source = ev.target.value }, 'source set'), style: inpS, children: srcOpt(false) }))
              }
              if (k === 'watch') {
                els.push(fld('Twist of'), jsx('select', { value: e.source || '', onChange: (ev) => mutateMx(p => { p.matrix[i].source = ev.target.value }, 'source set'), style: inpS, 'data-tip': 'this control being moved at all (physical twist or panel) fires the then-edges', children: srcOpt(true) }))
              }
              if (k === 'lfo' || k === 'move') {
                els.push(fld('Target'), jsx('select', { value: e.target || '', onChange: (ev) => mutateMx(p => { p.matrix[i].target = ev.target.value }, 'target set'), style: inpS, children: tgtOpt }))
              }
              if (k === 'lfo') {
                els.push(
                  fld('Depth'),
                  jsx('input', { type: 'number', min: -2, max: 2, step: 0.1, defaultValue: e.depth != null ? e.depth : 1, onBlur: (ev) => commitNum(i, 'depth', ev, -2, 2, 1), 'data-tip': 'depth (-2..2)', style: { ...inpS, width: 58 } }, 'd-' + (e.depth != null ? e.depth : 1)),  // value-keyed: when lanes shift index after a delete, the uncontrolled box REMOUNTS with the new lane's value instead of showing the dead lane's number
                  fld('Mode'),
                  jsx('select', { value: e.mode || 'add', onChange: (ev) => mutateMx(p => { p.matrix[i].mode = ev.target.value }, 'mode set'), style: inpS, children: ['add', 'mul', 'set'].map(m => jsx('option', { value: m, children: m }, m)) }),
                  fld('Curve'),
                  jsx('select', { value: e.curve || 'lin', onChange: (ev) => mutateMx(p => { p.matrix[i].curve = ev.target.value }, 'curve set'), style: inpS, children: ['lin', 'exp', 'log', 's', 'step4', 'snap12'].map(c => jsx('option', { value: c, children: c }, c)) }),
                  fld('Slew'),
                  jsx('input', { type: 'number', min: 0, max: 1, step: 0.05, defaultValue: e.slew || 0, onBlur: (ev) => commitNum(i, 'slew', ev, 0, 1, 0), 'data-tip': 'slew 0..1', style: { ...inpS, width: 54 } }, 's-' + (e.slew || 0)))
                if (isL) els.push(
                  fld('Wave'),
                  jsx('select', { value: e.wave || (prof.lfo || {}).wave || 'sine', onChange: (ev) => mutateMx(p => { p.matrix[i].wave = ev.target.value }, 'wave set'), style: inpS, 'data-tip': 'sh = sample-and-hold: a new random step every cycle', children: WAV.map(w => jsx('option', { value: w, children: w === 'sh' ? 'sh (rand)' : w }, w)) }),
                  fld('Rate Hz'),
                  jsx('input', { type: 'number', min: 0.01, max: 10, step: 0.1, defaultValue: e.rate_hz != null ? e.rate_hz : ((prof.lfo || {}).rate_hz != null ? (prof.lfo || {}).rate_hz : 2), onBlur: (ev) => commitNum(i, 'rate_hz', ev, 0.01, 10, 2), 'data-tip': "this lane's own LFO rate (Hz) — lanes are independent", style: { ...inpS, width: 54 } }, 'r-' + (e.rate_hz != null ? e.rate_hz : ((prof.lfo || {}).rate_hz != null ? (prof.lfo || {}).rate_hz : 2))),
                  fld('Phase'),
                  jsx('input', { type: 'number', min: 0, max: 1, step: 0.05, defaultValue: e.phase || 0, onBlur: (ev) => commitNum(i, 'phase', ev, 0, 1, 0), 'data-tip': 'start offset 0..1 of a cycle — de-syncs two lanes at the same rate', style: { ...inpS, width: 52 } }, 'p-' + (e.phase || 0)))
              }
              if (k === 'move') {
                els.push(
                  fld('From'),
                  jsx('input', { type: 'number', min: 0, max: 127, step: 1, defaultValue: e.from != null ? e.from : 0, onBlur: (ev) => commitInt(i, 'from', ev, 0, 127, 0), 'data-tip': 'start value 0..127', style: { ...inpS, width: 48 } }, 'f-' + (e.from != null ? e.from : 0)),
                  fld('To'),
                  jsx('input', { type: 'number', min: 0, max: 127, step: 1, defaultValue: e.to != null ? e.to : 127, onBlur: (ev) => commitInt(i, 'to', ev, 0, 127, 127), 'data-tip': 'end value 0..127', style: { ...inpS, width: 48 } }, 't-' + (e.to != null ? e.to : 127)),
                  fld('Over ms'),
                  jsx('input', { type: 'number', min: 10, max: 600000, step: 100, defaultValue: e.duration_ms != null ? e.duration_ms : 5000, onBlur: (ev) => commitInt(i, 'duration_ms', ev, 10, 600000, 5000), 'data-tip': 'duration in milliseconds', style: { ...inpS, width: 66 } }, 'm-' + (e.duration_ms != null ? e.duration_ms : 5000)),
                  fld('Curve'),
                  jsx('select', { value: e.curve || 'lin', onChange: (ev) => mutateMx(p => { p.matrix[i].curve = ev.target.value }, 'curve set'), style: inpS, children: ['lin', 'exp', 'log', 's', 'step4', 'snap12'].map(c => jsx('option', { value: c, children: c }, c)) }),
                  fld('Hold'),
                  jsx('input', { type: 'checkbox', checked: e.hold !== false, onChange: (ev) => mutateMx(p => { p.matrix[i].hold = ev.target.checked }, 'hold set'), style: { accentColor: 'var(--ui-accent)' }, 'data-tip': 'on: stays at the end value · off: returns to the captured base when the ramp completes. Stop/panic always restores the captured value' }))
              }
              if (k === 'when') {
                els.push(
                  fld('Level'),
                  jsx('input', { type: 'number', min: 0, max: 127, step: 1, defaultValue: e.level != null ? e.level : 90, onBlur: (ev) => commitInt(i, 'level', ev, 0, 127, 90), 'data-tip': 'crossing point 0..127', style: { ...inpS, width: 48 } }, 'l-' + (e.level != null ? e.level : 90)),
                  fld('Dir'),
                  jsx('select', { value: e.crosses || 'up', onChange: (ev) => mutateMx(p => { p.matrix[i].crosses = ev.target.value }, 'crosses set'), style: inpS, children: ['up', 'down'].map(x => jsx('option', { value: x, children: x }, x)) }),
                  fld('Once'),
                  jsx('input', { type: 'checkbox', checked: e.once !== false, onChange: (ev) => mutateMx(p => { p.matrix[i].once = ev.target.checked }, 'once set'), style: { accentColor: 'var(--ui-accent)' }, 'data-tip': 'on: fires one time · off: re-arms when the source drops back below (or rises above) the level' }))
              }
              if (k === 'when' || k === 'watch') {
                els.push(fld('Then'))
                ;(e.then || []).forEach((t, j) => {
                  const tk = t.kind || 'move'
                  els.push(
                    // regression guard UI8: switching a then-child lfo<->move used to touch ONLY
// kind — the form DISPLAYED defaults (from 0, to 127, tri 0.5) but the doc
// stored none, so Save died on the validator unless the user blurs every
// box. Re-shape the child the moment the kind changes (setKind law for
// nested rows, mirrors the top-level edge switch).
jsx('select', { value: tk, onChange: (ev) => { const nv = ev.target.value; mutateMx(p => { const t = p.matrix[i].then[j]; t.kind = nv; if (nv === 'move') { delete t.wave; delete t.rate_hz; delete t.center; delete t.amp; delete t.depth; if (t.from == null) t.from = 0; if (t.to == null) t.to = 127; if (t.duration_ms == null) t.duration_ms = 3000 } else { delete t.from; delete t.to; delete t.duration_ms; if (t.wave == null) t.wave = 'tri'; if (t.rate_hz == null) t.rate_hz = 0.5; if (t.center == null) t.center = 64; if (t.amp == null) t.amp = 25 } }, 'then kind set') }, style: inpS, children: ['move', 'lfo'].map(x => jsx('option', { value: x, children: x }, x)) }, 'tk-' + i + '-' + j),
                    fld('Target'),
                    jsx('select', { value: t.target || '', onChange: (ev) => thenSet(i, j, 'target', ev.target.value, 'then target set'), style: inpS, children: tgtOpt }),
                    tk === 'move'
                      ? jsxs('span', { style: { display: 'inline-flex', gap: 4, alignItems: 'center' }, children: [
                          fld('From'), jsx('input', { type: 'number', min: 0, max: 127, step: 1, defaultValue: t.from != null ? t.from : 0, onBlur: (ev) => commitThenInt(i, j, 'from', ev, 0, 127, 0), style: { ...inpS, width: 46 } }, 'tf-' + i + '-' + j + '-' + (t.from != null ? t.from : 0)),
                          fld('To'), jsx('input', { type: 'number', min: 0, max: 127, step: 1, defaultValue: t.to != null ? t.to : 127, onBlur: (ev) => commitThenInt(i, j, 'to', ev, 0, 127, 127), style: { ...inpS, width: 46 } }, 'tt-' + i + '-' + j + '-' + (t.to != null ? t.to : 127)),
                          fld('Ms'), jsx('input', { type: 'number', min: 10, max: 600000, step: 100, defaultValue: t.duration_ms != null ? t.duration_ms : 3000, onBlur: (ev) => commitThenInt(i, j, 'duration_ms', ev, 10, 600000, 3000), style: { ...inpS, width: 60 } }, 'tm-' + i + '-' + j + '-' + (t.duration_ms != null ? t.duration_ms : 3000)),
                        ] }, 'tmv-' + i + '-' + j)
                      : jsxs('span', { style: { display: 'inline-flex', gap: 4, alignItems: 'center' }, children: [
                          fld('Wave'), jsx('select', { value: t.wave || 'tri', onChange: (ev) => thenSet(i, j, 'wave', ev.target.value, 'then wave set'), style: inpS, children: WAV.map(w => jsx('option', { value: w, children: w }, w)) }),
                          fld('Hz'), jsx('input', { type: 'number', min: 0.01, max: 10, step: 0.1, defaultValue: t.rate_hz != null ? t.rate_hz : 0.5, onBlur: (ev) => commitThenNum(i, j, 'rate_hz', ev, 0.01, 10, 0.5), 'data-tip': 'oscillation rate — 0.5 Hz = full back-and-forth every 2 s', style: { ...inpS, width: 50 } }, 'tr-' + i + '-' + j + '-' + (t.rate_hz != null ? t.rate_hz : 0.5)),
                          fld('Center'), jsx('input', { type: 'number', min: 0, max: 127, step: 1, defaultValue: t.center != null ? t.center : 64, onBlur: (ev) => commitThenInt(i, j, 'center', ev, 0, 127, 64), 'data-tip': 'the swing orbits THIS value, not the knob position', style: { ...inpS, width: 46 } }, 'tc-' + i + '-' + j + '-' + (t.center != null ? t.center : 64)),
                          fld('Amp'), jsx('input', { type: 'number', min: 0, max: 127, step: 1, defaultValue: t.amp != null ? t.amp : 25, onBlur: (ev) => commitThenInt(i, j, 'amp', ev, 0, 127, 25), 'data-tip': '± around center (center 50 amp 17 ≈ swing 30%..70%)', style: { ...inpS, width: 46 } }, 'ta-' + i + '-' + j + '-' + (t.amp != null ? t.amp : 25)),
                        ] }, 'tl-' + i + '-' + j),
                    jsx('button', { onClick: () => thenDel(i, j), style: tinyS, 'data-tip': 'remove this then-entry', children: '×' }, 'tx-' + i + '-' + j))
                })
                els.push(
                  jsx('button', { onClick: () => thenAdd(i, 'move'), style: tinyS, 'data-tip': 'then: run a ramp', children: '+move' }),
                  jsx('button', { onClick: () => thenAdd(i, 'lfo'), style: tinyS, 'data-tip': 'then: start oscillating (center/amp — e.g. swing 30%↔70% every 2 s)', children: '+lfo' }))
              }
              els.push(jsx('button', { onClick: () => mutateMx(p => { p.matrix = p.matrix.filter((_, j) => j !== i) }, 'lane removed'), style: tinyS, children: '×' }))
              return els
            })(),
          ] }, 'mx-' + i)),  // index-only key: the old key embedded source+target, so editing either REMOUNTED the row and stole focus mid-edit
      edit && prof && (prof.matrix || []).some(e => (e.source || 'lfo') === 'lfo')
        ? jsxs('div', { style: { display: 'flex', gap: 6, alignItems: 'center', marginTop: 4, fontSize: 12, color: 'var(--ui-text-secondary)' }, children: [
            jsx('span', { children: 'Default LFO (lanes without their own)' }),
            jsx('select', { value: (prof.lfo || {}).wave || 'sine', onChange: (ev) => mutateMx(p => { p.lfo = { ...(p.lfo || {}), wave: ev.target.value } }, 'lfo wave set'), style: inpS, children: ['sine', 'tri', 'saw', 'sh'].map(w => jsx('option', { value: w, children: w === 'sh' ? 'sh (rand)' : w }, w)) }),
            jsx('input', { type: 'number', min: 0.01, max: 10, step: 0.1, defaultValue: (prof.lfo || {}).rate_hz != null ? (prof.lfo || {}).rate_hz : 2, onBlur: (ev) => { const v = Math.max(0.01, Math.min(10, parseFloat(ev.target.value) || 2)); ev.target.value = v; const cur = (prof.lfo || {}).rate_hz != null ? (prof.lfo || {}).rate_hz : 2; if (v === cur) return; mutateMx(p => { p.lfo = { ...(p.lfo || {}), rate_hz: v } }, 'lfo rate set') }, 'data-tip': 'LFO rate Hz (validator cap 10)', style: { ...inpS, width: 58 } }),
            jsx('span', { style: { fontSize: 11 }, children: 'Hz — hot-applies to a running matrix' }),
          ] })
        : null,
      // Deep rinse (blocker): this panel renders even when prof is NULL but a
      // matrix is running (the #10 gate's whole point — an invalid active
      // profile must not strand the engine); every prof deref here used to
      // TypeError and blank the pane.
      !((prof && prof.matrix) || []).length ? jsx('div', { style: { fontSize: 12, color: 'var(--ui-text-secondary)' }, children: 'No modulation lanes yet — add one below, then Start.' }) : null,
      jsxs('div', { style: { display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 8, alignItems: 'center' }, children: [
        fld('Source'),
        jsx('select', { value: mx.src, onChange: (e) => setMx({ ...mx, src: e.target.value }), style: inpS,
          children: [jsx('option', { value: 'lfo', children: 'LFO' }, 'lfo')].concat(ctrlIds.map(cid => jsx('option', { value: cid, children: (prof.controls[cid] || {}).label || cid }, cid))) }),
        fld('Target'),
        jsx('select', { value: mx.tgt, onChange: (e) => setMx({ ...mx, tgt: e.target.value }), style: inpS,
          children: [jsx('option', { value: '', children: 'target…' }, 'ph')].concat(ctrlIds.map(cid => jsx('option', { value: cid, children: (prof.controls[cid] || {}).label || cid }, cid))) }),
        fld('Depth'),
        jsx('input', { type: 'number', min: -2, max: 2, step: 0.1, defaultValue: mx.depth, onBlur: (e) => setMx({ ...mx, depth: Math.max(-2, Math.min(2, parseFloat(e.target.value) || 0)) }), 'data-tip': 'depth (-2..2)', style: { ...inpS, width: 56 } }),  // review: controlled parseFloat||0 ate the decimal point mid-typing ("0.5" became 0) — uncontrolled blur like the lane rows
        mx.src === 'lfo' ? fld('Wave') : null,
        mx.src === 'lfo' ? jsx('select', { value: mx.wave, onChange: (e) => setMx({ ...mx, wave: e.target.value }), style: inpS, children: ['sine', 'tri', 'saw', 'sh'].map(w => jsx('option', { value: w, children: w === 'sh' ? 'sh (rand)' : w }, w)) }) : null,
        mx.src === 'lfo' ? fld('Rate Hz') : null,
        mx.src === 'lfo' ? jsx('input', { type: 'number', min: 0.01, max: 10, step: 0.1, defaultValue: mx.rate, onBlur: (e) => setMx({ ...mx, rate: Math.max(0.01, Math.min(10, parseFloat(e.target.value) || 2)) }), 'data-tip': 'LFO rate Hz (validator cap 10)', style: { ...inpS, width: 56 } }) : null,
        jsx('button', { onClick: () => {
          const tgt = mx.tgt || ctrlIds[0]
          if (!tgt) { setStatus('matrix: pick a target'); return }
          // mutateMx (not mutate): a RUNNING matrix hot-applies the new lane too.
          // Multi-matrix law: the lane carries its OWN oscillator — adding a
          // second lane never disturbs the first.
          mutateMx(p => {
            const edge = { source: mx.src, target: tgt, depth: mx.depth, mode: 'add' }
            if (mx.src === 'lfo') { edge.wave = mx.wave; edge.rate_hz = mx.rate }
            p.matrix = (p.matrix || []).concat([edge])
          }, 'lane added')
        }, 'data-tip': 'Add another modulation lane — every lane has its own LFO (wave + rate) and runs independently of the others', style: { border: '1px solid var(--ui-accent)', color: 'var(--ui-accent)', background: 'transparent', padding: '4px 10px', borderRadius: 6, cursor: 'pointer' }, children: '+ matrix lane' }),
      ] }),
      st && st.matrix && st.matrix.running && Object.keys(st.matrix.targets || {}).length
        ? jsx('div', { style: { fontSize: 11, color: 'var(--ui-accent)', marginTop: 6 }, children: 'live: ' + Object.entries(st.matrix.targets).map(([k, v]) => `${(((prof && prof.controls) || {})[k] || {}).label || k}=${v}`).join('  ') })
        : null,
    ] }) : null,
    // record-a-ride -> .mid export
    jsxs('div', { style: { marginTop: 16, border: '1px solid var(--ui-stroke-secondary)', borderRadius: 10, padding: 12 }, children: [
      jsxs('div', { style: { display: 'flex', gap: 8, alignItems: 'center' }, children: [
        jsx('div', { style: { fontSize: 11, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.12em', color: 'var(--ui-text-secondary)' }, children: 'Record & export' }),
        st && st.recorder && st.recorder.recording ? jsx('span', { style: { fontSize: 11, color: 'var(--ui-accent)' }, children: `● recording ${(st.recorder.elapsed_ms / 1000).toFixed(1)}s` }) : null,
        jsx('input', { placeholder: 'take name', value: recName, onChange: (e) => setRecName(e.target.value), style: { ...inpS, width: 150 } }),
        jsx('span', { style: { fontSize: 11, color: 'var(--ui-text-secondary)' }, children: 'BPM' }),
        jsx('input', { type: 'number', min: 10, max: 999, value: recBpm, onChange: (e) => setRecBpm(e.target.value), 'data-tip': 'tempo of the exported .mid (beats per minute) — NOT the recording length', style: { ...inpS, width: 64 } }),
        jsx('span', { style: { flex: 1 } }),
        st && st.recorder && st.recorder.recording
          ? jsx('button', { onClick: () => api.rest('/rec/stop', { method: 'POST', body: { bpm: Math.max(10, Math.min(999, parseFloat(recBpm) || 120)), name: recName || 'take' } }).then(r => {
              if (r && r.ok && r.mid_b64) {
                const bin = atob(r.mid_b64), bytes = new Uint8Array(bin.length)
                for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
                const a = document.createElement('a')
                a.href = URL.createObjectURL(new Blob([bytes], { type: 'audio/midi' }))
                a.download = r.filename || 'take.mid'
                a.click()
                setTimeout(() => URL.revokeObjectURL(a.href), 10000)  // deep rinse: blob URLs were never revoked
                setStatus(`exported ${r.notes} notes, ${r.cc_tracks} CC track(s) — saved ${r.path}` + (r.pending_overwritten ? ' — ⚠ this replaced an earlier UNEXPORTED take' : ''))
              } else setStatus(`recorder: ${(r && r.error) || 'unreachable'}` + (r && r.pending_take ? ' — the take is still held (see export pending take)' : ''))
              refresh(); loadTakes()
            }).catch(() => setStatus('recorder stop: backend unreachable — the take is still recording')), style: { border: '1px solid var(--ui-accent)', color: 'var(--ui-accent)', background: 'transparent', padding: '4px 12px', borderRadius: 6, cursor: 'pointer' }, children: 'Stop & export .mid' })
          : jsx('button', { onClick: () => api.rest('/rec/start', { method: 'POST' }).then(r => { setStatus(r && r.ok ? 'recording — twist knobs / send notes, then Stop & export' : `recorder: ${(r && r.error) || 'unreachable'}`); refresh() }).catch(() => setStatus('recorder start: backend unreachable')), style: { border: '1px solid var(--ui-stroke-secondary)', color: 'var(--ui-text-secondary)', background: 'transparent', padding: '4px 12px', borderRadius: 6, cursor: 'pointer' }, children: 'Record' }),
        // regression guard UI4: the button used to vanish the moment a stopped take
        // waited for export — and it refused to stop a live recording. It now
        // shows whenever ANYTHING is discardable (recording OR pending take)
        // and the backend stops honouring both halves.
        st && st.recorder && (st.recorder.recording || st.recorder.pending_take)
          ? jsx('button', { onClick: () => api.rest('/rec/discard', { method: 'POST' }).then(r => { setStatus(r && r.ok ? (r.stopped_recording ? 'recording stopped — nothing written' : 'pending take discarded — nothing written') : `recorder discard: ${(r && r.error) || 'unreachable'}`); refresh() }).catch(() => setStatus('recorder discard: backend unreachable')), 'data-tip': 'Stop recording WITHOUT producing a .mid, or drop a pending unexported take — junk takes die here (one Undo does not apply: nothing was saved)', style: { border: '1px solid var(--ui-stroke-secondary)', color: 'var(--ui-text-secondary)', background: 'transparent', padding: '4px 12px', borderRadius: 6, cursor: 'pointer' }, children: 'Discard' })
          : null,
        // an unexported take is VISIBLE now, with its retry.
        // Before, a failed export left the take held in the backend while the
        // Record button invited a fresh take whose Stop would silently
        // OVERWRITE the first performance.
        st && st.recorder && !st.recorder.recording && st.recorder.pending_take
          ? jsx('button', { onClick: () => api.rest('/rec/export', { method: 'POST', body: { bpm: Math.max(10, Math.min(999, parseFloat(recBpm) || 120)), name: recName || 'take' } }).then(r => {
              if (r && r.ok && r.mid_b64) {
                const bin = atob(r.mid_b64), bytes = new Uint8Array(bin.length)
                for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
                const a = document.createElement('a')
                a.href = URL.createObjectURL(new Blob([bytes], { type: 'audio/midi' }))
                a.download = r.filename || 'take.mid'
                a.click()
                setTimeout(() => URL.revokeObjectURL(a.href), 10000)
                setStatus(`re-exported pending take — saved ${r.path}`)
              } else setStatus(`export: ${(r && r.error) || 'unreachable'}${r && r.pending ? ' (take still held)' : ''}`)
              refresh(); loadTakes()
            }).catch(() => setStatus('export: backend unreachable')), 'data-tip': 'A take is recorded but not exported yet (a previous export failed or was cancelled at stop) — write it to a .mid now', style: { border: '1px solid var(--ui-accent)', color: 'var(--ui-accent)', background: 'transparent', padding: '4px 12px', borderRadius: 6, cursor: 'pointer' }, children: 'export pending take' })
          : null,
        jsx('button', { onClick: () => api.rest('/rec/open', { method: 'POST' }).then(r => setStatus(r && r.ok ? `opened ${r.path}` : `open folder: ${(r && r.error) || 'unreachable'}`)).catch(() => setStatus('open folder: backend unreachable')), 'data-tip': 'Open the takes folder in Windows Explorer', style: { border: '1px solid var(--ui-stroke-secondary)', color: 'var(--ui-text-secondary)', background: 'transparent', padding: '4px 12px', borderRadius: 6, cursor: 'pointer' }, children: 'Open folder' }),
      ] }),
      jsx('div', { style: { fontSize: 11, color: 'var(--ui-text-secondary)', marginTop: 4 }, children: 'Captures every CC ride and note (hardware or on-screen) and exports a Standard MIDI File any DAW imports.' }),
      takes && takes.length ? jsx('div', { style: { marginTop: 8, borderTop: '1px solid var(--ui-stroke-secondary)', paddingTop: 6 }, children: takes.map(t => jsxs('div', { style: { display: 'flex', gap: 8, alignItems: 'center', fontSize: 12, color: 'var(--ui-text-secondary)', marginBottom: 3 }, children: [
        jsx('span', { style: { flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }, children: `${t.name} · ${(t.size / 1024).toFixed(1)} KB · ${new Date(t.mtime * 1000).toLocaleString()}` }),
        jsx('button', { onClick: () => dlTake(t.name), style: tinyS, children: 'download' }),
        jsx('button', { onClick: () => delTake(t.name), style: tinyS, children: 'delete' }),
      ] }, 'take-' + t.name)) }) : null,
    ] }),
    // MIDI piano (owner ask 2026-11-09): same box style as matrix / record.
    // Key count + lowest note come from the registered preset's 'keyboard'
    // declaration — the MicroFreak preset shows its exact 25 keys (C3 up).
    // Live notes (from the real synth or elsewhere) light the keys; clicking
    // a key plays it on the active profile's channel.
    prof && prof.keyboard ? jsxs('div', { style: { marginTop: 16, border: '1px solid var(--ui-stroke-secondary)', borderRadius: 10, padding: 12 }, children: [
      jsx('div', { style: { fontSize: 11, fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.12em', color: 'var(--ui-text-secondary)' }, children: `Piano (${prof.keyboard.keys} keys — matches ${prof.device})` }),
      // the piano was the only send omitting its bus — deck law
    // says every send carries {chan, bus}.
    jsx(PianoBox, { first: prof.keyboard.first, keys: prof.keyboard.keys, notes: (st && st.notes) || [], onNote: (note, velocity) => api.rest('/note', { method: 'POST', body: { note, velocity, channel: Number(prof.channel) || 0, bus: activeBusId } }).then(r => { if (!r || !r.ok) setStatus(`piano note lost: ${(r && r.error) || 'backend unreachable'} — a dropped note-off means a stuck note`) }).catch(() => setStatus('piano note lost: backend unreachable — a dropped note-off means a stuck note')), key: 'piano' }),
    ] }, 'piano-box') : null,
    busy ? jsx('div', { style: { fontSize: 11, color: 'var(--ui-text-secondary)' }, children: 'saving…' }) : null,
    // drag overlays: rubber band (selection) + group ghost (move)
    // Import file input lives at ROOT (always mounted): it used to sit inside
    // the SETTINGS panel, and closing the menu unmounted it before the picker
    // could fire — React 19 root-delegation never saw the change (import was
    // silently dead, review 2026-11-09).
    jsx('input', { type: 'file', accept: '.json,application/json', style: { display: 'none' }, ref: fileRef, onChange: onPickFile }, 'file-in'),
    band ? jsx('div', { 'data-ov': 'band', style: { position: 'fixed', left: Math.min(band.x0, (bandPosRef.current || band).x1), top: Math.min(band.y0, (bandPosRef.current || band).y1), width: Math.abs((bandPosRef.current || band).x1 - band.x0), height: Math.abs((bandPosRef.current || band).y1 - band.y0), border: '1px solid var(--ui-accent)', background: 'rgba(80,160,255,0.10)', zIndex: 9998, pointerEvents: 'none' } }, 'band') : null,
    drag ? jsx('div', { 'data-ov': 'ghost', style: { position: 'fixed', left: (dragPosRef.current || drag).px + 12, top: (dragPosRef.current || drag).py + 12, padding: '4px 9px', borderRadius: 6, fontSize: 12, background: 'var(--ui-bg-secondary)', border: '1px solid var(--ui-accent)', color: 'var(--ui-text-primary)', zIndex: 9999, pointerEvents: 'none' }, children: `${drag.cids.length} control${drag.cids.length > 1 ? 's' : ''}` }, 'ghost') : null,
    secDrag && prof ? jsx('div', { 'data-ov': 'secghost', style: { position: 'fixed', left: (secPosRef.current || secDrag).px + 12, top: (secPosRef.current || secDrag).py + 12, padding: '4px 9px', borderRadius: 6, fontSize: 12, background: 'var(--ui-bg-secondary)', border: '1px solid var(--ui-accent)', color: 'var(--ui-text-primary)', zIndex: 9999, pointerEvents: 'none' }, children: `category: ${((prof.sections || {})[secDrag.sid] || {}).label || secDrag.sid}` }, 'secghost') : null,
    // footer truth line
    // Deck surfaces (2026-11-09): every profile after the active one renders
    // as its own bordered card — live values from ITS bus blob, sends to ITS
    // bus+channel. Visually separate by construction (owner: "all my stuff in
    // 1 place", still one screen, still one workspace).
    ...(((st && st.deck) || []).slice(1).map(d => {
      const doc = deckDocs[d.profile_id]
      if (!d.valid) return jsxs('div', { style: { marginTop: 14, border: '1px solid var(--ui-stroke-secondary)', borderRadius: 10, padding: '8px 12px', fontSize: 12, color: 'var(--ui-text-secondary)' }, children: [`${d.profile_id}: profile invalid — fix or remove it`, jsx('button', { onClick: () => deckSet(deckRef.current.filter(x => x !== d.profile_id)), style: { ...tinyS, marginLeft: 8 }, children: 'remove from deck' })] }, 'deck-bad-' + d.profile_id)
      if (!doc) return null // doc still loading — one poll, then it renders
      const busSt = ((st.buses) || []).find(b => b.bus === (doc.bus || 'primary')) || null
      return jsx(DeckSurface, {
        key: 'deck-' + d.profile_id, doc, busSt, busDraft, setBusDraft, setStatus,
        busIds: [...new Set(((st.buses) || []).map(b => b.bus))],
        onActivate: () => deckSet([d.profile_id, ...deckRef.current.filter(x => x !== d.profile_id)]),
        onRemove: () => deckSet(deckRef.current.filter(x => x !== d.profile_id)),
        onBus: (b) => setDeckBus(doc, b),
      })
    })),
    jsx('div', { style: { fontSize: 11, color: 'var(--ui-text-secondary)', marginTop: 16 }, children: `backend: ${st ? st.backend : '?'} · pid ${st ? st.pid : '?'} · profile: ${prof ? prof.profile_id : '—'}${st && st.active_profile && prof && st.active_profile !== prof.profile_id ? ' (state says ' + st.active_profile + ')' : ''}` }),
  ] })
}

// Full-spectrum law (owner ruling): any hue via a wheel, stored as #rrggbb
// (validator _HEX mirrors this). neon() = the FL-mixer-style strong version of
// a color for value indicators; dim() = the quiet track version.
function hexToHsl(hex) {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex || '')
  if (!m) return { h: 200, s: 80, l: 55 }
  const n = parseInt(m[1], 16)
  const r = ((n >> 16) & 255) / 255, g = ((n >> 8) & 255) / 255, b = (n & 255) / 255
  const mx = Math.max(r, g, b), mn = Math.min(r, g, b), l = (mx + mn) / 2
  let h = 0, s = 0
  if (mx !== mn) {
    const d = mx - mn
    s = l > 0.5 ? d / (2 - mx - mn) : d / (mx + mn)
    h = mx === r ? (g - b) / d + (g < b ? 6 : 0) : mx === g ? (b - r) / d + 2 : (r - g) / d + 4
    h *= 60
  }
  return { h: Math.round(h), s: Math.round(s * 100), l: Math.round(l * 100) }
}
function hslToHex(h, s, l) {
  s /= 100; l /= 100
  const k = (n) => (n + h / 30) % 12
  const a = s * Math.min(l, 1 - l)
  const f = (n) => l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)))
  const to255 = (x) => Math.round(x * 255).toString(16).padStart(2, '0')
  return `#${to255(f(0))}${to255(f(8))}${to255(f(4))}`
}
const neon = (hex) => { const { h, s, l } = hexToHsl(hex); return hslToHex(h, Math.max(s, 90), Math.min(65, Math.max(52, l))) }
const dim = (hex) => { const { h, s } = hexToHsl(hex); return hslToHex(h, Math.min(s, 60), 38) }

// Color-mode law: every colorable part is its own click target — dashed ring
// when pickable, solid accent when picked; one central wheel paints the pick.
const partRing = (part, kind) => part && part.mode
  ? { outline: part.sel[kind + ':' + part.cid] ? '2px solid var(--ui-accent)' : '1px dashed rgba(160,160,160,0.75)', outlineOffset: 2, borderRadius: 6 }
  : {}
const partTap = (part, kind) => part && part.mode
  ? (e) => { e.stopPropagation(); part.toggle(kind + ':' + part.cid) }
  : undefined
// The control NAME is its own colorable part (n:) — separate from the value.
const nameDiv = (part, ctrl) => jsx('div', {
  onClick: partTap(part, 'n'),
  style: { fontSize: 11, color: (part && part.nameColor) || 'var(--ui-text-primary)', ...partRing(part, 'n') },
  children: ctrl.label })

// Color helpers (module scope): hex <-> rgb for canvas tints, hsl -> hex for
// the rainbow-ifier (even hue spread across picked units).
const hexRgb = (h) => ({ r: parseInt(h.slice(1, 3), 16), g: parseInt(h.slice(3, 5), 16), b: parseInt(h.slice(5, 7), 16) })
const hslHex = (hh, s, l) => {
  const f = (n) => { const k = (n + hh / 30) % 12, a = s * Math.min(l, 1 - l)
    return l - a * Math.max(-1, Math.min(k - 3, Math.min(9 - k, 1))) }
  const t = (v) => Math.round(v * 255).toString(16).padStart(2, '0')
  return '#' + t(f(0)) + t(f(8)) + t(f(4))
}
function rainbow(text, size = 18) {
  const chars = [...text]
  return jsxs('span', { style: { fontWeight: 800, fontSize: size, letterSpacing: '0.05em' }, children:
    chars.map((ch, i) => jsx('span', { style: { color: `hsl(${Math.round((i * 360) / Math.max(1, chars.length))} 90% 62%)` }, children: ch }, 'rb' + i)) }, 'rainbow')
}
function HueWheel({ color, onChange, size = 110 }) {
  const ref = useRef(null)
  const [hsl, setHsl] = useState(() => hexToHsl(color || '#0091ff'))
  // Review: the wheel used to seed ONCE and never re-sync — deselect a red
  // knob, pick a blue one, and the thumb/gradient/onChange base still said
  // red. Re-seed when the picked part's color changes (our own paint echoes
  // back as the same value — hsl->hex->hsl round-trips stable, no jitter).
  useEffect(() => { setHsl(hexToHsl(color || '#0091ff')) }, [color])
  const ring = size / 2 - 8
  const stops = [0, 60, 120, 180, 240, 300, 360]
    .map((d, i, a) => `hsl(${d} ${Math.max(hsl.s, 40)}% 55%)${i < a.length - 1 ? ',' : ''}`).join('')
  const rad = (hsl.h - 90) * Math.PI / 180
  const mr = ring * (hsl.s / 100)
  const pick = (e) => {
    const b = ref.current.getBoundingClientRect()
    const cx = b.left + b.width / 2, cy = b.top + b.height / 2
    const h = (((Math.atan2(e.clientY - cy, e.clientX - cx) * 180 / Math.PI + 90) % 360) + 360) % 360
    const s = Math.round(Math.min(1, Math.hypot(e.clientX - cx, e.clientY - cy) / ring) * 100)
    const next = { h: Math.round(h), s, l: hsl.l }
    setHsl(next)
    onChange(hslToHex(next.h, next.s, next.l))
  }
  return jsxs('div', { style: { display: 'inline-flex', gap: 8, alignItems: 'center', verticalAlign: 'middle' }, children: [
    jsx('div', { ref,
      onPointerDown: (e) => {
        e.preventDefault(); e.stopPropagation()
        pick(e)
        // Deep rinse: cancel only unhooks — pick() already applied live,
        // there is nothing to commit or abort.
        startPointerDrag({ onMove: pick })
      },
      style: { position: 'relative', width: size, height: size, borderRadius: '50%', cursor: 'crosshair', flexShrink: 0,
        background: `conic-gradient(${stops})` },
      children: jsx('div', { style: { position: 'absolute', width: 14, height: 14, borderRadius: '50%', border: '2px solid #fff', boxShadow: '0 0 4px rgba(0,0,0,0.6)', pointerEvents: 'none',
        left: size / 2 + mr * Math.cos(rad) - 7, top: size / 2 + mr * Math.sin(rad) - 7,
        background: hslToHex(hsl.h, hsl.s, hsl.l) } }) }),
    jsx('div', { style: { display: 'flex', flexDirection: 'column', gap: 4, alignItems: 'center' }, children: [
      jsx('input', { type: 'range', min: 20, max: 80, value: hsl.l,
        onInput: (e) => { const l = +e.target.value; setHsl({ ...hsl, l }); onChange(hslToHex(hsl.h, hsl.s, l)) },
        'data-tip': 'Brightness', style: { writingMode: 'vertical-rl', direction: 'rtl', height: size - 20, accentColor: hslToHex(hsl.h, hsl.s, hsl.l) } }),
    ] }),
  ] })
}

function renderGrid(list, renderCtrl, edit, ops) {
  const { rename, setWidget, del, rebindClick, rebind, renaming, setRenaming, commitRen, setCc, setStatus, sel, drag, startCardDrag, startBand, sid, colorMode, csel, togglePart, specSources, patchSpecCtrl, resizeAnalyzer, rz, ctrls } = ops
  // (tinyS is module-scope now — the old local `tiny` was a byte-duplicate
  // re-allocated per renderGrid call.)
  const dragging = (drag && drag.cids) || []
  const dropCell = drag && drag.over && drag.over.sid === sid ? { col: drag.over.col, row: drag.over.row } : null
  // Proportional preview law (owner bug 2026-11-09): the drop placeholder is
  // the SAME footprint as what's being dragged — a 4-cell analyzer previews
  // 4 cells wide, not one. B5 preview (owner ruling 2026-11-10): for a GROUP
  // the outline shows EVERY member at its offset cell — the real footprint,
  // not just the largest single box. effW, not raw .w (the toolbar-analyzer
  // default lived in THREE places and the old preview committed 1 vs 4).
  // Grid law: one row div per y, one slot per x — empty cells stay empty
  // (gaps are real), drops land in the cell under the pointer.
  const byRow = new Map()
  const owned = new Set()
  for (const c of list) {
    const y = c.y || 0
    if (!byRow.has(y)) byRow.set(y, new Map())
    let x = c.x || 0
    while (byRow.get(y).has(x)) x += 1  // B2 defence (): exact-cell collisions shift to the next free slot at render time — a control can NEVER silently vanish from the Map (hand-edited/imported docs)
    byRow.get(y).set(x, c)
    owned.add(y + ':' + x)
  }
  const rowKeys = [...byRow.keys()].sort((a, b) => a - b)
  // w-span law (analyzer boxes are 4 cells wide): a control with w>1 renders
  // once at its x and COVERS the cells to its right — covered slots render
  // nothing, so the grid stays honest (one slot per x, gaps still real).
  const cover = new Set()
  for (const c of list) { const w = Math.max(1, c.w || 1); for (let k = 1; k < w; k++) { const ck = (c.y || 0) + ':' + ((c.x || 0) + k); if (!owned.has(ck)) cover.add(ck) } }  // B2 defence: a cell OWNED by a control is never swallowed by a neighbour's span — collisions stay visible, never invisible
  if (dropCell && !byRow.has(dropCell.row)) { byRow.set(dropCell.row, new Map()); rowKeys.push(dropCell.row); rowKeys.sort((a, b) => a - b) }
  const phByRow = {}
  if (dropCell && dragging.length) {
    const cellOf = (id) => list.find(x => x.__cid === id) || (ctrls || {})[id] || {}
    const axP = Math.min(...dragging.map(id => cellOf(id).x || 0))
    const ayP = Math.min(...dragging.map(id => cellOf(id).y || 0))
    for (const id of dragging) {
      const oc = cellOf(id)
      const gy = dropCell.row + ((oc.y || 0) - ayP), gx = dropCell.col + ((oc.x || 0) - axP)
      ;(phByRow[gy] = phByRow[gy] || []).push({ x: gx, w: effW(oc), id })
      if (!byRow.has(gy)) { byRow.set(gy, new Map()); rowKeys.push(gy); rowKeys.sort((a, b) => a - b) }
    }
  }
  // footprint cells may reach past the doc's own width — the preview rows
  // must exist for every placeholder column too (B5).
  const maxX = Math.max(0, ...list.map(c => (c.x || 0) + Math.max(1, c.w || 1) - 1), ...Object.values(phByRow).flat().map(p => p.x + p.w - 1))
  // DENSE ROWS law (owner 2026-11-10 night): every y from 0..maxY renders as
  // a REAL row. An empty row is a visible, droppable gap ("gaps are real"
  // made literal); emptying a row must NEVER reflow the rows below it upward
  // — that collapse-and-reflow was half of the "everything I never touched
  // moved away" symptom, and the missing row was half of "I can't fit things
  // in there": the space you saw simply had no DOM cell to aim at.
  const maxY = Math.max(0, ...list.map(c => c.y || 0), ...Object.keys(phByRow).map(Number))
  for (let y = 0; y <= maxY; y++) if (!byRow.has(y)) { byRow.set(y, new Map()); rowKeys.push(y) }
  rowKeys.sort((a, b) => a - b)
  const card = (c) => jsxs('div', {
    'data-card': c.__cid,
    style: { position: 'relative', width: ((rz && rz.cid === c.__cid ? rz.w : c.w) > 1 ? (rz && rz.cid === c.__cid ? rz.w : c.w) * CELL + ((rz && rz.cid === c.__cid ? rz.w : c.w) - 1) * 6 : CELL), borderRadius: 8, opacity: dragging.includes(c.__cid) ? 0.35 : 1,
      // color-coded box: explicit box_color (b: part) wins, else the control's
      // hue tints the card; hairline ring in the strong version
      background: c.box_color ? c.box_color + '33' : c.color ? c.color + '22' : 'transparent',
      boxShadow: c.color ? `inset 0 0 0 1px ${neon(c.color)}88` : (c.box_color ? `inset 0 0 0 1px ${neon(c.box_color)}66` : 'none'),
      border: edit && !colorMode ? (sel[c.__cid] ? '1px solid var(--ui-accent)' : '1px dashed var(--ui-stroke-secondary)') : 'none',
      ...(colorMode ? { outline: csel['b:' + c.__cid] ? '2px solid var(--ui-accent)' : '1px dashed rgba(160,160,160,0.75)', outlineOffset: 2 } : {}) },
    children: [
      // Edit law: in edit mode the card is the drag handle (widgets ignore
      // pointer input then). Click toggles selection; drag moves the group.
      // Color mode: the card is the BOX part (b:) — click picks it.
      colorMode ? jsx('div', { onClick: (e) => { e.stopPropagation(); togglePart('b:' + c.__cid) }, style: { cursor: 'copy' }, children: renderCtrl(c) })
        : edit ? jsx('div', { onPointerDown: (e) => startCardDrag(e, c), style: { cursor: 'move' }, children: renderCtrl(c) }) : renderCtrl(c),
      edit ? jsxs('div', { style: { display: 'flex', gap: 2, justifyContent: 'center', flexWrap: 'wrap', paddingBottom: 4, ...(colorMode ? { opacity: 0.35, pointerEvents: 'none' } : {}) }, children: [
        renaming && renaming.cid === c.__cid
          ? jsx('input', { autoFocus: true, value: renaming.val, onChange: (e) => setRenaming({ cid: c.__cid, val: e.target.value }), onKeyDown: (e) => { if (e.key === 'Enter') commitRen(); else if (e.key === 'Escape') setRenaming(null) }, onBlur: commitRen, style: { ...tinyS, width: 82, fontSize: 11, padding: '2px 5px', background: 'var(--ui-bg-secondary)', color: 'var(--ui-text-primary)' } })
          : jsx('button', { onClick: () => rename(c.__cid), 'data-tip': 'Rename this control (Enter saves, Esc cancels)', style: tinyS, children: 'name' }),
        // Analyzer edit row (owner ruling 2026-11-09): no rebind, no widget
        // picker — just name, spectrum style, audio source, delete.
        ...(c.widget === 'analyzer'
          ? [jsx('select', { value: c.source || 'pc', onChange: (e) => patchSpecCtrl(c.__cid, { source: e.target.value }), 'data-tip': 'What this analyzer listens to — a device on this screen, all MIDI, desktop audio, microphone, or everything', style: { ...tinyS, fontSize: 10, maxWidth: 140 }, children: (specSources || []).map(s => jsx('option', { value: s.value, children: s.label }, s.value)) }),
             jsx('select', { value: c.type || 'bars', onChange: (e) => patchSpecCtrl(c.__cid, { type: e.target.value }), 'data-tip': 'Spectrum style — the ported strip concepts + radial/ring', style: { ...tinyS, fontSize: 10 }, children: SPEC_STYLES.map(t => jsx('option', { value: t, children: t }, t)) })]
          : [
        jsx('button', { onClick: () => rebindClick(c.__cid), 'data-tip': 'Re-point this control at a different CC: click, then twist a real knob — or type a CC in the box that appears', style: { ...tinyS, ...(rebind && rebind.cid === c.__cid ? { color: 'var(--ui-accent)', borderColor: 'var(--ui-accent)' } : {}) }, children: 'rebind' }),
        rebind && rebind.cid === c.__cid
          ? jsx('input', { type: 'number', min: 0, max: 127, placeholder: 'CC', onKeyDown: (e) => { if (e.key !== 'Enter') return; const cc = parseInt(e.target.value, 10); if (cc >= 0 && cc <= 127) setCc(c.__cid, cc); else setStatus(`rebind refused: "${e.target.value}" is not 0-127`) }, style: { ...tinyS, width: 46, fontSize: 11, padding: '2px 4px', background: 'var(--ui-bg-secondary)', color: 'var(--ui-text-primary)' } })
          : null,
        jsx('select', { value: c.widget || 'knob', onChange: (e) => setWidget(c.__cid, e.target.value), 'data-tip': 'Change how this control looks and acts: knob, slider (measured labels make it detent/stepped), latching toggle, momentary button, or label',
          style: { ...tinyS, fontSize: 10 },
          children: ['knob', 'slider', 'toggle', 'momentary', 'label'].map(w => jsx('option', { value: w, children: w }, w)) }),
        // Wire remap UI (LMMS-borrow law, keys shipped 7d107bf; UI owner ask
        // 2026-11-10): invert + out_min/out_max live ONLY at the wire boundary
        // — the surface (knob, shadow, matrix, pane) always speaks 0..127.
        jsx('button', { onClick: () => mutate(p => { if (p.controls[c.__cid].invert) delete p.controls[c.__cid].invert; else p.controls[c.__cid].invert = true }, 'wire invert toggled'),
          'data-tip': 'Invert on the WIRE only: the device sees 127-value, the surface stays 0..127 (matrix + colors unaffected)',
          style: { ...tinyS, fontSize: 10, ...(c.invert ? { color: 'var(--ui-accent)', borderColor: 'var(--ui-accent)' } : {}) }, children: 'inv' }),
        jsx('input', { type: 'number', min: 0, max: 127, placeholder: 'wmin',
          key: 'om-' + c.__cid + '-' + (c.out_min != null ? c.out_min : 'x'),
          defaultValue: c.out_min != null ? c.out_min : '',
          onBlur: (ev) => { const raw = ev.target.value.trim()
            if (raw === '') { if (c.out_min != null || c.out_max != null) mutate(p => { delete p.controls[c.__cid].out_min; delete p.controls[c.__cid].out_max }, 'wire range cleared')
              return }
            const a = parseInt(raw, 10), b = c.out_max != null ? c.out_max : 127
            mutate(p => { p.controls[c.__cid].out_min = a; p.controls[c.__cid].out_max = b }, `wire range ${a}..${b}`) },
          onKeyDown: (ev) => { if (ev.key === 'Enter') ev.target.blur() },
          'data-tip': 'Wire range low (0-127): surface 0..127 maps onto wmin..wmax on the device. Leave BOTH boxes empty for no remap',
          style: { ...tinyS, width: 40, fontSize: 10, padding: '2px 3px', background: 'var(--ui-bg-secondary)', color: 'var(--ui-text-primary)' } }),
        jsx('input', { type: 'number', min: 0, max: 127, placeholder: 'wmax',
          key: 'ox-' + c.__cid + '-' + (c.out_max != null ? c.out_max : 'x'),
          defaultValue: c.out_max != null ? c.out_max : '',
          onBlur: (ev) => { const raw = ev.target.value.trim()
            if (raw === '') { if (c.out_min != null || c.out_max != null) mutate(p => { delete p.controls[c.__cid].out_min; delete p.controls[c.__cid].out_max }, 'wire range cleared')
              return }
            const b = parseInt(raw, 10), a = c.out_min != null ? c.out_min : 0
            mutate(p => { p.controls[c.__cid].out_min = a; p.controls[c.__cid].out_max = b }, `wire range ${a}..${b}`) },
          onKeyDown: (ev) => { if (ev.key === 'Enter') ev.target.blur() },
          'data-tip': 'Wire range high — validator refuses unless 0 <= wmin < wmax <= 127 (named error, nothing half-written)',
          style: { ...tinyS, width: 40, fontSize: 10, padding: '2px 3px', background: 'var(--ui-bg-secondary)', color: 'var(--ui-text-primary)' } }),
          ]),
        jsx('button', { onClick: () => del(c.__cid), 'data-tip': 'Delete this control (its matrix edges and aliases go too)', style: tinyS, children: '×' }),
      ] }) : null,
      edit && c.widget === 'analyzer' ? jsx('span', { onPointerDown: (e) => resizeAnalyzer(e, c), 'data-tip': 'Pull me to resize — snaps to grid cells (1-8 wide)', style: { position: 'absolute', right: -3, bottom: -3, width: 18, height: 18, cursor: 'nwse-resize', color: 'var(--ui-accent)', fontSize: 14, lineHeight: '18px', textAlign: 'center', touchAction: 'none', zIndex: 3 }, children: '⌟' }) : null,
    ] }, 'w-' + c.__cid)
  const rowDivs = rowKeys.map(y => {
    const cells = []
    for (let x = 0; x <= maxX; x++) {
      if (cover.has(y + ':' + x)) continue // inside a w>1 span — the span owns this slot
      const ph = (phByRow[y] || []).find(p => p.x === x)
      if (ph)
        cells.push(jsx('div', { style: { width: ph.w * CELL + (ph.w - 1) * 6, minHeight: 88, border: '2px dashed var(--ui-accent)', borderRadius: 8, background: 'rgba(80,160,255,0.10)' } }, 'drop-ph-' + ph.id))
      const c = byRow.get(y).get(x)
      // Drag law (owner ruling): a lifted card LEAVES its cell — no ghost box
      // left behind acting as an element; the cell is empty while it flies.
      if (c && dragging.includes(c.__cid)) cells.push(jsx('div', { style: { width: effW(c) * CELL + (effW(c) - 1) * 6, minHeight: 88 }, 'data-vacated': c.__cid }, 'vac-' + c.__cid))  // review: vacated cell keeps the card's FULL span — a lifted analyzer must not re-flow the row; minHeight keeps the ROW from collapsing while the card flies
      else if (c) cells.push(card(c))
      else if (!ph) cells.push(jsx('div', { style: { width: CELL, minHeight: 88 }, 'data-empty': y + '-' + x }, 'e-' + y + '-' + x))  // DENSE ROWS: an empty cell has the height of a control slot — the gap is visible AND hittable
    }
    return jsx('div', { 'data-row': String(y), style: { display: 'flex', gap: 6, alignItems: 'flex-start', marginBottom: 6 }, children: cells }, 'row-' + y)
  })
  return jsx('div', { 'data-grid': sid,
    onPointerDown: (e) => { if (edit && !colorMode && !(e.target.closest && e.target.closest('[data-card]'))) startBand(e) },
    style: {}, children: rowDivs })
}

// ---------------------------------------------------------------------------
// DeckSurface (2026-11-09): a SECOND profile on the same screen. Laws:
// visually separate (bordered card + device header), values live from ITS
// bus blob in /state.buses, every send carries ITS bus+channel. Editing,
// matrix, record and learn follow the ACTIVE (first) surface — this one is a
// live, playable mirror until the deck editor grows per-surface.
function DeckSurface({ doc, busSt, busIds, busDraft, setBusDraft, onActivate, onRemove, onBus, setStatus }) {
  const chan = Number(doc.channel) || 0
  const bus = doc.bus || 'primary'
  const ctx = { chan, bus }
  const ccMap = {}
  for (const c of (busSt && busSt.cc) || []) ccMap[c.channel + ':' + c.cc] = c
  const controls = Object.entries(doc.controls || {}).map(([cid, c]) => ({ ...c, __cid: cid, ...(c.widget === 'analyzer' && !c.w ? { w: 4 } : {}) }))  // same analyzer w default law as the active surface
  const sections = Object.entries(doc.sections || {}).sort((a, b) => (a[1].order || 0) - (b[1].order || 0))
  const secColor = {}
  for (const [sid, sec] of sections) if (sec.color) secColor[sid] = sec.color
  const renderCtrl = (c) => {
    if (c.widget === 'analyzer') return jsx('span', { style: { fontSize: 10, color: 'var(--ui-text-secondary)', padding: 4 }, children: '∿ ' + (c.label || 'analyzer') })
    const ent = ccMap[chan + ':' + c.cc]
    const v = ent ? ent.value : null
    const color = c.color || secColor[c.section || ''] || null
    const args = { ctrl: c, value: v, edit: false, color, part: null, ctx }
    if (c.widget === 'slider' || c.widget === 'stepped')
      return (c.labels && Object.keys(c.labels).length) ? jsx(Stepped, { ...args, key: c.__cid }) : jsx(Slider, { ...args, key: c.__cid })
    if (c.widget === 'toggle') return jsx(Toggle, { ...args, key: c.__cid })
    if (c.widget === 'momentary') return jsx(Momentary, { ...args, key: c.__cid })
    if (c.widget === 'label') return jsx(LabelWidget, { ctrl: c, color, part: null, key: c.__cid })
    return jsx(Knob, { ...args, key: c.__cid })
  }
  const gops = { sel: {}, drag: null, colorMode: false, csel: {}, sid: '' }
  const connected = busSt && busSt.connected
  const drafting = busDraft && busDraft.pid === doc.profile_id
  return jsxs('div', { 'data-deck': '1', // deck surfaces are NOT drop targets for the active profile's drags — without this marker a knob released over a deck card lands in the deck doc's section (and vanishes)
    style: { marginTop: 18, border: '1px solid var(--ui-stroke-secondary)', borderRadius: 10, padding: '8px 12px 12px' },
    children: [
      jsxs('div', { style: { display: 'flex', gap: 8, alignItems: 'center', marginBottom: 8, flexWrap: 'wrap' }, children: [
        jsx('span', { 'data-tip': connected ? `bus "${bus}" connected` : `bus "${bus}" not connected — make this surface active, pick the port, Connect`, style: { width: 8, height: 8, borderRadius: '50%', background: connected ? '#3fb950' : 'var(--ui-stroke-secondary)', boxShadow: connected ? '0 0 6px #3fb950' : 'none' } }),
        jsx('span', { style: { fontWeight: 700, fontSize: 13 }, children: doc.device || doc.profile_id }),
        jsx('span', { style: { fontSize: 10, color: 'var(--ui-text-secondary)' }, children: `ch ${chan}${connected ? '' : ' · not connected'}` }),  // regression review debt sweep: 0-based like every other surface/tooltip
        jsx('select', { value: bus, onChange: (e) => onBus(e.target.value), 'data-tip': 'Which MIDI bus this surface rides. Each bus owns ONE exclusive device pair — give each physical device its own bus (two profiles may share a bus for two layouts of one device)', style: { ...tinyS, padding: '3px 6px', background: 'var(--ui-bg-secondary)', color: 'var(--ui-text-primary)' },
          children: [jsx('option', { value: 'primary', children: 'bus: primary' }, 'primary')]
            .concat(busIds.filter(b => b !== 'primary').map(b => jsx('option', { value: b, children: 'bus: ' + b }, b)))
            .concat([jsx('option', { value: '__new', children: '+ new bus…' }, '__new')]) }),
        drafting ? jsxs('span', { style: { display: 'inline-flex', gap: 4, alignItems: 'center' }, children: [
          jsx('input', { autoFocus: true, placeholder: 'bus slug (a-z0-9-)', value: busDraft.val, onChange: (e) => setBusDraft({ pid: doc.profile_id, val: e.target.value }), onKeyDown: (e) => { if (e.key === 'Enter') { const v = busDraft.val.trim(); if (/^[a-z0-9][a-z0-9_-]{0,63}$/.test(v)) { setBusDraft(null); onBus(v) } else setBusDraft({ pid: doc.profile_id, val: busDraft.val + ' ' }) } else if (e.key === 'Escape') setBusDraft(null) }, style: { ...inpS, width: 130 } }),
          jsx('button', { onClick: () => { const v = busDraft.val.trim(); if (/^[a-z0-9][a-z0-9_-]{0,63}$/.test(v)) { setBusDraft(null); onBus(v) } else if (setStatus) setStatus('bus slug: a-z 0-9 or - _ (start letter/digit, max 64)') }, style: tinyS, children: 'ok' }),  // regression review debt sweep: an invalid slug used to do NOTHING visibly
        ] }) : null,
        jsx('button', { onClick: onActivate, 'data-tip': 'Make this the active surface — editing, matrix, record and learn follow it', style: tinyS, children: 'make active' }),
        jsx('button', { onClick: onRemove, 'data-tip': 'Remove this device from the screen (does not disconnect its ports)', style: tinyS, children: '×' }),
      ] }),
      ...sections.map(([sid, sec]) => jsxs('div', { style: { marginBottom: 10 }, children: [
        jsx('div', { style: { fontSize: sec.font_size || 11, fontWeight: sec.bold ? 700 : 600, textTransform: 'uppercase', letterSpacing: '0.12em', color: sec.color ? neon(sec.color) : 'var(--ui-text-secondary)', marginBottom: 4 }, children: sec.label || sid }, 'sh-' + sid),
        renderGrid(controls.filter(c => c.section === sid), renderCtrl, false, { ...gops, sid }),
      ] }, 'sec-' + sid)),
      renderGrid(controls.filter(c => !c.section), renderCtrl, false, { ...gops, sid: '' }),
    ] })
}

export default {
  id: 'z-midi-console',
  name: "Z's MIDI Console",
  register(ctx) {
    api = ctx
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: PAGE }, render: () => jsx(Surface, {}) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: PAGE, label: 'MIDI Console', codicon: 'dashboard' } },
      { id: 'open', area: PALETTE_AREA, data: { id: 'z-midi-console.open', label: 'Open MIDI Console', run: () => host.navigate(PAGE) } },
    ])
  },
}
