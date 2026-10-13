# 로컬 PC에서 Codex 토큰 비교 실행

이 문서는 운영자 실행 패키지 기준이다. 공개 GitHub 저장소에는 정답 계산 모듈 `evaluator.py`를 넣지 않았다.
패키지를 별도 폴더에 풀고 VS Code 터미널에서 실행한다. 모델에는 실행기가 만든 `workspaces/조건-번호/`만 작업 폴더로 제공한다.
게이트웨이 주소나 API 키를 입력할 필요 없이 로컬 Codex의 기존 인증을 사용한다.

## 1. 준비

Python 3.12 이상, Git, Codex CLI가 같은 터미널의 PATH에 있어야 한다. macOS/Linux 터미널 또는 Windows의 WSL 터미널을 사용한다.
이번 검증 환경은 Linux, Python 3.12, Codex CLI 0.159.2였다. Windows 네이티브 실행은 검증하지 않았다.

```bash
cd token-efficiency-lab
python3 --version
git --version
codex --version
codex login status
```

로그인되지 않았다면 이 PC 터미널에서 `codex login`을 실행한다. 클라우드 환경에 제시됐던 로그인 코드는 사용하지 않는다.
`login status`가 성공해도 실제 호출이 만료된 인증으로 실패할 수 있으므로, 실행기는 각 모델에 짧은 실제 호출을 먼저 보낸다.
기존 전역 Codex 지침과 설정은 이어받는다. 실험 중 CLI 버전, 설정, 전역 지침을 바꾸지 않는다.

## 2. 첫 실행: 세 조건을 한 번씩

```bash
python3 run_actual_study.py --out ../token-study-pilot --repetitions 1
```

하나의 명령이 모델 연결 확인 → 세 조건의 개발 → 독립 자동 평가 → 사용량 비교를 실행한다.
모델 호출이 실제로 이루어지며 로컬 계정의 사용량에 반영된다. 터미널을 열어 둔다.
완료된 단계를 다시 실행해도 덮어쓰지 않는다. 미완료 실행이 남아 있으면 원본 로그를 보존하고 점검을 요청한다.

| 조건 | 기본 모델 | 작업 |
|---|---|---|
| `single` | `gpt-6-astra` | 전체 개발 |
| `single_mid` | `gpt-6.1-sol` | 전체 개발 |
| `local_packet` | Astra → Sol → `gpt-6-luna` | 설계 → 구현 → 리뷰, 실패 시 구현으로 반환 |

위 ID는 클라우드 Codex의 실제 모델 목록에서 확인했다. 로컬 계정의 사용 가능 여부는 사전 호출로 별도 확인한다.
분업 조건은 각 역할에 새 CLI 프로세스를 할당하고 같은 작업 폴더의 파일을 전달한다.
조건 간에는 별도 작업 폴더와 새 세션을 사용한다. 이 명령은 여러 터미널의 순차 인계를 자동화하며, 동시 구현 효과는 측정하지 않는다.

모델이 지원되지 않으면 로컬 `codex`의 `/model`에서 실제 ID를 확인한 뒤 `study.models.example.json`을 복사해 수정한다.
상위 단독 모델과 planner, 구현 단독 모델과 implementer는 각각 같아야 한다. 조용히 다른 모델로 대체하지 않는다.

```bash
cp study.models.example.json local.models.json
# local.models.json의 다섯 ID를 로컬에서 지원하는 값으로 수정
python3 run_actual_study.py --out ../token-study-local-models --repetitions 1 --models local.models.json
```

모델·반복 수·실행기·평가기·CLI 버전을 바꿨으면 새 출력 폴더를 사용한다.
실험용 출력 폴더는 실행기가 새로 만들므로 미리 만들지 않는다.

## 3. 반복 측정과 결과 전달

파일럿은 실행 확인이며 승자를 고르는 근거로 충분하지 않다. 조건별 5회 탐색 비교는 새 폴더에서 실행한다.

```bash
python3 run_actual_study.py --out ../token-study-5 --repetitions 5
```

사용자 지정 모델을 썼다면 위 명령에도 같은 `--models local.models.json`을 붙인다.
표본마다 조건 순서를 섞으며, 모델의 난수 seed를 제어한다는 뜻은 아니다. 같은 번호끼리 평가 데이터 seed를 맞춘다.

