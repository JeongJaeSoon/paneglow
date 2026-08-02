# Codex Micro — 하드웨어 실측 노트

> 이 기기에서 직접 확인한 사실만 적는다. 설계 결정은 여기 없다 —
> [설계 문서](design.html)를 참조.
>
> **측정 환경**: macOS · VID `0x303A` / PID `0x8360` · firmware **v0.4.1** · 2026-07-25
> 다른 펌웨어에서는 다를 수 있다.

각 항목은 근거 등급을 붙인다.

| 등급 | 뜻 |
|---|---|
| **실측** | 이 기기에서 직접 확인 |
| **번들** | 벤더 앱 번들에서 발견한 문자열/코드 (동작은 미확인) |
| **추정** | 위 둘에서 추론. 확인 필요 |

---

## 1. 물리 배치와 명칭

```text
행 1    ◯ 노브        [A1] [A2]        ● 조이스틱
행 2    [A3]  [A4]   [A5]  [A6]
행 3    [C1⚡] [C2✓]  [C3✗] [C4⤳]
행 4    ⋮LED×3 ◉터치  [═ C5+C6 🎤 ═]  [C7🌀]
```

| 부위 | 명칭 | 벤더 id | 등급 |
|---|---|---|---|
| 위 반투명 6개 | Agent 키 A1~A6 | `AG00`~`AG05` | 실측 |
| 아래 흰색 | Command 키 C1~C7 | `ACT06`~`ACT12` | 실측(일부) |
| 왼쪽 위 원형 | 노브 (로터리 엔코더) | `ENC_CW` / `ENC_CC` / `ENC_CLK` | 실측 |
| 오른쪽 위 | 조이스틱 (아날로그) | `v.oai.rad` | 번들 |
| 왼쪽 아래 검정 | 터치 센서 | — | 실측(동작) |
| 그 옆 흰 LED 3개 | 레이어 인디케이터 | — | 실측(동작) |

**C5·C6은 넓은 캡 하나를 공유**한다 — 스위치는 7개, 키캡은 6개. 누르면 두 id가 함께 발생한다(등급: 번들. 직접 확인 안 함).

> ⚠️ **키캡과 id는 고정 관계가 아니다.** 캡은 교체·재배열할 수 있으므로 코드는 위치(id)로만 다뤄야 한다.

### 터치 센서

| 조작 | 효과 | 등급 |
|---|---|---|
| 탭 | 레이어 순환 (최대 6) | 실측 |
| 3초 홀드 → 탭 | BLE 채널 1/2/3 순환, 4번째 탭은 WIRED | 벤더 문서 |

---

## 2. 레이어별 동작 ★ 가장 중요

`device.status.layer_index`는 **1부터 시작**하며 화면의 Layer 번호와 그대로 일치한다(실측: 사용자가 Layer 1/2/3으로 옮길 때마다 값이 1/2/3으로 따라옴).

| 기능 | Layer 1 | Layer 2+ | 등급 |
|---|---|---|---|
| A키 → `v.oai.hid` | ✅ | ❌ | 실측 (양쪽) |
| C키 → `v.oai.hid` | ✅ `ACT06`·`ACT08`·`ACT12` 확인 | ❌ | 실측 |
| 노브 → `v.oai.hid` | ✅ `ENC_CW`/`ENC_CC`/`ENC_CLK` | 미확인 | 실측 (L1) |
| A키 → 표준 스캔코드 | ❌ | ✅ Work Louder 설정대로 | 실측 (양쪽) |
| `v.oai.thstatus` (A1~A6 개별) | ✅ | ❌ 무시됨 | 실측 (양쪽, 재현) |
| `v.oai.rgbcfg` → `keys` | C1~C7만 | **A+C 전체가 한 존** | 실측 (양쪽) |
| `v.oai.rgbcfg` → `ambient` (테두리) | ✅ | ✅ | 실측 (양쪽) |

**해석**: Layer 1은 Codex 모드다. Agent 키가 "특별한 존"으로 존재하고 키는 벤더 채널로만 나간다.
Layer 2+에서는 Agent 키가 일반 백라이트에 흡수되고 키는 평범한 QMK처럼 스캔코드를 낸다.
Work Louder Input 앱의 Layer 2 설정에 Backlight/Underglow 두 존만 있는 것과 일치한다.

> Layer 2에서 `thstatus`가 안 되는 것이 **슬립 때문이 아님**을 확인했다 —
> 같은 조건에서 `rgbcfg`는 정상 동작했다.

### 입력 이벤트 형식 (실측)

```json
{"m": "v.oai.hid", "p": {"k": "AG01", "act": 1}}   // press
{"m": "v.oai.hid", "p": {"k": "AG01", "act": 0}}   // release
{"m": "v.oai.hid", "p": {"k": "ENC_CW", "act": 2}} // 엔코더 틱
```

**동시 입력이 정확히 판별된다** — `ACT12`+`AG00`~`AG05` 여섯 조합 전부, 3개 동시(`ACT12+AG04+AG05`)까지 실측.

> ⚠️ **노브는 `act`가 1이 아니다.** 회전은 `act=2`로 오고 press/release 쌍이 없다.
> 키와 같은 `act==1` 필터를 걸면 다이얼이 통째로 죽는다.

---

## 3. 조명 — 세 존은 독립

| 명령 | 물리 부위 (Layer 1) | 등급 |
|---|---|---|
| `v.oai.thstatus` | **A1~A6** 개별 | 실측 |
| `v.oai.rgbcfg` → `keys` | **C1~C7** 백라이트 | 실측 |
| `v.oai.rgbcfg` → `ambient` | **테두리** (언더글로우) | 실측 |
| `lights.preview` | 백라이트 + 테두리 | 실측 |

> `lights.preview`는 **동작한다.** FreeMicro 문서가 "v0.4.1에서 무효"라고 적었으나 실제로는 색이 바뀌었다.

**LED는 읽을 수 없다.** 조명 메서드는 전부 쓰기 전용이고 `device.status`·`ui.active_screen` 어디에도 LED 정보가 없다. 따라서 누가 덮어썼는지 감지할 방법이 없다.

### `rgbcfg` 의 효과(`e`) — 기기가 스스로 애니메이션한다

`{"e":효과, "b":밝기, "s":속도, "c":색}` 의 `e` 를 0~6으로 훑어 테두리에서 실측했다
(2026-08-02, v0.4.1). **초안은 `e` 를 0(off) / 1(solid) 두 값으로만 알고 있었다.**

| `e` | 동작 | 등급 |
|---|---|---|
| 0 | 소등 | 실측 |
| 1 | 계속 켜짐 | 실측 |
| 2 | 회전 (한 점이 테두리를 돈다) | 실측 |
| 3 | 무지개 회전 — `c` 를 무시한다 | 실측 |
| **4** | **깜빡임** | 실측 |
| 5 | 계속 켜짐 (1과 구분되지 않았다) | 실측 |
| **6** | **부드러운 펄스** (밝기가 오르내린다) | 실측 |

**이게 중요한 이유**: 메시지 한 번이면 기기가 알아서 계속 움직인다. 호스트가 주기적으로
쏠 필요가 없으므로 트래픽도, 데몬이 멈춰 애니메이션이 굳는 위험도 없다.

`s`(속도)는 1과 3에서 차이가 났으나 **같은 효과의 속도 차이는 눈으로 구분하기 어렵다.**
의미를 나눌 축으로는 `s` 가 아니라 `e` 를 쓸 것.

### ⚠️ 마지막 write 뒤에 시간을 줘야 반영된다

write 직후 프로세스를 끝내면 **그 색이 기기에 반영되지 않고 직전 색이 남는다.** 실측:
테두리를 파랑 → 오렌지로 바꾸고 곧바로 종료했더니 파랑이 남았다. 같은 순서에
1초 대기를 넣으면 오렌지가 남는다.

