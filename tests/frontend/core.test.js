// Unit tests for static/core.js (the dashboard's data logic). Node 18+, no dependencies.
// Run from the repo root:  node --test tests/frontend/
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const PS = require(path.join(__dirname, '..', '..', 'static', 'core.js'));

// Default zone is India (UTC+5:30): the half-hour offset catches UTC-vs-local hour bugs.
process.env.TZ = 'Asia/Kolkata';
const withTZ = (tz, fn) => () => {
  const prev = process.env.TZ;
  process.env.TZ = tz;
  try { fn(); } finally { process.env.TZ = prev; }
};
// Local wall-clock time in the current TZ -> unix seconds.
const at = (y, m, d, h = 0, mi = 0, s = 0) => new Date(y, m - 1, d, h, mi, s).getTime() / 1000;
const H = 3600, D = 86400;

test('dur formats compact durations', () => {
  const cases = [[0, '0s'], [-5, '0s'], [59, '59s'], [59.6, '1m'], [60, '1m'], [3599, '59m'], [3600, '1h'],
    [3660, '1h 1m'], [86399, '23h 59m'], [86400, '1d'], [90000, '1d 1h'], [10 * D + 5 * H, '10d 5h']];
  for (const [s, want] of cases) assert.equal(PS.dur(s), want, String(s));
});

test('pct rounds sensibly and never shows 100% for real downtime', () => {
  assert.equal(PS.pct(100), '100%');
  assert.equal(PS.pct(99.996), '100%');
  assert.equal(PS.pct(99.99), '99.99%');
  assert.equal(PS.pct(99.95), '99.95%');
  assert.equal(PS.pct(97.23), '97.2%');
  assert.equal(PS.pct(0), '0.0%');
});

test('overlap and end handle ongoing outages', () => {
  const now = 1000;
  assert.equal(PS.end([100, 200], now), 200);
  assert.equal(PS.end([100, null], now), now);
  assert.equal(PS.overlap([100, 200], 0, 1000, now), 100);   // fully inside
  assert.equal(PS.overlap([100, 200], 150, 1000, now), 50);  // clipped at window start
  assert.equal(PS.overlap([100, 200], 0, 120, now), 20);     // clipped at window end
  assert.equal(PS.overlap([100, 200], 300, 400, now), 0);    // outside
  assert.equal(PS.overlap([900, null], 0, 2000, now), 100);  // ongoing runs until now
});

test('dataStart is the earlier of tracking start and the oldest outage', () => {
  assert.equal(PS.dataStart([], 500), 500);
  assert.equal(PS.dataStart([[900, 950], [300, 400]], 500), 300); // inferred outage may predate tracking
});

test('midnight, addDays and isoDay use the local calendar', () => {
  const t = at(2026, 10, 20, 13, 45);
  assert.equal(PS.midnight(t), at(2026, 10, 20));
  assert.equal(PS.addDays(at(2026, 10, 31), 1), at(2026, 11, 1));
  assert.equal(PS.isoDay(at(2026, 1, 5, 23, 59)), '2026-01-05');
});

test('buckets: 15-minute periods up to 6 hours, aligned to the local clock', () => {
  const to = at(2026, 10, 21, 10, 57), from = to - 3 * H;   // the 3h default view
  const {unit, hourly, list} = PS.buckets(from, to);
  assert.equal(unit, 'quarter');
  assert.equal(hourly, true);
  assert.deepEqual(list[0], [at(2026, 10, 21, 7, 45), at(2026, 10, 21, 8)]);
  assert.deepEqual(list.at(-1), [at(2026, 10, 21, 10, 45), at(2026, 10, 21, 11)]);
  assert.equal(list.length, 13);
  for (const [a, b] of list) assert.equal(b - a, 15 * 60);
  assert.equal(PS.buckets(to - 6 * H, to).unit, 'quarter');
  assert.equal(PS.buckets(to - 6 * H - 1, to).unit, 'hour');
});

