// Browser tests, run by tests/test_ui.py against a live server on a fixture library.
// Usage: node ui_tests.js <base_url>
// Search endpoints are intercepted with page.route so results/latency are deterministic
// and no external API is contacted.
const { chromium } = require('playwright');
const BASE = process.argv[2];
const sleep = ms => new Promise(r => setTimeout(r, ms));
let failures = 0;
function check(cond, msg) { if (!cond) { failures++; console.log('FAIL: ' + msg); } else console.log('ok: ' + msg); }

function result(tag, i, source = 'iTunes') {
  // Inline SVG thumbnails so <img> never errors (makeCell hides cells whose image fails)
  const svg = 'data:image/svg+xml,' + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>');
  return { url: `https://example.invalid/${encodeURIComponent(tag)}/${source}/${i}.jpg`, thumb: svg,
           label: `${tag} result ${i}`, source, quality: '1000px' };
}

// delays: {key -> ms}; key is the artist name (artist search) or "fast:<album>" / "slow:<album>"
async function mockSearch(page, delays) {
  await page.route('**/api/search/**', async route => {
    const u = new URL(route.request().url());
    let key, tag, body;
    if (u.pathname.endsWith('/artist')) {
      tag = u.searchParams.get('name'); key = tag;
      body = { results: [1, 2, 3].map(i => result(tag, i, 'Deezer')), page: 0,
               sources: [{ key: 'deezer', label: 'Deezer', status: 'ok', count: 3 }] };
    } else {
      const phase = u.pathname.endsWith('/fast') ? 'fast' : 'slow';
      tag = u.searchParams.get('album'); key = `${phase}:${tag}`;
      body = phase === 'fast'
        ? { results: [1, 2].map(i => result(tag, i)), sources: [{ key: 'itunes', label: 'iTunes', status: 'ok', count: 2 }] }
        : { results: [1].map(i => result(tag, i, 'CAA')), sources: [{ key: 'caa', label: 'CAA', status: 'ok', count: 1 },
                                                                  { key: 'web', label: 'Web', status: 'unavailable', count: 0, message: 'rate limited' }] };
    }
    await sleep(delays[key] ?? 0);
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  });
}

// Record every label that ever appears in a grid, to catch a stale flash, not just the final state.
async function watchGrid(page, gridId) {
  await page.evaluate(id => {
    window.__seen = new Set();
    const grid = document.getElementById(id);
    const rec = () => grid.querySelectorAll('.img-cell img').forEach(i => window.__seen.add(i.alt));
    new MutationObserver(rec).observe(grid, { childList: true, subtree: true });
  }, gridId);
}
const seen = page => page.evaluate(() => [...window.__seen]);
const gridLabels = (page, id) => page.$$eval(`#${id} .img-cell img`, els => els.map(e => e.alt));

