#!/usr/bin/env bash
# GitHub 라벨·마일스톤·이슈를 한 번에 만든다. repo 생성 후 한 번만 실행.
# 재실행해도 안전하도록 이미 있는 것은 건너뛴다.
set -euo pipefail

REPO="${1:-JeongJaeSoon/paneglow}"
echo "→ $REPO"

label() {  # name color description
  gh label create "$1" --repo "$REPO" --color "$2" --description "$3" 2>/dev/null \
    || gh label edit "$1" --repo "$REPO" --color "$2" --description "$3" >/dev/null
  echo "  label: $1"
}

milestone() {  # title description
  if gh api "repos/$REPO/milestones" --jq '.[].title' | grep -qx "$1"; then
    echo "  milestone(있음): $1"
  else
    gh api "repos/$REPO/milestones" -f title="$1" -f description="$2" >/dev/null
    echo "  milestone: $1"
  fi
}

issue() {  # title milestone labels body
  gh issue create --repo "$REPO" --title "$1" --milestone "$2" --label "$3" --body "$4" \
    | sed 's|^|  issue: |'
}

echo "[1/3] 라벨"
label "phase-0"      "5319E7" "구현 전 관문 검증"
label "phase-1"      "0E8A16" "표시와 이동 — 최소 동작"
label "phase-2"      "1D76DB" "게이트 · MOD · 테두리 · 승인"
label "pure"         "C5DEF5" "하드웨어 없이 테스트되는 순수 로직"
label "hardware"     "D93F0B" "실기가 필요하다"
label "blocked"      "B60205" "선행 검증 결과를 기다린다"
label "docs"         "BFD4F2" "문서"

echo "[2/3] 마일스톤"
milestone "Phase 0 — 관문 검증"  "훅 상태 매핑과 키 전송. 실패하면 설계 일부를 덜어낸다."
milestone "Phase 1 — 표시와 이동" "6키가 현재 탭 pane 상태를 보여주고 눌러서 이동한다."
milestone "Phase 2 — 게이트와 조작" "두 게이트, 세대, MOD 탭 뷰, 테두리, 승인."

echo "[3/3] 이슈"

issue "Phase 0: 훅 이벤트 페이로드 확인" "Phase 0 — 관문 검증" "phase-0,blocked" \
"설계 전체가 여기 걸려 있다. 실패하면 Phase 1 의 Task 3 이후가 무효다.