test('buckets: hourly up to 2 days, aligned to local (not UTC) hours', () => {
  const to = at(2026, 10, 20, 13, 45), from = to - D;
  const {unit, hourly, list} = PS.buckets(from, to);
  assert.equal(unit, 'hour');
  assert.equal(hourly, true);
  assert.equal(list.length, 25);
  for (const [a, b] of list) {
    assert.equal(new Date(a * 1000).getMinutes(), 0, 'bucket starts on a local hour in UTC+5:30');
    assert.equal(b - a, H);
  }
  assert.ok(list[0][0] <= from && list.at(-1)[1] >= to);
});

test('buckets: daily beyond 2 days, contiguous local days covering the window', () => {
  const to = at(2026, 10, 20, 9), from = to - 7 * D;
  const {unit, hourly, list} = PS.buckets(from, to);
  assert.equal(unit, 'day');
  assert.equal(hourly, false);
  assert.equal(list.length, 8);
  assert.equal(list[0][0], at(2026, 10, 13));
  for (let i = 1; i < list.length; i++) assert.equal(list[i][0], list[i - 1][1]);
  assert.ok(list.at(-1)[1] > to);
});

test('buckets: daylight-saving days are 23 and 25 hours long', withTZ('America/New_York', () => {
  const spring = PS.buckets(at(2026, 3, 6), at(2026, 3, 10)).list.find(([a]) => PS.isoDay(a) === '2026-03-08');
  const autumn = PS.buckets(at(2026, 10, 30), at(2026, 11, 3)).list.find(([a]) => PS.isoDay(a) === '2026-11-01');
  assert.equal(spring[1] - spring[0], 23 * H);
  assert.equal(autumn[1] - autumn[0], 25 * H);
}));

test('stats: counts, downtime, uptime and reliability figures', () => {
  const from = 0, to = 10 * H, now = to;
  const O = [[8 * H, 9 * H, 0], [1 * H, 1.5 * H, 0], [4 * H, 6 * H, 0]];
  const s = PS.stats(O, from, to, 0, now);
  assert.equal(s.count, 3);
  assert.equal(s.down, 3.5 * H);
  assert.equal(s.uptime, 65);
  assert.equal(s.avg, 3.5 * H / 3);
  assert.deepEqual(s.longest, [4 * H, 6 * H, 0]);
  assert.equal(s.longestLen, 2 * H);
  assert.equal(s.longestPower, 2.5 * H);           // 1.5h -> 4h
  assert.equal(s.between, (2.5 * H + 2 * H) / 2);   // gaps between consecutive outages
  assert.equal(s.perDay, 3 / (10 / 24));
});

test('stats: window clips downtime but keeps full outage length; ongoing uses now', () => {
  const now = 10 * H;
  const s = PS.stats([[-2 * H, 1 * H, 0], [9 * H, null, 0]], 0, now, -100 * H, now);
  assert.equal(s.count, 2);
  assert.equal(s.down, 1 * H + 1 * H);         // clipped to the window
  assert.equal(s.longestLen, 3 * H);           // full length of the first outage
  assert.equal(s.longestPower, 8 * H);
});

test('stats: empty window and window before history starts', () => {
  const s = PS.stats([], 0, 10 * H, 5 * H, 10 * H);
  assert.deepEqual([s.count, s.down, s.uptime, s.avg, s.longest, s.between], [0, 0, 100, null, null, null]);
  assert.equal(s.longestPower, 5 * H);          // measured from when history began
  assert.equal(s.perDay, 0);
});

test('stats: outageDays counts distinct local days with a cut', () => {
  const O = [[at(2026, 10, 1, 8), at(2026, 10, 1, 9)], [at(2026, 10, 1, 20), at(2026, 10, 1, 21)], [at(2026, 10, 3, 2), at(2026, 10, 3, 3)]];
  assert.equal(PS.stats(O, at(2026, 9, 30), at(2026, 10, 5), 0, at(2026, 10, 5)).outageDays, 2);
});

