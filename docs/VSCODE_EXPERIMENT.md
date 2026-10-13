# Codex CLI를 이용한 VS Code 토큰 효율 실험

목표는 동일한 품질을 달성할 때 단일 모델보다 여러 모델의 역할 분담이 총 토큰을 줄이는지 확인하는 것이다.
회사 게이트웨이는 사용하지 않는다. 기존 지원 티켓 미션과 독립 품질 평가 기준을 그대로 사용한다.

## 비교에서 구분할 것

GitHub 이슈에 구현 지침을 쓰는 방법도 인수인계 방식이다. 로컬 파일로 옮기면 전송 도구 호출을 줄일 수 있으나,
동일한 지침을 수신 모델이 읽는 토큰은 여전히 든다. 파일에 썼다는 사실만으로 토큰이 줄어들지는 않는다.
같은 폴더의 CLI 프로세스도 대화 기록·내부 컨텍스트를 공유하지 않는다.

| 조건 | 변경점 | 판단할 질문 |
|---|---|---|
| single | 상위 모델 하나 | 기존 품질·토큰 기준선은 무엇인가? |
| single_mid | 구현 모델 하나 | 고급 설계 모델 없이도 품질을 달성하는가? |
| issue_handoff | 상위 모델 설계 → 이슈 → 구현 모델 → 검증 모델 | 기존 분업 방법은 얼마나 쓰는가? |
| local_handoff | 같은 모델·같은 지침을 파일로 전달 | 이슈 대신 파일을 써서 줄어드는 부분은 얼마인가? |
| local_packet | 작업 명세와 필요한 코드·실패 내역만 전달 | 전달 내용 축소가 추가로 도움이 되는가? |

처음에는 single, single_mid, local_packet을 seed 1~5에서 비교한다. 개선이 보이면 이슈·동일 지침 파일 전달 조건으로 원인을 분리한다.
single_mid가 가장 효율적이라면 분업 대신 중간 모델을 기본으로 사용하는 편이 낫다.
하나의 미션 결과로 모든 코드베이스에서의 최적 방식을 결론 내리지 않는다.

## 첫 실행: 터미널은 셋, 활성 모델은 하나

`terminal_relay.py`는 local_packet 조건의 실행 보조 도구다. Python 프로세스가 파일 상태를 확인하고,
자기 역할 차례에만 설정된 Codex CLI를 호출한다. 기다리는 동안에는 LLM 호출을 하지 않는다.
각 단계는 새 `codex exec` 세션을 사용한다. 장기 대화 세션과의 비교는 이후 별도 실험으로 한다.

흐름은 planner → implementer → reviewer다. 검증 실패 시 실패 내역을 구현 모델로 돌려보낸다.
초기 구현과 재시도 두 번까지 실패하면 `needs_redesign`으로 종료한다. 자동으로 고급 모델을 계속 호출하지 않는다.
검증 모델의 pass는 `ready_for_quality_evaluation`이며 독립 품질 평가 통과를 뜻하지 않는다.

공유 폴더 구조:

```text
token_saver/
  mission/MISSION.md
  data/
  solution.py                 # 구현 모델만 변경
  .coord/
    plan.md                   # 설계 모델의 짧은 작업 명세
    implementation.md         # 변경 파일·검증 결과·미해결 사항
    review.json               # pass/fail/blocked와 재현 가능한 실패
    state.json                # Python 실행기가 관리
    logs/                     # CLI별 원본 이벤트 로그
```

한 번에 하나의 모델만 실행하므로 코드 동시 수정과 테스트 중 파일 변경을 줄인다.
역할별 파일 수정 제한은 프롬프트와 협력 규약이며 OS 접근 제어가 아니다.
같은 폴더에서 구현 모델 여러 개를 동시에 실행하는 실험은 첫 비교에서 제외한다.
나중에 병렬화가 필요하면 worktree로 분리하고 합치는 비용까지 측정한다.

## VS Code에서 시작

