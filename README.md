# token_saver

**동일한 개발 품질을 달성할 때까지의 누적 토큰을 줄이는 방법을 실험한다.**
현재 연구 대상은 **VS Code에서 사용하는 Codex CLI와 Codex 모델들**이다. 회사 게이트웨이 연결은 뒤로 미뤘다.
여러 터미널에서 같은 폴더를 사용하며 파일로 작업을 넘기는 실행 절차는 [VS Code 실험 안내](docs/VSCODE_EXPERIMENT.md)에 있다.
대기 중 LLM을 호출하지 않는 `terminal_relay.py`, VS Code 터미널 tasks, Codex JSONL 사용량 importer를 제공한다.
실험 미션은 [지원 티켓 분석 CLI 구현](mission/MISSION.md)이다. 자료 분석부터 구현·테스트·오류 수정까지 수행하여,
고정된 품질 게이트를 통과하는 완성된 결과물을 제출한다. 이 저장소는 출발점이며 정답 구현은 포함하지 않는다.

| 비교 조건 | 방식 |
|---|---|
| single | 상위 모델 하나로 설계·구현·검증 분석 수행 |
| routing | 설계·구현·검증 분석에 서로 다른 모델 배정 |
| routing_context | routing + 짧은 작업 명세·관련 코드·실패 차이만 전달 |

주 지표는 **T_pass: 첫 품질 통과 시점까지 모든 모델 호출의 입력+출력 토큰 합계**다.
기준선 대비 절감률은 `1 - T_pass(비교 방식) / T_pass(single)`로 계산한다.
설계·분석·요약·분류·재시도·서브 에이전트·LLM 검증 토큰을 모두 포함한다.
실패 실행도 집계하고 성공률·완료 시간·현금 비용을 함께 보고한다. 실제 절감률은 아직 측정하지 않았다.

## 미션 시작

`mission/MISSION.md`, `data/tickets.csv`, `data/policies.json`을 읽고 Python 3.12 표준 라이브러리만 사용하는 `solution.py`를 구현한다.

```bash
python solution.py --tickets data/tickets.csv --policies data/policies.json --out output
```

`output/report.json`에 팀별 SLA 통계와 원본 행 근거를 제공한다. README와 테스트도 제출한다.
평가자는 별도 데이터로 100점 중 90점 이상 및 집계·중복·시간대·SLA 경계·근거·오류 처리·정책·재현성 필수 항목을 확인한다.
평가 데이터와 정답 계산 모듈은 개발 모델이 접근하지 못하는 운영자 환경에 보관한다.

## 사용량 기록

이 저장소의 `lab.py`는 회사 게이트웨이 또는 코딩 도구가 제공하는 usage를 기록·비교한다.
모델 호출과 자동 라우팅은 실행기나 게이트웨이에서 수행한다. 계측 CLI만으로 라우팅이 구현되지는 않는다.
현재 제공하는 실행기는 planner → implementer → reviewer의 정해진 순서로 Codex CLI를 호출한다.
난이도를 자동 판정해 모델을 선택하는 라우터는 아니다.

`setup.example.json`을 복사하고 모델 ID·버전·base commit·캐시 정책·도구 정보를 실제 값으로 고정한다.
API 키 등 비밀값을 기록하지 않는다. `experiment.json`의 역할과 실행 한도도 실험 전에 확정한다.

```bash
cp setup.example.json setup.json
python lab.py new-run --run runs/single-1 --strategy single --seed 1 --setup setup.json
python lab.py record --run runs/single-1 --provider openai --usage usage.json \
  --request-id gw-001 --model actual-model-id --stage plan --outcome success
```

`usage.json`은 호출의 최종 usage 객체다. 지원 형식은 OpenAI Responses·Chat Completions,
Anthropic 및 회사 정규화 형식이다. 예시는 `schemas/usage.example.json`에 있다.
실패 요청도 기록하며 usage를 모르면 `--provider missing`을 사용한다. 누락이 있으면 T_pass를 확정하지 않는다.
스트리밍 중간 사용량을 반복 합산하지 않는다. 동일 request_id 중복 기록은 거절한다.

운영자 전용 평가 모듈을 보유한 담당자가 다음을 실행한다. 이 공개 저장소에는 정답을 숨기기 위해 evaluator.py를 넣지 않았다.
평가 모듈은 `evaluate(candidate, seed, timeout)` 함수로 score, checks, critical_passed, candidate_sha256을 반환하는 모듈이다.
현재 운영자 패키지는 별도로 준비되어 있으며, 평가 데이터 생성과 전체 검증 테스트를 포함한다.

```bash
python lab.py evaluate --run runs/single-1 --candidate /submission/solution.py \
  --evaluator /operator/evaluator.py --submission-accepted
python lab.py compare --runs runs --out comparison.json
python -m unittest discover -s tests -v
```

`--submission-accepted`는 담당자가 사용 설명·테스트·하드코딩 없는 제출을 검토했음을 뜻한다.
평가는 Python subprocess로 실행하며, 신뢰할 수 없는 제출은 별도 격리 환경에서 실행한다.
`prepare` 명령은 운영자 패키지가 있는 환경에서만 추가 반복용 자료를 생성할 수 있다.

## 실험 규칙

동일한 base commit·클라이언트·도구 권한·캐시 정책·시간 제한으로 시작한다.
seed별로 세 조건을 모두 수행하고 실행 순서를 섞는다. 이전 실행의 코드·계획은 재사용하지 않는다.
처음 5쌍은 탐색 실험이며, 선택한 방식을 새 seed 10쌍 이상으로 재확인한다.
성공한 양쪽 실행의 paired 절감률과 전체 성공률을 함께 비교한다.

`compare`는 중앙 T_pass, 성공률, paired 절감률 중앙값, **실패까지 포함한 소비 토큰 / 성공 실행 수**를 출력한다.
서로 다른 base commit·도구 설정은 섞어 비교하지 않는다. 모든 호출 usage가 확보되어야 확정 지표가 된다.
계측 값은 운영자가 수입한 기록이므로 실제 판단 전 게이트웨이 원본 로그와 대조한다.
공급자별 tokenizer가 다르므로 토큰 수와 비용은 별도 지표다.

한도: 90분, 100만 토큰, 150호출, 평가 10회. 평가 시 한도를 확인하고 실시간 중단은 실행기에서 처리한다.
사람이 코드를 수정한 실행은 순수 자동 개발 비교에서 제외한다. 미션 종료는 품질 통과이며 이후 업로드는 별도 운영 비용이다.
목표는 이 조건에서 효율이 좋은 방식을 찾는 것이며 모든 프로젝트에서의 최소 토큰을 증명하는 것은 아니다.
