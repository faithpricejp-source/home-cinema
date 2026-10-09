#!/bin/zsh
# launchd（如 com.example.home-cinema.autoscan）调用：Movie/ 或 TV/ 顶层有变化（或每 6 小时兜底）→ 扫描 + 补元数据。
# 单实例锁；先等 90 秒让拷贝/移动落定；外置卷没挂就静默退出。
export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
# Kimi-P1: 锁路径可用环境变量注入（launchd 不设它，生产行为不变；测试用它指到临时目录）
LOCK="${AUTOSCAN_LOCK:-/tmp/homecinema-autoscan.lock}"
# Kimi-P1: 持锁中被 SIGKILL/断电时 trap 不执行，锁目录残留，裸 mkdir 会让此后每次
# 触发都「skip: another scan running」并 exit 0 静默停摆。抢锁失败时看锁内 pid：
# 进程还活着且锁龄未超过兜底触发周期（launchd StartInterval=6 小时）才让位，
# 否则（没写 pid / pid 已死 / 锁龄超限可能是 PID 复用）清残留重抢。
if ! mkdir "$LOCK" 2>/dev/null; then
  pid=$(cat "$LOCK/pid" 2>/dev/null)
  age=$(( $(date +%s) - $(stat -f %m "$LOCK" 2>/dev/null || date +%s) ))
  # pid 文件在 /tmp，可能被第三方写入：先校验纯数字再 kill -0
  if [ -n "$pid" ] && [[ "$pid" == <-> ]] && kill -0 "$pid" 2>/dev/null && [ "$age" -lt 21600 ]; then
    echo "$(date '+%F %T') skip: another scan running"; exit 0
  fi
  rm -rf "$LOCK"
  mkdir "$LOCK" 2>/dev/null || { echo "$(date '+%F %T') skip: cannot take lock"; exit 0; }
fi
echo $$ > "$LOCK/pid"  # Kimi-P1: 记下持锁进程，供下次判断锁是否残留
trap 'rm -rf "$LOCK"' EXIT  # Kimi-P1: 锁内有 pid 文件 rmdir 删不掉，改 rm -rf
MEDIA="${HOMECINEMA_MEDIA_DIR:-$HOME/Movies}"  # 片库根目录，外置卷没挂就静默退出
[ -d "$MEDIA" ] || { echo "$(date '+%F %T') skip: volume not mounted"; exit 0; }
sleep 90
cd "${0:A:h}/.." || exit 1
echo "$(date '+%F %T') scan start"
# 10-07：再补演职员与人名别名（只拉新片/新人，增量），否则新片按人名搜不到
.venv/bin/python -m homecinema scan && .venv/bin/python -m homecinema fetch-metadata && .venv/bin/python -m homecinema fetch-extras
rc=$?
echo "$(date '+%F %T') scan end rc=$rc"
exit $rc