(async () => {
  const browser = await chromium.launch();
  const errors = [];
  const newPage = async () => {
    const p = await browser.newPage();
    p.on('pageerror', e => errors.push(e.message));
    return p;
  };

  // ── 1. Artist A → artist B: A's late response is dropped ────────────────
  {
    const page = await newPage();
    await mockSearch(page, { 'Alpha': 1500, '!!!': 100 });
    await page.goto(BASE + '/' + encodeURIComponent('Alpha') + '/');
    await page.waitForSelector('#step-artist:not(.hidden)');
    await page.waitForTimeout(200);
    await page.selectOption('#jump-to', { label: '!!! (0/1)' });
    await page.waitForSelector('#step-artist:not(.hidden)');
    check((await page.textContent('#sa-name')) === '!!!', 'artist step switched to !!!');
    await watchGrid(page, 'sa-grid');
    await page.waitForTimeout(2200);   // well past Alpha's 1500ms response
    const labels = await gridLabels(page, 'sa-grid');
    check(labels.length === 3 && labels.every(l => l.startsWith('!!!')), `artist grid only has !!! results: ${JSON.stringify(labels)}`);
    check((await seen(page)).every(l => l.startsWith('!!!')), 'artist grid never showed Alpha results');
    await page.close();
  }

  // ── 2. Album → album, fast phase stale ──────────────────────────────────
  {
    const page = await newPage();
    await mockSearch(page, { 'fast:First': 1500, 'slow:First': 1500, 'fast:Second': 50, 'slow:Second': 50 });
    await page.goto(BASE + '/Alpha/First/');
    await page.waitForSelector('#step-album:not(.hidden)');
    await page.waitForTimeout(200);
    await page.click('#alb-skip-btn');
    await page.waitForFunction(() => document.getElementById('alb-name').textContent === 'Second');
    await watchGrid(page, 'alb-grid');
    await page.waitForTimeout(3500);
    const labels = await gridLabels(page, 'alb-grid');
    check(labels.length === 3 && labels.every(l => l.startsWith('Second')), `album grid only has Second results: ${JSON.stringify(labels)}`);
    check((await seen(page)).every(l => l.startsWith('Second')), 'album grid never showed First results (fast phase)');
    await page.close();
  }

  // ── 3. Two-phase: First's fast phase landed, its slow (CAA) phase is late ─
  {
    const page = await newPage();
    await mockSearch(page, { 'fast:First': 50, 'slow:First': 2500, 'fast:Second': 50, 'slow:Second': 50 });
    await page.goto(BASE + '/Alpha/First/');
    await page.waitForFunction(() => document.querySelectorAll('#alb-grid .img-cell').length === 2);
    await page.click('#alb-skip-btn');
    await page.waitForFunction(() => document.getElementById('alb-name').textContent === 'Second');
    await watchGrid(page, 'alb-grid');
    await page.waitForTimeout(3200);
    const labels = await gridLabels(page, 'alb-grid');
    check(labels.length === 3 && labels.every(l => l.startsWith('Second')), `two-phase: only Second results: ${JSON.stringify(labels)}`);
    check((await seen(page)).every(l => l.startsWith('Second')), 'two-phase: First CAA result never shown');
    const chips = await page.textContent('#alb-sources');
    check(chips.includes('iTunes (2)') && chips.includes('CAA (1)') && chips.includes('Web (unavailable)'),
          `source chips show per-source status: ${JSON.stringify(chips)}`);
    const status = await page.textContent('#alb-search-status');
    check(status === '3 results', `status line belongs to Second: ${JSON.stringify(status)}`);
    await page.close();
  }

  // ── 4. "Load more" keeps the current generation (results are appended) ─
  {
    const page = await newPage();
    let n = 0;
    await page.route('**/api/search/artist**', route => {
      const pg = +new URL(route.request().url()).searchParams.get('page');
      const res = Array.from({ length: 10 }, (_, i) => result('Alpha', pg * 10 + i, 'Deezer'));
      n++;
      route.fulfill({ contentType: 'application/json', body: JSON.stringify({ results: res, page: pg,
        sources: [{ key: 'deezer', label: 'Deezer', status: 'ok', count: 10 }] }) });
    });
    await page.goto(BASE + '/Alpha/');
    await page.waitForSelector('#sa-load-more:not(.hidden)');
    await page.click('#sa-load-more');
    await page.waitForFunction(() => document.getElementById('sa-search-status').textContent === '20 results');
    check(true, 'load more appends to the current search (20 results)');
    await page.close();
  }

  // ── 5. Wizard ends on the artist list after the last item ───────────────
  {
    const page = await newPage();
    await mockSearch(page, {});
    await page.goto(BASE + '/');
    await page.waitForSelector('#v-artists:not(.hidden)');
    await page.click('#wiz-start-btn');
    let steps = 0;
    while (steps < 50) {
      const onList = await page.isVisible('#v-artists');
      if (onList) break;
      const pos = await page.textContent('#wiz-pos');
      if (await page.isVisible('#step-artist')) await page.click('#sa-skip-btn');
      else await page.click('#alb-skip-btn');
      steps++;
      if (steps === 1) console.log('  wizard first position: ' + pos);
    }
    check(await page.isVisible('#v-artists'), `wizard returned to the artist list after ${steps} steps`);
    check(new URL(page.url()).pathname === '/', 'URL reset to / at the end of the wizard');
    await page.close();
  }

  // ── 6a. Embedding on the current album shows the result banner ───────────
  {
    const page = await newPage();
    await mockSearch(page, {});
    // Missing report says which folders still lack cover.jpg
    await page.goto(BASE + '/');
    await page.waitForSelector('#v-artists:not(.hidden)');
    await page.click('text=Missing Report');
    const report = await page.textContent('#missing-body');
    check(report.includes('100% Orange Juice — Multi  (missing in: Album folder, CD 2)'),
          'missing report lists the folders without cover.jpg');
    check(report.includes('Alpha — Second'), 'embedded-only album is listed as missing');
    await page.goto(BASE + '/' + encodeURIComponent('100% Orange Juice') + '/Multi/');
    await page.waitForSelector('#step-album:not(.hidden)');
    const rows = async () => page.$$eval('#alb-disc-covers .disc-info-row', rs => rs.map(r => r.textContent));
    const before = await rows();
    check(JSON.stringify(before) === JSON.stringify(['✗Album folder', '✓CD 1', '✗CD 2']),
          `per-folder cover.jpg status before: ${JSON.stringify(before)}`);
    await page.setInputFiles('#alb-upload-input', process.argv[3]);
    await page.waitForSelector('#alb-done:not(.hidden)', { timeout: 10000 }).catch(() => {});
    const txt = await page.textContent('#alb-done-txt');
    check(await page.isVisible('#alb-done') && txt.includes('Embedded into all 2 tracks'),
          `result banner shown after embedding: ${JSON.stringify(txt)}`);
    check(await page.textContent('#alb-skip-btn') === 'Done →', 'Skip button turns into Done →');
    const after = await rows();
    check(after.length === 3 && after.every(r => r.startsWith('✓')), `every folder has cover.jpg after embedding: ${JSON.stringify(after)}`);
    await page.close();
  }

  // ── 6. Navigating away during an embed: completion lands on the right album ─
  {
    const page = await newPage();
    await mockSearch(page, {});
    // Hold the progress stream back so the embed is still "running" when we navigate.
    await page.route('**/api/embed/**', async route => { await sleep(1500); route.continue(); });
    page.on('dialog', d => d.dismiss());
    await page.goto(BASE + '/Alpha/Second/');
    await page.waitForSelector('#alb-use-emb-btn:not(.hidden)');
    await page.click('#alb-use-emb-btn');
    await page.waitForTimeout(300);
    // "Jump to artist" works mid-embed (Next/Skip are deliberately blocked while it runs).
    await page.selectOption('#jump-to', { label: '!!! (0/1)' });
    await page.waitForTimeout(2500);   // embed finishes while the !!! artist step is shown
    await page.click('#sa-skip-btn');  // → !!! / Thr!!!er (no cover); must not be blocked any more
    await page.waitForFunction(() => document.getElementById('alb-name').textContent === 'Thr!!!er',
                               null, { timeout: 5000 });
    const st = await page.evaluate(() => {
      const al = (a, b) => lib.artists.find(x => x.name === a).albums.find(x => x.name === b);
      return { second: al('Alpha', 'Second').has_cover, thriller: al('!!!', 'Thr!!!er').has_cover,
               doneShown: !document.getElementById('alb-done').classList.contains('hidden') };
    });
    check(st.second === true, 'embedded album (Alpha/Second) marked as having a cover');
    check(st.thriller === false, 'album on screen (!!!/Thr!!!er) NOT marked as covered');
    check(!st.doneShown, '"Embedded" banner not shown on the unrelated album');
    await page.close();
  }

  // ── 7. Setup screen: Change library… → Browse… → Save & scan ────────────
  {
    const page = await newPage();
    await mockSearch(page, {});
    await page.goto(BASE + '/');
    await page.waitForSelector('#v-artists:not(.hidden)');
    const root = (await (await page.request.get(BASE + '/api/config')).json()).library_root;
    const parent = root.replace(/[\\/]Music$/, '');
    await page.click('#hdr button:has-text("Settings")');
    await page.click('text=Change library…');
    check(await page.inputValue('#su-path') === root, 'setup screen is prefilled with the current library');
    await page.fill('#su-path', parent);
    await page.click('#su-local button:has-text("Browse")');
    await page.waitForSelector('.fb-item:has-text("Music")');
    await page.click('.fb-item:has-text("Music")');
    await page.waitForSelector('.fb-item:has-text("Alpha")');
    await page.click('#fb-use');
    check(await page.inputValue('#su-path') === root, 'folder browser fills in the chosen folder');
    await page.click('text=Save & scan library');
    await page.waitForSelector('#v-artists:not(.hidden)', { timeout: 10000 });
    const n = await page.$$eval('#artist-list-body tr', r => r.length);
    check(n === 3, `library scanned after setup (${n} artists)`);
    await page.close();
  }

  check(errors.length === 0, 'no page errors ' + JSON.stringify(errors));
  await browser.close();
  console.log(failures ? `${failures} FAILED` : 'ALL PASSED');
  process.exit(failures ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });
