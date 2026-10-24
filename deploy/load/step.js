// 공급 실측 한 계단 (#11 측정 조건): 초당 RATE건을 STEP_SECONDS초 동안, / · /api/trending 반반.
// User-Agent는 k6 기본값 (지표에서는 source="user"). 측정 창은 #11에 기록해 #12 · SLI 보고에서 뺀다
import http from 'k6/http';
import exec from 'k6/execution';

const RATE = Number(__ENV.RATE);
const PATHS = ['/', '/api/trending'];

export const options = {
  discardResponseBodies: true,
  summaryTrendStats: ['med', 'p(95)'],
  scenarios: {
    step: {
      executor: 'constant-arrival-rate', rate: RATE, timeUnit: '1s', duration: `${__ENV.STEP_SECONDS}s`,
      // 3초 넘게 밀리면 VU가 모자라 못 보낸 요청(dropped_iterations)이 생긴다 → 오류로 센다
      preAllocatedVUs: RATE, maxVUs: RATE * 3,
    },
  },
};

export default function () {
  http.get(__ENV.TARGET + PATHS[exec.scenario.iterationInTest % 2], { timeout: '10s' });
}

// ladder.sh가 읽는 한 줄: p50 p95(ms) 보낸 수 실패 수 못 보낸 수 (실패 = 연결 실패 · 10초 초과 · 4xx/5xx)
export function handleSummary(data) {
  const v = (name, key) => (data.metrics[name] ? data.metrics[name].values[key] : 0);
  const line = [v('http_req_duration', 'med'), v('http_req_duration', 'p(95)'), v('http_reqs', 'count'),
    v('http_req_failed', 'passes'), v('dropped_iterations', 'count')];
  return { [__ENV.OUT]: line.join(' ') + '\n' };
}