설계의 "데몬 종료 시 LED를 끄고 나간다"가 **그냥 쓰고 나가면 동작하지 않는다**는 뜻이다.
종료 경로에 flush(짧은 대기 또는 런루프 pump)가 필요하다.

### 공장 상태색 (벤더 문서)

```text
idle #FFFFFF · working #304FFE · waiting #FF6D00 · done #00FF4C · error #FF0033 · 없음 off
```

### 벤더와의 공존 (실측)

- **채널 동시 open 가능** — Work Louder Input 앱이 실행 중이어도 우리가 벤더 채널을 열 수 있다.
- **테두리는 60초간 유지됐다** — ChatGPT 앱이 켜진 Layer 1에서 테두리를 칠하고 관찰.
  단, 관찰 중 **벤더가 실제로 LED를 다시 그렸는지는 확인하지 못했다.** 조건을 좁혀 재측정 필요.

---

## 4. 프로토콜

- USB HID, 벤더 컬렉션 usage page `0xFF00`, **Report ID 6**
- 프레이밍이 전송별로 다름 — USB `[0x02][len][json]` 63B / BLE `[0x06][0x02][len][json]` 64B
- `v.oai.*`는 **notification**. `id`를 넣으면 `404 Method not found`
- `hidapi`의 `open_path()`는 항상 실패한다. IOKit을 직접 쓴다

> ⚠️ **성공 리턴 코드는 아무것도 증명하지 않는다.**
> 잘못 프레이밍된 write도 `kIOReturnSuccess`를 반환하고 조용히 버려진다.
> 유일하게 믿을 수 있는 건 `device.status` 왕복이다.

### 여는 방법 — Input Monitoring은 필요 없다

**초기 기록은 "Input Monitoring 권한이 필요하다"였고 그것은 틀렸다.**
`IOServiceGetMatchingServices`에 VID/PID를 넣어 이 기기만 매칭하면 권한 없이 열리고
읽고 쓴다. 2026-07-26 실측 — 권한이 부여되지 않은 평범한 파이썬으로 왕복 성공.

권한이 필요하다고 오해한 원인은 `IOHIDManagerSetDeviceMatching(NULL)`로 **모든 HID
기기**를 매칭한 뒤 여는 방식이었다. 거기엔 키보드가 포함되고, 그건 당연히 권한이
필요하다. 그 실패가 "이 기기를 쓰려면 권한이 필요하다"로 잘못 일반화됐다.
`IOHIDManagerOpen`은 권한이 없으면 `kIOReturnExclusiveAccess`(`0xE00002C5`)를 낸다.

**기기 식별에 `PrimaryUsagePage == 0xFF00`을 쓰면 안 된다.** macOS가 내주는
IOHIDDevice는 **하나뿐**이고 그 주 usage는 키보드다:

```text
usage_page=0x0001  usage=0x0006     ← Generic Desktop / Keyboard
```

`0xFF00`은 그 안의 **하위 컬렉션**이라 주 usage로는 잡히지 않는다.
`0xFF00`으로 필터링하면 기기를 못 찾는다. VID/PID로만 매칭할 것.

실측 왕복 (권한 없는 인터프리터):

```console
IOServiceGetMatchingServices -> success
matched 1 IOHIDDevice(s) for 0x303a/0x8360
  usage_page=0x0001 usage=0x0006  open -> success
  SetReport -> success
  <- {"result":{"version":"v0.4.1","profile_index":0,"layer_index":1,
      "battery":100,"is_charging":false},"id":42,"method":"device.status"}
```

### 전송 판별 — `Transport` 속성을 쓴다

프레이밍이 전송별로 다르고 틀리면 조용히 버려지므로 판별이 필요하다.
IOKit의 `Transport` 속성이 신뢰할 수 있는 신호다. **양쪽 다 실측했다.**

