# 지원 티켓 분석 도구 구현 미션 v1

자료를 읽고 팀별 SLA 분석과 원본 행 근거를 제공하는 재현 가능한 Python CLI를 구현한다.
외부 라이브러리와 네트워크 없이 Python 3.12에서 동작해야 한다.

실행 인터페이스:

```bash
python solution.py --tickets tickets.csv --policies policies.json --out output
```

출력은 `output/report.json`이다. 입력 파일을 수정하지 않는다. 같은 입력에 같은 결과를 내며,
잘못된 행이 섞여 있어도 유효한 행의 분석을 끝낸다. 잘못된 정책 파일은 종료 코드 2를 반환한다.

## 입력

CSV는 UTF-8이며 다음 헤더를 가진다.

`ticket_id,revision,team,priority,opened_at,first_response_at,subject`

CSV 레코드 번호는 헤더를 1로 하여 2부터 시작한다. 따옴표 안 줄바꿈도 레코드 하나다.
`ticket_id`, `team`은 앞뒤 공백을 제거하며 빈 값은 오류다. `revision`은 0 이상의 정수다.
priority는 정책에 정의된 값을 사용하며 대소문자를 구분한다. 빈 응답 시각은 미응답이다.
시각은 시간대 오프셋이 있는 ISO 8601이며 `Z`도 허용한다. 계산은 UTC로 한다.

정책 예:

```json
{"as_of":"2026-01-03T00:00:00Z","sla_hours":{"p1":1,"p2":4,"p3":24}}
```

`as_of`는 시간대가 있는 시각, `sla_hours`는 비어 있지 않은 객체이며 값은 유한한 양수다.
숫자 문자열과 boolean은 정책 값으로 허용하지 않는다. 이 조건을 어기면 종료 코드 2다.

## 정제·계산 규칙

각 행을 먼저 검증하고 유효한 행끼리 중복을 제거한다. 같은 ID는 revision이 가장 큰 행을 선택한다.
revision도 같으면 입력에서 나중에 나온 행을 선택한다. 유효하지 않은 최신 행은 이전 유효한 행을 대체하지 않는다.

행 오류는 다음 순서에서 **최초 하나만** 기록한다.

1. 빈 ID: `EMPTY_ID`
2. revision이 정수가 아니거나 음수: `BAD_REVISION`
3. 빈 team: `EMPTY_TEAM`
4. 정의되지 않은 priority: `BAD_PRIORITY`
5. opened_at이 없거나 해석 불가 또는 시간대 없음: `BAD_OPENED`
6. opened_at > as_of: `FUTURE_OPENED`
7. 비어 있지 않은 first_response_at이 해석 불가 또는 시간대 없음: `BAD_RESPONSE`
8. 응답이 시작 시각보다 이름: `RESPONSE_BEFORE_OPENED`
9. 응답이 as_of보다 늦음: `FUTURE_RESPONSE`

응답한 티켓은 `(응답 시각 - 시작 시각) <= SLA`이면 `met`, 초과하면 `breached`다.
미응답은 `(as_of - 시작 시각) > SLA`이면 `breached`, 그렇지 않으면 `pending`이다.
정확히 경계에 있는 응답은 준수이며, 정확히 경계에 있는 미응답은 pending이다.
응답 시간은 분 단위 숫자로 기록하고 미응답은 null이다. 소수 6자리까지 반올림한다.

## 출력 계약

다음 예시는 구조만 설명한다. 숫자는 실제 입력에서 계산한다.

```json
{
  "summary": {"total":1,"answered":1,"pending":0,"sla_met":1,"sla_breached":0,"pending_within_sla":0},
  "teams": [{"team":"Support","total":1,"answered":1,"pending":0,"sla_met":1,"sla_breached":0,"pending_within_sla":0}],
  "tickets": [{"ticket_id":"T001","team":"Support","priority":"p1","revision":2,"source_row":3,"first_response_minutes":30.0,"sla_status":"met"}],
  "errors": [{"row":4,"ticket_id":"T002","code":"BAD_PRIORITY"}]
}
```

summary와 각 team의 pending은 **모든 미응답 수**다. 따라서 overdue 미응답은 pending과 sla_breached에 모두 포함된다.
`total = answered + pending = sla_met + sla_breached + pending_within_sla`다.
teams는 team, tickets는 ticket_id, errors는 row 기준 오름차순이다. 같은 대소문자 정렬은 Python 문자열 순서다.
summary는 데이터가 없을 때도 모든 키를 0으로 가진다. teams/tickets/errors는 빈 배열일 수 있다.

## 완료 조건

분석 로직·CLI·사용 설명·검증 테스트를 제출한다. 기능 평가 100점 중 90점 이상이며,
집계·시간대·SLA 경계·근거·오류 처리·재현성 필수 항목을 모두 통과해야 한다.
실험 담당자가 별도 데이터로 평가한다. 점수만 높이기 위한 정답 하드코딩은 금지한다.
