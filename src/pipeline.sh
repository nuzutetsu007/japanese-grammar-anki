#!/usr/bin/env bash
# 一键流水线(幂等,可反复执行;每步只处理「待处理」的行)
#
#   expand(仅新增) → verify → repair 循环 → build_cards → export_anki
#
# 用法:
#   bash src/pipeline.sh              # 默认只扩 status=new,不动已验证内容
#   REEXPAND=1 bash src/pipeline.sh   # 换了提示词:重跑全部旧内容
#   WORKERS=12 bash src/pipeline.sh   # 提高并发(受 API 配额限制,未必更快)
#   CHAPTER=N2文法 bash src/pipeline.sh
set -u
set -o pipefail
cd "$(dirname "$0")/.."

PY=${PY:-python3}
W=${WORKERS:-8}
CH=${CHAPTER:-}
LOG=data/out/logs
mkdir -p "$LOG"
CHARG=""
[ -n "$CH" ] && CHARG="--chapter $CH"

say() { printf '\n\033[1;36m=== %s ===\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }

run() {          # run <日志名> <命令...>;返回命令的退出码
  local name="$1"; shift
  say "$name"
  "$@" 2>&1 | tee -a "$LOG/$name.log" | tail -n 60
  return "${PIPESTATUS[0]}"
}

run_or_die() {   # 关键阶段:失败就中止,不再往下跑
  local name="$1"; shift
  if ! run "$name" "$@"; then
    say "阶段 $name 失败,流水线中止(产物可能不完整)"
    exit 1
  fi
}

say "流水线开始 (workers=$W chapter=${CH:-全部})"

# ---- 阶段 2:扩写 ----------------------------------------------------------
# 默认只扩 status=new,不会动 verified / needs_review / expanded 里的旧内容。
# 只有换了提示词、确定要全量重写时,才显式 REEXPAND=1 —— 旧行会标 :superseded。
if [ -n "${REEXPAND:-}" ]; then
  say "REEXPAND=1 → 重跑 verified/needs_review/expanded(旧内容标 :superseded)"
  run_or_die expand_old "$PY" -u src/expand.py --status verified,needs_review,expanded \
      --new-version --workers "$W" $CHARG
fi
run_or_die expand_new "$PY" -u src/expand.py --status new --workers "$W" $CHARG

# ---- 阶段 3 + 3.5:自检 → 定向修复 → 再自检 --------------------------------
for round in 1 2 3; do
  run_or_die "verify_r$round" "$PY" -u src/verify.py --status expanded --workers "$W" $CHARG
  left=$("$PY" - "$CH" <<'EOF'
import sys; sys.path.insert(0,'src')
from common import connect
ch = sys.argv[1] if len(sys.argv) > 1 else ""
conn = connect()
if ch:
    n = conn.execute("SELECT COUNT(*) c FROM grammar_point WHERE status='needs_review'"
                     " AND chapter LIKE ?", (f"%{ch}%",)).fetchone()["c"]
else:
    n = conn.execute("SELECT COUNT(*) c FROM grammar_point WHERE status='needs_review'").fetchone()["c"]
print(n)
EOF
)
  say "第 $round 轮自检后 needs_review = $left"
  [ "$left" = "0" ] && break
  run_or_die "repair_r$round" "$PY" -u src/repair.py --workers "$W" $CHARG
done

# ---- 阶段 5:例句朗读音频(可选;VOICEVOX 没起就跳过) -----------------------
# 幂等:已生成过的例句直接跳过,重跑几乎不花时间。
if curl -sf -m 3 "${VOICEVOX_URL:-http://127.0.0.1:50021}/version" >/dev/null 2>&1; then
  run_or_die tts "$PY" -u src/tts.py --workers "$W" --gc
else
  say "VOICEVOX 不可达(${VOICEVOX_URL:-http://127.0.0.1:50021}) → 跳过 tts,卡片不带音频"
fi

# ---- 阶段 6 + 7:生成卡片 → 打包 -------------------------------------------
run_or_die build_cards "$PY" -u src/build_cards.py $CHARG
run_or_die export_anki "$PY" -u src/export_anki.py $CHARG

say "完成。卡片产物在 data/out/"