| 연결 | `Transport` | 프레이밍 | 왕복 | 점등 |
|---|---|---|---|---|
| 유선 | `USB` (추정) | `[0x02][len][json]` 63B | ✅ | ✅ |
| 무선 | `Bluetooth Low Energy` | `[0x06][0x02][len][json]` 64B | ✅ | ✅ |

```console
Transport              Bluetooth Low Energy
Manufacturer           Work Louder
Product                Codex Micro #1
PrimaryUsagePage       0x0001 (1)
PrimaryUsage           0x0006 (6)
MaxOutputReportSize    0x0040 (64)
MaxInputReportSize     0x0040 (64)

Transport says 'Bluetooth Low Energy' -> try BLE first
  BLE: 1 packet(s) x 64 bytes
  -> REPLY  fw=v0.4.1 layer=1 battery=98
```

`"USB"`가 들어 있으면 USB, 아니면 BLE로 고르면 된다. 사용자가 유선↔무선을
오가도 자동으로 맞는다.

> **더 나은 신호일 가능성** (미확인): `MaxOutputReportSize`가 BLE에서 **64**로
> 나왔다. USB에서 **63**이면 전송 이름을 볼 필요 없이 이 숫자를 프레임 크기로
> 그대로 쓸 수 있다. 문자열 매칭보다 안전하다. **USB에서 이 값을 재보지 않았다** —
> 다음에 유선 연결할 때 확인할 것.

BLE로 전환은 터치 센서 3초 홀드 후 탭(§1). 무선일 때는 배터리 값이 100 미만으로
내려간다(98 관측) — 유선일 때는 충전 중이라 100이었다.

### 주요 메서드

| 메서드 | 내용 | 등급 |
|---|---|---|
| `device.status` | `{version, profile_index, layer_index, battery, is_charging}` 읽기 전용 | 실측 |
| `v.oai.thstatus` | `[{id, c, b, e, s}, …]` — Agent 키 개별 | 실측 |
| `v.oai.rgbcfg` | `{ambient:{e,b,s,c}, keys:{…}}` | 실측 |
| `lights.preview` | `{backlight:{effect,brightness,speed,color}, underglow:{…}}` | 실측 |
| `host.focused_app` | `{appName, process, path}` — 호스트가 포커스 앱을 알림 | 번들 |
| `ui.active_screen` | `{screen_name}` 반환 | 번들 |
| `sys.bootloader` | ⚠️ DFU로 재부팅. 정상 운용 중 호출 금지 | 번들 |

효과 코드: `0 off · 1 solid · 2 snake · 3 rainbow · 4 breath · 5 gradient · 6 shallowBreath`

---

## 5. 벤더 소프트웨어

### ChatGPT.app

Codex 스레드 상태를 Layer 1의 A1~A6에 그린다. Codex App Server의 JSON-RPC 클라이언트다.

| 항목 | 내용 | 등급 |
|---|---|---|
| Agent 키 동작 | 싱글탭 = 백그라운드 스레드 선택, 더블탭 = 창을 앞으로 | 벤더 문서 |
| 노브 | `ENC_CW`→ArrowUp, `ENC_CC`→ArrowDown, `ENC_CLK` 500ms 홀드 → 설정 | 번들 |

### Work Louder Input (`/Applications/input.app`)

레이어별 키맵·LED 설정 도구. **USB 연결이 필요하다** (BLE로는 기기를 못 찾음).

- **AppSense**: frontmost 앱을 **1초 폴링**해 `host.focused_app`을 보내고, 디바이스가 링크된 레이어로 자동 전환한다. 앱이 켜져 있어야 동작하며, 켜져 있으면 다른 프로그램의 전송을 1초마다 덮어쓴다. (번들)
- Layer 1은 앱에서 편집 불가 — "To edit this layer please use Codex Micro app"