| 결과 파일 | 내용 |
|---|---|
| `results-summary.json` | 공유할 요약: 모델, 성공률, 조건별 누적 토큰, 완료 시간, 단계별 사용량 |
| `comparison.json` | 전체 비교 원장; 통과까지 토큰 중앙값, 짝지은 절감률, 실패 포함 성공당 소비 토큰 |
| `protocol.json` | 고정 모델·순서·한도·버전·코드 해시 |
| `preflight/checks.jsonl` | 준비용 실제 호출의 사용량과 실패 이력, 본 실험과 별도 집계 |
| `runs/*/logs/` | 각 CLI 원본 JSONL·오류 로그·프롬프트·사용량 요약 |
| `runs/*/evaluations.json` | 독립 기능 점수와 그 시점까지 사용량 |
| `workspaces/*/` | 각 조건이 만든 실제 코드·README·테스트 |

먼저 `results-summary.json`을 대화에 첨부하면 수치를 분석할 수 있다. 품질 결론을 확정하려면 생성 코드도 검토한다.
요약 파일에는 인증 값, 원문 대화, CLI 오류 로그, 로컬 홈 경로를 포함하지 않는다.

## 측정 기준과 해석

주 지표는 첫 **자동 품질 게이트** 통과까지의 입력+출력 토큰이다. 설계·구현·리뷰·재시도·도구 결과를 처리하는 호출을 포함한다.
캐시·추론 토큰은 합계의 부분집합이므로 다시 더하지 않는다. 단위 `calls`는 API 요청 수가 아니라 새 CLI 스레드 수다.
인증·준비용 모델 확인·실험 도구 제작·결과 해석은 개발 지표에서 제외하며, 준비용 호출 사용량은 따로 보관한다.
원본 usage가 없으면 미계측이며 0토큰 성공이나 확정 절감률로 처리하지 않는다. 청구액은 CLI에서 얻지 못하므로 비용은 미확정이다.

자동 게이트는 기능 90점 이상, 모든 필수 항목, 비어 있지 않은 README, 통과하는 unittest를 요구한다.
보고서의 `passed`/`accepted`는 이 자동 게이트의 잠정 결과다. 모델 리뷰의 `pass`만으로 통과하지 않는다.
소스의 하드코딩, 평가 코드 접근, 금지된 역할의 수정 여부를 검토하기 전에는 최종 품질 통과나 우승을 확정하지 않는다.
개발 모델에는 정답을 전달하지 않지만 같은 OS 계정에서 읽기까지 완전히 격리하지는 않는다. 작업 범위 지침과 사후 소스 검토를 사용한다.

단독 조건도 분업 조건도 구현 기회는 최대 3회다. 호출당 15분 제한, 평가 시 실행당 90분·100만 토큰 한도를 확인한다.
전체 연구는 완료된 실행 사이에서 누적 100만 토큰을 확인하고 다음 실행을 시작하지 않는다. 호출 중 실시간 토큰 차단은 아니므로 초과할 수 있다.
한도 때문에 5회가 다 끝나지 않았다면 완료된 반복 수를 그대로 보고하며 5회 결과로 표현하지 않는다.

중단된 요청도 원본 로그와 미계측 상태를 남긴다. 인증/네트워크/한도 오류 후 부분 실행을 삭제해서 성공한 결과만 비교하지 않는다.
`blocked.json`이 있으면 해당 실행과 로그를 점검한다. 자동 재실행으로 비용과 시간을 숨기지 않는다.

## 실행하지 않고 준비만 확인

```bash
# 모델 호출 없음: 컨트롤러 테스트는 모의 CLI를 사용하며 절감률 실험이 아님
python3 -m unittest discover -s tests -v
python3 run_actual_study.py --out ../token-study-prepared --repetitions 1 --prepare-only
# 짧은 실제 모델 확인만 하고 개발을 시작하지 않음
python3 run_actual_study.py --out ../token-study-prepared --repetitions 1 --check
```

공개 저장소의 실행기만 사용한다면 운영자 패키지의 평가기를 `--evaluator /별도경로/evaluator.py`로 지정한다.
운영자 패키지에는 기본 경로에 평가기가 있으므로 이 옵션이 필요 없다.