test('stepPoints: sorted, spans the window, low inside outages and high outside', () => {
  const O = [[200, 300, 0]], pts = PS.stepPoints(O, 0, 1000, 1000, 10);
  assert.deepEqual(pts[0], [0, 1]);
  assert.deepEqual(pts.at(-1), [1000, 1]);
  for (let i = 1; i < pts.length; i++) assert.ok(pts[i][0] >= pts[i - 1][0]);
  assert.ok(pts.some(p => p[0] === 200 && p[1] === 0));
  assert.ok(pts.some(p => p[0] === 300 && p[1] === 1));
  for (const [t, v] of pts) if (t > 200 && t < 300) assert.equal(v, 0);
});

test('stepPoints: window starting inside an ongoing outage starts low and stays low', () => {
  const pts = PS.stepPoints([[-50, null, 0]], 0, 100, 100, 4);
  assert.ok(pts.every(([, v]) => v === 0));
});

test('bucketRows: counts starts per bucket and clips downtime', () => {
  const list = [[0, 100], [100, 200]], O = [[90, 130, 0], [150, 160, 0]];
  assert.deepEqual(PS.bucketRows(O, list, 200), [{a: 0, b: 100, n: 1, d: 10}, {a: 100, b: 200, n: 1, d: 40}]);
});

test('uptimeRows: a period with no power at all is 0% (e.g. the current hour during an outage)', () => {
  // Live case from the Pi: power out since 09:53; at 10:57 the 10:00 hour is 0%, the 09:00 hour 88.3%.
  const now = at(2026, 10, 21, 10, 57), O = [[at(2026, 10, 21, 9, 53), null, 0]];
  const {list} = PS.buckets(now - 24 * H, now);
  const rows = PS.uptimeRows(O, list, now - 30 * H, now);
  assert.equal(rows.at(-1).u, 0);
  assert.equal(rows.at(-2).u.toFixed(1), '88.3');
});

test('uptimeRows: null outside history, percentage inside', () => {
  const list = [[0, 100], [100, 200], [200, 300]];
  const rows = PS.uptimeRows([[150, 175, 0]], list, 100, 250);
  assert.deepEqual(rows.map(r => r.u), [null, 75, 100]);   // last bucket measured only up to now
});

test('timeline: newest day first, outages split at midnight with local hours', () => {
  const O = [[at(2026, 10, 18, 22), at(2026, 10, 19, 2), 0], [at(2026, 10, 20, 8, 30), null, 1]];
  const now = at(2026, 10, 20, 9), r = PS.timeline(O, at(2026, 10, 18), now, now);
  assert.deepEqual(r.days, [at(2026, 10, 20), at(2026, 10, 19), at(2026, 10, 18)]);
  assert.equal(r.truncated, false);
  const blocks = r.blocks.map(b => b.slice(0, 3));
  assert.deepEqual(blocks, [[2, 22, 24], [1, 0, 2], [0, 8.5, 9]]);
  assert.equal(r.blocks[2][5], 1);  // inferred flag carried through
});

test('timeline: limited to the latest N days', () => {
  const now = at(2026, 10, 20, 12), r = PS.timeline([], now - 200 * D, now, now, 120);
  assert.equal(r.days.length, 120);
  assert.equal(r.truncated, true);
  assert.equal(r.days[0], at(2026, 10, 20));
});

test('timeline: a full outage on a 25-hour DST day spans 0 to 24 on the clock', withTZ('America/New_York', () => {
  const day = at(2026, 11, 1), now = at(2026, 11, 3);
  const r = PS.timeline([[day, PS.addDays(day, 1), 0]], at(2026, 10, 31), now, now);
  const b = r.blocks.find(x => r.days[x[0]] === day);
  assert.deepEqual(b.slice(1, 3), [0, 24]);
}));

test('calendar: whole months and hours down per day since history began', () => {
  const now = at(2026, 10, 20, 12);
  const O = [[at(2026, 10, 19, 23), at(2026, 10, 20, 1), 0]];
  const c = PS.calendar(O, at(2026, 10, 13), now, at(2026, 10, 15), now);
  assert.equal(c.first, at(2026, 10, 1));
  assert.equal(c.last, at(2026, 10, 31));
  assert.equal(c.data[0][0], '2026-10-15');            // no cells before history began
  assert.deepEqual(c.data.at(-1), ['2026-10-20', 1]);
  assert.deepEqual(c.data.at(-2), ['2026-10-19', 1]);
  assert.equal(c.data.length, 6);
});

