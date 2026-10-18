'use strict';
/* 特定技能1号(電気電子機器組立て)本地刷题 —— 原生 JS,零依赖。
   规矩:题目/解释等一切文本一律用 textContent 渲染(防 XSS);不引任何外部资源。 */

// ================================================================ 基础工具

const LETTERS = ['A', 'B', 'C', 'D'];
const KIND_LABEL = { ox: '学科 真偽(○×)', choice: '実技 4択' };
const ORIGIN_LABEL = { official: '官方样本', textbook: '教材练习', ai: 'AI 生成' };
const OX_LABEL = { A: 'A 正しい', B: 'B 間違い' };

function $(sel) { return document.querySelector(sel); }

function el(tag, opts, children) {
  const node = document.createElement(tag);
  opts = opts || {};
  if (opts.text !== undefined && opts.text !== null) node.textContent = String(opts.text);
  if (opts.cls) node.className = opts.cls;
  if (opts.attrs) {
    Object.keys(opts.attrs).forEach(function (k) {
      if (opts.attrs[k] !== null && opts.attrs[k] !== undefined) node.setAttribute(k, String(opts.attrs[k]));
    });
  }
  (children || []).forEach(function (c) { if (c) node.appendChild(c); });
  return node;
}

function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

function chapterLabel(ch) { return ch ? ch : '(未分類)'; }
function originLabel(o) { return ORIGIN_LABEL[o] || o || '(未知)'; }
function pct(n, d) { return d ? Math.round(100 * n / d) : 0; }
function fmtTime(ms) {
  const s = Math.max(0, Math.ceil(ms / 1000));
  const m = Math.floor(s / 60);
  return String(m).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0');
}

// ================================================================ 状态 / 请求

const state = {
  meta: null,
  tab: 'practice',
  filters: { kind: 'both', chapter: '', origin: '', limit: 20 },
  loading: false,
  wrongItems: [],
  exam: null,          // {session, questions, answers, startedAt, remainMs, timerId, submitted, result, dom, navCells}
  unloadHandler: null,
};
let activeCard = null;   // 当前可见的题卡(键盘用)

let bannerTimer = null;
function showBanner(msg, kind) {
  const b = $('#banner');
  b.textContent = msg;
  b.className = 'banner ' + (kind === 'ok' ? 'ok' : 'err');
  if (bannerTimer) clearTimeout(bannerTimer);
  bannerTimer = setTimeout(function () { b.className = 'banner hidden'; }, kind === 'ok' ? 4000 : 9000);
}

async function api(path, opts) {
  let res;
  try {
    res = await fetch(path, opts);
  } catch (e) {
    showBanner('连不上本地服务(服务是不是关了?): ' + e.message, 'err');
    throw e;
  }
  let data = null;
  const text = await res.text();
  if (text) { try { data = JSON.parse(text); } catch (e) { data = null; } }
  if (!res.ok) {
    const msg = (data && data.error) ? data.error : (res.status + ' ' + res.statusText);
    showBanner('请求失败: ' + msg, 'err');
    const err = new Error(msg);
    err.status = res.status;
    throw err;
  }
  return data;
}