Python 3.12와 Codex CLI를 준비하고 로그인은 사용자의 기존 CLI 설정을 사용한다.
이 환경에서 `codex-cli 0.159.2`의 help로 --model, --cd, exec --json,
--output-last-message, --output-schema와 stdin 프롬프트 지원을 확인했다.
사용자 컴퓨터에서도 `codex --version`과 `codex exec --help`로 같은 옵션이 있는지 확인한다.
model_id는 CLI에서 실제 선택 가능한 ID를 사용한다. 에스트라·소리·테라라는 별칭만으로 ID를 추측하지 않는다.

```bash
cp cli.models.example.json cli.models.json
# cli.models.json의 REPLACE_*를 실제 사용 가능한 모델 ID로 변경
python3 terminal_relay.py init --config cli.models.json
```

실제 모델 호출 전에 계측 실행을 시작한다. setup.json에는 client를 Codex CLI 버전으로,
모델 ID·추론 강도·base commit·캐시 정책·도구 권한을 실제 값으로 적는다.

```bash
python3 lab.py new-run --run runs/local-packet-1 --strategy local_packet \
  --seed 1 --setup setup.json --config experiments/vscode-cli.json
```

VS Code의 `Tasks: Run Task`에서 `token_saver: open three workers`를 실행하면 전용 터미널 셋을 연다.
또는 서로 다른 터미널에서 같은 폴더를 작업 위치로 하여 아래 명령을 각각 실행한다.

```bash
# 터미널 1
python3 terminal_relay.py worker --role planner
# 터미널 2
python3 terminal_relay.py worker --role implementer
# 터미널 3
python3 terminal_relay.py worker --role reviewer
```

`python3 terminal_relay.py status`로 진행 상태를 확인한다. CLI 이벤트는 .coord/logs에 수집한다.
blocked이면 로그와 원인을 확인한다. 오류나 과도하게 긴 인수인계 문서를 조용히 잘라서 다음 단계로 보내지 않는다.
중단 후에는 활성 CLI 프로세스가 종료됐는지 확인하고 새 실험 checkout에서 재시작한다.
상태 파일이나 lock을 임의로 지우고 기존 작업과 겹쳐 실행하지 않는다.

이 실행기는 OS 명령 목록(argv)을 shell 없이 실행한다. 지정한 CLI는 일반 파일·쉘 도구 권한을 사용한다.
Codex sandbox는 workspace-write로 두며 프롬프트의 역할 제한과 별개다. planner/reviewer의 소스 변경이 있었다면
해당 실행은 규약 위반으로 기록하고 순수 역할 분담 비교에서 제외한다.

## 인수인계 내용

planner는 목적·관련 파일·허용 변경·인터페이스/불변 조건·완료 조건·테스트 명령·모호한 부분만 작성한다.
구현 코드 전체나 장문의 설계 설명은 넘기지 않는다. 요구사항은 경로로 참조하고 필요할 때 읽게 한다.
implementer는 변경 파일·실제로 실행한 테스트와 결과·미해결 사항만 남긴다.
reviewer는 코드와 테스트를 확인한 뒤 재현 가능한 실패를 반환한다. 실행하지 않은 테스트를 통과라고 쓰지 않는다.

기본 결과 패킷 제한은 UTF-8 8 KiB다. 이는 운영상 길이 제한이며 토큰 수나 최적 길이라고 주장하지 않는다.
너무 짧아 인터페이스와 완료 조건이 빠지면 구현 재시도 비용이 늘 수 있다. 규칙을 바꾸면 모든 비교 실행을 다시 한다.
각 모델이 추가로 필요한 코드를 읽는 것을 무조건 막지는 않는다. 파일 읽기와 도구 출력도 전체 토큰에 반영된다.

## CLI 사용량 수집

`codex exec --json`의 turn.completed usage를 수집한다. 성공한 fresh exec의 최종 합계에서
input_tokens + output_tokens만 더하고 cached_input_tokens와 reasoning_output_tokens를 다시 더하지 않는다.
진단 항목은 별도로 기록한다. 현금 청구 정보가 없는 구독형 CLI는 비용을 unknown으로 표시한다.

