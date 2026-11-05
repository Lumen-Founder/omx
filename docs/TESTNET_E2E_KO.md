# 테스트넷 E2E: x402 견적 → OMX 주문 → 빌더 수수료 (사용자 실행)

- **확인할 것:**
  1. 에이전트가 x402로 Pre-Trade 견적을 결제한다(Base Sepolia, 시험용 USDC).
  2. 그 견적이 OMX 주문으로 이어진다.
  3. HL 테스트넷 체결에 우리 빌더 수수료가 붙는다.
  4. 긴급 중지가 동작한다.
  5. 감사 기록이 맞는다.
- 모든 돈은 시험용(가치 없음)이다. 메인넷은 쓰지 않는다.
- 키 파일은 사용자 기기에만 있다. 키 내용은 누구에게도 보여 주지 않는다.

## 시험용 주소 (`tools/new_test_wallet.py`로 만든 시험 지갑, 공개값)
| 용도 | 주소 | 키 파일 |
|---|---|---|
| HL 테스트넷 빌더 + x402 수취 | `<시험빌더주소>` | `C:\omx-testnet\builder_test.key` |
| Base Sepolia 결제(에이전트 역할) | `<결제지갑주소>` | `C:\omx-testnet\x402_payer_test.key` |
| 사용자 에이전트(API) 지갑 | 사용자가 만든 것 | `C:\omx-testnet\agent.key` |

메인넷 빌더 주소(B1-0)는 이것과 별도로 사용자 지갑으로 만든다.

## 순서 (한 단계씩)
1. **테스트넷 USDC 받기**
   - app.hyperliquid-testnet.xyz/drip 에서 받는다.
   - 같은 주소로 메인넷에 입금한 이력이 있어야 한다.
2. **빌더 주소 채우기**
   - 테스트넷 화면의 "보내기"로 `<시험빌더주소>`에 USDC 110을 보낸다.
   - HL은 빌더 주소의 perps 계정가치가 100 USDC 이상이어야 한다.
   - "보내기"로 보낸 USDC는 받는 쪽의 **현물(spot)**에 들어간다(2026-11-05 실측: spot 110, perps 0). 그래서 다음 명령으로 perps로 옮긴다. 빌더 시험 키로 서명하는 테스트넷 전용 명령이고, 사용자가 실행한다.
     ```powershell
     cd C:\omx
     .\.venv\Scripts\python.exe -B tools\testnet_usd_class_transfer.py --key C:\omx-testnet\builder_test.key --amount 110 --to-perp
     ```
3. **Base Sepolia 시험 USDC 받기**
   - https://faucet.circle.com 에서 네트워크를 Base Sepolia로 고르고 `<결제지갑주소>`를 넣는다.
   - 가스는 필요 없다. 결제는 서명만 하고 정산자가 가스를 낸다.
4. **설정·자격** (창 A)
   ```powershell
   cd C:\omx
   $py  = 'C:\omx\.venv\Scripts\python.exe'
   $cfg = 'C:\omx-testnet\omx.toml'
   & $py -B -m omx.cli --config $cfg init --account <메인지갑주소> --network testnet --key-path C:\omx-testnet\agent.key --enable-agent --builder-address <시험빌더주소> --fee-tenths-bps 10
   $t = (Get-Content $cfg) -replace '^min_day_ntl_usd = .*', 'min_day_ntl_usd = 0.0' -replace '^min_depth_usd = .*', 'min_depth_usd = 0.0'
   [IO.File]::WriteAllLines($cfg, $t)
   ```
   - `init`이 자격 질문까지 묻고 기록한다. 한국 개인이면 답은 `no`, `kr`, `yes`, Enter, `no`, `yes`다. 순서대로 법인 여부, 거주국, 지금 그 나라에 있는지, 국적(Enter면 거주국), VPN, 약관 동의다.
   - 따로 `eligibility`를 실행할 필요는 없다. 30일마다 다시 확인할 때만 쓴다.
