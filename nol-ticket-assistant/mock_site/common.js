// 모의 사이트 공통: 시나리오와 진행 상태는 localStorage 로 팝업 창과 공유한다.
const MOCK = {
  scenario() { return localStorage.getItem('mock.scenario') || 'normal'; },
  get(k) { return JSON.parse(localStorage.getItem('mock.' + k) || 'null'); },
  set(k, v) { localStorage.setItem('mock.' + k, JSON.stringify(v)); },
  PRICES: { VIP: 170000, R: 140000, S: 110000 },
};