계획서 **Task 0** 을 그대로 수행한다 → \`docs/superpowers/plans/2026-07-25-paneglow-phase1.md\`

## 답을 찾아야 할 것

- [ ] 어떤 훅 이벤트가 실제로 오는가
- [ ] \`Notification\` 페이로드에 **권한 프롬프트를 식별할 필드**가 있는가 ← 승인 기능의 생사
- [ ] \`Stop\` 페이로드에 **에러를 구분할 필드**가 있는가 ← \`error\` 상태의 생사
- [ ] \`tty\` / \`cwd\` / \`session_id\` 를 어디서 얻는가

## 분기

| 결과 | 영향 |
|---|---|
| \`waiting\` 판별 불가 | \`waiting\` 상태와 승인 기능 제거, 팔레트 4색으로 축소 |
| \`error\` 구분 불가 | \`error\` 제거, \`Stop\` 은 항상 \`done\` |
| \`tty\` 없음 | \`pid\` → \`ps -o tty=\` 유도 경로 추가 |

산출물: \`docs/verification/hook-events.md\`"

issue "Phase 0: pane 에 키를 전송할 수단 확인" "Phase 0 — 관문 검증" "phase-0,blocked,hardware" \
"승인·거절(C2/C3)의 실제 수단. 검증된 iTerm2 동작은 조회와 **포커스 이동**뿐이고 전송은 0건이다.

## 확인할 것

- [ ] iTerm2 API 로 특정 pane 에 텍스트/키를 보낼 수 있는가 (\`async_send_text\`)
- [ ] Claude Code 권한 프롬프트가 무엇을 기대하는가 (\`y\` / Enter / 방향키+Enter)
- [ ] 포커스가 그 pane 에 있을 때만 보내는 것으로 충분한가

실패하면 승인 기능을 Phase 2 에서 제거하고 **표시와 이동만** 남긴다."

issue "Phase 1: 상태 어휘와 우선순위" "Phase 1 — 표시와 이동" "phase-1,pure" \
"계획서 **Task 1**. \`src/paneglow/state.py\`

\`AgentState\` · \`PRIORITY\` · \`highest()\`. 하드웨어도 파일도 모르는 순수 모듈.
테스트 4개가 계획서에 그대로 있다."

issue "Phase 1: 세션 스토어 (원자적 쓰기)" "Phase 1 — 표시와 이동" "phase-1,pure" \
"계획서 **Task 2**. \`src/paneglow/store.py\`

훅은 짧게 여러 개가 겹쳐 실행되므로 **temp → fsync → rename** 이 필수다.
\`rev\` 역행 방지와 \`by_tty\` 최신 선택, 그리고 **TTL 이 아니라 iTerm2 생존 여부**로 정리한다.

의존: 상태 어휘"

issue "Phase 1: 훅 분류기" "Phase 1 — 표시와 이동" "phase-1,pure,blocked" \
"계획서 **Task 3**. \`src/paneglow/hook.py\`

**Phase 0 훅 검증 결과를 반영해서 작성한다.** \`waiting\`/\`error\` 판별이 불가로 나오면
해당 분기와 테스트를 제거하고 진행한다.

의존: 상태 어휘, 세션 스토어, Phase 0 결과"

issue "Phase 1: 렌더러 (순수 함수)" "Phase 1 — 표시와 이동" "phase-1,pure" \
"계획서 **Task 4**. \`src/paneglow/render.py\`

로직의 심장이며 하드웨어 없이 전부 테스트된다. 테스트 12개가 계획서에 있다.

특히 \`overflow()\` 가 중요하다 — 6키에 못 올라간 pane 을 테두리 집계에 넘기지 않으면
**7번째 pane 이 어디에도 안 보이고 사라진다.**

의존: 상태 어휘"

issue "Phase 1: 프로토콜 (메시지와 프레이밍)" "Phase 1 — 표시와 이동" "phase-1,pure" \
"계획서 **Task 5**. \`src/paneglow/protocol.py\`

이 기기의 가장 큰 함정. **잘못 프레이밍한 write 도 성공을 반환하고 조용히 버려진다.**

- USB \`[0x02][len][json]\` 63B / BLE \`[0x06][0x02][len][json]\` 64B
- \`v.oai.*\` 는 notification — \`id\` 를 넣으면 404
- 빈 슬롯은 \`{c:0, b:0, e:off}\` — 어둡게가 아니라 꺼짐"

issue "Phase 1: 설정 로드" "Phase 1 — 표시와 이동" "phase-1,pure" \
"계획서 **Task 6**. \`src/paneglow/config.py\`

틀린 값은 기본값으로 떨어뜨리고 경고를 모은다 — **기동을 막지 않는다.**
\`C5\`/\`C6\` 은 넓은 캡을 공유해 두 id 가 오므로 MOD 후보에서 제외한다."

issue "Phase 1: iTerm2 어댑터" "Phase 1 — 표시와 이동" "phase-1" \
"계획서 **Task 7**. \`src/paneglow/iterm.py\`

pane **발견**은 iTerm2 가 한다 — \`jobName\` 이 버전 문자열이면 Claude Code 다.
덕분에 훅이 아직 안 붙은 세션도 자리를 차지한다.

\`flatten()\` 은 순수 함수라 가짜 트리로 단위 테스트한다. 나머지는 \`integration\` 마커.

의존: 렌더러(\`Pane\`)"

issue "Phase 1: 패드 어댑터 (왕복 검증)" "Phase 1 — 표시와 이동" "phase-1,hardware" \
"계획서 **Task 8**. \`src/paneglow/pad.py\`

\`hidapi\` 는 이 기기에서 \`open_path()\` 가 항상 실패한다 — IOKit 을 직접 쓴다.
IOKit 경계를 네 메서드로 좁혀두었으니 그것만 ctypes 로 채운다.

**완료 신호는 \`device.status\` 왕복 성공이다.** 성공 리턴 코드는 아무것도 증명하지 않는다.

의존: 프로토콜"

issue "Phase 1: 첫 통합 — 실제로 빛나게 한다" "Phase 1 — 표시와 이동" "phase-1,hardware" \
"계획서 **Task 9**. \`src/paneglow/cli.py\`

\`once\` / \`doctor\` / \`hook\` 세 커맨드.

**완료 판정: 훅을 설치하고 \`paneglow once\` 를 실행했을 때
A1~A6 이 현재 탭의 pane 상태대로 켜진다.**

\`hook\` 은 무슨 일이 있어도 0 을 반환해야 한다 — 아니면 에이전트가 방해받는다.

의존: 전부"

echo
echo "완료. 다음: gh issue list --repo $REPO"
