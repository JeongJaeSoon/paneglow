# Phase 0 — Claude Code Desktop 세션 딥링크 실측

> 데스크톱 전환의 관문. "키를 누르면 그 세션이 열린다"가 여기 걸려 있었다.
> **측정**: 2026-08-02 · Claude.app 1.24012.9 · Claude Code 2.1.219/2.1.220

---

## 결론

```
claude://claude.ai/claude-code-desktop/<local_sessionId>
```

**된다.** 앱이 앞으로 나오고 그 세션이 포커스된다. 살아 있는 세션·오래된 세션 양쪽에서 확인했다.

| 질문 | 답 |
|---|---|
| 외부에서 특정 세션을 열 수 있는가 | **✅ 가능** |
| 훅의 `session_id`를 그대로 쓸 수 있는가 | **❌ 불가** — `local_<uuid>`로 변환해야 한다 (§3) |
| 매핑 수단이 있는가 | ✅ `cliSessionId` 필드 (§3) |
| 권한이 필요한가 | ❌ 불필요. `open(1)`만 쓴다 |
| `claude://resume` 을 쓰면 되는가 | **❌ 쓰면 안 된다** — 사본 세션을 만든다 (§4) |

---

## 1. URL 형태 — host가 아니라 경로다

라우트를 host 자리에 넣으면 **조용히 실패한다.** 에러도 로그도 없이 엉뚱한 화면이 열린다.

| 시도한 URL | 결과 |
|---|---|
| `claude://claude.ai/claude-code-desktop/local_…` | ✅ `setFocusedSession` 발생 |
| `claude://claude-code-desktop/local_…` | ❌ 앱만 앞으로. 세션 안 열림 |
| `claude://claude-code-desktop/_/local_…` | ❌ 무반응 |
| `claude://local_sessions/local_…` | ❌ 무반응 |
| `claude://claude.ai/epitaxy/local_…` | ❌ 무반응 |
| `claude://cowork/local_…` | ❌ `warn: unrecognized cowork path` |
| `claude://code/local_…` | ❌ `warn: unrecognized code path` |

이유는 네비게이터에 있다:

```js
function Uee(e, t, n) {
  const [r, i, ...o] = t.pathname.split("/");        // 앞 두 조각을 버린다
  const s = o.length > 0 ? `/${e}/${o.join("/")}` : `/${e}`;
  n.webContents.loadURL(new URL(s, It()).toString())
}
```

`claude://claude-code-desktop/local_x` 는 `pathname="/local_x"` 라서 `split` 결과가 `["", "local_x"]`,
`o`가 비어 **세션 id가 통째로 먹히고** `/claude-code-desktop` 만 열린다.
`claude://claude.ai/claude-code-desktop/local_x` 여야 `pathname="/claude-code-desktop/local_x"` 가 되어
`o = ["local_x"]` 로 살아남는다. 앱 자신이 쓰는 `claude://claude.ai/mcp-auth-callback/sdk` 와 같은 모양이다.

> **host는 `claude.ai` 고정이 아니라 "버려지는 자리"** 일 가능성이 높다. 하지만 앱이 쓰는 형태를
> 그대로 따르는 편이 안전하다.

라우트 접두사 상수는 셋이고 그중 딥링크 호스트 열거형에도 있는 것은 `claude-code-desktop` 하나다:

```js
const gg = "/code", Qs = "/epitaxy", u6 = "/claude-code-desktop"
```

`/epitaxy` 가 세션 라우트의 정규형(`desktopCodeSessionRoute = CD`, `CD(e) = ${Qs}/${e}`)이지만
**딥링크로는 도달할 수 없다.** 호스트 열거형에 없어서 `default:` 분기로 떨어지고, 그 분기는 host를 버린다.

---

## 2. 관측 방법

`claudeURLHandler` 의 `info` 로그는 **파일에 안 남는다**(warn·error만 남는다).
성공은 로그 부재로 확인할 수 없으므로 다른 신호를 썼다.

| 신호 | 위치 |
|---|---|
| **`[CCD] LocalSessions.setFocusedSession: sessionId=local_…`** | `~/Library/Logs/Claude/main.log` |
| `lastFocusedAt` | `claude-code-sessions/*/*/local_*.json` |

