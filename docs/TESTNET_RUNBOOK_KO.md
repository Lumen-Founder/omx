# OMX 테스트넷 통합 시험 절차 (최종본, 커밋 기준은 git log 맨 위)

## 이 시험의 성격
- **목적:** 실제 HL 테스트넷에서 다섯 가지를 확인한다.
  1. 서명
  2. 주문 형식과 가격 단위 반올림
  3. 체결 수수료 해석
  4. 긴급 중지가 걸린 주문을 실제로 취소하는지
  5. 감사 기록 사슬
- **모의 거래소로는 확인할 수 없는 것:** 실제 응답 형식, 거부 사유, 요청 한도, 만료 시각(`expiresAfter`) 처리.
- **실행 주체:** 사용자가 자기 PowerShell에서 실행한다.
  - 키 파일은 사용자가 만들고 사용자 기기에만 둔다.
  - 키 파일 내용은 대화·이슈·다른 곳에 붙여 넣지 않는다.
- **자금:** 테스트넷 USDC만 쓴다(실제 가치 없음). 메인넷 자금·메인넷 주문은 없다.
- **크기:** HL 최소 주문 금액(10 USD)을 조금 넘는 크기만 쓴다. 이번 시험은 빌더 수수료 없이(0) 진행한다.

## 사용자 몫 (화면 작업, 한 번만)
1. app.hyperliquid-testnet.xyz 에 자기 지갑을 연결하고, 화면의 Faucet 안내대로 테스트넷 USDC를 받는다. Faucet이 같은 주소의 메인넷 입금 이력을 요구하면, 그 조건을 채울지는 사용자가 정한다.
2. 같은 화면의 API 메뉴에서 **API(에이전트) 지갑**을 만든다. 이 지갑은 거래만 할 수 있고 출금할 수 없다.
3. 그 에이전트 키를 `C:\omx-testnet\agent.key` 파일에 한 줄로 저장한다.
4. 메인 지갑 주소(공개값, 0x로 시작하는 40자리)를 메모해 둔다. `init`에 쓴다.

## 창 A: 준비
```powershell
cd C:\omx
$py  = 'C:\omx\.venv\Scripts\python.exe'
$cfg = 'C:\omx-testnet\omx.toml'
& $py -B -m omx.cli --config $cfg init --account <메인지갑주소> --network testnet --key-path C:\omx-testnet\agent.key
```
- `init`은 자격 질문 6개를 묻고 나서 설정 파일을 쓴다.

테스트넷은 거래대금과 호가 깊이가 작다. 그래서 유동성 하한 두 값을 테스트넷 설정에서만 0으로 낮춘다. 메인넷 설정에는 쓰지 않는다.
```powershell
$t = (Get-Content $cfg) -replace '^min_day_ntl_usd = .*', 'min_day_ntl_usd = 0.0' -replace '^min_depth_usd = .*', 'min_depth_usd = 0.0'
[IO.File]::WriteAllLines($cfg, $t)   # BOM 없는 UTF-8로 저장(설정 읽기에 필요)
& $py -B -m omx.cli --config $cfg eligibility   # 자격 질문: 거주지·현재 위치를 따로 묻는다
```

## 창 B: 데몬 (새 PowerShell 창, 시험 내내 열어 둔다)
```powershell
cd C:\omx
& 'C:\omx\.venv\Scripts\python.exe' -B -m omx.cli --config C:\omx-testnet\omx.toml daemon
```
- 이 창이 사람 확인 창이다. 승인·중지 해제 같은 요청이 오면 이 창에 `OMX CONFIRMATION NEEDED [코드]`와 요청 내용, 요청 id가 뜬다.
- 내용이 맞으면 이 창에 `confirm 코드`를 정확히 입력한다. 아니면 `deny 코드`를 입력한다. 180초 안에 입력해야 한다.

## 창 A: 시험
가격은 테스트넷 화면의 ETH 현재가를 보고 정한다. 수량 0.005 ETH는 ETH 가격이 2,200 USD보다 낮으면 0.01로 바꾼다(주문 금액 10 USD 이상 필요).
```powershell
& $py -B -m omx.cli --config $cfg fees
# 1) 걸어 두는 지정가: 현재가의 99.3 %(70 bps 아래). 가격 범위(±100 bps) 안이고 post-only라서 즉시 체결되지 않는다
& $py -B -m omx.cli --config $cfg submit ETH buy 0.005 --algo limit --limit-px <현재가x0.993> --post-only
& $py -B -m omx.cli --config $cfg status <intent_id>
# 2) 긴급 중지: 걸린 주문이 화면에서 사라지는지 확인
& $py -B -m omx.cli --config $cfg kill --reason "testnet kill test"
# 3) 중지 해제: 이 창에 I UNDERSTAND 입력 -> 창 B에 뜬 코드로 confirm 코드 입력
& $py -B -m omx.cli --config $cfg unkill
# 4) 시장가 매수 후 reduce-only 매도로 포지션 0
& $py -B -m omx.cli --config $cfg submit ETH buy 0.005 --algo market --max-slippage-bps 30
& $py -B -m omx.cli --config $cfg submit ETH sell 0.005 --algo market --reduce-only --max-slippage-bps 30
# 5) 감사 기록 사슬 검사
& $py -B -m omx.cli --config $cfg audit verify
```
- `submit`은 intent id를 출력한다. 그 id를 `status`에 쓴다.
- 응답 시간이 지나면 id와 `status` 안내가 나온다. 이때 다시 제출하지 말고 `status`부터 본다.
- 시험이 끝나면 창 B에서 `Ctrl+C`로 데몬을 끈다.

## 통과 기준
| 확인 | 기대 |
|---|---|
| post-only 지정가 | 테스트넷 화면에 그 가격으로 보이고, `status`가 working |
| `kill` | 그 주문이 화면에서 사라지고, 종료 코드 0, `WARNING` 없음 |
| `unkill` | 창 B에서 confirm한 뒤에만 해제됨 |
| 시장가 매수 → reduce-only 매도 | 둘 다 체결되고 포지션 0. 화면의 수수료와 `status`의 수수료가 같음 |
| `audit verify` | ok |

## 실패했을 때
이슈에 다음 두 가지만 붙인다:
- 화면에 나온 메시지
- `C:\omx-testnet\state` 안의 감사 기록 파일(`audit.jsonl`) 경로

키 파일과 설정 파일의 키 경로 밖 내용은 주지 않아도 된다. 키 파일은 어떤 경우에도 주지 않는다.