function postJSON(path, body) {
  return api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

function setUnloadGuard(on) {
  if (on && !state.unloadHandler) {
    state.unloadHandler = function (e) {
      e.preventDefault();
      e.returnValue = '模拟考进行中,离开会丢失进度,确定吗?';
      return e.returnValue;
    };
    window.addEventListener('beforeunload', state.unloadHandler);
  } else if (!on && state.unloadHandler) {
    window.removeEventListener('beforeunload', state.unloadHandler);
    state.unloadHandler = null;
  }
}

// ================================================================ 题卡(练习 / 错题本共用)

class PracticeCard {
  constructor(item, opts) {
    this.item = item;
    this.opts = opts || {};
    this.chosen = null;
    this.result = null;
    this.busy = false;
    this.startedAt = Date.now();
    this.buttons = {};
  }

  render() {
    const it = this.item;
    const card = el('div', { cls: 'card' });
    const head = el('div', { cls: 'cardhead' });
    head.appendChild(el('span', { cls: 'badge kind-' + it.kind, text: KIND_LABEL[it.kind] || it.kind }));
    if (it.chapter) head.appendChild(el('span', { cls: 'tag', text: chapterLabel(it.chapter) }));
    if (it.ref) head.appendChild(el('span', { cls: 'tag', text: it.ref }));
    if (it.pages) head.appendChild(el('span', { cls: 'tag pages', text: '教材 p.' + it.pages }));
    card.appendChild(head);
    card.appendChild(el('div', { cls: 'stem', text: it.stem }));

    const box = el('div', { cls: 'choices ' + (it.kind === 'ox' ? 'ox' : 'choice') });
    const self = this;
    if (it.kind === 'ox') {
      ['A', 'B'].forEach(function (letter) {
        const btn = el('button', { cls: 'opt', attrs: { type: 'button' } },
          [el('span', { cls: 'ctext', text: OX_LABEL[letter] })]);
        btn.addEventListener('click', function () { self.choose(letter); });
        self.buttons[letter] = btn;
        box.appendChild(btn);
      });
    } else {
      (it.choices || []).forEach(function (text, i) {
        const letter = LETTERS[i];
        if (!letter) return;
        const btn = el('button', { cls: 'opt', attrs: { type: 'button' } }, [
          el('span', { cls: 'letter', text: letter }),
          el('span', { cls: 'ctext', text: text }),
        ]);
        btn.addEventListener('click', function () { self.choose(letter); });
        self.buttons[letter] = btn;
        box.appendChild(btn);
      });
    }
    card.appendChild(box);
    this.feedback = el('div', { cls: 'feedback hidden' });
    card.appendChild(this.feedback);
    this.card = card;
    return card;
  }

  handleKey(e) {
    if (e.key === 'Enter' || e.key === ' ') {
      if (this.chosen) {
        e.preventDefault();
        if (this.opts.onNext) this.opts.onNext();
      }
      return;
    }
    if (this.chosen || this.busy) return;
    const k = String(e.key).toUpperCase();
    if (this.item.kind === 'ox') {
      if (k === 'A' || e.key === '1') this.choose('A');
      else if (k === 'B' || e.key === '2') this.choose('B');
    } else {
      if (LETTERS.indexOf(k) >= 0) this.choose(k);
      else if (['1', '2', '3', '4'].indexOf(e.key) >= 0) this.choose(LETTERS[Number(e.key) - 1]);
    }
  }

  async choose(letter) {
    if (this.chosen || this.busy) return;
    this.busy = true;
    const elapsed = Date.now() - this.startedAt;
    try {
      const res = await postJSON('/api/answer',
        { question_id: this.item.id, chosen: letter, elapsed_ms: elapsed });
      this.chosen = letter;
      this.result = res;
      this.reveal();
      if (this.opts.onAnswered) {
        const note = await this.opts.onAnswered(this.item, res);
        if (note) this.setNote(note);
      }
    } catch (e) {
      /* 顶部条已经提示过了 */
    } finally {
      this.busy = false;
    }
  }

  reveal() {
    const res = this.result;
    const self = this;
    Object.keys(this.buttons).forEach(function (letter) {
      const btn = self.buttons[letter];
      btn.disabled = true;
      if (letter === res.answer) btn.classList.add('correct');
      else if (letter === self.chosen) btn.classList.add('wrong');
    });

    clear(this.feedback);
    this.feedback.classList.remove('hidden');
    this.feedback.appendChild(el('div', {
      cls: 'verdict ' + (res.correct ? 'ok' : 'ng'),
      text: res.correct ? '✓ 答对了' : '✗ 答错了(正确答案 ' + res.answer + ')',
    }));

    const expl = el('div', { cls: 'expl' });
    expl.appendChild(el('div', { cls: 'expl-label', text: '中文解释' }));
    expl.appendChild(el('div', { text: res.explanation_zh || '(暂无中文解释)' }));
    this.feedback.appendChild(expl);

    if (res.explanation_ja) {
      const d = el('details');
      d.appendChild(el('summary', { text: '教材解説(日语原解说)' }));
      d.appendChild(el('div', { cls: 'ja', text: res.explanation_ja }));
      this.feedback.appendChild(d);
    }

    this.noteSlot = el('div');
    this.feedback.appendChild(this.noteSlot);

    const row = el('div', { cls: 'nextrow' });
    const nextBtn = el('button', { cls: 'btn primary', attrs: { type: 'button' }, text: this.opts.nextLabel || '下一题 →' });
    nextBtn.addEventListener('click', function () { if (self.opts.onNext) self.opts.onNext(); });
    row.appendChild(nextBtn);
    row.appendChild(el('span', { cls: 'kbdhint', text: '键盘:Enter = 下一题 · A/B/1-4 = 选答案' }));
    this.feedback.appendChild(row);

    // 答完把「判定 + 解释」滑进视野(仅当它在屏幕外时);「下一题」按钮底部常驻,不用找
    requestAnimationFrame(function () {
      const rect = self.feedback.getBoundingClientRect();
      if (rect.top > window.innerHeight - 120) {
        window.scrollTo({ top: window.scrollY + rect.top - 96, behavior: 'smooth' });
      }
    });
  }

  setNote(text) {
    if (!this.noteSlot) return;
    this.noteSlot.textContent = text || '';
    this.noteSlot.className = text ? 'note' : '';
  }
}

class PracticeSession {
  constructor(stage, items, opts) {
    this.stage = stage;
    this.items = items || [];
    this.opts = opts || {};
    this.idx = 0;
    this.nAnswered = 0;
    this.nCorrect = 0;
  }

  start() { this.show(); }

  show() {
    clear(this.stage);
    if (this.idx >= this.items.length) { this.showDone(); return; }
    const item = this.items[this.idx];
    const line = el('div', { cls: 'progressline' });
    line.appendChild(el('span', { cls: 'p', text: '第 ' + (this.idx + 1) + '/' + this.items.length + ' 题' }));
    if (this.nAnswered) {
      line.appendChild(el('span', { cls: 'muted', text: '已答 ' + this.nAnswered + ' · 对 ' + this.nCorrect }));
    }
    this.stage.appendChild(line);

    const self = this;
    const card = new PracticeCard(item, {
      nextLabel: this.idx + 1 >= this.items.length ? '完成 ✓' : '下一题 →',
      onNext: function () { self.next(); },
      onAnswered: async function (it, res) {
        self.nAnswered += 1;
        if (res.correct) self.nCorrect += 1;
        if (self.opts.onAnswered) return self.opts.onAnswered(it, res, self);
        return '';
      },
    });
    activeCard = card;
    const node = card.render();
    this.stage.appendChild(node);
    node.scrollIntoView({ block: 'start' });   // 题卡贴顶(顶栏之下),跳过筛选面板,不重复上下翻
  }

  next() { this.idx += 1; this.show(); }

  showDone() {
    activeCard = null;
    clear(this.stage);
    const panel = el('div', { cls: 'panel' });
    panel.appendChild(el('h2', { text: '本轮完成' }));
    if (this.items.length) {
      panel.appendChild(el('div', {
        text: '共 ' + this.items.length + ' 题 · 答对 ' + this.nCorrect + ' 题 · 正确率 '
          + pct(this.nCorrect, this.items.length) + '%',
      }));
    }
    const row = el('div', { cls: 'row' });
    const self = this;
    if (this.opts.onRestart) {
      const b = el('button', { cls: 'btn primary', attrs: { type: 'button' }, text: this.opts.restartLabel || '再来一批' });
      b.addEventListener('click', function () { self.opts.onRestart(); });
      row.appendChild(b);
    }
    if (this.opts.onExit) {
      const b = el('button', { cls: 'btn', attrs: { type: 'button' }, text: this.opts.exitLabel || '返回' });
      b.addEventListener('click', function () { self.opts.onExit(); });
      row.appendChild(b);
    }
    if (row.childNodes.length) panel.appendChild(row);
    this.stage.appendChild(panel);
    panel.scrollIntoView({ block: 'start' });
    if (this.opts.onDone) this.opts.onDone();
  }
}

// ================================================================ 练习

function selectField(label, options, value, onChange) {
  const sel = el('select');
  options.forEach(function (pair) {
    const opt = el('option', { text: pair[1], attrs: { value: pair[0] } });
    if (pair[0] === value) opt.selected = true;
    sel.appendChild(opt);
  });
  sel.addEventListener('change', function () { onChange(sel.value); });
  return el('label', { cls: 'field' }, [el('span', { text: label }), sel]);
}

function renderPractice(view) {
  clear(view);
  const f = state.filters;
  const meta = state.meta;

  const panel = el('div', { cls: 'panel' });
  panel.appendChild(el('h2', { text: '练习' }));
  const row = el('div', { cls: 'row' });

  row.appendChild(selectField('题型',
    [['both', '混合'], ['ox', '○× 学科'], ['choice', '4択 実技']],
    f.kind, function (v) { f.kind = v; }));

  const chapOpts = [['', '全部章节']];
  if (meta) {
    meta.chapters.forEach(function (c) {
      chapOpts.push([c.chapter, chapterLabel(c.chapter) + ' (' + (c.ox + c.choice) + ')']);
    });
  }
  row.appendChild(selectField('章节', chapOpts, f.chapter, function (v) { f.chapter = v; }));

  const originOpts = [['', '全部来源']];
  if (meta) {
    Object.keys(meta.origins).forEach(function (o) {
      originOpts.push([o, originLabel(o) + ' (' + meta.origins[o] + ')']);
    });
  }
  row.appendChild(selectField('来源', originOpts, f.origin, function (v) { f.origin = v; }));

  row.appendChild(selectField('题量',
    [['10', '10 题'], ['20', '20 题'], ['30', '30 题'], ['50', '50 题']],
    String(f.limit), function (v) { f.limit = Number(v); }));

  const startBtn = el('button', { cls: 'btn primary', attrs: { type: 'button' }, text: '开始 / 换一批' });
  row.appendChild(startBtn);
  panel.appendChild(row);
  panel.appendChild(el('p', { cls: 'muted', text: '随机出题;答完立即给对错和中文解释。' }));
  view.appendChild(panel);

  const stage = el('div');
  view.appendChild(stage);
  startBtn.addEventListener('click', function () { startPractice(stage, startBtn); });
  clear(stage);
  stage.appendChild(el('div', { cls: 'empty', text: '选好筛选条件,点「开始 / 换一批」' }));
}

async function startPractice(stage, btn) {
  if (state.loading) return;
  state.loading = true;
  btn.disabled = true;
  const f = state.filters;
  const qs = new URLSearchParams({
    kind: f.kind, chapter: f.chapter, origin: f.origin,
    order: 'random', limit: String(f.limit),
  });
  try {
    const data = await api('/api/questions?' + qs.toString());
    clear(stage);
    if (!data.items.length) {
      activeCard = null;
      stage.appendChild(el('div', { cls: 'empty', text: '这个筛选条件下没有可用题,换个条件试试' }));
      return;
    }
    if (data.items.length < f.limit) {
      showBanner('该筛选下只有 ' + data.items.length + ' 题(共 ' + data.total + ' 题),已全部取来', 'ok');
    }
    const sess = new PracticeSession(stage, data.items, {
      restartLabel: '换一批',
      onRestart: function () { startPractice(stage, btn); },
    });
    sess.start();
  } catch (e) {
    /* 顶部条已经提示过了 */
  } finally {
    state.loading = false;
    btn.disabled = false;
  }
}

// ================================================================ 错题本

async function renderWrong(view) {
  clear(view);
  view.appendChild(el('div', { cls: 'muted', text: '加载中…' }));
  let data;
  try {
    data = await api('/api/wrong');
  } catch (e) {
    clear(view);
    view.appendChild(el('div', { cls: 'empty', text: '加载失败,请看上方提示' }));
    return;
  }
  state.wrongItems = data.items;

  clear(view);
  const panel = el('div', { cls: 'panel' });
  panel.appendChild(el('h2', { text: '错题本' }));
  const info = el('div', { cls: 'muted', text: '共 ' + data.total + ' 题 · 连续答对 2 次自动移出' });
  panel.appendChild(info);
  const listBox = el('div');
  const stage = el('div');
  const btnRow = el('div', { cls: 'row' });

  function drawList(items) {
    clear(listBox);
    items.forEach(function (it) {
      const row = el('div', { cls: 'wrongitem' });
      const left = el('div', { cls: 'wstem', attrs: { title: it.stem }, text: it.stem });
      const right = el('div', { cls: 'wmeta' });
      right.appendChild(el('span', { cls: 'badge kind-' + it.kind, text: it.kind === 'ox' ? '○×' : '4択' }));
      right.appendChild(el('span', { text: ' 错 ' + it.wrong_count + ' 次 · 连对 ' + it.streak + '/2' }));
      row.appendChild(left);
      row.appendChild(right);
      listBox.appendChild(row);
    });
  }

  if (!data.total) {
    panel.appendChild(el('div', { cls: 'empty', text: '太强了,错题本是空的' }));
    panel.appendChild(el('p', { cls: 'muted', text: '去「练习」做几题吧;答错过的题会出现在这里。' }));
  } else {
    const reviewBtn = el('button', { cls: 'btn primary', attrs: { type: 'button' }, text: '开始复习' });
    btnRow.appendChild(reviewBtn);
    panel.appendChild(btnRow);
    drawList(data.items);
    reviewBtn.addEventListener('click', function () {
      reviewBtn.disabled = true;
      clear(stage);
      const items = state.wrongItems.slice();
      const sess = new PracticeSession(stage, items, {
        restartLabel: '再复习一遍',
        onRestart: function () { renderWrong(view); },
        exitLabel: '返回错题本',
        onExit: function () { renderWrong(view); },
        onAnswered: async function (item) {
          const fresh = await api('/api/wrong');
          state.wrongItems = fresh.items;
          info.textContent = '共 ' + fresh.total + ' 题 · 连续答对 2 次自动移出';
          drawList(fresh.items);
          const still = fresh.items.filter(function (x) { return x.id === item.id; })[0];
          if (!still) return '已移出错题本 🎉';
          return '该题还要再连对 ' + (2 - still.streak) + ' 次才移出(当前连对 ' + still.streak + '/2)';
        },
        onDone: function () { reviewBtn.disabled = false; },
      });
      sess.start();
    });
  }
  panel.appendChild(listBox);
  view.appendChild(panel);
  view.appendChild(stage);
}

// ================================================================ 模拟考

function renderExam(view) {
  const ex = state.exam;
  if (ex && ex.result) { renderExamResult(view); return; }
  if (ex && ex.paper) { renderExamPaper(view); return; }
  renderExamIntro(view);
}

function renderExamIntro(view) {
  clear(view);
  const panel = el('div', { cls: 'panel' });
  panel.appendChild(el('h2', { text: '模拟考' }));
  panel.appendChild(el('div', { text: '学科(○×)30 题 + 実技(4択)10 题,80 分钟,交卷后自动判分。' }));
  panel.appendChild(el('ul', {}, [
    el('li', { text: '学科 30 题:答对 20 题以上合格(○× 真偽)' }),
    el('li', { text: '実技 10 题:答对 6 题以上合格(4択)' }),
    el('li', { text: '两科都过 = 综合合格;未作答按错算' }),
  ]));
  const meta = state.meta;
  if (meta) {
    panel.appendChild(el('div', {
      cls: 'muted',
      text: '当前题库可用:○× ' + meta.kinds.ox + ' 题 · 4択 ' + meta.kinds.choice + ' 题',
    }));
    if (meta.kinds.choice < 10) {
      panel.appendChild(el('div', {
        cls: 'exam-warn',
        text: '実技題不足,本次只有 ' + meta.kinds.choice + ' 题(真题 10 题,池子里还不够)',
      }));
    }
  }
  const btn = el('button', { cls: 'btn primary', attrs: { type: 'button' }, text: '开始模拟考' });
  const row = el('div', { cls: 'row' }, [btn]);
  panel.appendChild(row);
  view.appendChild(panel);

  btn.addEventListener('click', function () { startExam(view, btn); });
}

async function startExam(view, btn) {
  btn.disabled = true;
  try {
    const data = await postJSON('/api/exam/start', {});
    state.exam = {
      session: data,
      minutes: data.minutes || 80,
      questions: data.ox.concat(data.choice),
      answers: {},
      startedAt: Date.now(),
      remainMs: (data.minutes || 80) * 60000,
      timerId: null,
      submitted: false,
      paper: true,
      result: null,
      dom: {},
      navCells: {},
    };
    setUnloadGuard(true);
    startExamTimer();
    renderExamPaper(view);
  } catch (e) {
    btn.disabled = false;
  }
}

function startExamTimer() {
  const ex = state.exam;
  if (!ex) return;
  if (ex.timerId) clearInterval(ex.timerId);
  const tick = function () {
    ex.remainMs = ex.minutes * 60000 - (Date.now() - ex.startedAt);
    const t = document.getElementById('examTimer');
    if (t) {
      t.textContent = fmtTime(ex.remainMs);
      t.classList.toggle('low', ex.remainMs <= 5 * 60000);
    }
    if (ex.remainMs <= 0 && !ex.submitted) submitExam(true);
  };
  tick();
  ex.timerId = setInterval(tick, 1000);
}

function examChoices(q) {
  if (q.kind === 'ox') return [['A', OX_LABEL.A], ['B', OX_LABEL.B]];
  return (q.choices || []).map(function (t, i) { return [LETTERS[i], t]; }).filter(function (p) { return !!p[0]; });
}

function renderExamPaper(view) {
  clear(view);
  const ex = state.exam;
  const nOx = ex.session.ox.length;
  const nTotal = ex.questions.length;

  // 顶部:倒计时 + 状态 + 交卷
  const head = el('div', { cls: 'exam-head' });
  const left = el('div');
  left.appendChild(el('div', { cls: 'timer', attrs: { id: 'examTimer' }, text: fmtTime(ex.remainMs) }));
  left.appendChild(el('div', { cls: 'muted', text: '剩余时间(到 0 自动交卷)' }));
  const right = el('div', { cls: 'row' });
  ex.statusEl = el('span', { cls: 'muted' });
  const submitBtn = el('button', { cls: 'btn primary', attrs: { type: 'button' }, text: '交卷' });
  submitBtn.addEventListener('click', function () { submitExam(false); });
  right.appendChild(ex.statusEl);
  right.appendChild(submitBtn);
  head.appendChild(left);
  head.appendChild(right);
  view.appendChild(head);

  if (ex.session.kinds.choice < 10) {
    view.appendChild(el('div', {
      cls: 'exam-warn',
      text: '実技題不足,本次只有 ' + ex.session.kinds.choice + ' 题(合格线仍按 6 题算)',
    }));
  }

  // 题号导航
  const navPanel = el('div', { cls: 'panel' });
  navPanel.appendChild(el('h2', { text: '题号导航' }));
  const nav = el('div', { cls: 'navgrid' });
  ex.questions.forEach(function (q, i) {
    const b = el('button', { attrs: { type: 'button' }, text: String(i + 1) });
    if (ex.answers[q.id]) b.classList.add('done');
    b.addEventListener('click', function () {
      const node = document.getElementById('examq-' + q.id);
      if (node) node.scrollIntoView({ block: 'center' });
    });
    ex.navCells[q.id] = b;
    nav.appendChild(b);
  });
  navPanel.appendChild(nav);
  navPanel.appendChild(el('div', { cls: 'muted', text: '已答绿色 / 未答灰色;点击题号跳到该题' }));
  view.appendChild(navPanel);

  // 学科区
  const oxPanel = el('div', { cls: 'panel' });
  oxPanel.appendChild(el('h2', { text: '学科 真偽(○×)' + (nOx ? ' · 第 1 - ' + nOx + ' 题' : ' · 无题') }));
  ex.session.ox.forEach(function (q, i) { oxPanel.appendChild(examQuestionBlock(ex, q, i + 1)); });
  view.appendChild(oxPanel);

  // 実技区
  const chPanel = el('div', { cls: 'panel' });
  chPanel.appendChild(el('h2', {
    text: '実技 4択' + (ex.session.choice.length ? ' · 第 ' + (nOx + 1) + ' - ' + nTotal + ' 题' : ' · 无题'),
  }));
  ex.session.choice.forEach(function (q, i) { chPanel.appendChild(examQuestionBlock(ex, q, nOx + i + 1)); });
  view.appendChild(chPanel);

  const foot = el('div', { cls: 'row' });
  const submit2 = el('button', { cls: 'btn primary', attrs: { type: 'button' }, text: '交卷并看结果' });
  submit2.addEventListener('click', function () { submitExam(false); });
  foot.appendChild(submit2);
  view.appendChild(foot);

  updateExamStatus();
}

function examQuestionBlock(ex, q, no) {
  const box = el('div', { cls: 'card exam-q', attrs: { id: 'examq-' + q.id } });
  const head = el('div', { cls: 'cardhead' });
  head.appendChild(el('span', { cls: 'badge kind-' + q.kind, text: KIND_LABEL[q.kind] || q.kind }));
  head.appendChild(el('span', { cls: 'tag', text: '第 ' + no + ' 题' }));
  if (q.chapter) head.appendChild(el('span', { cls: 'tag', text: chapterLabel(q.chapter) }));
  if (q.pages) head.appendChild(el('span', { cls: 'tag pages', text: '教材 p.' + q.pages }));
  box.appendChild(head);
  box.appendChild(el('div', { cls: 'stem', text: q.stem }));

  const chBox = el('div', { cls: 'choices ' + (q.kind === 'ox' ? 'ox' : 'choice') });
  const btns = {};
  examChoices(q).forEach(function (pair) {
    const letter = pair[0];
    const kids = q.kind === 'ox'
      ? [el('span', { cls: 'ctext', text: pair[1] })]
      : [el('span', { cls: 'letter', text: letter }), el('span', { cls: 'ctext', text: pair[1] })];
    const btn = el('button', { cls: 'opt', attrs: { type: 'button' } }, kids);
    btn.addEventListener('click', function () { pickExamAnswer(ex, q, letter); });
    if (ex.answers[q.id] === letter) btn.classList.add('picked');
    btns[letter] = btn;
    chBox.appendChild(btn);
  });
  box.appendChild(chBox);
  if (ex.answers[q.id]) box.classList.add('answered');
  ex.dom[q.id] = { btns: btns, box: box };
  return box;
}

function pickExamAnswer(ex, q, letter) {
  if (ex.submitted) return;
  ex.answers[q.id] = letter;
  const d = ex.dom[q.id];
  if (d) {
    Object.keys(d.btns).forEach(function (l) { d.btns[l].classList.toggle('picked', l === letter); });
    d.box.classList.add('answered');
  }
  const cell = ex.navCells[q.id];
  if (cell) cell.classList.add('done');
  updateExamStatus();
}

function updateExamStatus() {
  const ex = state.exam;
  if (!ex || !ex.statusEl) return;
  const n = Object.keys(ex.answers).length;
  ex.statusEl.textContent = '已答 ' + n + '/' + ex.questions.length;
}

async function submitExam(auto) {
  const ex = state.exam;
  if (!ex || ex.submitted) return;
  const unanswered = ex.questions.length - Object.keys(ex.answers).length;
  if (!auto && unanswered > 0) {
    if (!window.confirm('还有 ' + unanswered + ' 题没作答(未作答按错算),确定交卷吗?')) return;
  }
  ex.submitted = true;
  if (ex.timerId) { clearInterval(ex.timerId); ex.timerId = null; }
  setUnloadGuard(false);
  const elapsed = Date.now() - ex.startedAt;
  try {
    const res = await postJSON('/api/exam/submit', {
      session_id: ex.session.session_id,
      answers: ex.answers,
      elapsed_ms: elapsed,
    });
    ex.result = res;
    ex.paper = false;
    showBanner(auto ? '时间到,已自动交卷' : '已交卷', 'ok');
  } catch (e) {
    ex.submitted = false;
    setUnloadGuard(true);
    startExamTimer();
    return;
  }
  if (state.tab === 'exam') renderExam($('#view'));
}

function renderExamResult(view) {
  clear(view);
  const ex = state.exam;
  const r = ex.result;
  const totalCorrect = r.ox.correct + r.choice.correct;
  const totalAll = r.ox.total + r.choice.total;

  const head = el('div', { cls: 'panel result-head' });
  head.appendChild(el('div', { cls: 'big ' + (r.passed ? 'pass' : 'fail'), text: r.passed ? '合格 🎉' : '不合格' }));
  head.appendChild(el('div', { cls: 'muted', text: '学科 ' + r.ox.correct + '/' + r.ox.total + '(≥' + r.ox.threshold + ') · 実技 '
    + r.choice.correct + '/' + r.choice.total + '(≥' + r.choice.threshold + ') · 两科都过才合格' }));
  head.appendChild(el('div', { cls: 'muted', text: '总正确率 ' + pct(totalCorrect, totalAll) + '%(' + totalCorrect + '/' + totalAll + ')' }));
  view.appendChild(head);

  const grid = el('div', { cls: 'statgrid' });
  [['学科 真偽', r.ox], ['実技 4択', r.choice]].forEach(function (pair) {
    const s = pair[1];
    const st = el('div', { cls: 'stat' });
    st.appendChild(el('div', { cls: 'k', text: pair[0] }));
    st.appendChild(el('div', { cls: 'v', text: s.correct + '/' + s.total }));
    st.appendChild(el('span', { cls: 'pill ' + (s.passed ? 'pass' : 'fail'), text: s.passed ? '合格' : '不合格' }));
    st.appendChild(el('div', { cls: 'k', text: '合格线 ' + s.threshold + ' · 正确率 ' + pct(s.correct, s.total) + '%' }));
    grid.appendChild(st);
  });
  view.appendChild(grid);

  const btnRow = el('div', { cls: 'row' });
  const again = el('button', { cls: 'btn primary', attrs: { type: 'button' }, text: '再来一场' });
  again.addEventListener('click', function () {
    state.exam = null;
    renderExamIntro(view);
  });
  const toStats = el('button', { cls: 'btn', attrs: { type: 'button' }, text: '看统计' });
  toStats.addEventListener('click', function () { setTab('stats'); });
  btnRow.appendChild(again);
  btnRow.appendChild(toStats);
  view.appendChild(btnRow);

  const qmap = {};
  ex.questions.forEach(function (q) { qmap[q.id] = q; });

  const panel = el('div', { cls: 'panel' });
  panel.appendChild(el('h2', { text: '逐题回顾' }));
  (r.details || []).forEach(function (d, i) {
    const q = qmap[d.question_id] || { kind: d.kind, choices: [] };
    const box = el('div', { cls: 'review-item ' + (d.correct ? 'correct' : 'wrong') });
    const head2 = el('div', { cls: 'cardhead' });
    head2.appendChild(el('span', { cls: 'badge kind-' + d.kind, text: KIND_LABEL[d.kind] || d.kind }));
    head2.appendChild(el('span', { cls: 'tag', text: '第 ' + (i + 1) + ' 题' }));
    head2.appendChild(el('span', { cls: 'tag', text: d.correct ? '✓ 答对' : '✗ 答错' }));
    if (d.chapter) head2.appendChild(el('span', { cls: 'tag', text: chapterLabel(d.chapter) }));
    if (d.pages) head2.appendChild(el('span', { cls: 'tag pages', text: '教材 p.' + d.pages }));
    box.appendChild(head2);
    box.appendChild(el('div', { cls: 'stem', text: d.stem }));
    const your = el('div');
    your.appendChild(el('span', { cls: 'expl-label', text: '你的选择 ' }));
    your.appendChild(el('span', {
      text: d.chosen ? (d.chosen + '. ' + choiceText(q, d.chosen)) : '(未作答)',
    }));
    const rightAns = el('div');
    rightAns.appendChild(el('span', { cls: 'expl-label', text: '正确答案 ' }));
    rightAns.appendChild(el('span', { text: d.answer + '. ' + choiceText(q, d.answer) }));
    box.appendChild(your);
    box.appendChild(rightAns);
    box.appendChild(el('div', { cls: 'expl' }, [el('div', { text: d.explanation_zh || '(暂无中文解释)' })]));
    if (d.explanation_ja) {
      const det = el('details');
      det.appendChild(el('summary', { text: '教材解説(日语原解说)' }));
      det.appendChild(el('div', { cls: 'ja', text: d.explanation_ja }));
      box.appendChild(det);
    }
    panel.appendChild(box);
  });
  view.appendChild(panel);
}

function choiceText(q, letter) {
  if (!letter) return '';
  if (q && q.kind === 'ox') return OX_LABEL[letter] || '';
  const idx = LETTERS.indexOf(letter);
  if (q && q.choices && q.choices[idx] !== undefined) return q.choices[idx];
  return '';
}

// ================================================================ 统计

async function renderStats(view) {
  clear(view);
  view.appendChild(el('div', { cls: 'muted', text: '加载中…' }));
  let s;
  try {
    s = await api('/api/stats');
  } catch (e) {
    clear(view);
    view.appendChild(el('div', { cls: 'empty', text: '加载失败,请看上方提示' }));
    return;
  }
  clear(view);

  const grid = el('div', { cls: 'statgrid' });
  grid.appendChild(el('div', { cls: 'stat' }, [
    el('div', { cls: 'k', text: '可用题数' }),
    el('div', { cls: 'v', text: s.usable_total }),
    el('div', { cls: 'k', text: '○× ' + s.by_kind.ox.n + ' 题池 / 4択 ' + s.by_kind.choice.n + ' 题池' }),
  ]));
  grid.appendChild(el('div', { cls: 'stat' }, [
    el('div', { cls: 'k', text: '累计答题' }),
    el('div', { cls: 'v', text: s.attempts.n }),
    el('div', { cls: 'k', text: '正确率 ' + (s.attempts.rate === null || s.attempts.rate === undefined ? '—' : s.attempts.rate + '%') }),
  ]));
  grid.appendChild(el('div', { cls: 'stat' }, [
    el('div', { cls: 'k', text: '错题本' }),
    el('div', { cls: 'v', text: s.wrong_book_size }),
    el('div', { cls: 'k', text: '连续答对 2 次移出' }),
  ]));
  view.appendChild(grid);

  const kindPanel = el('div', { cls: 'panel' });
  kindPanel.appendChild(el('h2', { text: '分题型正确率' }));
  const t1 = el('table');
  t1.appendChild(el('thead', {}, [el('tr', {}, [
    el('th', { text: '题型' }), el('th', { cls: 'num', text: '答题数' }),
    el('th', { cls: 'num', text: '答对' }), el('th', { cls: 'num', text: '正确率' }),
  ])]));
  const tb1 = el('tbody');
  [['ox', '学科 真偽(○×)'], ['choice', '実技 4択']].forEach(function (pair) {
    const d = s.by_kind[pair[0]] || { n: 0, correct: 0 };
    tb1.appendChild(el('tr', {}, [
      el('td', { text: pair[1] }),
      el('td', { cls: 'num', text: d.n }),
      el('td', { cls: 'num', text: d.correct }),
      el('td', { cls: 'num', text: d.n ? pct(d.correct, d.n) + '%' : '—' }),
    ]));
  });
  t1.appendChild(tb1);
  kindPanel.appendChild(t1);
  view.appendChild(kindPanel);

  const sessPanel = el('div', { cls: 'panel' });
  sessPanel.appendChild(el('h2', { text: '最近考试(最近 10 场)' }));
  if (!s.sessions.length) {
    sessPanel.appendChild(el('div', { cls: 'empty', text: '还没有考试记录' }));
  } else {
    const t2 = el('table');
    t2.appendChild(el('thead', {}, [el('tr', {}, [
      el('th', { text: '开始时间' }), el('th', { text: '结束时间' }),
      el('th', { cls: 'num', text: '得分' }), el('th', { text: '分科' }), el('th', { text: '结果' }),
    ])]));
    const tb2 = el('tbody');
    s.sessions.forEach(function (x) {
      let kindTxt = '—';
      if (x.detail && x.detail.kinds) {
        const k = x.detail.kinds;
        kindTxt = '学科 ' + k.ox.correct + '/' + k.ox.total + ' · 実技 ' + k.choice.correct + '/' + k.choice.total;
      }
      const pill = x.passed === null
        ? el('span', { cls: 'pill na', text: '未交卷' })
        : el('span', { cls: 'pill ' + (x.passed ? 'pass' : 'fail'), text: x.passed ? '合格' : '不合格' });
      tb2.appendChild(el('tr', {}, [
        el('td', { text: x.started_at || '—' }),
        el('td', { text: x.finished_at || '—' }),
        el('td', { cls: 'num', text: x.score === null || x.score === undefined ? '—' : x.score + '%' }),
        el('td', { text: kindTxt }),
        el('td', {}, [pill]),
      ]));
    });
    t2.appendChild(tb2);
    sessPanel.appendChild(t2);
  }
  view.appendChild(sessPanel);
}

// ================================================================ 路由 / 启动

const TABS = { practice: renderPractice, wrong: renderWrong, exam: renderExam, stats: renderStats };

function setTab(name) {
  if (!TABS[name]) return;
  state.tab = name;
  activeCard = null;
  document.querySelectorAll('#tabs button').forEach(function (b) {
    b.classList.toggle('active', b.dataset.tab === name);
  });
  const view = $('#view');
  TABS[name](view);
}

function onKeydown(e) {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const tag = (e.target && e.target.tagName) || '';
  if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
  if (state.tab !== 'practice' && state.tab !== 'wrong') return;
  if (!activeCard) return;
  activeCard.handleKey(e);
}

async function boot() {
  document.querySelectorAll('#tabs button').forEach(function (b) {
    b.addEventListener('click', function () { setTab(b.dataset.tab); });
  });
  document.addEventListener('keydown', onKeydown);
  try {
    state.meta = await api('/api/meta');
    $('.foot').textContent = '本地题库 · 可用 ' + state.meta.usable_total + ' 题(○× '
      + state.meta.kinds.ox + ' / 4択 ' + state.meta.kinds.choice + ') · 数据只在 127.0.0.1 · 不联网';
  } catch (e) {
    /* 顶部条已经提示过了 */
  }
  setTab('practice');
}

document.addEventListener('DOMContentLoaded', boot);