공개 Codex 소스의 이벤트 프로세서는 thread total usage를 최종 이벤트에 옮긴다.
따라서 resume한 세션의 누적 합계를 새 호출 사용량으로 더하면 중복 집계할 수 있다.
이 도구는 fresh exec만 대상으로 하며 같은 thread ID를 다시 import하면 거절한다.
이벤트 스키마가 버전마다 달라질 수 있으므로 최초 실제 실행 로그에서 usage를 확인한다.

```bash
python3 lab.py import-codex --run runs/local-packet-1 \
  --events .coord/logs/001-planner.jsonl --model 실제설계모델ID --stage plan
python3 lab.py import-codex --run runs/local-packet-1 \
  --events .coord/logs/002-implementer.jsonl --model 실제구현모델ID --stage implement
python3 lab.py import-codex --run runs/local-packet-1 \
  --events .coord/logs/003-reviewer.jsonl --model 실제검증모델ID --stage test
```

재시도 로그도 모두 import한다. 단계별 파일 이름과 모델은 .coord/state.json의 history와 설정을 대조한다.
자동 사용량 import는 현재 제공하지 않으므로 독립 평가 전에 운영자가 모든 로그를 수집해야 한다.
완료 usage가 없는 실패·중단 로그는 missing으로 기록하여 절감률을 확정하지 않는다.
calls는 여기서 CLI 실행 수이며 내부 API 요청 수가 아니다. 기존 게이트웨이 실험과 이를 섞어 비교하지 않는다.
재사용/resume 기록은 이 importer에 넣지 말고 누적량의 차이를 검증하는 별도 계측을 먼저 준비한다.

single 기준선도 새 codex exec --json 실행과 같은 도구 권한으로 수집한다.
모델별 자동 서브 에이전트가 개입하면 이를 기록한다. 부모 usage가 자식 usage를 포함하는지 확인되지 않았다면
하위 로그를 임의로 더하거나 누락시키지 말고 해당 실행의 전체 계측을 미확정으로 둔다.

```bash
codex exec --json --model 실제상위모델ID --cd . --sandbox workspace-write \
  --output-last-message baseline-result.md "mission/MISSION.md에 따라 자료 분석부터 구현과 테스트까지 수행하라." > baseline.jsonl
```

최종 quality 평가와 compare 절차는 기존 lab.py와 동일하다. 평가 코드는 개발 모델이 접근하지 못하는 운영자 환경에 둔다.
실제 모델 호출 이전에는 절감률을 만들지 않는다. 현재 도구의 모의 CLI 테스트는 실행 흐름 검증이며 모델 효율 실험이 아니다.

## 결과에 따라 바꿀 방향

- local_handoff와 issue_handoff가 비슷하면 전달 매체보다 재분석·컨텍스트 중복을 먼저 줄인다.
- local_packet의 구현 재시도가 늘면 명세가 부족한지 확인하고 설계 깊이와 전달 범위를 조정한다.
- single_mid가 가장 효율적이면 구현 모델을 기본으로 두고 어려운 실패 때만 상위 모델을 호출한다.
- reviewer 토큰이 큰데 실제 결함 발견이 적으면 자동 테스트를 먼저 실행하고 실패·위험 변경만 모델에 검토시킨다.
- 여러 역할이 저장소 전체를 반복 읽으면 작은 코드 인덱스와 파일 범위를 사용한다. 인덱스 생성 토큰도 집계한다.
- 모든 분업 조건이 single보다 나쁘면 간단한 작업은 단일 모델로 처리하고 복잡한 변경만 분리한다.

참고한 공개 이벤트 소스:
https://github.com/openai/codex/blob/main/codex-rs/exec/src/exec_events.rs
https://github.com/openai/codex/blob/main/codex-rs/exec/src/event_processor_with_jsonl_output.rs
조사 시 blob SHA: 06565c097b28d3bf2ad592943cc30c776b45e236 / 73e0c9d911c82aae67c44e6b1a4ce54c7092b6be.