test('weekHour: credits minutes to the correct local hour (UTC+5:30 regression)', () => {
  // Tue 20 Oct 2026, 19:00–19:30 IST. Bucketing by UTC hours would wrongly put this at 18:30–19:30.
  const g = PS.weekHour([[at(2026, 10, 20, 19), at(2026, 10, 20, 19, 30), 0]], 0, at(2026, 10, 21), at(2026, 10, 21));
  assert.equal(g[1][19], 30);   // Monday = 0, so Tuesday = 1
  assert.equal(g.flat().reduce((a, b) => a + b), 30);
});

test('weekHour: splits across hours and days, clipped to the window', () => {
  // Sun 18 Oct 23:30 -> Mon 19 Oct 01:15
  const O = [[at(2026, 10, 18, 23, 30), at(2026, 10, 19, 1, 15), 0]];
  const g = PS.weekHour(O, 0, at(2026, 10, 19, 1), at(2026, 10, 21));
  assert.equal(g[6][23], 30);   // Sunday
  assert.equal(g[0][0], 60);    // Monday 00:00-01:00
  assert.equal(g[0][1], 0);     // clipped at the window end
});

test('hourCounts: outage starts by local hour within the window', () => {
  const O = [[at(2026, 10, 20, 19, 45)], [at(2026, 10, 19, 19, 5)], [at(2026, 10, 20, 7)], [at(2026, 9, 1, 19)]];
  const c = PS.hourCounts(O, at(2026, 10, 1), at(2026, 10, 21));
  assert.equal(c[19], 2);
  assert.equal(c[7], 1);
  assert.equal(c.reduce((a, b) => a + b), 3);
});

test('durationGroups: boundaries and totals, ongoing measured to now', () => {
  const O = [[0, 299], [0, 300], [0, 1799], [0, 1800], [0, 7199], [0, 7200], [100, null]];
  const g = PS.durationGroups(O, 0, 20000, 20000);
  assert.deepEqual(g.map(r => r.n), [1, 2, 2, 2]);
  assert.deepEqual(g.map(r => r.name), ['Under 5 min', '5–30 min', '30 min – 2 h', 'Over 2 h']);
  assert.equal(g[3].d, 7200 + 19900);
});

test('weekHourCoverage: seconds of each local weekday-hour inside the window', () => {
  const week = PS.weekHourCoverage(at(2026, 10, 19), at(2026, 10, 26));          // Mon 19 Oct .. Mon 26 Oct
  assert.ok(week.flat().every(s => s === H));
  const part = PS.weekHourCoverage(at(2026, 10, 20, 19, 30), at(2026, 10, 20, 21));  // Tue 19:30-21:00 IST
  assert.equal(part[1][19], 30 * 60);
  assert.equal(part[1][20], H);
  assert.equal(part.flat().reduce((a, b) => a + b), 90 * 60);
});

test('weekHourAverage: average minutes per hour, 0-60, null where the window has no data', () => {
  // Two weeks; a 30-min outage in one Tue 19:00 hour -> 15 min per Tue 19:00 hour on average.
  const from = at(2026, 10, 12), to = at(2026, 10, 26);
  const O = [[at(2026, 10, 20, 19), at(2026, 10, 20, 19, 30), 0], [at(2026, 10, 14, 8), at(2026, 10, 14, 10), 0]];
  const avg = PS.weekHourAverage(O, from, to, to);
  assert.equal(avg[1][19], 15);
  assert.equal(avg[2][8], 30);     // Wed 08:00 out in 1 of 2 weeks
  assert.equal(avg[2][9], 30);
  assert.equal(avg[0][0], 0);
  assert.ok(avg.flat().every(m => m >= 0 && m <= 60));
  const short = PS.weekHourAverage(O, at(2026, 10, 20, 19), at(2026, 10, 20, 20), at(2026, 10, 21));
  assert.equal(short[1][19], 30);  // the single covered hour
  assert.equal(short[1][18], null);
});
