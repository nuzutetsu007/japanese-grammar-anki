"""公共工具:路径、数据库连接、run_log、模型调用(走 Groq API)。

设计说明
--------
本项目通过 **Groq 的 OpenAI 兼容 API** 调用小模型(qwen3.8-27b / gpt-oss-120b)。

  * 不再 shell 出 `pi` CLI 子进程 —— 每条文法点起一个进程开销太大;
    改成 HTTP 直调,200 条文法点几十秒就能跑完。
  * API key 从项目根目录的 `.env` 读(`GROQ_API_KEY=...`),不写进代码。
  * **模型容灾**:一个模型被限流(429)或出错 → 冷却它 → 自动换下一个。
  * 依赖 `httpx`(系统 python3 已装)。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "db" / "grammar.sqlite"
SCHEMA_PATH = ROOT / "src" / "schema.sql"
PROMPTS_DIR = ROOT / "prompts"
INPUT_TOC = ROOT / "input" / "toc" / "toc_ocr.json"
OUT_DIR = ROOT / "data" / "out"
MEDIA_DIR = OUT_DIR / "media"       # 例句朗读 mp3(会被打进 .apkg 的 media_files)


def load_env() -> None:
    """读项目根目录的 .env(已存在的环境变量不覆盖)。不依赖 python-dotenv。"""
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()

# ------------------------------------------------------------------ provider
# 全部走 **OpenAI 兼容** 端点,所以只要一份代码。
# key 从 .env 读(见上面的 load_env)。
PROVIDERS: dict[str, dict] = {
    "groq": {
        "base": os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/"),
        "key_env": "GROQ_API_KEY",
        # 免费层,每模型独立:RPM 30 / TPM 8K / OTPM 1K / RPD 1K / TPD 200K
        "limits": {"rpm": 30, "tpm": 8_000, "otpm": 1_000,
                   "rpd": 1_000, "tpd": 200_000,
                   # ⚠️ Groq 免费层按「声明的 max_tokens」扣 TPM,不是按实际产出。
                   # 声明 2600 就扣 2600 —— 8K TPM 一分钟只够 3 个请求。
                   # 所以大输出必须让 Google 干,Groq 只接小输出(verify 之类)。
                   "charge_declared": True},
    },
    "openrouter": {
        "base": os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        "key_env": "OPENROUTER_API_KEY",
        # 免费模型(:free):账号有余额时 1000 RPD(否则 50 RPD),约 20 RPM。
        # 注意:OpenRouter 的免费额度是**账号级共享**的,所以这里给每个模型
        # 一个偏保守的 rpd(几个模型加起来 ≈1000),避免超额。
        # 免费模型经常被上游挤到 429,容灾会自动换下一个。
        "limits": {"rpm": 20, "tpm": 200_000, "otpm": 0,
                   "rpd": 200, "tpd": 2_000_000,
                   "charge_declared": False},
    },
    "google": {
        "base": "https://generativelanguage.googleapis.com/v1beta/openai",
        "key_env": "GOOGLE_API_KEY",
        # 免费层:每 project 每 model 15 RPM(实测错误里 quotaValue: 15)
        # TPM 很大、**没有 OTPM 限制** → 长输出(6 条例句)用 Google 最稳
        "limits": {"rpm": 15, "tpm": 250_000, "otpm": 0,
                   "rpd": 1000, "tpd": 1_000_000,
                   # Google 按**实际用量**扣,没有声明值惩罚 → 大输出主力
                   "charge_declared": False},
    },
}
DEFAULT_PROVIDER = os.environ.get("GRAMMAR_PROVIDER", "google")

# 模型池:格式 "provider:model,provider:model,...",可用 GRAMMAR_MODELS 覆盖。
# 选择逻辑是「谁剩余配额多就用谁」,所以顺序只影响打平的时候。
DEFAULT_MODELS = os.environ.get(
    "GRAMMAR_MODELS",
    # 大输出主力:Google(250K TPM,按实际用量扣)
    "google:gemini-flash-lite-latest,"
    "google:gemini-3.1-flash-lite,"
    # OpenRouter 免费模型(账号有余额 → 1000 RPD)
    "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free,"
    "openrouter:nvidia/nemotron-3-super-120b-a12b:free,"
    "openrouter:qwen/qwen3.8-27b:free,"
    "openrouter:stealth/space-bunny-alpha,"
    # Groq:只接小输出(按声明 max_tokens 扣 TPM,大输出会饿死)
    "groq:openai/gpt-oss-120b,"
    "groq:qwen/qwen3.8-27b,"
    "groq:openai/gpt-oss-20b",
)
QUOTA_COOLDOWN = int(os.environ.get("GRAMMAR_QUOTA_COOLDOWN", "60"))  # 秒
ERR_COOLDOWN = int(os.environ.get("GRAMMAR_ERR_COOLDOWN", "20"))
DEFAULT_MAX_TOKENS = int(os.environ.get("GRAMMAR_MAX_TOKENS", "4096"))
WAIT_TIMEOUT = int(os.environ.get("GRAMMAR_WAIT_TIMEOUT", "1800"))  # 等配额的上限秒数
COOLDOWN: dict[str, float] = {}   # "provider:model" -> 冷却到期的 unix 时间

# 每日用量账本(落盘,重启不清零):data/out/usage/YYYY-MM-DD.json
USAGE_DIR = OUT_DIR / "usage"
_usage_lock = threading.Lock()

_clients: dict[str, object] = {}
_clients_lock = threading.Lock()

# ---------------------------------------------------------------- 令牌桶
# _window[key] = [[ts, in_tok, out_tok], ...],只统计最近 60 秒。
_window: dict[str, list[list]] = {}
_win_lock = threading.Lock()


def split_spec(spec: str) -> tuple[str, str]:
    """'google:gemini-3.1-flash-lite' → ('google', 'gemini-3.1-flash-lite')。"""
    spec = spec.strip()
    if ":" in spec:
        head, tail = spec.split(":", 1)
        if head in PROVIDERS:
            return head, tail.strip()
    return DEFAULT_PROVIDER, spec


def limits_of(prov: str, model: str) -> dict:
    """某模型的限额。可用 GRAMMAR_{PROVIDER}_{LIMIT} 全局覆盖。"""
    base = dict(PROVIDERS.get(prov, {}).get("limits") or {})
    for k in list(base):
        base[k] = int(os.environ.get(f"GRAMMAR_{prov.upper()}_{k.upper()}", base[k]))
    return base


def _usage_path() -> Path:
    return USAGE_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.json"


def _load_usage() -> dict:
    path = _usage_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _add_usage(key: str, in_tok: int, out_tok: int) -> None:
    with _usage_lock:
        USAGE_DIR.mkdir(parents=True, exist_ok=True)
        data = _load_usage()
        u = data.setdefault(key, {"req": 0, "in": 0, "out": 0})
        u["req"] += 1
        u["in"] += int(in_tok)
        u["out"] += int(out_tok)
        _usage_path().write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def usage_today() -> dict:
    return _load_usage()


def _prune(key: str, t: float) -> list[list]:
    w = _window.setdefault(key, [])
    while w and t - w[0][0] > 60.0:
        w.pop(0)
    return w


def _acquire_slot(keys: list[str], est_in: int, est_out: int, est_declared: int,
                  daily: dict):
    """挑一个还在分钟配额内的模型,并占一个预估额度的位置。

    返回 (key, slot, all_too_large):
      - (key, slot, False) 拿到位置
      - (None, None, False) 暂时没有(冷却中/分钟配额满/今日额度用完)→ 等一会儿会好
      - (None, None, True)  **结构性放不下**:链上每个模型都是因为「输入+声明的输出
        本身就超过它的 TPM/OTPM」而被跳过。这种情况等 1800s 也没用,调用方应当
        直接失败,否则会把整个 loop 卡死在等配额上。
    """
    now_t = time.time()
    best = None
    # 只要有任何一个候选是因为「暂时性」原因被跳过,就说明等等是值得的。
    all_too_large = True
    with _win_lock:
        for key in keys:
            if COOLDOWN.get(key, 0) > now_t:
                all_too_large = False
                continue
            prov, model = split_spec(key)
            lim = limits_of(prov, model)
            w = _prune(key, now_t)
            used_in = sum(s[1] for s in w)
            used_out = sum(s[2] for s in w)
            otpm = lim.get("otpm") or 0
            # Groq 等按「声明的 max_tokens」扣额度:若声明值本身就超过 OTPM,
            # 这个模型的请求必被拒,直接跳过,不要发出去反复重试。
            # Google 等按实际用量扣:用预估产出,并裁到 OTPM 以内。
            if lim.get("charge_declared"):
                need_out = est_declared
                if otpm and est_declared > otpm:
                    continue          # 结构性:声明的输出就比 OTPM 大,永远发不出去
            else:
                need_out = est_out
                if otpm:
                    need_out = min(need_out, otpm)
            if len(w) + 1 > lim.get("rpm", 10 ** 9):
                all_too_large = False
                continue
            tpm = lim.get("tpm", 10 ** 9)
            if used_in + used_out + est_in + need_out > tpm:
                if est_in + need_out > tpm:
                    continue          # 结构性:分钟窗口全空也放不下
                all_too_large = False
                continue
            if otpm and used_out + need_out > otpm:
                all_too_large = False
                continue
            d = daily.get(key)
            if d:
                tpd = lim.get("tpd", 10 ** 9)
                if d["req"] + 1 > lim.get("rpd", 10 ** 9):
                    all_too_large = False
                    continue
                if d["in"] + d["out"] + est_in + need_out > tpd:
                    if est_in + need_out > tpd:
                        continue      # 结构性:今日全空也放不下
                    all_too_large = False
                    continue
            head = (used_in + used_out) / max(tpm, 1)
            if best is None or head < best[0]:
                best = (head, key, w)
        if best is not None:
            slot = [time.time(), est_in, est_out]
            best[2].append(slot)
            return best[1], slot, False
    return None, None, all_too_large


def _estimate_tokens(messages: list[dict], max_tokens: int) -> tuple[int, int]:
    """粗估 (输入 token, 输出 token)。日文大致 1 字 ≈ 1 token,乘 1.15 保守一点。"""
    chars = sum(len(m.get("content") or "") for m in messages)
    return int(chars * 1.15), max(int(max_tokens * 0.65), 64)


# ---------------------------------------------------------------- HTTP

def http_client(prov: str = ""):
    """按 provider 建/复用 httpx.Client(key 不同)。"""
    prov = prov or DEFAULT_PROVIDER
    if prov not in _clients:
        with _clients_lock:
            if prov not in _clients:
                import httpx

                cfg = PROVIDERS.get(prov)
                if not cfg:
                    raise RuntimeError(f"未知 provider: {prov}")
                key = os.environ.get(cfg["key_env"], "")
                if not key:
                    raise RuntimeError(f"{cfg['key_env']} 没设置(放到项目根目录 .env 里)")
                _clients[prov] = httpx.Client(
                    base_url=cfg["base"],
                    headers={"Authorization": f"Bearer {key}"},
                    timeout=httpx.Timeout(240.0, connect=20.0),
                    limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
                )
    return _clients[prov]


CONFIDENCE_LEVELS = ("established", "common_claim", "mnemonic_only", "uncertain")
EXPANSION_FIELDS = (
    "connection",
    "meaning",
    "kanji_writing",
    "etymology",
    "usage_note",
    "contrast",
)


def now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    _migrate(conn)
    conn.commit()


# 老库升级用的补列(新装的库 executeScript 已经建好,这里对已有库幂等补上)
_MIGRATIONS = [
    ("grammar_point", "reading_romaji", "TEXT"),
    ("book_fact", "source_gloss", "TEXT"),
    ("example", "conn", "TEXT"),
    ("example", "audio", "TEXT"),
    ("example", "audio_speaker", "INTEGER"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, col, decl in _MIGRATIONS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if cols and col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            print(f"    [migrate] {table}.{col} 已补上", flush=True)


def log_run(conn, stage, prompt_version, started_at, rows_ok, rows_failed, note="") -> None:
    conn.execute(
        "INSERT INTO run_log (stage, prompt_version, started_at, finished_at, rows_ok, rows_failed, note)"
        " VALUES (?,?,?,?,?,?,?)",
        (stage, prompt_version, started_at, now(), rows_ok, rows_failed, note),
    )
    conn.commit()


def cards_path_for(chapter: str = "") -> Path:
    """按章节选 cards.json 路径(build_cards / export_anki 共用)。

    全量用 cards.json;指定章节用 cards_<chapter>.json,
    避免「只生成一个章节」时覆盖全量产物。"""
    if not chapter:
        return OUT_DIR / "cards.json"
    safe = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", chapter)
    return OUT_DIR / f"cards_{safe}.json"


def read_prompt(version: str) -> str:
    path = PROMPTS_DIR / f"{version}.md"
    if not path.exists():
        raise FileNotFoundError(f"prompt not found: {path}")
    return path.read_text(encoding="utf-8")


class ApiError(RuntimeError):
    """带 HTTP 状态码的模型调用错误,方便区分 429 / 5xx。"""

    def __init__(self, status: int, body: str, retry_after: float | None = None):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body
        self.retry_after = retry_after

    @property
    def is_quota(self) -> bool:
        low = self.body.lower()
        return self.status == 429 or "quota" in low or "rate limit" in low

    @property
    def is_too_large(self) -> bool:
        """Groq 的 OTPM 拒绝:预期输出超过每分钟上限。重试常常就过。"""
        low = self.body.lower()
        return "request too large" in low or "otpm" in low

    @property
    def is_transient(self) -> bool:
        return self.status in (408, 409, 425, 500, 502, 503, 504)


def _parse_retry_after(body: str, header: str | None) -> float | None:
    """从 header 或 body 里挖出建议的重试秒数(Groq / Google 格式都兼容)。"""
    cands: list[float] = []
    if header:
        try:
            cands.append(float(header))
        except ValueError:
            pass
    for pat in (r"retry in ([0-9.]+)s", r'"retryDelay"\s*:\s*"([0-9.]+)s'):
        m = re.search(pat, body)
        if m:
            cands.append(float(m.group(1)))
    return max(cands) if cands else None


def resolve_chain(spec: str = "") -> list[str]:
    """把 'provider:model,provider:model,...' 解析成列表。

    不带 provider 前缀的条目用 DEFAULT_PROVIDER。
    """
    spec = (spec or "").strip() or DEFAULT_MODELS
    chain: list[str] = []
    for part in spec.split(","):
        part = part.strip().lstrip("~")
        if not part:
            continue
        prov, model = split_spec(part)
        chain.append(f"{prov}:{model}")
    seen = set()
    out = [m for m in chain if not (m in seen or seen.add(m))]
    if out:
        return out
    return [f"{split_spec(p)[0]}:{split_spec(p)[1]}" for p in DEFAULT_MODELS.split(",") if p.strip()]


def _post(key: str, messages: list[dict], *, timeout: int, max_tokens: int,
          temperature: float, json_mode: bool) -> tuple[str, int, int]:
    """发一次 chat/completions,返回 (content, 输入 token, 输出 token)。"""
    prov, model = split_spec(key)
    body: dict = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        # Groq 硬性要求:messages 里必须出现 "json" 字样,否则直接 400
        # ("'messages' must contain the word 'json'")。其它 provider 无害,
        # 所以统一补一句,省得 Groq 三个模型每次白打一个 400。
        if "json" not in json.dumps(messages, ensure_ascii=False).lower():
            msgs = list(messages) + [{"role": "user",
                                      "content": "只输出 JSON,不要任何解释或围栏。"}]
        else:
            msgs = messages
        body["messages"] = msgs
        body["response_format"] = {"type": "json_object"}
    r = http_client(prov).post("/chat/completions", json=body, timeout=timeout)
    if r.status_code != 200:
        raise ApiError(r.status_code, r.text,
                       _parse_retry_after(r.text, r.headers.get("retry-after")))
    data = r.json()
    ch = data.get("choices") or []
    if not ch:
        raise RuntimeError(f"空 choices: {json.dumps(data)[:200]}")
    msg = ch[0].get("message") or {}
    content = (msg.get("content") or "").strip()
    if not content:
        # 有些 reasoning 模型偶尔只给 reasoning,退化用 reasoning 里的 JSON
        content = (msg.get("reasoning") or "").strip()
    if not content:
        raise RuntimeError(f"空 content: {json.dumps(data)[:200]}")
    u = data.get("usage") or {}
    return content, int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)


def chat(
    messages: list[dict],
    model: str = "",
    *,
    retries: int = 3,
    timeout: int = 240,
    chain_spec: str = "",
    max_tokens: int = DEFAULT_MAX_TOKENS,
    expect_out: int = 0,
    temperature: float = 0.3,
    json_mode: bool = False,
    **_ignored,
) -> tuple[str, str]:
    """多 provider 模型池 + 配额感知调度。

    * 每个 `provider:model` 单独算 60 秒滑窗的 RPM / TPM / OTPM 配额,
      发请求前先占额度,算完用真实 usage 回填 —— 所以基本不吃 429。
    * 每日用量落盘(`data/out/usage/YYYY-MM-DD.json`),跨进程/重启累计,
      到 RPD / TPD 上限就自动跳过该模型。
    * 并发线程各自挑「剩余配额比例最高」的模型 → 天然负载均衡。
    * 真遇上 429/5xx 就冷却该模型换下一个;OTPM 那种「请求太大」会原地短重试。

    返回 (content 文本, 实际成功的 "provider:model")。
    """
    chain = resolve_chain(chain_spec) if chain_spec else (
        [f"{split_spec(model)[0]}:{split_spec(model)[1]}"] if model.strip() else resolve_chain())
    est_in, est_out = _estimate_tokens(messages, max_tokens)
    if expect_out:
        est_out = expect_out
    errors: list[str] = []
    deadline = time.time() + WAIT_TIMEOUT
    waited_msg = False
    too_large: dict[str, int] = {}

    calls = 0
    max_calls = max(retries, 2) * len(chain) * 2
    while time.time() < deadline and calls < max_calls:
        daily = _load_usage()
        key, slot, all_too_large = _acquire_slot(chain, est_in, est_out, max_tokens, daily)
        if key is None:
            if all_too_large:
                # 等多久都没用(输入+声明输出 > 所有模型的 TPM/OTPM),
                # 直接失败,别让 loop 白等 WAIT_TIMEOUT。
                raise RuntimeError(
                    f"请求对链上所有模型都太大(est_in={est_in} out={max_tokens}),"
                    f"等配额也没用: {', '.join(chain)}")
            if not waited_msg:
                print(f"    [throttle] 配额都在冷却/用满,排队等待(上限 {WAIT_TIMEOUT}s)…",
                      flush=True)
                waited_msg = True
            time.sleep(1.0)
            continue
        calls += 1
        try:
            out, tin, tout = _post(key, messages, timeout=timeout, max_tokens=max_tokens,
                                   temperature=temperature, json_mode=json_mode)
            # Groq 已经按声明值扣过了,不能缩小;Google 按实际用量,回填真实值
            declared = limits_of(*split_spec(key)).get("charge_declared")
            slot[1] = tin or est_in
            slot[2] = max_tokens if declared else (tout or est_out)
            _add_usage(key, tin, tout)
            COOLDOWN.pop(key, None)
            return out, key
        except ApiError as e:
            slot[1] = slot[2] = 0                # 失败不占额度
            errors.append(f"{key}: {e}")
            if e.is_too_large and too_large.get(key, 0) < 4:
                # 「预期输出太大」是估算抖动,原地等一会儿重试经常就过
                too_large[key] = too_large.get(key, 0) + 1
                time.sleep(min(4 + 3 * too_large[key], 15))
                continue
            cool = QUOTA_COOLDOWN if e.is_quota else ERR_COOLDOWN
            if e.retry_after:
                cool = max(cool, e.retry_after)
            COOLDOWN[key] = time.time() + min(cool, 300)
            print(f"    [failover] {key} {e.status} → 冷却 {min(cool, 300):.0f}s,换模型", flush=True)
        except Exception as e:  # noqa: BLE001  (网络超时等)
            slot[1] = slot[2] = 0
            errors.append(f"{key}: {type(e).__name__}: {e}")
            COOLDOWN[key] = time.time() + ERR_COOLDOWN
            print(f"    [failover] {key} {type(e).__name__} → 冷却 {ERR_COOLDOWN}s,换模型", flush=True)

    raise RuntimeError("所有模型都失败/超时: " + " | ".join(errors[-4:]))


def chat_json(messages: list[dict], model: str = "", *, retries: int = 3,
              validate=None, max_tokens: int = DEFAULT_MAX_TOKENS,
              expect_out: int = 0, temperature: float = 0.3, **kw) -> dict:
    """调用模型并要求返回 JSON;解析失败(或 validate 不通过)会自动重试。"""
    last_err: Exception | None = None
    for attempt in range(retries):
        text, used = chat(messages, model, retries=2, json_mode=True,
                          max_tokens=max_tokens, expect_out=expect_out,
                          temperature=temperature, **kw)
        try:
            data = parse_json(text)
            if validate and not validate(data):
                raise ValueError(f"JSON 结构不符合预期: {list(data)[:8]}")
            data["_model"] = used
            return data
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"    [retry {attempt + 1}] JSON 解析失败: {e}", flush=True)
            time.sleep(1)
    raise RuntimeError(f"无法取得合法 JSON: {last_err}")


def parse_json(text: str) -> dict:
    """宽松解析模型返回的 JSON(容忍 ```json 围栏和前后杂文)。"""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    t = t.strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start != -1 and end > start:
            return json.loads(t[start : end + 1])
        raise