`setFocusedSession` 이 1차 신호다. `lastFocusedAt` 은 **이미 포커스된 세션을 다시 열면 안 바뀐다** —
이것만 보면 성공을 실패로 오판한다.

---

## 3. id 공간이 둘이다 ⚠️

훅이 주는 id와 앱이 쓰는 id가 **다른 uuid**다. 접두사만 붙이는 변환이 아니다.

```
훅 payload          session_id   = 998574ae-6e08-4711-a87a-a57b73df7975
~/.claude/sessions/  sessionId   = 998574ae-…                    ← 같다
앱                   sessionId   = local_b25f3a00-1b28-4b76-…    ← 다른 uuid
                     cliSessionId= 998574ae-…                    ← 여기서 만난다
```

매핑은 이 파일들에 있다:

```
~/Library/Application Support/Claude/claude-code-sessions/<org>/<account>/local_<uuid>.json
  { "sessionId": "local_…", "cliSessionId": "…", "title": "…",
    "lastActivityAt": …, "lastFocusedAt": …, "isArchived": false }
```

- 파일 하나가 **200KB 안팎**(트랜스크립트 포함)이다. 폴링 루프에서 읽을 물건이 아니다.
- `<org>`·`<account>`는 계정별 uuid이므로 **glob으로 찾아야 한다.** 하드코딩 불가.
- 실측 시점에 15개 중 살아 있는 세션은 2개였다 — 죽은 세션·아카이브된 세션이 계속 남는다.

**따라서 매핑은 키를 누른 그 순간에만 조회한다.** 15 × 200KB 스캔은 사람이 못 느낀다.

---

## 4. `claude://resume` 은 쓰면 안 된다

존재하고 동작하지만 **용도가 다르다.** "CLI 세션을 데스크톱으로 가져오기"다.

```js
case Fr.Resume: {
  const l = i.searchParams.get("session");           // 원시 uuid (C5 = uuid 정규식)
  return l && C5.test(l) ? (… importCliSession(l) …) : false
}

async importCliSession(e) {
  const r = `${LOCAL_SESSION_PREFIX}${e}`;           // "local_" + cliSessionId
  if (this.sessions.get(r)) { … return r; }          // 이미 import 됐으면 그대로
  const i = await this.diskTranscript.resolveProjectDirForSession(e);
  if (!i) throw new Error(`CLI session transcript not found: ${e}`);
  …                                                  // 트랜스크립트를 읽어 새 세션을 만든다
}
```

import된 CLI 세션의 id는 `local_<cliSessionId>` 인데, **데스크톱 세션의 id는 그 규칙을 안 따른다**(§3).
그래서 데스크톱 세션에 `resume` 을 걸면 `sessions.get()` 이 빗나가고 트랜스크립트를 다시 읽어
**사본 세션이 생긴다.**

훅 payload의 `session_id`를 그대로 넘길 수 있다는 점이 유혹적이지만, 그 대가가 세션 중복이다.
`claude-code-desktop` 경로 + 매핑 조회가 옳다.

---

## 5. 설계에 반영할 것

1. **딥링크는 `claude://claude.ai/claude-code-desktop/<local_sessionId>`.** host 자리에 라우트를 넣지 말 것.
2. **`deeplink.py` 는 매핑 조회를 포함한다** — `cliSessionId` 로 `local_*.json` 을 훑어 `sessionId` 를 얻는다.
   키를 누른 순간에만. 캐시 없이 시작한다.
3. 매핑을 못 찾으면 **아무것도 하지 않는다.** 앱을 앞으로 내는 폴백도 두지 않는다 —
   엉뚱한 세션이 열린 것처럼 보이는 편이 더 나쁘다.
4. `claude://resume` 은 쓰지 않는다.

---

## 6. 미확인

| 항목 | 왜 |
|---|---|
| host를 `claude.ai` 말고 다른 값으로 둬도 되는가 | 되는 형태를 찾은 뒤 더 흔들지 않았다 |
| 새로 만든 세션의 `local_*.json` 이 언제 쓰이는가 | 갓 생긴 세션은 잠시 매핑이 없을 수 있다 |
| 앱이 꺼져 있을 때 | 실측 중 앱이 계속 떠 있었다. `open` 이 앱을 띄우는 것까지는 표준 동작 |
| 계정이 여럿일 때 `<org>/<account>` 가 여러 개인 경우 | 실측 환경은 하나뿐이었다 |
