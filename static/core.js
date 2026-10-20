// Pure data logic for the dashboard: no DOM, no ECharts, no clock reads.
// Every function that needs "now" takes it as an argument so it can be unit tested.
// Outage rows are [start, end|null, inferred] in unix seconds (end null = ongoing).
// The server inlines this file into index.html; Node tests require() it.
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.PS = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  const HOUR = 3600, DAY = 86400;

  function dur(s) {
    s = Math.max(0, Math.round(s));
    if (s < 60) return s + 's';
    const m = Math.floor(s / 60), h = Math.floor(m / 60), d = Math.floor(h / 24);
    if (h < 1) return m + 'm';
    if (d < 1) return h + 'h' + (m % 60 ? ' ' + (m % 60) + 'm' : '');
    return d + 'd' + (h % 24 ? ' ' + (h % 24) + 'h' : '');
  }
  const pct = u => (u >= 99.995 ? '100' : u.toFixed(u >= 99.9 ? 2 : 1)) + '%';

  // Local-time calendar helpers (DST-safe: they step calendar days, not 86400 s).
  const midnight = ts => { const d = new Date(ts * 1000); d.setHours(0, 0, 0, 0); return d.getTime() / 1000; };
  const addDays = (ts, n) => { const d = new Date(ts * 1000); d.setDate(d.getDate() + n); return d.getTime() / 1000; };
  const isoDay = ts => {
    const d = new Date(ts * 1000);
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
  };

  const end = (o, now) => o[1] ?? now;
  const overlap = (o, a, b, now) => Math.max(0, Math.min(end(o, now), b) - Math.max(o[0], a));
  const dataStart = (outages, trackingSince) => outages.reduce((m, o) => Math.min(m, o[0]), trackingSince);
  const inWindow = (outages, from, to, now) => outages.filter(o => overlap(o, from, to, now) > 0);
  const downtime = (outages, a, b, now) => outages.reduce((s, o) => s + overlap(o, a, b, now), 0);

  // Hourly buckets for windows up to 2 days, local calendar days otherwise.
  // Hours are aligned to the local clock, not UTC (matters in e.g. UTC+5:30).
  function buckets(from, to) {
    const list = [];
    if (to - from <= 2 * DAY) {
      const d = new Date(from * 1000); d.setMinutes(0, 0, 0);
      for (let a = d.getTime() / 1000; a < to; a += HOUR) list.push([a, a + HOUR]);
      return {hourly: true, list};
    }
    for (let a = midnight(from); a < to; ) { const b = addDays(a, 1); list.push([a, b]); a = b; }
    return {hourly: false, list};
  }

  // Reliability numbers for the stat tiles. `start` is when history begins.
  function stats(outages, from, to, start, now) {
    const os = inWindow(outages, from, to, now).sort((a, b) => a[0] - b[0]);
    const lo = Math.max(from, start), span = Math.max(1, to - lo);
    const down = downtime(os, from, to, now);
    const lens = os.map(o => end(o, now) - o[0]);
    let longest = null;
    for (const o of os) if (!longest || end(o, now) - o[0] > end(longest, now) - longest[0]) longest = o;
    // Stretches with power: gaps between outages inside the window, plus both edges.
    const gaps = [], between = [];
    let cur = lo;
    for (const o of os) { if (o[0] > cur) gaps.push(o[0] - cur); cur = Math.max(cur, end(o, now)); }
    if (to > cur) gaps.push(to - cur);
    for (let i = 1; i < os.length; i++) between.push(Math.max(0, os[i][0] - end(os[i - 1], now)));
    const avg = a => a.length ? a.reduce((s, x) => s + x, 0) / a.length : null;
    return {
      count: os.length,
      perDay: os.length / (span / DAY),
      down,
      uptime: 100 * (1 - down / span),
      avg: avg(lens),
      longest,
      longestLen: longest ? end(longest, now) - longest[0] : null,
      longestPower: gaps.length ? Math.max(...gaps) : null,
      between: avg(between),
      outageDays: new Set(os.map(o => isoDay(o[0]))).size,
    };
  }

  // Step-chart points: transitions plus evenly spaced samples (so a tooltip works anywhere).
  function stepPoints(outages, from, to, now, samples = 300) {
    const os = inWindow(outages, from, to, now);
    const stateAt = t => os.some(o => o[0] <= t && t < end(o, now)) ? 0 : 1;
    const pts = [[from, stateAt(from)]];
    for (const o of os) {
      if (o[0] > from) pts.push([o[0], 0]);
      if (end(o, now) < to) pts.push([end(o, now), 1]);
    }
    for (let i = 1; i < samples; i++) { const t = from + (to - from) * i / samples; pts.push([t, stateAt(t)]); }
    pts.push([to, stateAt(to - 1)]);
    return pts.sort((a, b) => a[0] - b[0]);
  }

  // Per bucket: outages started (n) and seconds without power (d).
  const bucketRows = (outages, list, now) => list.map(([a, b]) => ({
    a, b, n: outages.filter(o => o[0] >= a && o[0] < b).length, d: downtime(outages, a, b, now),
  }));

  // Per bucket uptime %, measured only over time since history began; null if no data.
  const uptimeRows = (outages, list, start, now) => list.map(([a, b]) => {
    const lo = Math.max(a, start), hi = Math.min(b, now);
    return {a, u: hi <= lo ? null : 100 * (1 - downtime(outages, lo, hi, now) / (hi - lo))};
  });

  // Timeline rows: newest day first (at most `max`), outages split at midnight.
  // Each block: [dayIndex, startHour, endHour, outageStart, outageEnd, inferred].
  function timeline(outages, from, to, now, max = 120) {
    const days = [];
    for (let a = midnight(to); a >= midnight(from) && days.length < max; a = addDays(a, -1)) days.push(a);
    const idx = new Map(days.map((d, i) => [d, i])), blocks = [];
    for (const o of outages) {
      for (let a = midnight(o[0]); a < end(o, now); a = addDays(a, 1)) {
        if (!idx.has(a)) continue;
        const b = addDays(a, 1), s = Math.max(o[0], a), e = Math.min(end(o, now), b);
        // Hours from local midnight are measured on the wall clock, so DST days still span 0–24.
        if (e > s) blocks.push([idx.get(a), wallHours(a, s), wallHours(a, e) || 24, o[0], o[1], o[2]]);
      }
    }
    return {days, blocks, truncated: days.length === max};
  }
  const wallHours = (dayStart, ts) => {
    if (ts >= addDays(dayStart, 1)) return 24;
    const d = new Date(ts * 1000);
    return d.getHours() + d.getMinutes() / 60 + d.getSeconds() / 3600;
  };

  // Calendar: whole months covering the window (at most about a year) and hours down per day.
  function calendar(outages, from, to, start, now) {
    const sd = new Date(Math.max(from, addDays(midnight(now), -365)) * 1000);
    const first = new Date(sd.getFullYear(), sd.getMonth(), 1).getTime() / 1000;
    const ed = new Date(to * 1000), last = new Date(ed.getFullYear(), ed.getMonth() + 1, 0).getTime() / 1000;
    const data = [];
    for (let a = Math.max(first, midnight(start)); a <= now; a = addDays(a, 1)) {
      data.push([isoDay(a), downtime(outages, a, addDays(a, 1), now) / HOUR]);
    }
    return {first, last, data};
  }

  // Minutes without power per [weekday][hour], Monday = 0, in local time.
  function weekHour(outages, from, to, now) {
    const grid = [...Array(7)].map(() => Array(24).fill(0));
    for (const o of inWindow(outages, from, to, now)) {
      const s = Math.max(o[0], from), e = Math.min(end(o, now), to);
      // Walk local clock hours so half-hour-offset time zones are bucketed correctly.
      let a = s;
      while (a < e) {
        const d = new Date(a * 1000), next = new Date(d); next.setMinutes(60, 0, 0);
        const b = Math.min(e, next.getTime() / 1000);
        grid[(d.getDay() + 6) % 7][d.getHours()] += (b - a) / 60;
        a = b;
      }
    }
    return grid;
  }

  // Outages starting in each local hour of the day.
  function hourCounts(outages, from, to) {
    const counts = Array(24).fill(0);
    for (const o of outages) if (o[0] >= from && o[0] < to) counts[new Date(o[0] * 1000).getHours()]++;
    return counts;
  }

  const DURATION_GROUPS = [['Under 5 min', 0, 300], ['5–30 min', 300, 1800], ['30 min – 2 h', 1800, 7200], ['Over 2 h', 7200, Infinity]];
  function durationGroups(outages, from, to, now) {
    const os = inWindow(outages, from, to, now);
    return DURATION_GROUPS.map(([name, lo, hi]) => {
      const g = os.filter(o => { const d = end(o, now) - o[0]; return d >= lo && d < hi; });
      return {name, n: g.length, d: g.reduce((s, o) => s + end(o, now) - o[0], 0)};
    });
  }

  return {dur, pct, midnight, addDays, isoDay, end, overlap, dataStart, inWindow, downtime, buckets, stats,
          stepPoints, bucketRows, uptimeRows, timeline, calendar, weekHour, hourCounts, durationGroups};
});