4-1. **계정 유형 확인** (바꿀 필요 없음)
   - OMX는 통합 계정(unifiedAccount, 이용자 기본값)과 standard 계정(disabled / default)을 모두 지원한다.
   - 통합 계정이면 잔고를 현물 USDC `total`(spotClearinghouseState)에서 읽는다. 포지션은 퍼프 상태에서 읽는다.
   - `total`에 퍼프 미실현손익이 이미 들어 있는지는 HL 문서에 적혀 있지 않다. 그래서 확인 전까지는 두 후보 중 낮은 값을 쓰고 `EQUITY_BASIS_UNVERIFIED`를 표시한다(doctor·account 출력).
   - 포트폴리오 마진(portfolioMargin)·DEX abstraction이면 새 주문을 거부한다(`ACCOUNT_MODE_UNSUPPORTED`). 긴급 중지는 그대로 된다. 이때는 계정 유형을 Unified 또는 Manual / Standard로 바꾼다.
   - 선택: Manual / Standard로 바꿔도 된다(포트폴리오 화면의 "계정 유형", 지갑 서명). 그때는 현물 → 퍼프로 USDC를 옮긴다. HL 문서는 자동매매 이용자에게 standard를 권한다.
4-2. **잔고 기준 측정** (통합 계정일 때, 키 불필요·읽기 전용)
   - 테스트넷 화면에서 작은 포지션 하나를 연다(예: ETH 15 USD).
   - 창 A에서 실행한다. 30초 간격으로 10번 읽는다(약 5분).
     ```powershell
     & $py -B tools\measure_account_mode.py --config $cfg --samples 10 --interval 30
     ```
   - 마지막 `=== verdict ===` 줄이 `candidate A` 또는 `candidate B`면 그 출력 전체를 이슈에 붙인다. `INCONCLUSIVE`면 가격이 움직일 때 한 번 더 실행한다.
   - 이 도구는 설정의 계정 주소만 읽고 키 파일을 열지 않으며 서명·주문을 하지 않는다.
5. **빌더 수수료 승인** — 사용자 지갑 서명
   ```powershell
   & $py -B -m omx.cli --config $cfg approve-builder
   ```
   - 브라우저에 페이지가 열린다. 메인 지갑(MetaMask 등)을 연결하고 서명한다.
   - 1 bp = 체결 금액의 0.01 %이고, 철회는 `approve-builder --revoke`로 한다.
   - 페이지에 오류가 나면 그 문구를 그대로 이슈에 붙인다. 브라우저 직접 전송(CORS)이 막혔는지는 아직 확인되지 않았다.
6. **점검**
   ```powershell
   & $py -B -m omx.cli --config $cfg doctor
   ```
   - 모든 줄이 OK여야 한다. FAIL이면 그 출력만 이슈에 붙인다.
7. **데몬** (창 B, 시험 내내 열어 둔다)
   ```powershell
   cd C:\omx
   & 'C:\omx\.venv\Scripts\python.exe' -B -m omx.cli --config C:\omx-testnet\omx.toml daemon
   ```
8. **E2E 실행** (창 C)
   ```powershell
   cd C:\omx-pretrade
   .\.venv\Scripts\python.exe -B e2e\run_e2e.py --mode testnet --payer-key C:\omx-testnet\x402_payer_test.key --pay-to <시험빌더주소> --omx-config C:\omx-testnet\omx.toml --coin ETH --side buy --notional-usd 15
   ```
   - 끝나면 긴급 중지가 켜진 상태로 남는다.
   - 창 A에서 `& $py -B -m omx.cli --config $cfg unkill`을 실행하고 `I UNDERSTAND`를 입력한다.
   - 그다음 창 B에 뜬 `confirm 코드`를 입력하면 풀린다.
9. **결과 전달**
   - `C:\omx-pretrade\e2e\out\e2e_report_testnet.json` 경로만 알려 준다. 키는 들어 있지 않다.
   - 남은 포지션은 테스트넷 화면에서 닫거나, 런북의 reduce-only 매도로 닫는다.

## 통과 기준
| 확인 | 기대 |
|---|---|
| x402 결제 | Base Sepolia 거래 해시가 보고서에 있음, 결제 1회 |
| 견적 → 주문 | OMX 의도 id가 `ag-q-<quote_id>` |
| 빌더 | 모든 주문에 `b = 0xa678…37f2`, `f = 10`. 체결의 builderFee = 명목 × 0.01 % |
| 빌더 수입 | 테스트넷에서 빌더 주소의 추천(referral) 보상이 늘어남(화면 확인) |
| 중지·감사 | kill 뒤 대기 주문 0, audit verify ok |