> ⚠️ **Setup 탭의 펌웨어 플래싱을 누르지 말 것.**
> 목록에 Codex Micro용이 없다(nomad / knob / KnobF1 / Creator Micro V2 / XYZ R2).
> 이 기기의 MCU는 **ESP32**라 RP2040용 Creator Micro V2 펌웨어는 위험하고,
> 성공해도 Agent 키·AppSense 등 전용 기능이 사라진다.
>
> **BLE 연결 중에는 앱이 기기를 못 찾고 "bootloader mode"로 오진한다** (실측).
> 전원 버튼으로 껐다 켜면 WIRED로 복귀한다.

### Codex 상태를 얻는 경로 (번들, 미검증)

ChatGPT 앱이 상태를 받아오는 경로. 우리도 붙을 수 있을지 모르나 인증·핸드셰이크는 미파악이다.

| 경로 | 내용 |
|---|---|
| `ws://codex-app-server/rpc` | WebSocket RPC 엔드포인트 |
| `~/.codex/ipc/ipc.sock` | 유닉스 소켓 — **파일 존재는 실측** |
| `thread/status/changed` | 상태 변화 알림. `params.status`에 상태가 실리는 것으로 보임 |
| `thread/started`·`archived`·`deleted` | 수명주기 알림 |
| `~/.codex/.codex-global-state.json` | UI 상태 저장소. `unread-thread-ids-by-host-v1`로 "읽지 않은 스레드"는 알 수 있으나 **실시간 상태는 없음** (실측) |

**공개 API가 아니므로 앱 업데이트마다 깨질 수 있다.**

---

## 6. iTerm2 쪽 실측

| 항목 | 결과 |
|---|---|
| AppleScript로 탭·pane·tty 조회 | ✅ |
| Python API로 분할 트리 순회 | ✅ (`Splitter vertical=… children=N`) |
| pane별 `tty`·`cwd`·`jobName` | ✅ |
| **백그라운드 pane 선택** | ✅ `async_activate(select_tab=False, order_window_front=False)` — Finder를 앞에 둔 채 pane만 이동, iTerm2 창은 올라오지 않음 |
| 분할 트리 `vertical` 플래그 방향 | ✅ `vertical=True` = **좌우** 배치 (구분선이 세로) |
| pane 절대 좌표 API | ❌ 없다. `Session`에 `frame`·`origin` 없음(2.20). `grid_size`(셀 수)만 있다 |

> **iTerm2는 열을 먼저 묶는다.** 2x2 탭의 트리는 `Splitter(vertical=True)[좌열, 우열]`이라
> 깊이 우선으로 훑으면 **좌상·좌하·우상·우하**가 나온다. 사람이 읽는 순서
> (좌상·우상·좌하·우하)와 다르므로 좌표를 계산해 `(y, x)`로 정렬해야 한다.
>
> 게다가 구분선을 드래그하면 열마다 나뉘는 높이가 달라져 **균등 분할 가정으로는 순서가
> 뒤바뀐다.** 실측: 왼쪽을 69/23으로, 오른쪽을 46/46으로 나누면 우하 pane이 좌하보다
> 위에 온다. `grid_size`(셀 수)로 가중해야 맞는다.

> **`jobName`으로 Claude Code pane을 식별할 수 있다** — 버전 문자열(`2.1.220` 형태)로 나온다.
> 단 이 표기에 의존하므로 Claude Code 업데이트로 깨질 수 있다.

Python API는 iTerm2 설정에서 활성화해야 한다(`EnableAPIServer`). 유닉스 소켓(`~/Library/Application Support/iTerm2/private/socket`)으로 붙으므로 **TCC 권한은 필요 없다.**

---

## 7. 알려진 간섭

벤더 공식 경고:

> "Karabiner and Logitech Options + that has input monitoring permissions could interfere with the communication between Codex and the Micro."

미해결 이슈로 표시돼 있다("we're already working on a fix").
