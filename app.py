"""
DART 재무제표 분석기 - 통합 백엔드 서버
- DART OpenAPI: 재무제표 5종
- 한국투자증권 KIS: 실시간 주가/시가총액/PER + 과거 차트 데이터
- 하이브리드 매크로 API: 국내(네이버 실시간) + 글로벌(야후 파이낸스 실시간) 통합
- 네이버 검색: 뉴스
- Gemini AI: 퀀트 투자 자문 리포트 생성 (가시성 복원, CSS 격리, 에러 방어 완벽 적용)
"""
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from concurrent.futures import ThreadPoolExecutor
import urllib.request
import urllib.parse
import json
import os
import zipfile
import xml.etree.ElementTree as ET
import io
import time
import threading
import webbrowser
import re
import traceback
from datetime import datetime, timedelta

# ─────────────────────────────────────────
# API 키 설정 (배포 환경변수에서 로드 — 소스코드에 절대 하드코딩하지 않음)
# ─────────────────────────────────────────
DART_API_KEY     = os.environ.get('DART_API_KEY', '')
KIS_APP_KEY      = os.environ.get('KIS_APP_KEY', '')
KIS_APP_SECRET   = os.environ.get('KIS_APP_SECRET', '')
KIS_BASE_URL     = 'https://openapi.koreainvestment.com:9443'
NAVER_CLIENT_ID  = os.environ.get('NAVER_CLIENT_ID', '')
NAVER_CLIENT_SECRET = os.environ.get('NAVER_CLIENT_SECRET', '')
GEMINI_API_KEY   = os.environ.get('GEMINI_API_KEY', '')

# 필수 키 누락 시 서버 시작 시점에 명확히 경고 (배포 환경변수 설정 실수 방지)
_MISSING_KEYS = [k for k, v in {
    'DART_API_KEY': DART_API_KEY, 'KIS_APP_KEY': KIS_APP_KEY,
    'KIS_APP_SECRET': KIS_APP_SECRET, 'NAVER_CLIENT_ID': NAVER_CLIENT_ID,
    'NAVER_CLIENT_SECRET': NAVER_CLIENT_SECRET, 'GEMINI_API_KEY': GEMINI_API_KEY,
}.items() if not v]
if _MISSING_KEYS:
    print(f"[경고] 다음 환경변수가 설정되지 않았습니다: {', '.join(_MISSING_KEYS)}")
    print("       해당 기능은 배포 환경(Railway 등)에서 오류를 반환합니다.\n")

# ─────────────────────────────────────────
# 포트폴리오 공개용 방어 로직 (간단 캐시 + 요청 제한)
# 다수의 방문자가 동시에 클릭해도 외부 API(KIS/Gemini/DART) 호출량이
# 폭증하지 않도록 메모리 캐시와 IP당 요청 제한을 둔다.
# ─────────────────────────────────────────
_CACHE = {}
_CACHE_TTL_SEC = 600  # 10분 캐시: 같은 요청은 10분 내 재호출하지 않음

def _cache_get(key):
    hit = _CACHE.get(key)
    if hit and (time.time() - hit[0]) < _CACHE_TTL_SEC:
        return hit[1]
    return None

def _cache_set(key, value):
    _CACHE[key] = (time.time(), value)

_RATE_LIMIT = {}
_RATE_LIMIT_WINDOW_SEC = 60
_RATE_LIMIT_MAX_REQ = 20  # IP당 분당 20회 (외부 API 호출 엔드포인트 기준)

def _rate_limited(ip):
    now = time.time()
    hits = [t for t in _RATE_LIMIT.get(ip, []) if now - t < _RATE_LIMIT_WINDOW_SEC]
    hits.append(now)
    _RATE_LIMIT[ip] = hits
    return len(hits) > _RATE_LIMIT_MAX_REQ

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ─────────────────────────────────────────
# 프론트엔드 HTML (가시성 복구 및 CSS 충돌 제거)
# ─────────────────────────────────────────
INDEX_HTML = r'''<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DART 재무제표 분석기 · PRO</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<script src="https://cdn.jsdelivr.net/npm/html2pdf.js@0.10.1/dist/html2pdf.bundle.min.js"></script>
<link href="https://fonts.googleapis.com/css2?family=Gowun+Batang:wght@400;700&family=Noto+Sans+KR:wght@300;400;500;700;900&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@300;400;500;700;900&family=JetBrains+Mono:wght@400;700&display=swap');
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Noto Sans KR',sans-serif;background:#0f0f17;color:#e4e4e7;font-size:13px;min-height:100vh; overflow-x: hidden;}

/* --- 상단 타이틀 및 컨트롤 바 --- */
.title-bar{background:#0a0a13;border-bottom:1px solid #27272a;padding:8px 16px;display:flex;align-items:center;justify-content:center;position:relative}
.title-bar .dot{width:12px;height:12px;border-radius:50%;background:#ef4444;position:absolute;left:20px;top:50%;transform:translateY(-50%);box-shadow:0 0 8px rgba(239,68,68,.4)}
.title-bar .title-text{font-size:13px;color:#a1a1aa;font-weight:700;letter-spacing:.5px}
.title-bar .phase-badge{margin-left:10px;background:#10b981;color:#000;font-weight:800;font-size:10px;padding:2px 6px;border-radius:4px;}

.ctrl-bar{background:#18181b;border-bottom:1px solid #27272a;padding:10px 16px;display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.ctrl-left{display:flex;align-items:center;gap:12px;flex-wrap:wrap;flex:1}
.ctrl-group{display:flex;align-items:center;gap:6px}
.ctrl-label{font-size:11px;color:#71717a;white-space:nowrap;font-weight:700}
input[type=text],select{background:#0f0f17;border:1px solid #3f3f46;color:#e4e4e7;padding:4px 10px;border-radius:5px;font-family:'Noto Sans KR',sans-serif;font-size:12px;outline:none;height:30px;transition:border-color .2s}
input[type=text]:focus,select:focus{border-color:#3b82f6}
input[type=text]{width:150px}

.btn{height:30px;padding:0 14px;border-radius:5px;border:1px solid #3f3f46;background:#27272a;color:#e4e4e7;font-family:'Noto Sans KR',sans-serif;font-size:12px;font-weight:700;cursor:pointer;white-space:nowrap;transition:all .2s}
.btn:hover{background:#3f3f46;border-color:#52525b}
.btn.primary{background:#3b82f6;color:#fff;border-color:#3b82f6;}
.btn.primary:hover{background:#2563eb}
.btn.dash-btn{background:rgba(16,185,129,0.1); border-color:#10b981; color:#10b981;}
.btn.dash-btn:hover{background:#10b981; color:#000;}
.btn.ai-btn {background: linear-gradient(135deg, #6366f1, #a855f7); border:none; color:white; border-radius:6px;}
.btn.ai-btn:hover {opacity:0.9; transform:translateY(-1px); box-shadow:0 4px 12px rgba(168,85,247,0.3);}
.btn:disabled {opacity:0.4; cursor:not-allowed; transform:none !important; box-shadow:none !important;}

/* 토글 스위치 */
.toggle-wrap{display:flex;align-items:center;gap:6px}
.toggle-label{font-size:11px;color:#a1a1aa;font-weight:500;cursor:pointer;user-select:none;}
.toggle{appearance:none;width:32px;height:18px;background:#3f3f46;border-radius:10px;cursor:pointer;position:relative;transition:background .2s;}
.toggle:checked{background:#3b82f6}
.toggle::after{content:'';position:absolute;top:3px;left:3px;width:12px;height:12px;border-radius:50%;background:#fff;transition:left .2s}
.toggle:checked::after{left:17px}

/* 주식 정보 패널 */
.stock-panel{display:flex;align-items:center;gap:16px;padding:6px 12px;background:#0a0a13;border:1px solid #27272a;border-radius:6px;min-height:44px;flex-shrink:0;}
.stock-cell{display:flex;flex-direction:column;gap:2px;min-width:75px}
.stock-cell .label{font-size:10px;color:#71717a;font-weight:700;letter-spacing:0.5px;}
.stock-cell .val{font-size:14px;font-weight:800;color:#e4e4e7;font-family:'JetBrains Mono',monospace}
.stock-cell .val.up{color:#ef4444}
.stock-cell .val.dn{color:#3b82f6}
.stock-cell .sub{font-size:10px;color:#a1a1aa;font-family:'JetBrains Mono',monospace}
.stock-cell .sub.up{color:#ef4444}
.stock-cell .sub.dn{color:#3b82f6}

/* 탭 UI */
.tabs{background:#18181b;border-bottom:1px solid #27272a;display:flex;padding:0 16px; overflow-x: auto;}
.tab{padding:10px 20px;font-size:13px;font-weight:500;color:#71717a;border-bottom:3px solid transparent;cursor:pointer;transition:all .2s;white-space:nowrap}
.tab:hover{color:#e4e4e7;background:rgba(255,255,255,0.02);}
.tab.active{color:#3b82f6;border-bottom-color:#3b82f6;font-weight:800;}

/* 메인 영역 및 공통 텍스트 */
.main{padding:16px; max-width: 1300px; margin: 0 auto;}
.empty{text-align:center;padding:100px 20px;color:#71717a;font-size:14px;line-height:2;}
.empty strong{color:#e4e4e7; font-size:16px; display:block; margin-bottom:10px; font-weight:800;}
.spinner-wrap{display:flex;align-items:center;justify-content:center;gap:12px;padding:80px 20px;color:#a1a1aa;font-size:14px; font-weight:700;}
.spinner{width:24px;height:24px;border:3px solid #27272a;border-top-color:#3b82f6;border-radius:50%;animation:sp .7s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
.err{background:rgba(239,68,68,.08);border:1px solid #ef4444;color:#fca5a5;padding:16px 20px;border-radius:6px;margin:16px 0;font-size:13px;line-height:1.7; font-weight:500;}

/* 타이틀 장식 */
.sec-title{padding:8px 6px;font-size:14px;font-weight:800;color:#e4e4e7;letter-spacing:.5px;display:flex;align-items:center;gap:10px; margin-top:32px; margin-bottom:12px;}
.sec-title::before{content:'';width:4px;height:14px;background:#3b82f6;border-radius:2px}

/* --- 검색 드롭다운 --- */
.search-wrap{position:relative}
.dropdown{position:absolute;top:38px;left:0;background:#18181b;border:1px solid #3f3f46;border-radius:6px;min-width:320px;max-height:300px;overflow-y:auto;z-index:999;box-shadow:0 12px 36px rgba(0,0,0,.7);display:none}
.dd-item{padding:12px 16px;cursor:pointer;display:flex;gap:12px;align-items:center;font-size:12px;border-bottom:1px solid #27272a;transition:background .1s}
.dd-item:hover{background:#27272a}
.dd-code{font-size:11px;color:#a1a1aa;font-family:'JetBrains Mono',monospace;min-width:60px; font-weight:700;}
.dd-name{flex:1;color:#e4e4e7; font-weight:700;}
.dd-corp{font-size:10px;color:#52525b;font-family:'JetBrains Mono',monospace}

/* --- (격리) 재무제표 테이블 기본 UI --- */
.tbl-wrap{overflow-x:auto;margin-bottom:16px;border:1px solid #27272a;border-radius:8px;background:#0a0a13; box-shadow:0 4px 12px rgba(0,0,0,0.15);}
.tbl-wrap table{width:100%;border-collapse:collapse;font-size:13px}
.tbl-wrap thead th{padding:10px 16px;text-align:right;color:#a1a1aa;font-weight:700;font-size:12px;border-bottom:1px solid #27272a;white-space:nowrap;background:#18181b;letter-spacing:.5px;}
.tbl-wrap thead th:first-child{text-align:left;min-width:200px;position:sticky;left:0;background:#18181b;z-index:2}
.tbl-wrap thead th.sum-col{background:#1f1f23;color:#e4e4e7;border-left:1px solid #3f3f46}
.tbl-wrap tbody tr{border-bottom:1px solid #18181b;transition:background .1s}
.tbl-wrap tbody tr:hover td{background:#1f1f23 !important}
.tbl-wrap tbody tr:last-child{border-bottom:none}
.tbl-wrap td{padding:8px 16px;text-align:right;white-space:nowrap;font-family:'JetBrains Mono',monospace; color:#d4d4d8;}
.tbl-wrap td:first-child{text-align:left;position:sticky;left:0;background:#0a0a13;z-index:1;font-family:'Noto Sans KR',sans-serif; color:#a1a1aa; font-weight:500;}
.tbl-wrap td.sum-col{background:#15151a;border-left:1px solid #3f3f46;font-weight:800; color:#fff;}

/* 재무 테이블 행 하이라이팅 */
tr.m-row td{background:#13131a;font-weight:800;color:#f4f4f5}
tr.m-row td:first-child{background:#13131a; color: #fff;}
tr.s-row td{background:#0a0a13;color:#e4e4e7}
tr.s-row td:first-child{background:#0a0a13;padding-left:36px;color:#a1a1aa}
tr.r-row td{background:#0a0a13;color:#e4e4e7}
tr.r-row td:first-child{background:#0a0a13;color:#a1a1aa}
.tbl-wrap td.up{color:#ef4444;font-weight:800}        
.tbl-wrap td.dn{color:#3b82f6;font-weight:800}        
.yoy-pct{font-size:11px;margin-left:6px;font-weight:700; padding:2px 4px; border-radius:4px; background:rgba(255,255,255,0.05);}
.yoy-pct.up{color:#fca5a5; background:rgba(239,68,68,0.15);} 
.yoy-pct.dn{color:#93c5fd; background:rgba(59,130,246,0.15);}
.sum-pct.up{color:#ef4444;font-weight:800} 
.sum-pct.dn{color:#3b82f6;font-weight:800} 
.sum-pct.zero{color:#71717a}
.dim{color:#52525b}

/* 상단 메인 차트 패널 */
.chart-panel { display: none; flex-direction: column; background: #0a0a13; border: 1px solid #10b981; border-radius: 8px; padding: 16px; margin-bottom: 20px; height: 280px; box-shadow: 0 10px 25px rgba(16,185,129,0.05); }
.chart-panel.active { display: flex; animation: slideDown 0.3s ease-out forwards; }
@keyframes slideDown { from { opacity:0; transform:translateY(-10px); } to { opacity:1; transform:translateY(0); } }
.chart-controls { display:flex; gap:8px; margin-bottom: 12px; justify-content:flex-end;}
.chart-controls button { background: #18181b; border: 1px solid #3f3f46; color: #a1a1aa; border-radius: 4px; padding: 4px 12px; font-size: 11px; font-weight:700; cursor:pointer;}
.chart-controls button.active { background: #10b981; color:#000; border-color: #10b981;}
.chart-wrapper { flex: 1; position: relative; width: 100%; height: 100%; min-height: 0; }

/* AI 테이블 코멘트 액션바 */
.ai-action-bar { text-align:right; margin-bottom: 32px; margin-top:8px;}
.ai-btn-sm { font-size:12px; height:28px; padding:0 14px; background:rgba(168, 85, 247, 0.1); border:1px solid #a855f7; color:#d8b4fe; border-radius:6px; cursor:pointer; font-weight:700; transition:all 0.2s;}
.ai-btn-sm:hover { background:#a855f7; color:#fff; box-shadow:0 4px 12px rgba(168,85,247,0.3);}
.ai-btn-sm:disabled { opacity:0.5; cursor:not-allowed; }
.ai-comment-box { display:none; background:#121217; border-left:4px solid #a855f7; border-radius:0 8px 8px 0; padding:16px; margin-bottom:32px; color:#e4e4e7; font-size:13px; line-height:1.7; box-shadow: 0 4px 16px rgba(0,0,0,0.15);}
.ai-comment-box strong { color:#d8b4fe; font-size: 14px; margin-bottom:6px; display:inline-block;}

/* --- (격리) 매크로 대시보드 UI --- */
.macro-grid { display:grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap:16px; margin-bottom: 32px; }
.macro-card { background:#121217; border:1px solid #27272a; border-radius:10px; padding:16px 20px; height: 130px; display:flex; flex-direction:column; position:relative; overflow:hidden; box-shadow:0 4px 16px rgba(0,0,0,0.2);}
.macro-card::before { content:''; position:absolute; top:0; left:0; right:0; height:3px; background:var(--hc); opacity:0.9;}
.macro-card .mc-title { font-size:12px; font-weight:800; color:#a1a1aa; margin-bottom:8px; display:flex; justify-content:space-between; align-items:center; z-index:2; position:relative; }
.macro-card .mc-val-wrap { display:flex; align-items:baseline; gap:8px; z-index:2; position:relative; }
.macro-card .mc-val { font-family:'JetBrains Mono', monospace; font-size:22px; font-weight:800; color:#fff; letter-spacing:-0.5px;}
.macro-card .mc-unit { font-size:12px; color:#a1a1aa; font-weight:600;}
.macro-card .mc-diff { font-family:'JetBrains Mono', monospace; font-size:12px; font-weight:800;}
.macro-card .mc-diff.up { color:#ef4444;}
.macro-card .mc-diff.dn { color:#3b82f6;}
.mc-chart-wrap { position:absolute; bottom:0; left:0; right:0; height:60px; opacity:0.25; z-index:1; }


/* =========================================================================
   [CSS 격리] QUANTUM AI SCENARIO CSS - 다운사이징 최적화
   ========================================================================= */
:root { 
  --q-gold:#d8aa5c; 
  --q-gold-dim:#9c7b41; 
  --q-up:#ef4444; 
  --q-down:#3b82f6; 
  --q-flat:#c8a24b; 
  --q-panel:#18181b; 
  --q-panel2:#27272a; 
  --q-line:#3f3f46;
  --q-text:#f4f4f5;
  --q-subtext:#a1a1aa;
}
.wrap-q { max-width: 900px; margin: 0 auto; color:var(--q-text); font-family:'Noto Sans KR', sans-serif; font-size:14px; line-height:1.7;}
.wrap-q b, .wrap-q strong { font-weight:800; color:#fff; }
.wrap-q .mono{font-family:'JetBrains Mono',monospace;font-feature-settings:"tnum"}

.q-head {padding:24px 0 32px;border-bottom:1px solid var(--q-line)}
.kicker{font-family:'JetBrains Mono',monospace;font-size:11px;letter-spacing:.2em;color:var(--q-gold);text-transform:uppercase;margin-bottom:12px; font-weight:800;}
h1.q-title{font-weight:900;font-size:28px;line-height:1.3;letter-spacing:-.5px; color:#ffffff; margin-bottom:12px;}
h1.q-title em{color:var(--q-gold);font-style:normal}
.q-thesis{margin-top:24px;border:1px solid #3f3f46;border-left:4px solid var(--q-gold);background:#121217;border-radius:10px;padding:24px 32px; box-shadow:0 8px 24px rgba(0,0,0,0.2);}
.q-thesis .lab{font-family:'JetBrains Mono',monospace;font-size:12px;letter-spacing:.1em;color:var(--q-gold);text-transform:uppercase; margin-bottom:12px; font-weight:800;}
.q-thesis p{margin-top:8px;font-size:15px;color:#ffffff; font-weight:500; line-height:1.6;}

.q-sec-h{display:flex;align-items:baseline;gap:12px;margin:40px 0 16px;padding-bottom:12px;border-bottom:1px solid var(--q-line)}
.q-sec-n{font-family:'JetBrains Mono',monospace;font-size:14px;color:var(--q-gold-dim);font-weight:800}
.q-sec-h h2{font-size:20px;font-weight:900; color:#ffffff;}

.traps{display:flex;flex-direction:column;gap:12px}
.trap{display:flex;gap:16px;background:var(--q-panel);border:1px solid var(--q-line);border-radius:10px;padding:20px}
.trap .no{font-family:'JetBrains Mono',monospace;font-size:16px;font-weight:900;color:var(--q-down);flex:0 0 auto;}
.trap .tx{color:#e4e4e7; font-size:14px;}

.q-grid2{display:grid;grid-template-columns:repeat(auto-fit, minmax(320px, 1fr));gap:16px}
.q-card{background:var(--q-panel);border:1px solid var(--q-line);border-radius:10px;padding:20px}
.q-card h3{font-size:14px;font-weight:800;color:var(--q-gold);margin-bottom:16px;}
.q-card table {width:100%; border-collapse:collapse; font-size:13px;}
.q-card table td {padding:8px 0; border-bottom:1px solid var(--q-line); white-space:normal;}
.q-card table tr:last-child td{border-bottom:none;}
.q-card table td.k {color:var(--q-subtext); width:35%; vertical-align:top; font-weight:700;} 
.q-card table td.v {text-align:right; font-family:'JetBrains Mono',monospace; color:#fff; font-weight:700;}

.scn{display:flex;flex-direction:column;gap:16px}
.sc{background:var(--q-panel);border:1px solid var(--q-line);border-radius:10px;padding:20px;border-left:5px solid var(--q-line)}
.sc.dir-up{border-left-color:var(--q-up)} .sc.dir-down{border-left-color:var(--q-down)} .sc.dir-flat{border-left-color:var(--q-flat)}
.sc .top{display:flex;align-items:center;gap:12px;}
.sc .badge{font-family:'JetBrains Mono',monospace;font-size:11px;padding:4px 8px;border-radius:5px;background:var(--q-panel2);color:#a1a1aa;font-weight:800;}
.sc .why{margin-top:12px;font-size:14px;color:#d4d4d8; line-height:1.7;}

.ladder{background:var(--q-panel);border:1px solid var(--q-line);border-radius:10px;padding:12px 20px}
.lvl{display:flex;align-items:center;gap:16px;padding:16px 0;border-bottom:1px dashed var(--q-line)}
.lvl:last-child{border-bottom:none}
.lvl .px{font-family:'JetBrains Mono',monospace;font-size:16px;font-weight:800;width:100px; color:#fff;}
.lvl .pill{font-family:'JetBrains Mono',monospace;font-size:11px;padding:4px 10px;border-radius:5px;font-weight:800;}

/* 차트 컨테이너 다이어트 */
.qchart-box{position:relative; width:100%; height:300px; box-sizing: border-box; background:var(--q-panel); border:1px solid var(--q-line); border-radius:10px; padding:44px 20px 16px; margin-bottom:20px;}
.qchart-box.tall{height:350px;}
.qchart-inner { position:relative; width:100%; height:100%; min-height:0; } 
.qchart-cap{position:absolute;top:16px;left:20px;font-size:12px;color:var(--q-subtext);font-weight:800;letter-spacing:.5px;text-transform:uppercase;}

.persp-grid{display:grid;grid-template-columns:repeat(auto-fit, minmax(320px, 1fr));gap:16px;}
.persp-card{background:var(--q-panel);border:1px solid var(--q-line);border-radius:10px;padding:20px;border-top:4px solid var(--q-gold-dim); box-shadow:0 6px 16px rgba(0,0,0,0.15);}
.persp-card h4{font-size:15px;color:#fff;font-weight:900;margin-bottom:10px;display:flex;align-items:center;justify-content:space-between;gap:8px;}
.persp-pill{font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:800;padding:4px 10px;border-radius:5px;white-space:nowrap;}
.pill-buy{color:#ef4444;background:rgba(239,68,68,.15);}
.pill-sell{color:#3b82f6;background:rgba(59,130,246,.15);}
.pill-hold{color:#d8aa5c;background:rgba(216,170,92,.15);}
.imp-pill{font-size:11px;font-weight:800;padding:4px 10px;border-radius:5px;font-family:'JetBrains Mono',monospace;margin-left:10px;white-space:nowrap; display:inline-block;}
.imp-pos{color:#ef4444;background:rgba(239,68,68,.15);}
.imp-neg{color:#3b82f6;background:rgba(59,130,246,.15);}
.imp-neu{color:#a1a1aa;background:rgba(161,161,170,.15);}

.persp-score{font-family:'JetBrains Mono',monospace;font-size:12px;color:var(--q-subtext); margin-bottom:12px;}
.persp-view{font-size:14px;color:#d4d4d8;line-height:1.6;}

.nd-card{background:var(--q-panel);border:1px solid var(--q-line);border-left:4px solid #3b82f6;border-radius:0 10px 10px 0;padding:16px 20px;margin-bottom:12px;}
.nd-head{font-size:14px;font-weight:800;color:#e4e4e7;line-height:1.5;}
.nd-insight{font-size:13px;color:#93c5fd;margin-top:10px;line-height:1.6;}

.wrap-q .macro-tbl{width:100%;border-collapse:collapse;background:var(--q-panel);border:1px solid var(--q-line);border-radius:10px;overflow:hidden;}
.wrap-q .macro-tbl td{padding:16px 20px;border-bottom:1px solid var(--q-line);font-size:14px;line-height:1.5; white-space:normal;}
.wrap-q .macro-tbl .mk{color:var(--q-subtext);width:30%;font-weight:800;vertical-align:top;}
.wrap-q .macro-tbl .mv{color:#e4e4e7;}

.cata{display:flex;gap:16px;background:var(--q-panel);border:1px solid var(--q-line);border-radius:10px;padding:20px;margin-bottom:12px;align-items:flex-start;}
.cata .cwhen{color:var(--q-gold);font-size:13px;font-weight:900;min-width:50px;flex:0 0 auto;}
.cata .cdir{font-family:'JetBrains Mono',monospace;font-weight:900;font-size:12px;padding:4px 10px;border-radius:5px;flex:0 0 auto;}
.cata .ctx{font-size:14px;color:#d4d4d8;line-height:1.6;}

.freshness{margin-top:24px;background:rgba(59,130,246,.08);border:1px solid #1e3a8a;border-left:4px solid #3b82f6;border-radius:10px;padding:16px 20px;font-size:13px;color:#bfdbfe;line-height:1.6;}
.asof-badge{display:inline-block;margin-left:12px;font-family:'JetBrains Mono',monospace;font-size:11px;color:#000;background:var(--q-gold);padding:3px 10px;border-radius:5px;font-weight:900;vertical-align:middle;}

.final-hero{margin-top:24px;background:linear-gradient(135deg,#1a1530,#121217);border:1px solid #a855f7;border-radius:12px;padding:28px 36px; box-shadow:0 8px 24px rgba(168,85,247,0.2);}
.final-hero .fh-lab{font-family:'JetBrains Mono',monospace;font-size:12px;letter-spacing:.15em;color:#c4b5fd;text-transform:uppercase; font-weight:800;}
.final-hero .fh-stance{font-size:28px;font-weight:900;color:#fff;margin-top:12px;}
.final-hero .fh-line{margin-top:12px;color:#e4e4e7;font-size:15px;line-height:1.5; font-weight:700;}
.final-hero .gauge{margin-top:16px;height:12px;background:#27272a;border-radius:6px;overflow:hidden;}
.final-hero .gauge i{display:block;height:100%;background:linear-gradient(90deg,#3b82f6,#d8aa5c,#ef4444);border-radius:6px;transition:width .9s ease;}
.final-hero .gauge-lab{font-family:'JetBrains Mono',monospace;font-size:11px;color:#a1a1aa;margin-top:8px; font-weight:700;}

.q-actions{display:flex;gap:12px;justify-content:flex-end;margin:24px 0 12px;flex-wrap:wrap;}
.q-actbtn{height:36px;padding:0 20px;border-radius:6px;font-size:13px;font-weight:800;cursor:pointer;border:1px solid;background:transparent;transition:all .2s;}
.q-actbtn.pdf{border-color:#ef4444;color:#fca5a5;} .q-actbtn.pdf:hover{background:#ef4444;color:#fff; box-shadow:0 4px 12px rgba(239,68,68,.3);}
.q-actbtn.save{border-color:#10b981;color:#6ee7b7;} .q-actbtn.save:hover{background:#10b981;color:#000; box-shadow:0 4px 12px rgba(16,185,129,.3);}
.q-actbtn.del{border-color:#71717a;color:#a1a1aa;} .q-actbtn.del:hover{background:#52525b;color:#fff;}

.prob-bar-container { display:flex; height:28px; border-radius:8px; overflow:hidden; margin-bottom:24px; font-family:'JetBrains Mono', monospace; }
.prob-bar-segment { display:flex; align-items:center; justify-content:center; color:#fff; font-size:12px; font-weight:800; transition:width 0.5s ease; white-space:nowrap; overflow:hidden;}
.q-footnote{margin-top:40px;padding-top:20px;border-top:1px solid var(--q-line);font-size:12px;color:#52525b;line-height:1.6;}

/* --- 관심종목 및 뉴스 UI (안전 격리) --- */
.dash-grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(280px, 1fr)); gap:16px; margin-top:20px; }
.dash-card { background:#121217; border:1px solid #27272a; border-radius:10px; padding:20px; position:relative; transition:border .2s;}
.dash-card:hover { border-color:#3b82f6; }
.dash-del { position:absolute; top:14px; right:16px; cursor:pointer; color:#71717a; font-size:16px;}
.dash-del:hover { color:#ef4444; }
.dash-title { font-size:15px; font-weight:800; color:#e4e4e7; cursor:pointer;}
.dash-code { font-family:'JetBrains Mono', monospace; font-size:11px; color:#a1a1aa; background:#18181b; padding:3px 6px; border-radius:4px; margin-left:8px;}
.arch-card .arch-meta{margin-top:10px;}
.arch-card .arch-tag{background:rgba(216,170,92,0.1); color:#d8aa5c; font-family:'JetBrains Mono', monospace; font-size:11px; padding:3px 6px; border-radius:4px; font-weight:700; margin-right:8px;}

.news-item { padding:16px 20px; background:#121217; border:1px solid #27272a; border-radius:10px; margin-bottom:12px; transition:border .2s;}
.news-item:hover { border-color:#60a5fa; }
.news-title { display:block; color:#60a5fa; font-size:14px; font-weight:800; text-decoration:none; margin-bottom:8px; line-height:1.4;}
.news-title:hover { text-decoration:underline; }
.news-desc { color:#a1a1aa; font-size:13px; line-height:1.6; }
.news-date { color:#52525b; font-size:11px; margin-top:10px; font-family:'JetBrains Mono', monospace; }

.quant-opts { background:#121217; border:1px solid #27272a; padding:24px; border-radius:10px; margin-bottom:32px; box-shadow:0 6px 16px rgba(0,0,0,0.15);}
.quant-opts-desc { color:#a1a1aa; margin-bottom:20px; font-size:14px; line-height:1.6; }
.quant-btn-group { display:flex; gap:10px; flex-wrap:wrap; }
.quant-btn-group .btn { height:36px; font-size:13px; padding:0 20px; border-radius:6px;}

</style>
</head>
<body>

<div class="title-bar">
  <div class="dot"></div>
  <span class="title-text">DART 재무제표 분석기 · 하이브리드 실시간 대시보드</span>
  <span class="phase-badge">PRO VER.</span>
</div>

<div class="ctrl-bar">
  <div class="ctrl-left">
    <div class="ctrl-group">
      <span class="ctrl-label">회사 검색:</span>
      <div class="search-wrap">
        <input type="text" id="corpInput" placeholder="회사명 입력 후 엔터" autocomplete="off" onkeydown="if(event.key==='Enter')doSearch()">
        <div class="dropdown" id="dropdown"></div>
      </div>
      <button class="btn primary" onclick="doSearch()">조회</button>
    </div>
    <div class="ctrl-group">
      <select id="repType" onchange="if(corp)loadFinance()"><option value="11011">연간 보고서</option><option value="11012">반기</option><option value="11013">1분기</option><option value="11014">3분기</option></select>
    </div>
    <div class="ctrl-group">
      <span class="ctrl-label">조회기간:</span>
      <select id="period" onchange="if(corp)loadFinance()"><option value="3">3년</option><option value="5" selected>5년</option><option value="7">7년</option><option value="10">10년</option></select>
    </div>
    <div class="ctrl-group">
      <span class="ctrl-label">단위:</span>
      <select id="unit" onchange="if(corp)rerender()"><option value="1">원</option><option value="1000">천원</option><option value="1000000">백만</option><option value="100000000" selected>억</option><option value="1000000000000">조</option></select>
    </div>
    <div class="toggle-wrap" style="margin-left:8px;">
      <input type="checkbox" class="toggle" id="yoyT" checked onchange="if(corp)rerender()"><label class="toggle-label" for="yoyT">증감 색상</label>
    </div>
    <div class="toggle-wrap" style="margin-right:12px;">
      <input type="checkbox" class="toggle" id="sumT" checked onchange="if(corp)rerender()"><label class="toggle-label" for="sumT">요약단</label>
    </div>
    <button class="btn dash-btn" onclick="toggleChartPanel()">📈 10년 차트</button>
  </div>

  <div class="stock-panel" id="stockPanel" style="display:none">
    <div class="stock-cell"><span class="label">현재가 (원)</span><span class="val" id="sPrice">-</span><span class="sub" id="sDiff">-</span></div>
    <div class="stock-cell"><span class="label">시가총액</span><span class="val" id="sCap">-</span><span class="sub" id="sVol">-</span></div>
    <div class="stock-cell"><span class="label">PER / PBR</span><span class="val" id="sPer">-</span><span class="sub" id="sEps">-</span></div>
  </div>
</div>

<div class="tabs">
  <div class="tab active" onclick="switchTab(0,this)">회사별 재무제표 분석</div>
  <div class="tab" onclick="switchTab(1,this)">실시간 글로벌 대시보드</div>
  <div class="tab" onclick="switchTab(2,this)">관심종목 보관함</div>
  <div class="tab" onclick="switchTab(3,this)">AI 시나리오 분석 (Quant)</div>
  <div class="tab" onclick="switchTab(4,this)">뉴스 / 이슈</div>
</div>

<div class="main" id="main">
  <div class="empty"><strong>회사명을 검색하세요</strong>DART 5개년 재무제표와 한국투자증권 실시간 시세가 연동됩니다.</div>
</div>

<script>
// ==========================================
// 1. 전역 변수 및 렌더링 유틸리티 (절대 보호 영역)
// ==========================================
const BASE = `http://${location.hostname}:8787`;
let corp = null;
let rawData = {};
let years = [];
let chartInstance = null;
let isChartOpen = false;
let favorites = JSON.parse(localStorage.getItem('quant_favs') || '[]');
let macroCharts = [];
let savedReports = JSON.parse(localStorage.getItem('quant_reports') || '[]');
let lastReport = null; 
let qCharts = [];

// 숫자 변환 및 차트 초기화
function num(s){ if(typeof s==='number') return s; const m=String(s||'').replace(/[, %]/g,'').match(/-?\d+(\.\d+)?/); return m?parseFloat(m[0]):0; }
function toEok(v){ return (v===null||v===undefined||isNaN(v))?null:Math.round(v/1e8); }
function destroyQCharts(){ qCharts.forEach(c=>{ try{c.destroy();}catch(e){} }); qCharts=[]; }

// 알약(Pill) 색상 클래스 (프론트 렌더링 에러 차단)
function stancePill(s){ 
    s=(s||'').trim(); 
    if(/매수|확대|bull|buy|상승|긍정/i.test(s)) return 'pill-buy'; 
    if(/매도|축소|숏|bear|sell|하락|부정/i.test(s)) return 'pill-sell'; 
    return 'pill-hold'; 
}
function impClass(s){ 
    s=(s||'').trim(); 
    if(/호재|긍정|우호|pos|상승/i.test(s)) return 'imp-pos'; 
    if(/악재|부정|경계|neg|하락/i.test(s)) return 'imp-neg'; 
    return 'imp-neu'; 
}

const MACRO_CONF = [
  {cat:'주요 지수', id:'KOSPI',    title:'KOSPI',            unit:'pt', dec:2, vol:'mid',  hc:'#ef4444'},
  {cat:'주요 지수', id:'KOSDAQ',   title:'KOSDAQ',           unit:'pt', dec:2, vol:'mid',  hc:'#ef4444'},
  {cat:'주요 지수', id:'SNP500',   title:'S&P 500',          unit:'pt', dec:2, vol:'mid',  hc:'#d8aa5c'},
  {cat:'주요 지수', id:'NASDAQ',   title:'나스닥',            unit:'pt', dec:2, vol:'mid',  hc:'#d8aa5c'},
  {cat:'주요 지수', id:'DOW',      title:'다우존스',          unit:'pt', dec:2, vol:'mid',  hc:'#d8aa5c'},
  {cat:'주요 지수', id:'RUSSELL',  title:'러셀2000(소형주)',  unit:'pt', dec:2, vol:'mid',  hc:'#d8aa5c'},
  {cat:'주요 지수', id:'NIKKEI',   title:'닛케이225',         unit:'pt', dec:2, vol:'mid',  hc:'#a855f7'},
  {cat:'주요 지수', id:'HANGSENG', title:'항셍',              unit:'pt', dec:2, vol:'mid',  hc:'#a855f7'},
  {cat:'주요 지수', id:'SHANGHAI', title:'상해종합',          unit:'pt', dec:2, vol:'mid',  hc:'#a855f7'},
  {cat:'주요 지수', id:'DAX',      title:'독일 DAX',          unit:'pt', dec:2, vol:'mid',  hc:'#60a5fa'},
  {cat:'주요 지수', id:'FTSE',     title:'영국 FTSE100',      unit:'pt', dec:2, vol:'mid',  hc:'#60a5fa'},
  {cat:'주요 지수', id:'ESTOXX',   title:'유로스톡스50',      unit:'pt', dec:2, vol:'mid',  hc:'#60a5fa'},
  {cat:'변동성·심리', id:'VIX',    title:'VIX 공포지수',      unit:'',   dec:2, vol:'xhigh',hc:'#f43f5e'},
  {cat:'금리·채권', id:'US10Y',    title:'미 국채 10년물',    unit:'%',  dec:3, vol:'mid',  hc:'#22d3ee'},
  {cat:'금리·채권', id:'US30Y',    title:'미 국채 30년물',    unit:'%',  dec:3, vol:'mid',  hc:'#22d3ee'},
  {cat:'금리·채권', id:'US05Y',    title:'미 국채 5년물',     unit:'%',  dec:3, vol:'mid',  hc:'#22d3ee'},
  {cat:'금리·채권', id:'US13W',    title:'미 국채 13주(단기)',unit:'%',  dec:3, vol:'mid',  hc:'#22d3ee'},
  {cat:'환율 (FX)', id:'USD',      title:'원/달러',           unit:'원', dec:2, vol:'low',  hc:'#3b82f6'},
  {cat:'환율 (FX)', id:'DXY',      title:'달러인덱스(DXY)',   unit:'',   dec:2, vol:'low',  hc:'#3b82f6'},
  {cat:'환율 (FX)', id:'EURUSD',   title:'유로/달러',         unit:'',   dec:4, vol:'low',  hc:'#3b82f6'},
  {cat:'환율 (FX)', id:'USDJPY',   title:'달러/엔',           unit:'',   dec:2, vol:'low',  hc:'#3b82f6'},
  {cat:'환율 (FX)', id:'USDCNY',   title:'달러/위안',         unit:'',   dec:3, vol:'low',  hc:'#3b82f6'},
  {cat:'원자재 (에너지·금속)', id:'WTI',    title:'WTI 원유',     unit:'$', dec:2, vol:'high', hc:'#f59e0b'},
  {cat:'원자재 (에너지·금속)', id:'BRENT',  title:'브렌트유',     unit:'$', dec:2, vol:'high', hc:'#f59e0b'},
  {cat:'원자재 (에너지·금속)', id:'NATGAS', title:'천연가스',     unit:'$', dec:3, vol:'xhigh',hc:'#f59e0b'},
  {cat:'원자재 (에너지·금속)', id:'GOLD',   title:'금 (Gold)',    unit:'$', dec:2, vol:'high', hc:'#eab308'},
  {cat:'원자재 (에너지·금속)', id:'SILVER', title:'은 (Silver)',  unit:'$', dec:3, vol:'high', hc:'#eab308'},
  {cat:'원자재 (에너지·금속)', id:'COPPER', title:'구리 (Copper)',unit:'$', dec:3, vol:'high', hc:'#f59e0b'},
  {cat:'곡물 (Grains)', id:'CORN',    title:'옥수수', unit:'¢', dec:2, vol:'high', hc:'#84cc16'},
  {cat:'곡물 (Grains)', id:'WHEAT',   title:'밀',     unit:'¢', dec:2, vol:'high', hc:'#84cc16'},
  {cat:'곡물 (Grains)', id:'SOYBEAN', title:'대두',   unit:'¢', dec:2, vol:'high', hc:'#84cc16'},
  {cat:'가상자산', id:'BTC', title:'비트코인',  unit:'$', dec:0, vol:'xhigh', hc:'#f7931a'},
  {cat:'가상자산', id:'ETH', title:'이더리움',  unit:'$', dec:2, vol:'xhigh', hc:'#a855f7'},
];
const VOL_TH = { low:0.6, mid:1.5, high:2.5, xhigh:4.0 };
function fmtMacro(v, dec){ if(v===null||v===undefined||isNaN(v)) return '-'; return Number(v).toLocaleString('en-US',{minimumFractionDigits:dec,maximumFractionDigits:dec}); }


// ==========================================
// 2. 검색, 탭 및 기초 데이터 연동
// ==========================================
async function doSearch() {
  const kw = document.getElementById('corpInput').value.trim();
  if (!kw) return;
  const dd = document.getElementById('dropdown');
  dd.innerHTML = '<div class="dd-item" style="color:#71717a">🔍 KR + US 동시 검색 중…</div>';
  dd.style.display = 'block';

  try {
    // KR + US 병렬 검색
    const [krRes, usRes] = await Promise.allSettled([
      fetch(`${BASE}/api/search?keyword=${encodeURIComponent(kw)}`).then(r=>r.json()),
      fetch(`${BASE}/api/us_search?keyword=${encodeURIComponent(kw)}`).then(r=>r.json()),
    ]);

    const krList = (krRes.status==='fulfilled' ? krRes.value.list : null) || [];
    const usList = (usRes.status==='fulfilled' ? usRes.value.list : null) || [];

    dd.innerHTML = '';

    if (!krList.length && !usList.length) {
      dd.innerHTML = '<div class="dd-item" style="color:#ef4444">검색 결과 없음 (KR · US 모두)</div>';
      return;
    }

    // 티커 패턴(2~5 대문자) 이면 US 우선, 아닐 경우 KR 우선
    const isTicker = /^[A-Z]{1,5}$/.test(kw.trim().toUpperCase());
    const ordered = isTicker ? [...usList, ...krList] : [...krList, ...usList];

    // 단일 결과면 바로 선택
    if (ordered.length === 1) { pickCorp(ordered[0]); return; }

    ordered.forEach(c => {
      const el = document.createElement('div');
      el.className = 'dd-item';
      const isUS = c.market === 'US';
      const badge = isUS
        ? '<span style="font-size:10px;background:rgba(59,130,246,0.25);color:#60a5fa;padding:2px 6px;border-radius:3px;font-weight:800;margin:0 6px;">🇺🇸 US</span>'
        : '<span style="font-size:10px;background:rgba(239,68,68,0.2);color:#fca5a5;padding:2px 6px;border-radius:3px;font-weight:800;margin:0 6px;">🇰🇷 KR</span>';
      const typeTag = (c.quote_type && c.quote_type !== 'EQUITY')
        ? `<span style="font-size:10px;color:#a1a1aa;margin-left:4px;">${c.quote_type}</span>` : '';
      const code = isUS ? (c.ticker||c.stock_code) : (c.stock_code||'비상장');
      el.innerHTML = `<span class="dd-code">${code}</span>${badge}<span class="dd-name">${c.corp_name}</span>${typeTag}`;
      el.onclick = () => pickCorp(c);
      dd.appendChild(el);
    });
  } catch(e) {
    dd.innerHTML = `<div class="dd-item" style="color:#ef4444">서버 연결 실패: ${e.message}</div>`;
  }
}

function pickCorp(c) {
  corp = c;
  document.getElementById('corpInput').value = c.corp_name;
  document.getElementById('dropdown').style.display = 'none';
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.tab')[0].classList.add('active');
  loadFinance();
}

document.addEventListener('click', e => { if (!e.target.closest('.search-wrap')) { const dd = document.getElementById('dropdown'); if(dd) dd.style.display = 'none'; } });

function switchTab(idx, el) {
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active')); el.classList.add('active');
  const main = document.getElementById('main');
  if (idx===0) { if(corp) { if(Object.keys(rawData).length > 0) rerender(); else loadFinance(); } else main.innerHTML = '<div class="empty"><strong>회사명을 검색하세요</strong>DART 재무제표가 출력됩니다.</div>'; }
  else if (idx===1) { renderGlobalDashboard(main); }
  else if (idx===2) { renderWatchlist(main); }
  else if (idx===3) { renderQuantumTab(main); }
  else if (idx===4) { if(corp) fetchNews(corp.corp_name); else main.innerHTML = '<div class="empty">종목을 검색하세요</div>'; }
}


// ==========================================
// 3. 재무 데이터 (DART) & 차트 (KIS) 렌더링
// ==========================================
async function loadStock(code) {
  const panel = document.getElementById('stockPanel');
  panel.style.display = 'flex'; panel.classList.add('loading');
  try {
    const r = await fetch(`${BASE}/api/stock?code=${code}`);
    const d = await r.json();
    panel.classList.remove('loading');
    if (d.error || !d.price) {
      document.getElementById('sPrice').textContent = 'KIS API 에러'; document.getElementById('sDiff').textContent = '권한 확인';
      document.getElementById('sCap').textContent = '-'; document.getElementById('sPer').textContent = '-'; return;
    }
    const price = parseInt(d.price).toLocaleString(), diff = parseInt(d.diff), diffRate = parseFloat(d.diff_rate);
    const sign = d.diff_sign, isUp = (sign==='1'||sign==='2'), isDn = (sign==='4'||sign==='5');
    const sPrice = document.getElementById('sPrice'), sDiff = document.getElementById('sDiff');
    sPrice.textContent = price + ''; sPrice.className = 'val ' + (isUp?'up':isDn?'dn':'');
    sDiff.textContent = (isUp?'▲':isDn?'▼':'') + Math.abs(diff).toLocaleString() + ` (${diffRate}%)`; sDiff.className = 'sub ' + (isUp?'up':isDn?'dn':'');
    const capEok = parseInt(d.market_cap||'0');
    document.getElementById('sCap').textContent = capEok > 0 ? (capEok >= 10000 ? (capEok/10000).toFixed(2) + '조' : capEok.toLocaleString() + '억') : '-';
    document.getElementById('sVol').textContent = '거래 ' + parseInt(d.volume||'0').toLocaleString();
    document.getElementById('sPer').textContent = `${d.per||'-'} / ${d.pbr||'-'}`;
    document.getElementById('sEps').textContent = `EPS ${parseInt(d.eps||'0').toLocaleString()}`;
  } catch(e) { panel.classList.remove('loading'); }
}

function toggleChartPanel() {
    if(!corp) { alert("먼저 종목을 검색하세요."); return; }
    isChartOpen = !isChartOpen;
    const panel = document.getElementById('chartPanel');
    if(panel) {
        if(isChartOpen) { panel.classList.add('active'); if(!chartInstance) renderChart('1Y'); } 
        else { panel.classList.remove('active'); }
    }
}

async function renderChart(period) {
    if(!corp) return;
    document.querySelectorAll('.chart-controls button').forEach(b => b.classList.remove('active'));
    if(event && event.target.tagName === 'BUTTON') event.target.classList.add('active');
    else document.getElementById('btn_1y').classList.add('active');

    try {
        let labels = [], data = [];

        if (corp.market === 'US') {
            // US: 야후 파이낸스 차트
            const r = await fetch(`${BASE}/api/us_chart?ticker=${corp.ticker}&period=${period}`);
            const d = await r.json();
            if(d.status !== '000') return;
            labels = d.list.map(x => x.date);
            data   = d.list.map(x => x.close);
        } else {
            // KR: KIS 차트 (기존)
            if(!corp.stock_code) return;
            const r = await fetch(`${BASE}/api/chart?code=${corp.stock_code}&period=${period}`);
            const d = await r.json();
            if(d.status !== '000') return;
            const raw = d.list.reverse();
            labels = raw.map(x => x.stck_bsop_date.replace(/(\d{4})(\d{2})(\d{2})/, '$1-$2-$3'));
            data   = raw.map(x => parseInt(x.stck_clpr));
        }

        if(chartInstance) chartInstance.destroy();
        const ctx = document.getElementById('mainChart').getContext('2d');
        chartInstance = new Chart(ctx, {
            type: 'line',
            data: { labels, datasets: [{ label: '종가', data, borderColor: '#ef4444', backgroundColor: 'rgba(239, 68, 68, 0.15)', borderWidth: 1.5, pointRadius: 0, fill: true, tension: 0.1 }] },
            options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false }, tooltip: { mode: 'index', intersect: false } }, scales: { x: { grid: { display: false }, ticks: { color: '#71717a', maxTicksLimit: 6 } }, y: { grid: { color: '#27272a' }, ticks: { color: '#71717a' }, position: 'right' } } }
        });
    } catch(e) {}
}

async function loadFinance() {
  if (!corp) return;

  // ── 미국 종목 분기 ──
  if (corp.market === 'US') {
    const main = document.getElementById('main');
    const ticker = corp.ticker || corp.stock_code || '';
    const period = parseInt(document.getElementById('period').value);

    loadUsStock(ticker);  // 시세 패널 비동기

    // ETF는 SEC 재무 없음 → ETF 전용 UI
    if ((corp.quote_type || '') === 'ETF') {
      main.innerHTML = '<div class="spinner-wrap"><div class="spinner"></div> ETF 정보를 불러오는 중...</div>';
      renderUsEtfPanel(main, ticker);
      return;
    }

    main.innerHTML = '<div class="spinner-wrap"><div class="spinner"></div> SEC EDGAR 재무제표를 파싱하고 있습니다...</div>';
    try {
      const r = await fetch(`${BASE}/api/us_finance?ticker=${encodeURIComponent(ticker)}&period=${period}`);
      const d = await r.json();
      if (d.status !== '000') {
        // 재무 없음(ETF 등) → ETF 패널로 폴백
        renderUsEtfPanel(main, ticker);
        return;
      }
      rawData = d.data;
      years = d.years;
      isChartOpen = false; chartInstance = null;
      rerender();
    } catch(e) { main.innerHTML = `<div class="err">US 재무 조회 실패: ${e.message}</div>`; }
    return;
  }

  // ── 한국 종목 (기존) ──
  if(corp.stock_code) loadStock(corp.stock_code); else document.getElementById('stockPanel').style.display='none';
  const main = document.getElementById('main');
  main.innerHTML = '<div class="spinner-wrap"><div class="spinner"></div> DART 재무제표를 파싱하고 있습니다...</div>';
  const repCode = document.getElementById('repType').value;
  const period  = parseInt(document.getElementById('period').value);
  const curYear = new Date().getFullYear();
  years = Array.from({length: period}, (_, i) => curYear - 1 - i);

  try {
    const fetches = years.map(y => fetch(`${BASE}/api/finance?corp_code=${corp.corp_code}&year=${y}&rep_code=${repCode}&fs_div=CFS`).then(r => r.json()).catch(() => ({list:[]})));
    const results = await Promise.all(fetches);
    rawData = {}; years.forEach((y, i) => { rawData[y] = results[i].list || []; });
    isChartOpen = false; chartInstance = null;
    rerender();
  } catch(e) { main.innerHTML = `<div class="err">연결 실패: ${e.message}</div>`; }
}

// ── US 시세 패널 로딩 ──
async function loadUsStock(ticker) {
  const panel = document.getElementById('stockPanel');
  panel.style.display = 'flex';
  try {
    const r = await fetch(`${BASE}/api/us_stock?ticker=${ticker}`);
    const d = await r.json();
    if (d.error) {
      document.getElementById('sPrice').textContent = 'N/A';
      document.getElementById('sDiff').textContent = d.error.includes('401') ? '야후 인증 실패' : d.error.slice(0,30);
      return;
    }
    const isUp = d.diff >= 0;
    const sPrice = document.getElementById('sPrice'), sDiff = document.getElementById('sDiff');
    sPrice.textContent = '$' + Number(d.price).toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2});
    sPrice.className = 'val ' + (isUp ? 'up' : 'dn');
    sDiff.textContent = (isUp?'▲':'▼') + '$' + Math.abs(d.diff).toFixed(2) + ` (${Math.abs(d.diff_rate).toFixed(2)}%)`;
    sDiff.className = 'sub ' + (isUp ? 'up' : 'dn');
    const cap = d.market_cap;
    document.getElementById('sCap').textContent = cap >= 1e12 ? '$'+(cap/1e12).toFixed(2)+'T' : cap >= 1e9 ? '$'+(cap/1e9).toFixed(1)+'B' : '-';
    document.getElementById('sVol').textContent = '거래 ' + Number(d.volume||0).toLocaleString('en-US');
    document.getElementById('sPer').textContent = `${d.per||'-'} / ${d.pbr||'-'}`;
    document.getElementById('sEps').textContent = `EPS $${d.eps||'-'}`;
  } catch(e) {}
}

// ── ETF / 재무없는 US 종목 전용 패널 ──
async function renderUsEtfPanel(main, ticker) {
  try {
    const r = await fetch(`${BASE}/api/us_stock?ticker=${encodeURIComponent(ticker)}`);
    const d = await r.json();
    if (d.error) throw new Error(d.error);

    const isEtf = (d.quote_type || '').toUpperCase() === 'ETF';
    const capStr = !d.market_cap ? '-'
      : d.market_cap >= 1e12 ? '$'+(d.market_cap/1e12).toFixed(2)+'T'
      : d.market_cap >= 1e9  ? '$'+(d.market_cap/1e9).toFixed(1)+'B'
      : '$'+(d.market_cap/1e6).toFixed(0)+'M';

    const isFav = favorites.find(f => (f.corp_code||f.cik||f.ticker) === (corp.corp_code||corp.cik||corp.ticker));
    let html = `
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;margin-bottom:24px;">
        <div>
          <span style="font-size:22px;font-weight:900;color:#e4e4e7;">${corp.corp_name}</span>
          <span style="font-size:14px;color:#71717a;font-family:'JetBrains Mono';margin-left:8px;">${ticker}</span>
          <span style="font-size:11px;background:rgba(59,130,246,0.2);color:#60a5fa;padding:3px 8px;border-radius:4px;font-weight:800;margin-left:10px;">🇺🇸 ${isEtf?'ETF':'US EQUITY'}</span>
          <div style="font-size:12px;color:#52525b;margin-top:6px;">출처: Yahoo Finance · SEC EDGAR</div>
        </div>
        <button class="btn" onclick="toggleFavorite()" style="border-color:${isFav?'#ef4444':'#3f3f46'};color:${isFav?'#ef4444':'#a1a1aa'};">${isFav?'관심종목 해제':'관심종목 설정'}</button>
      </div>

      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:16px;margin-bottom:32px;">
        ${[
          ['현재가 (USD)', d.price ? '$'+Number(d.price).toLocaleString('en-US',{minimumFractionDigits:2}) : '-'],
          ['시가총액', capStr],
          ['PER', d.per ? d.per+'x' : '-'],
          ['PBR', d.pbr ? d.pbr+'x' : '-'],
          ['EPS', d.eps ? '$'+d.eps : '-'],
          isEtf ? ['비용비율', d.etf_expense_ratio!=null ? d.etf_expense_ratio+'%' : '-'] : ['통화', d.currency||'USD'],
        ].map(([k,v])=>`
          <div style="background:#121217;border:1px solid #27272a;border-radius:8px;padding:16px 20px;">
            <div style="font-size:11px;color:#71717a;font-weight:700;margin-bottom:8px;">${k}</div>
            <div style="font-size:20px;font-weight:800;color:#e4e4e7;font-family:'JetBrains Mono';">${v}</div>
          </div>`).join('')}
      </div>`;

    if (isEtf && (d.etf_ytd_return != null || d.etf_3y_return != null || d.etf_5y_return != null)) {
      html += `<div class="sec-title">수익률 (추적 수익)</div>
        <div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:32px;">
          ${[['YTD', d.etf_ytd_return],['3년', d.etf_3y_return],['5년', d.etf_5y_return]]
            .filter(([,v])=>v!=null)
            .map(([k,v])=>`<div style="background:#121217;border:1px solid #27272a;border-radius:8px;padding:20px 28px;text-align:center;">
              <div style="font-size:12px;color:#71717a;margin-bottom:8px;font-weight:700;">${k}</div>
              <div style="font-size:24px;font-weight:900;color:${v>=0?'#ef4444':'#3b82f6'};font-family:'JetBrains Mono';">${v>=0?'+':''}${v}%</div>
            </div>`).join('')}
        </div>`;
    }

    if (isEtf && d.etf_holdings && d.etf_holdings.length) {
      html += `<div class="sec-title">주요 보유 종목 (Top Holdings)</div>
        <div class="tbl-wrap"><table><thead><tr><th style="text-align:left;">종목명</th><th>비중</th></tr></thead><tbody>
        ${d.etf_holdings.map(h=>`<tr class="r-row"><td style="font-weight:700;">${h.name}</td><td style="text-align:right;font-family:'JetBrains Mono';font-weight:800;color:#e4e4e7;">${h.pct!=null?h.pct+'%':'-'}</td></tr>`).join('')}
        </tbody></table></div>`;
      if (d.etf_equity_pct != null || d.etf_bond_pct != null) {
        html += `<div style="display:flex;gap:12px;flex-wrap:wrap;margin:16px 0 32px;">
          ${d.etf_equity_pct!=null?`<div style="background:#121217;border:1px solid #27272a;border-radius:8px;padding:12px 20px;"><span style="color:#71717a;font-size:12px;font-weight:700;">주식 비중</span><b style="color:#ef4444;font-family:'JetBrains Mono';margin-left:10px;font-size:16px;">${d.etf_equity_pct}%</b></div>`:''}
          ${d.etf_bond_pct!=null?`<div style="background:#121217;border:1px solid #27272a;border-radius:8px;padding:12px 20px;"><span style="color:#71717a;font-size:12px;font-weight:700;">채권 비중</span><b style="color:#3b82f6;font-family:'JetBrains Mono';margin-left:10px;font-size:16px;">${d.etf_bond_pct}%</b></div>`:''}
        </div>`;
      }
    }

    if (!isEtf) {
      html += `<div class="err" style="background:rgba(59,130,246,0.08);border-color:#3b82f6;color:#93c5fd;margin-bottom:24px;">
        이 종목의 SEC EDGAR XBRL 재무 데이터를 찾을 수 없습니다.<br>
        최근 상장 종목이거나, EDGAR XBRL을 제출하지 않는 외국계 ADR일 수 있습니다.
      </div>`;
    }

    html += `<div class="chart-panel active" id="chartPanel" style="margin-top:8px;">
      <div class="chart-controls">
        <button onclick="renderChart('1M')">1개월</button>
        <button onclick="renderChart('3M')">3개월</button>
        <button id="btn_1y" onclick="renderChart('1Y')" class="active">1년</button>
        <button onclick="renderChart('3Y')">3년</button>
        <button onclick="renderChart('5Y')">5년</button>
      </div>
      <div class="chart-wrapper"><canvas id="mainChart"></canvas></div>
    </div>`;

    main.innerHTML = html;
    isChartOpen = true;
    renderChart('1Y');

  } catch(e) {
    main.innerHTML = `<div class="err">ETF/US 데이터 조회 실패: ${e.message}</div>`;
  }
}


function pick(year, names, sjDivs) {
  // US 종목: rawData[year]가 직접 {항목명:값} 객체
  if (corp && corp.market === 'US') {
    const row = rawData[year];
    if (!row) return null;
    for (const nm of names) {
      if (row[nm] !== undefined && row[nm] !== null) return row[nm];
    }
    return null;
  }
  // KR 종목: 기존 DART 배열 처리
  const list = rawData[year]; if (!list?.length) return null;
  for (const sj of sjDivs) for (const nm of names) {
    const it = list.find(r => r.sj_div === sj && r.account_nm?.includes(nm));
    if (it) { const v = parseInt((it.thstrm_amount||'0').replace(/,/g,'')); if (!isNaN(v)) return v; }
  }
  for (const nm of names) {
    const it = list.find(r => r.account_nm?.includes(nm));
    if (it) { const v = parseInt((it.thstrm_amount||'0').replace(/,/g,'')); if (!isNaN(v)) return v; }
  } return null;
}

const BS = ['BS'], IS = ['IS'], CIS = ['CIS','IS'], CF = ['CF'], SCE = ['SCE'];
function fmt(v) {
  if (v===null||v===undefined) return '<span class="dim">-</span>';
  // US 종목: SEC EDGAR 데이터가 이미 USD 백만 단위로 정규화됨
  if (corp && corp.market === 'US') {
    if (Math.abs(v) >= 1000000) return (v/1000000).toFixed(2) + 'T';
    if (Math.abs(v) >= 1000) return (v/1000).toFixed(1) + 'B';
    return v.toLocaleString('en-US', {minimumFractionDigits:0, maximumFractionDigits:0}) + 'M';
  }
  const u = parseInt(document.getElementById('unit').value);
  if (u === 1) return v.toLocaleString('ko-KR');
  if (u === 1000000000000) return (v/u).toFixed(2);
  return Math.round(v/u).toLocaleString('ko-KR');
}
function yoyClass(cur, prv) { if (!document.getElementById('yoyT').checked) return ''; if (cur===null||prv===null||prv===0) return ''; return cur > prv ? 'up' : (cur < prv ? 'dn' : ''); }
function yoyPct(cur, prv) { if (!document.getElementById('yoyT').checked) return ''; if (cur===null||prv===null||prv===0) return ''; const p = ((cur-prv)/Math.abs(prv)*100).toFixed(1); if (Math.abs(p) < 0.05) return ''; return `<span class="yoy-pct ${p>0?'up':'dn'}">${p>0?'▲':'▼'}${Math.abs(p)}%</span>`; }
function summary(vals) { const vv = vals.filter(v=>v!==null); if (vv.length<2) return '<span class="dim">-</span>'; const first = vals[0], last = vals[vals.length-1]; if (last===null||first===null||last===0) return '<span class="dim">-</span>'; const p = ((first-last)/Math.abs(last)*100).toFixed(1); if (Math.abs(p) < 0.05) return '<span class="sum-pct zero">0%</span>'; return `<span class="sum-pct ${p>0?'up':'dn'}">${p>0?'+':''}${p}%</span>`; }

function rerender() {
  if (!years.length) return;
  const showSum = document.getElementById('sumT').checked;
  const corpKey = corp.corp_code || corp.cik || '';
  const isFav = favorites.find(f => (f.corp_code || f.cik) === corpKey);
  const isUS = corp.market === 'US';
  const mktBadge = isUS
    ? `<span style="font-size:11px;background:rgba(59,130,246,0.2);color:#60a5fa;padding:3px 8px;border-radius:4px;font-weight:800;margin-left:10px;">🇺🇸 US · SEC EDGAR</span>`
    : `<span style="font-size:11px;background:rgba(239,68,68,0.2);color:#fca5a5;padding:3px 8px;border-radius:4px;font-weight:800;margin-left:10px;">🇰🇷 KR · DART</span>`;
  const tickerDisplay = isUS ? (corp.ticker||'') : (corp.stock_code||'비상장');
  let html = `<div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:24px; flex-wrap:wrap; gap:12px;">
      <div>
        <span style="font-size:22px; font-weight:900; color:#e4e4e7; letter-spacing:-0.5px;">${corp.corp_name}</span>
        <span style="font-size:14px; color:#71717a; font-weight:700; margin-left:8px; font-family:'JetBrains Mono';">${tickerDisplay}</span>
        ${mktBadge}
        ${isUS ? `<div style="font-size:12px;color:#52525b;margin-top:6px;">단위: USD (백만) · 출처: SEC EDGAR 10-K/10-Q</div>` : ''}
      </div>
      <button class="btn" onclick="toggleFavorite()" style="border-color:${isFav ? '#ef4444' : '#3f3f46'}; color:${isFav ? '#ef4444' : '#a1a1aa'};">${isFav ? '관심종목 해제' : '관심종목 설정'}</button>
    </div>`;
  html += `<div class="chart-panel ${isChartOpen ? 'active' : ''}" id="chartPanel">
        <div class="chart-controls"><button onclick="renderChart('1M')">1개월</button><button onclick="renderChart('3M')">3개월</button><button id="btn_1y" onclick="renderChart('1Y')" class="active">1년</button><button onclick="renderChart('3Y')">3년</button><button onclick="renderChart('5Y')">5년</button><button onclick="renderChart('10Y')">10년</button></div>
        <div class="chart-wrapper"><canvas id="mainChart"></canvas></div></div>`;
        
  // US 종목은 SEC 항목명(영문→한글 매핑 후 동일 pick() 사용)
  if (corp.market === 'US') {
    const BS_US=['BS'], IS_US=['IS'], CF_US=['CF'];
    html += makeTableWithAI("재무상태표 (Balance Sheet)", [
      {t:'m', label:'자산총계',           names:['자산총계'],               sjs:BS_US},
      {t:'s', label:'유동자산',           names:['유동자산'],               sjs:BS_US},
      {t:'s', label:'현금및현금성자산',   names:['현금및현금성자산'],       sjs:BS_US},
      {t:'s', label:'단기금융상품',       names:['단기금융상품'],           sjs:BS_US},
      {t:'s', label:'매출채권',           names:['매출채권'],               sjs:BS_US},
      {t:'s', label:'재고자산',           names:['재고자산'],               sjs:BS_US},
      {t:'s', label:'비유동자산',         names:['비유동자산'],             sjs:BS_US},
      {t:'s', label:'유형자산',           names:['유형자산'],               sjs:BS_US},
      {t:'s', label:'무형자산',           names:['무형자산'],               sjs:BS_US},
      {t:'m', label:'부채총계',           names:['부채총계'],               sjs:BS_US},
      {t:'s', label:'유동부채',           names:['유동부채'],               sjs:BS_US},
      {t:'s', label:'비유동부채',         names:['비유동부채'],             sjs:BS_US},
      {t:'m', label:'자본총계',           names:['자본총계'],               sjs:BS_US},
      {t:'s', label:'이익잉여금',         names:['이익잉여금'],             sjs:BS_US},
    ], showSum, 'bs-ai');
    html += renderRatioWithAI("재무비율", showSum, 'rt-ai');
    html += makeTableWithAI("손익계산서 (Income Statement)", [
      {t:'m', label:'매출액',             names:['매출액'],                 sjs:IS_US},
      {t:'s', label:'매출원가',           names:['매출원가'],               sjs:IS_US},
      {t:'m', label:'매출총이익',         names:['매출총이익'],             sjs:IS_US},
      {t:'s', label:'판매비와관리비',     names:['판매비와관리비'],         sjs:IS_US},
      {t:'m', label:'영업이익',           names:['영업이익'],               sjs:IS_US},
      {t:'s', label:'이자비용',           names:['이자비용'],               sjs:IS_US},
      {t:'m', label:'법인세차감전순이익', names:['법인세차감전순이익'],     sjs:IS_US},
      {t:'s', label:'법인세비용',         names:['법인세비용'],             sjs:IS_US},
      {t:'m', label:'당기순이익',         names:['당기순이익'],             sjs:IS_US},
    ], showSum, 'is-ai');
    html += makeTableWithAI("현금흐름표 (Cash Flow Statement)", [
      {t:'m', label:'영업활동현금흐름',   names:['영업활동현금흐름'],       sjs:CF_US},
      {t:'m', label:'투자활동현금흐름',   names:['투자활동현금흐름'],       sjs:CF_US},
      {t:'m', label:'재무활동현금흐름',   names:['재무활동현금흐름'],       sjs:CF_US},
      {t:'m', label:'기말 현금',           names:['기말 현금'],               sjs:CF_US},
    ], showSum, 'cf-ai');
    document.getElementById('main').innerHTML = html;
    if(isChartOpen) renderChart('1Y');
    return;
  }

  html += makeTableWithAI("재무상태표 (Balance Sheet)", [{t:'m', label:'자산총계', names:['자산총계'], sjs:BS}, {t:'s', label:'유동자산', names:['유동자산'], sjs:BS}, {t:'s', label:'현금및현금성자산', names:['현금및현금성자산'], sjs:BS}, {t:'s', label:'단기금융상품', names:['단기금융상품'], sjs:BS}, {t:'s', label:'매출채권', names:['매출채권','매출 채권'], sjs:BS}, {t:'s', label:'재고자산', names:['재고자산'], sjs:BS}, {t:'s', label:'비유동자산', names:['비유동자산'], sjs:BS}, {t:'s', label:'유형자산', names:['유형자산'], sjs:BS}, {t:'s', label:'무형자산', names:['무형자산'], sjs:BS}, {t:'m', label:'부채총계', names:['부채총계'], sjs:BS}, {t:'s', label:'유동부채', names:['유동부채'], sjs:BS}, {t:'s', label:'비유동부채', names:['비유동부채'], sjs:BS}, {t:'m', label:'자본총계', names:['자본총계'], sjs:BS}, {t:'s', label:'자본금', names:['자본금'], sjs:BS}, {t:'s', label:'이익잉여금', names:['이익잉여금'], sjs:BS}], showSum, 'bs-ai');
  html += renderRatioWithAI("재무비율", showSum, 'rt-ai');
  html += makeTableWithAI("손익계산서 (Income Statement)", [{t:'m', label:'매출액', names:['매출액','수익(매출액)','영업수익'], sjs:IS}, {t:'s', label:'매출원가', names:['매출원가'], sjs:IS}, {t:'m', label:'매출총이익', names:['매출총이익'], sjs:IS}, {t:'s', label:'판매비와관리비', names:['판매비와관리비'], sjs:IS}, {t:'m', label:'영업이익', names:['영업이익'], sjs:IS}, {t:'s', label:'금융수익', names:['금융수익'], sjs:IS}, {t:'s', label:'금융비용', names:['금융비용'], sjs:IS}, {t:'s', label:'기타수익', names:['기타수익','기타영업외수익'], sjs:IS}, {t:'s', label:'기타비용', names:['기타비용','기타영업외비용'], sjs:IS}, {t:'m', label:'법인세차감전순이익',names:['법인세비용차감전순이익','법인세차감전'], sjs:IS}, {t:'s', label:'법인세비용', names:['법인세비용'], sjs:IS}, {t:'m', label:'당기순이익', names:['당기순이익'], sjs:IS}], showSum, 'is-ai');
  html += makeTableWithAI("포괄손익계산서 (Comprehensive Income)", [{t:'m', label:'당기순이익', names:['당기순이익'], sjs:CIS}, {t:'s', label:'기타포괄손익', names:['기타포괄손익'], sjs:CIS}, {t:'m', label:'총포괄손익', names:['총포괄손익','포괄손익'], sjs:CIS}], showSum, 'cis-ai');
  html += makeTableWithAI("자본변동표 (Changes in Equity)", [{t:'m', label:'기초 자본', names:['기초자본','기초 자본'], sjs:SCE}, {t:'s', label:'당기순이익', names:['당기순이익'], sjs:SCE}, {t:'s', label:'배당', names:['배당'], sjs:SCE}, {t:'s', label:'자기주식', names:['자기주식'], sjs:SCE}, {t:'m', label:'기말 자본', names:['기말자본','기말 자본'], sjs:SCE}], showSum, 'sce-ai');
  html += makeTableWithAI("현금흐름표 (Cash Flow Statement)", [{t:'m', label:'영업활동현금흐름', names:['영업활동현금흐름','영업활동으로'], sjs:CF}, {t:'m', label:'투자활동현금흐름', names:['투자활동현금흐름','투자활동으로'], sjs:CF}, {t:'m', label:'재무활동현금흐름', names:['재무활동현금흐름','재무활동으로'], sjs:CF}, {t:'m', label:'현금및현금성자산순증가',names:['현금및현금성자산의증가','현금및현금성자산의순증가'], sjs:CF}, {t:'s', label:'기초 현금', names:['기초의현금','기초현금'], sjs:CF}, {t:'m', label:'기말 현금', names:['기말의현금','기말현금'], sjs:CF}], showSum, 'cf-ai');
  
  document.getElementById('main').innerHTML = html;
  if(isChartOpen) renderChart('1Y');
}

function makeTableWithAI(title, rows, showSum, boxId) {
  const isUS = corp && corp.market === 'US';
  const unit = isUS ? 'USD M' : ({1:'원',1000:'천원',1000000:'백만',100000000:'억',1000000000000:'조'}[document.getElementById('unit').value]);
  let h = `<div class="sec-title">${title}</div><div class="tbl-wrap"><table><thead><tr><th>항목</th>${years.map(y=>`<th>${y} (${unit})</th>`).join('')}${showSum?'<th class="sum-col">Summary</th>':''}</tr></thead><tbody>`;
  let dataForAI = {};
  rows.forEach(r => {
    const vals = years.map(y => pick(y, r.names, r.sjs));
    dataForAI[r.label] = vals;
    h += `<tr class="${r.t==='m'?'m-row':'s-row'}"><td>${r.label}</td>`;
    vals.forEach((v,i) => { const prev = i < vals.length-1 ? vals[i+1] : null; h += `<td class="${yoyClass(v, prev)}">${fmt(v)}${yoyPct(v, prev)}</td>`; });
    if (showSum) h += `<td class="sum-col">${summary(vals)}</td>`; h += '</tr>';
  });
  h += `</tbody></table></div><div class="ai-action-bar"><button class="ai-btn-sm" onclick='requestAIComment(this, ${JSON.stringify(dataForAI)}, "${title}")'>AI ${title.split(' ')[0]} 진단 코멘트</button></div><div class="ai-comment-box" id="${boxId}"></div>`;
  return h;
}

function renderRatioWithAI(title, showSum, boxId) {
  const ratios = [
    {label:'부채비율 (%)', fn: y=>{const d=pick(y,['부채총계'],BS), e=pick(y,['자본총계'],BS); return (d&&e)?(d/e*100):null;}, fmt: v => v===null?null:v.toFixed(1)+'%', invertColor:true},
    {label:'유동비율 (%)', fn: y=>{const a=pick(y,['유동자산'],BS), b=pick(y,['유동부채'],BS); return (a&&b)?(a/b*100):null;}, fmt: v => v===null?null:v.toFixed(1)+'%'},
    {label:'자기자본비율 (%)', fn: y=>{const e=pick(y,['자본총계'],BS), a=pick(y,['자산총계'],BS); return (e&&a)?(e/a*100):null;}, fmt: v => v===null?null:v.toFixed(1)+'%'},
    {label:'영업이익률 (%)', fn: y=>{const op=pick(y,['영업이익'],IS), rev=pick(y,['매출액','영업수익'],IS); return (op&&rev)?(op/rev*100):null;}, fmt: v => v===null?null:v.toFixed(1)+'%'},
    {label:'순이익률 (%)', fn: y=>{const n=pick(y,['당기순이익'],IS), rev=pick(y,['매출액','영업수익'],IS); return (n&&rev)?(n/rev*100):null;}, fmt: v => v===null?null:v.toFixed(1)+'%'},
    {label:'ROE (%)', fn: y=>{const n=pick(y,['당기순이익'],IS), e=pick(y,['자본총계'],BS); return (n&&e)?(n/e*100):null;}, fmt: v => v===null?null:v.toFixed(1)+'%'},
    {label:'ROA (%)', fn: y=>{const n=pick(y,['당기순이익'],IS), a=pick(y,['자산총계'],BS); return (n&&a)?(n/a*100):null;}, fmt: v => v===null?null:v.toFixed(1)+'%'},
  ];
  let h = `<div class="sec-title">${title}</div><div class="tbl-wrap"><table><thead><tr><th>지표</th>${years.map(y=>`<th>${y}</th>`).join('')}${showSum?'<th class="sum-col">Summary</th>':''}</tr></thead><tbody>`;
  let dataForAI = {};
  ratios.forEach(r => {
    const vals = years.map(r.fn);
    dataForAI[r.label] = vals;
    h += `<tr class="r-row"><td>${r.label}</td>`;
    vals.forEach((v,i) => {
      const prev = i < vals.length-1 ? vals[i+1] : null; let cls = yoyClass(v, prev); if(r.invertColor && cls !== '') cls = cls === 'up' ? 'dn' : 'up'; 
      let diffHtml = ''; if(document.getElementById('yoyT').checked && v!==null && prev!==null && prev!==0) { const diffPt = (v - prev).toFixed(1); if (Math.abs(diffPt) >= 0.1) { const dCls = diffPt > 0 ? (r.invertColor ? 'dn' : 'up') : (r.invertColor ? 'up' : 'dn'); diffHtml = `<span class="yoy-pct ${dCls}">${diffPt>0?'▲':'▼'}${Math.abs(diffPt)}%p</span>`; } }
      h += `<td class="${cls}">${v===null?'<span class="dim">-</span>':r.fmt(v)}${diffHtml}</td>`;
    });
    if (showSum) h += `<td class="sum-col">${summary(vals)}</td>`; h += '</tr>';
  });
  h += `</tbody></table></div><div class="ai-action-bar"><button class="ai-btn-sm" onclick='requestAIComment(this, ${JSON.stringify(dataForAI)}, "${title}")'>AI 지표 진단 코멘트</button></div><div class="ai-comment-box" id="${boxId}"></div>`;
  return h;
}

// 다중 클릭 방지 및 상태 관리
async function requestAIComment(btn, dataObj, title) {
    btn.innerText = "팩트 체크 및 논리 구성 중..."; btn.disabled = true;
    const box = btn.parentElement.nextElementSibling; box.style.display = 'block'; box.innerHTML = '<div class="spinner" style="border-top-color:#a855f7; display:inline-block; vertical-align:middle; width:20px; height:20px; margin-right:12px;"></div> 데이터를 분석하고 있습니다...';
    const prompt = `당신은 까칠한 월가의 회계사입니다. 다음은 대상 종목: ${corp.corp_name}의 ${years.length}년간 '${title}' 데이터(최신년도순)입니다.\n${JSON.stringify(dataObj)}\n위 데이터를 바탕으로 숫자 증감의 핵심, 위험 징후, 긍정적 턴어라운드를 분석해서 직설적이고 날카로운 2~3문장짜리 코멘트를 작성하세요. 평문으로 대답하세요.`;
    try {
        const r = await fetch(`${BASE}/api/ai`, { method:'POST', body:JSON.stringify({prompt}) }); const d = await r.json();
        if(d.status==='error') throw new Error(d.message);
        box.innerHTML = `<strong>[AI 회계사 진단]</strong><br>${d.advice.replace(/\n/g, '<br>')}`;
    } catch(e) { 
        box.innerHTML = `진단 실패: ${e.message}`; 
    } finally { 
        btn.innerText = `AI ${title.split(' ')[0]} 진단 코멘트`; 
        btn.disabled = false; 
    }
}

function toggleFavorite() {
    if(!corp) return;
    const key = corp.corp_code || corp.cik || '';
    const idx = favorites.findIndex(f => (f.corp_code || f.cik || '') === key);
    if(idx > -1) { favorites.splice(idx, 1); alert(`'${corp.corp_name}' 관심종목에서 해제되었습니다.`); } 
    else { favorites.push({ ...corp, addedAt: new Date().toLocaleDateString() }); alert(`'${corp.corp_name}' 관심종목에 설정되었습니다.`); }
    localStorage.setItem('quant_favs', JSON.stringify(favorites)); rerender();
}

async function renderGlobalDashboard(main){
    macroCharts.forEach(c=>{ try{c.destroy();}catch(e){} }); macroCharts = [];
    window.macroData = {};
    const cats = [...new Set(MACRO_CONF.map(c=>c.cat))];
    let html = `
        <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:16px;margin-bottom:16px;">
          <div class="sec-title" style="margin:0;">실시간 글로벌 매크로 대시보드</div>
          <div style="display:flex;gap:12px;align-items:center;">
            <button class="btn" onclick="renderGlobalDashboard(document.getElementById('main'))">↻ 새로고침</button>
            <button class="btn ai-btn" id="macroAiBtn" style="padding:0 24px;" onclick="requestMacroAI()">🧠 AI 거시·미시 진단</button>
          </div>
        </div>
        <div style="color:#a1a1aa;font-size:15px;margin-bottom:32px;line-height:1.7;">전 세계 지수·금리·환율·원자재·곡물·가상자산을 야후 파이낸스에서 실시간 수집합니다. 평소보다 크게 움직인 항목은 <b style="color:#ef4444;">급변</b> 태그로 강조되며, <b style="color:#a855f7;">AI 거시·미시 진단</b> 버튼을 누르면 이 수치와 야후 글로벌 뉴스를 종합해 현재 경제 국면·교차자산 신호·한국 시장 전이경로를 해석합니다.</div>
        <div id="macroAiArea"></div>`;
    cats.forEach(cat=>{
        html += `<div class="sec-title">${cat}</div><div class="macro-grid">`;
        MACRO_CONF.filter(c=>c.cat===cat).forEach(c=>{
            html += `
            <div class="macro-card" style="--hc:${c.hc}">
                <div class="mc-title"><span>${c.title}</span></div>
                <div class="mc-val-wrap"><span class="mc-val" id="val_${c.id}" style="color:#52525b;">···</span><span class="mc-unit">${c.unit}</span><span class="mc-diff" id="diff_${c.id}"></span></div>
                <div class="mc-chart-wrap"><canvas id="ch_${c.id}"></canvas></div>
            </div>`;
        });
        html += `</div>`;
    });
    main.innerHTML = html;

    let results = {};
    try {
        const types = MACRO_CONF.map(c=>c.id).join(',');
        const r = await fetch(`${BASE}/api/macro_batch?types=${types}`);
        results = await r.json();
    } catch(e){
        const a = document.getElementById('macroAiArea');
        if(a) a.innerHTML = `<div class="err">실시간 데이터 수집 실패: ${e.message}</div>`;
        return;
    }

    MACRO_CONF.forEach(c=>{
        const res = results[c.id];
        const valEl = document.getElementById(`val_${c.id}`);
        const diffEl = document.getElementById(`diff_${c.id}`);
        if(!valEl) return;
        if(!res || res.status!=='000' || !res.data || res.data.length<2){
            valEl.innerText='조회불가'; valEl.style.color='#52525b'; valEl.style.fontSize='16px'; return;
        }
        const dataArr = res.data;
        const curVal  = (res.current ?? dataArr[dataArr.length-1].value);
        const prevVal = dataArr[dataArr.length-2].value;
        const diff    = (res.diff ?? (curVal - prevVal));
        const rate    = (res.rate ?? (prevVal ? (diff/prevVal)*100 : 0));
        window.macroData[c.id] = {title:c.title, cat:c.cat, unit:c.unit, value:curVal, diff:diff, rate:rate, vol:c.vol};

        valEl.style.color = '#f4f4f5';
        valEl.innerText = fmtMacro(curVal, c.dec);
        const diffStr = diff>0 ? `▲ ${fmtMacro(Math.abs(diff),c.dec)} (${rate.toFixed(2)}%)`
                      : diff<0 ? `▼ ${fmtMacro(Math.abs(diff),c.dec)} (${Math.abs(rate).toFixed(2)}%)` : '-';
        diffEl.innerText = diffStr;
        diffEl.className = 'mc-diff ' + (diff>0?'up':diff<0?'dn':'');

        const th = VOL_TH[c.vol] || 1.5;
        const card = valEl.closest('.macro-card');
        if(card && Math.abs(rate) >= th){
            const col = diff>0?'#ef4444':'#3b82f6';
            card.style.boxShadow = `0 0 0 1px ${col}, 0 6px 20px rgba(0,0,0,.45)`;
            const t = card.querySelector('.mc-title');
            if(t && !t.querySelector('.abn')) t.insertAdjacentHTML('beforeend', `<span class="abn" style="font-size:11px;font-weight:800;color:${col};background:${col}26;padding:2px 6px;border-radius:4px;">급변</span>`);
        }

        const ctx = document.getElementById(`ch_${c.id}`).getContext('2d');
        const lineColor = diff>=0 ? '#ef4444' : '#3b82f6';
        macroCharts.push(new Chart(ctx, {
            type:'line',
            data:{ labels:dataArr.map(d=>d.time), datasets:[{ data:dataArr.map(d=>d.value), borderColor:lineColor, backgroundColor:lineColor+'1A', borderWidth:2, pointRadius:0, tension:0.1, fill:true }] },
            options:{ responsive:true, maintainAspectRatio:false, plugins:{legend:{display:false}, tooltip:{intersect:false, position:'nearest'}}, scales:{ x:{display:false}, y:{display:false, min:Math.min(...dataArr.map(d=>d.value))*0.995, max:Math.max(...dataArr.map(d=>d.value))*1.005} }, layout:{padding:0} }
        }));
    });
}

function renderWatchlist(main){
    let html = `<div class="sec-title" style="margin-top:0;">관심종목 보관함 <span style="color:#52525b;font-weight:500;font-size:13px;text-transform:none;">${favorites.length}건</span></div>`;
    if(favorites.length === 0){
        html += `<div class="empty"><strong>설정된 관심종목이 없습니다.</strong>‘회사별 재무제표 분석’ 탭에서 종목을 검색한 뒤 ‘관심종목 설정’ 버튼으로 추가하세요.</div>`;
        main.innerHTML = html; return;
    }
    html += `<div class="dash-grid">` + favorites.map((fav, i) => `
        <div class="dash-card">
            <span class="dash-del" onclick="favorites.splice(${i},1);localStorage.setItem('quant_favs', JSON.stringify(favorites));renderWatchlist(document.getElementById('main'));">✖</span>
            <div class="dash-title" onclick="pickCorp(${JSON.stringify(fav).replace(/"/g, '&quot;')})">${fav.corp_name} <span class="dash-code">${fav.stock_code||''}</span></div>
            <div style="font-size:13px; color:#71717a; margin-top:12px;">설정일: ${fav.addedAt||'-'}</div>
        </div>`).join('') + `</div>`;
    main.innerHTML = html;
}

async function requestMacroAI(){
    const btn = document.getElementById('macroAiBtn');
    const area = document.getElementById('macroAiArea');
    if(!window.macroData || Object.keys(window.macroData).length === 0){
        if(area) area.innerHTML = `<div class="err">대시보드 데이터가 아직 로딩되지 않았습니다. 잠시 후 다시 시도하세요.</div>`; return;
    }
    const orig = btn.innerText; btn.disabled = true; btn.innerText = '🧠 글로벌 뉴스 수집 + 추론 중...';
    area.innerHTML = `<div class="spinner-wrap"><div class="spinner" style="border-top-color:#a855f7;"></div> 야후 글로벌 뉴스 수집 및 거시·미시 추론 중... (최대 30초)</div>`;

    const snap = window.macroData;
    const lines = Object.keys(snap).map(k=>{ const d=snap[k]; return `${d.title} [${d.cat}]: ${fmtMacro(d.value,2)}${d.unit} (${d.rate>0?'+':''}${d.rate.toFixed(2)}%)`; });
    const abn = Object.keys(snap).map(k=>({ ...snap[k], th:(VOL_TH[snap[k].vol]||1.5) }))
        .filter(d=>Math.abs(d.rate) >= d.th)
        .sort((a,b)=>Math.abs(b.rate/b.th) - Math.abs(a.rate/a.th))
        .map(d=>`${d.title}: ${d.rate>0?'+':''}${d.rate.toFixed(2)}% (평소 임계 ±${d.th}% 초과)`);

    let news = [];
    try {
        const r = await fetch(`${BASE}/api/yahoo_news?q=${encodeURIComponent('stock market federal reserve economy oil inflation')}&n=10`);
        const d = await r.json();
        news = (d.news||[]).map(n=>`${n.title} (${n.publisher||''}${n.related?(' · 관련:'+n.related):''})`);
    } catch(e){}

    const isoToday = new Date().toISOString().slice(0,16).replace('T',' ');
    const prompt = `당신은 글로벌 매크로 헤지펀드의 수석 이코노미스트 겸 전략가다. 오늘은 ${isoToday} 이다.
[절대 원칙]
1. 사전지식·고정관념 배제. 데이터에 없는 수치를 지어내지 마라.
2. 거시(글로벌 경기·금리·달러)와 미시(섹터별 영향)를 분리해 다뤄라.
3. 한국 투자자 관점에서 원화·수출주·반도체 등으로의 전이 경로를 연결하라.

[실시간 글로벌 지표 스냅샷]
${lines.join('\n')}

[비정상 급변 항목]
${abn.length ? abn.join('\n') : '없음'}

[야후 파이낸스 헤드라인]
${news.length ? news.map((n,i)=>`(${i+1}) ${n}`).join('\n') : '(뉴스 수집 실패)'}

[출력 형식] 순수 JSON 하나만 출력.
{
 "as_of":"${isoToday}",
 "regime":"현재 시장 국면을 한 구절로 (예: 위험회피·달러강세 국면)",
 "risk_sentiment":"Risk-On | Risk-Off | 혼조 중 하나",
 "regime_desc":"국면 진단 2~3문장",
 "abnormal":[{"name":"항목명","move":"+0.0%","read":"왜 튀었고 무엇을 시사하는가"}],
 "macro_view":"거시 관점 종합 해석 4~6문장",
 "micro_view":[{"sector":"섹터/자산군","impact":"호재|악재|중립","note":"근거"}],
 "cross_asset":[{"signal":"교차자산 신호","insight":"해석"}],
 "kr_implication":"한국 증시·원화 함의 3~4문장",
 "watch":[{"item":"주시할 지표","why":"이유"}],
 "takeaway":"한 줄 결론"
}`;

    try {
        const r = await fetch(`${BASE}/api/ai`, { method:'POST', body:JSON.stringify({prompt}) });
        const d = await r.json();
        if(d.status==='error') throw new Error(d.message);
        let clean = d.advice.replace(/```json/g,'').replace(/```/g,'').trim();
        clean = clean.replace(/,\s*([\]}])/g, '$1'); 
        const s = clean.indexOf('{'), e = clean.lastIndexOf('}'); 
        if(s>=0 && e>s) clean = clean.slice(s, e+1);
        let obj; try{ obj = JSON.parse(clean); }
        catch(err){ area.innerHTML = `<div class="ai-comment-box" style="display:block;"><strong>[AI 거시 진단]</strong><br>${d.advice.replace(/\n/g,'<br>')}</div>`; return; }
        renderMacroAI(obj);
    } catch(e){
        area.innerHTML = `<div class="err"><strong>AI 진단 실패</strong><br>${e.message}</div>`;
    } finally { btn.disabled=false; btn.innerText=orig; }
}

function renderMacroAI(q){
    const area = document.getElementById('macroAiArea');
    const senti = (q.risk_sentiment||'').toLowerCase();
    const sentiColor = senti.includes('off') ? '#3b82f6' : senti.includes('on') ? '#ef4444' : '#d8aa5c';
    let h = `<div style="margin-bottom:40px;border:1px solid #a855f7;border-radius:16px;overflow:hidden; box-shadow:0 12px 40px rgba(168,85,247,0.2);">`;
    h += `<div style="background:linear-gradient(135deg,#1a1530,#121217);padding:32px 40px;">
        <div style="font-family:'JetBrains Mono',monospace;font-size:13px;letter-spacing:.15em;color:#c4b5fd;text-transform:uppercase; font-weight:800;">AI MACRO REGIME · 거시 국면 진단 <span style="color:#52525b;margin-left:12px;">AS OF ${q.as_of||''}</span></div>
        <div style="display:flex;align-items:center;gap:20px;margin-top:16px;flex-wrap:wrap;">
            <span style="font-size:32px;font-weight:900;color:#fff;">${q.regime||'-'}</span>
            <span style="font-family:'JetBrains Mono',monospace;font-size:15px;font-weight:800;padding:8px 16px;border-radius:8px;background:${sentiColor}22;color:${sentiColor};">${q.risk_sentiment||'-'}</span>
        </div>
        <div style="color:#d4d4d8;font-size:16px;line-height:1.8;margin-top:16px;">${q.regime_desc||''}</div>
    </div>`;
    h += `<div style="padding:32px 40px;background:#121217;">`;
    if((q.abnormal||[]).length){
        h += `<div class="sec-title" style="margin-top:0;">⚡ 비정상 급변 항목</div><div class="scn" style="margin-bottom:32px;">`;
        q.abnormal.forEach(a=>{ const up=String(a.move||'').includes('+'); h += `<div class="sc dir-${up?'up':'down'}"><div class="top"><span style="color:#fff;font-weight:800;font-size:16px;flex:1;">${a.name||''}</span><span style="font-family:'JetBrains Mono',monospace;font-weight:900;font-size:16px;color:${up?'#ef4444':'#3b82f6'};">${a.move||''}</span></div><div class="why">${a.read||''}</div></div>`; });
        h += `</div>`;
    }
    if(q.macro_view){ h += `<div class="sec-title">🌐 거시 관점 (Macro)</div><div class="freshness" style="margin-top:0;margin-bottom:32px;background:rgba(216,170,92,.06);border-color:rgba(216,170,92,.3);border-left-color:#d8aa5c;color:#e8d9b5;">${q.macro_view}</div>`; }
    
    // (격리) 퀀트 테이블 오염 방지용 wrap-q 할당
    h += `<div class="wrap-q">`;
    if((q.micro_view||[]).length){ h += `<div class="sec-title">🔬 미시 관점 (섹터·자산군 영향)</div><table class="macro-tbl" style="margin-bottom:32px;"><tbody>${q.micro_view.map(m=>`<tr><td class="mk">${m.sector||''}</td><td class="mv">${m.note||''}<span class="imp-pill ${impClass(m.impact)}">${m.impact||'중립'}</span></td></tr>`).join('')}</tbody></table>`; }
    h += `</div>`;
    
    if((q.cross_asset||[]).length){ h += `<div class="sec-title">🔗 교차자산 신호</div>${q.cross_asset.map(c=>`<div class="nd-card"><div class="nd-head">${c.signal||''}</div><div class="nd-insight">${c.insight||''}</div></div>`).join('')}`; }
    if(q.kr_implication){ h += `<div class="sec-title">🇰🇷 한국 시장 함의</div><div class="freshness" style="margin-top:0;margin-bottom:32px;">${q.kr_implication}</div>`; }
    if((q.watch||[]).length){ h += `<div class="sec-title">👁 앞으로 주시할 것</div><div class="traps" style="margin-bottom:32px;">${q.watch.map(w=>`<div class="trap"><span class="no" style="color:#d8aa5c;">▶</span><span class="tx"><b style="color:#fff;">${w.item||''}</b> — ${w.why||''}</span></div>`).join('')}</div>`; }
    if(q.takeaway){ h += `<div class="final-hero" style="margin-top:16px; box-shadow:none;"><div class="fh-lab">Takeaway · 핵심 결론</div><div class="fh-line" style="font-size:18px;color:#fff;margin-top:16px;">${q.takeaway}</div></div>`; }
    h += `</div></div>`;
    area.innerHTML = h;
    area.scrollIntoView({behavior:'smooth', block:'start'});
}

// ==========================================
// 4. 퀀트 AI 시나리오 분석 (완벽한 단일 기간 추론 및 확신도 강제 적용)
// ==========================================
function renderQuantumTab(main){
    if(!corp){ main.innerHTML = '<div class="empty"><strong>종목을 먼저 검색하세요.</strong>퀀트 리포트를 생성하기 위해 기준 데이터가 필요합니다.</div>'; return; }
    main.innerHTML = `<div class="quant-opts"><h2 style="font-size:20px; font-weight:900; color:#e4e4e7; margin-bottom:16px;">AI 시나리오 분석 · 실시간 그라운딩</h2><p class="quant-opts-desc">오늘 날짜 기준 <b>네이버 뉴스 · DART 재무 · 실시간 시세</b>에 더해 <b>야후 파이낸스 글로벌 매크로(지수·금리·환율·원자재·곡물)와 글로벌 헤드라인</b>까지 수집하여, 가치투자(버핏) · 거시경제(케인즈) · 트레이더(모건) · 회계 <b>4개 관점</b>과 <b>글로벌→국내 전이경로</b> 추론으로 멀티 시나리오 리포트를 생성합니다.</p><div class="quant-btn-group"><button class="btn q-btn" onclick="generateQuantumReport('1주일')">1주일</button><button class="btn q-btn" onclick="generateQuantumReport('1개월')">1개월</button><button class="btn q-btn" onclick="generateQuantumReport('3개월')">3개월</button><button class="btn q-btn" onclick="generateQuantumReport('6개월')">6개월</button><button class="btn q-btn" onclick="generateQuantumReport('1년')">1년</button><button class="btn ai-btn q-btn" onclick="generateQuantumReport('종합')">전체 종합 분석 (Deep Dive)</button></div></div><div id="aiResultArea"></div><div id="archiveArea"></div>`;
    renderArchive();
}

async function gatherMarketContext(){
    const ctx = { companyNews:[], marketNews:[], macroNews:[], momentum:null, priceTrend:null, macroData:[], macroAbnormal:[], globalNews:[] };
    const safeNews = async (kw,n)=>{ try{ const r=await fetch(`${BASE}/api/news?keyword=${encodeURIComponent(kw)}&display=${n}`); const d=await r.json(); return (d.items||[]).map(it=>({ title:it.title.replace(/<[^>]*>?/gm,'').replace(/&quot;/g,'"').replace(/&amp;/g,'&'), desc:it.description.replace(/<[^>]*>?/gm,'').replace(/&quot;/g,'"').replace(/&amp;/g,'&'), date:it.pubDate })); }catch(e){ return []; } };
    const [cNews, oNews, mNews, fxNews] = await Promise.all([ safeNews(corp.corp_name, 8), safeNews(`${corp.corp_name} 실적 전망 목표주가`, 5), safeNews('코스피 증시 전망 외국인 기관 수급', 5), safeNews('원달러 환율 나스닥 S&P500 미국 증시', 5) ]);
    const seen = new Set(); ctx.companyNews = cNews.concat(oNews).filter(n=>{ if(seen.has(n.title))return false; seen.add(n.title); return true; }).slice(0,10); ctx.marketNews = mNews; ctx.macroNews = fxNews;

    try {
        const types = MACRO_CONF.map(c=>c.id).join(',');
        const r = await fetch(`${BASE}/api/macro_batch?types=${types}`);
        const res = await r.json();
        MACRO_CONF.forEach(c=>{
            const d = res[c.id];
            if(!d || d.status!=='000' || !d.data || d.data.length<2) return;
            const arr = d.data;
            const cur  = (d.current ?? arr[arr.length-1].value);
            const prev = arr[arr.length-2].value;
            const diff = (d.diff ?? (cur-prev));
            const rate = (d.rate ?? (prev ? (diff/prev)*100 : 0));
            ctx.macroData.push(`${c.title} [${c.cat}]: ${cur.toLocaleString('en-US',{maximumFractionDigits:c.dec})}${c.unit} (${rate>0?'+':''}${rate.toFixed(2)}%)`);
            const th = VOL_TH[c.vol] || 1.5;
            if(Math.abs(rate) >= th) ctx.macroAbnormal.push(`${c.title}: ${rate>0?'+':''}${rate.toFixed(2)}% (평소 임계 ±${th}% 초과 → 비정상 급변)`);
        });
    } catch(e){}

    if(ctx.macroData.length === 0){
        const scrapeVal = (id) => { const el = document.getElementById(id); return el ? el.innerText : '조회불가'; };
        ['KOSPI','KOSDAQ','SNP500','NASDAQ','USD'].forEach(id=>{ const v=scrapeVal('val_'+id); if(v && v!=='조회불가' && v!=='···') ctx.macroData.push(`${id}: ${v} (${scrapeVal('diff_'+id)})`); });
    }

    try {
        const r = await fetch(`${BASE}/api/yahoo_news?q=${encodeURIComponent('stock market federal reserve economy oil inflation semiconductor')}&n=8`);
        const d = await r.json();
        ctx.globalNews = (d.news||[]).map(n=>({ title:n.title, publisher:n.publisher||'', related:n.related||'' }));
    } catch(e){}

    if(corp.stock_code){ try{ const r=await fetch(`${BASE}/api/chart?code=${corp.stock_code}&period=3M`); const d=await r.json(); if(d.status==='000' && d.list && d.list.length){ const rows = d.list.slice().reverse(); const closes = rows.map(x=>parseInt(x.stck_clpr)).filter(v=>!isNaN(v)); const labels = rows.map(x=>x.stck_bsop_date.replace(/(\d{4})(\d{2})(\d{2})/,'$2/$3')); if(closes.length>1){ const cur=closes[closes.length-1], first=closes[0]; const hi=Math.max(...closes), lo=Math.min(...closes); ctx.momentum = { current:cur, high:hi, low:lo, fromHigh:(((cur-hi)/hi)*100).toFixed(1), fromLow:(((cur-lo)/lo)*100).toFixed(1), chg3m:(((cur-first)/first)*100).toFixed(1) }; ctx.priceTrend = { labels, data: closes }; } } }catch(e){} }
    return ctx;
}

function buildDeepPrompt(periodType, ctx, sumData){
    const isoToday = new Date().toISOString().slice(0,10);
    const pPrice = document.getElementById('sPrice')?.innerText || '-', pPer = document.getElementById('sPer')?.innerText || '-', pCap = document.getElementById('sCap')?.innerText || '-';
    const newsBlock = (arr,label)=> arr.length ? `[${label}]\n` + arr.map((n,i)=>`(${i+1}) ${n.title} — ${n.desc}`).join('\n') : `[${label}] (수집된 기사 없음)`;
    const mom = ctx.momentum ? `최근 3개월 주가: 현재 ${ctx.momentum.current.toLocaleString()}원 / 3개월 등락 ${ctx.momentum.chg3m}% / 3개월 고점 대비 ${ctx.momentum.fromHigh}% / 저점 대비 +${ctx.momentum.fromLow}%` : '주가 모멘텀 데이터 없음';
    const macroBlock = ctx.macroData.length ? `[실시간 글로벌 거시 지표]\n` + ctx.macroData.join('\n') : `[실시간 글로벌 거시 지표] 조회 실패`;
    const abnBlock = (ctx.macroAbnormal&&ctx.macroAbnormal.length) ? `[⚡ 비정상 급변 항목]\n` + ctx.macroAbnormal.join('\n') : `[⚡ 비정상 급변 항목] 뚜렷한 임계 초과 없음`;
    const gNewsBlock = (ctx.globalNews&&ctx.globalNews.length) ? `[야후 파이낸스 글로벌 헤드라인]\n` + ctx.globalNews.map((n,i)=>`(${i+1}) ${n.title}${n.publisher?(' — '+n.publisher):''}`).join('\n') : `[야후 파이낸스 글로벌 헤드라인] (수집된 기사 없음)`;
    
    const isSumDataEmpty = Object.keys(sumData).length === 0 || !Object.values(sumData).some(v => v['매출'] !== null);
    const sumDataNote = isSumDataEmpty ? "\n[주의] 현재 종목의 재무 데이터가 존재하지 않습니다. 없는 숫자를 지어내지 마십시오." : "";

    const horizon = periodType === '종합' 
        ? '1주일, 1개월, 3개월, 6개월, 1년 등 5개 구간을 모두 다루어라.' 
        : `분석 구간은 오직 "${periodType}" 한 가지이다. predictions 배열에는 이 단일 기간의 예측 1개만 포함하라.`;

    return `당신은 글로벌 투자 리서치 위원회다. 오늘은 ${isoToday} 이다.
[절대 원칙]
1. 사전지식 배제. 제공된 데이터만으로 추론하라. 숫자 지어내기 엄금.
2. 하방 리스크를 직설적으로 지적하라. 
3. 전문가로서 책임감 부여: 결론이 명확하다면 확신도(confidence)는 60~95 사이로 부여해라. 낮은 수치로 도망치지 마라.
4. [강제] perspectives 배열은 반드시 "가치투자", "매크로/성장", "수급/모멘텀", "회계/재무"의 4가지 관점을 모두 포함하여 정확히 4개의 객체를 반환하라.
5. [강제] predictions 배열의 ret(기대수익률) 값은 % 기호나 문자를 완전히 제외하고 순수 숫자(예: 5.2, -3.1)로만 적어라.

[분석 대상] 종목: ${corp.corp_name} (${corp.stock_code||'비상장'}) / 시세: 현재가 ${pPrice} / PER·PBR ${pPer} / 시가총액 ${pCap}
${mom}
${macroBlock}
${abnBlock}
${gNewsBlock}

[DART 재무 요약 — 단위:원, 최신연도→과거]${sumDataNote}
${JSON.stringify(sumData)}

${newsBlock(ctx.companyNews,'종목 관련 최신 뉴스')}
${newsBlock(ctx.marketNews,'국내 증시 · 수급 뉴스')}

[예측 구간] ${horizon}
[출력 형식] 마크다운 없이 순수 JSON 객체 하나만 출력하라.
{
  "as_of": "${isoToday}", "title": "${corp.corp_name} 멀티 관점 시나리오 분석", "subtitle": "한 줄 부제", "thesis": "핵심 논점", "freshness": "반영한 최신 정보 요약",
  "consensus": {"target_price":"목표주가","op_estimate":"영업이익 추정치","evaluation":"평가(1문장)"},
  "news_digest": [ {"headline":"요지","insight":"의미"} ],
  "macro_snapshot": [ {"name":"환율/증시 동향","read":"방향","impact":"호재/악재/중립"} ],
  "radar": {"내재가치":0,"성장성":0,"수익성":0,"재무안정성":0,"모멘텀":0,"시장심리":0},
  "perspectives": [ 
    {"lens":"가치투자","stance":"매수/매도/중립","score":0,"view":"내용"},
    {"lens":"매크로/성장","stance":"매수/매도/중립","score":0,"view":"내용"},
    {"lens":"수급/모멘텀","stance":"매수/매도/중립","score":0,"view":"내용"},
    {"lens":"회계/재무","stance":"매수/매도/중립","score":0,"view":"내용"}
  ],
  "traps": [ {"no":"1","tx":"함정"} ], "macro_up": [ {"k":"우호 요인","v":"설명"} ], "macro_down": [ {"k":"경계 요인","v":"설명"} ],
  "predictions": [ {"period":"기간","trend":"상승/하락/횡보","prob":0,"ret":0,"desc":"근거"} ],
  "scenarios": [ {"type":"down","badge":"Bear","name":"하락 시나리오","return":"-00%","prob":0,"why":"논리"} ],
  "levels": [ {"price":"가격","desc":"단기 저항","type":"res"} ],
  "catalysts": [ {"when":"단기","event":"이벤트","dir":"up/down","note":"설명"} ],
  "final_call": {"stance":"스탠스","confidence":0,"one_liner":"결론"}
}`;
}

async function generateQuantumReport(periodType){
    const btns = document.querySelectorAll('.q-btn');
    btns.forEach(b => { b.disabled = true; b.style.opacity = '0.5'; });
    
    const resultArea = document.getElementById('aiResultArea');
    destroyQCharts();
    resultArea.innerHTML = '<div class="spinner-wrap"><div class="spinner" style="border-top-color:#ef4444;"></div> 구글 제미나이 4개 관점 AI 추론 중... (최대 30초 소요, 과부하 시 6회 자동 재시도)</div>';

    const sumData = {};
    years.forEach(y=>{ sumData[y] = {'자산':pick(y,['자산총계'],BS), '매출':pick(y,['매출액','영업수익'],IS), '영업이익':pick(y,['영업이익'],IS), '순이익':pick(y,['당기순이익'],IS)}; });
    const finTrend = { years: years.slice().reverse(), 매출:[], 영업이익:[], 순이익:[] };
    finTrend.years.forEach(y=>{ finTrend.매출.push(toEok(pick(y,['매출액','영업수익'],IS))); finTrend.영업이익.push(toEok(pick(y,['영업이익'],IS))); finTrend.순이익.push(toEok(pick(y,['당기순이익'],IS))); });

    let ctx; try{ ctx = await gatherMarketContext(); }catch(e){ ctx = {companyNews:[],marketNews:[],macroNews:[],momentum:null,priceTrend:null,macroData:[]}; }
    const prompt = buildDeepPrompt(periodType, ctx, sumData);

    try{
        const r = await fetch(`${BASE}/api/ai`, { method:'POST', body:JSON.stringify({prompt}) });
        const d = await r.json();
        if(d.status==='error') throw new Error(d.message);
        
        let clean = d.advice.replace(/```json/g,'').replace(/```/g,'').trim();
        const s = clean.indexOf('{'), e = clean.lastIndexOf('}'); 
        if(s>=0 && e>s) clean = clean.slice(s, e+1);
        
        const q = JSON.parse(clean);
        const meta = { corp_name: corp.corp_name, stock_code: corp.stock_code||'', periodType, savedAt: new Date().toLocaleString('ko-KR'), priceText: document.getElementById('sPrice').innerText };
        lastReport = { q, meta, finTrend, priceTrend: ctx.priceTrend };
        renderReport(lastReport, false);
    }catch(e){
        let errStr = e.message;
        if(errStr.includes('503') || errStr.includes('429')) errStr = "AI 서버 대기열이 매우 깁니다(구글 클라우드 과부하). 자동 재시도 6회마저 모두 실패했습니다. 잠시 후 다시 시도해주세요.";
        else if(errStr.includes('JSON')) errStr = "AI가 반환한 데이터를 분석하는 중 구조적 에러가 발생했습니다 (JSON Parsing Error). 다시 시도해 주세요.";
        resultArea.innerHTML = `<div class="err"><strong>분석 중 오류 발생</strong><br>${errStr}</div>`;
    } finally {
        btns.forEach(b => { b.disabled = false; b.style.opacity = '1'; });
    }
}

function renderReport(report, isArchived){
    const resultArea = document.getElementById('aiResultArea'); destroyQCharts();
    const q = report.q, meta = report.meta, sc = q.scenarios || [], fc = q.final_call || {};
    
    // 확률 정규화
    let pDown = num((sc.find(s=>s.type==='down')||{}).prob) || 33;
    let pFlat = num((sc.find(s=>s.type==='flat')||{}).prob) || 34;
    let pUp = num((sc.find(s=>s.type==='up')||{}).prob) || 33;
    const totProb = pDown + pFlat + pUp;
    if (totProb > 0 && totProb !== 100) { pDown = Math.round((pDown/totProb)*100); pFlat = Math.round((pFlat/totProb)*100); pUp = 100 - pDown - pFlat; }
    
    const conf = Math.max(0,Math.min(100, parseInt(fc.confidence)||50));
    const titleHtml = meta.corp_name ? (q.title||'').split(meta.corp_name).join(`<em>${meta.corp_name}</em>`) : (q.title||'');
    let n=0; const sec = (t)=>{ n++; return `<div class="q-sec-h"><span class="q-sec-n">${String(n).padStart(2,'0')}</span><h2>${t}</h2></div>`; };
    const hasFin = report.finTrend && report.finTrend.years && report.finTrend.years.length && (report.finTrend.매출.some(v=>v!=null));
    const hasPrice = report.priceTrend && report.priceTrend.data && report.priceTrend.data.length;
    
    // 버튼
    const actions = isArchived ? 
        `<div class="q-actions"><button class="q-actbtn pdf" onclick="downloadPDF()">PDF 저장</button><button class="q-actbtn del" onclick="clearReportView()">닫기 (목록으로)</button></div>` : 
        `<div class="q-actions"><button class="q-actbtn pdf" onclick="downloadPDF()">PDF 저장</button><button class="q-actbtn save" onclick="saveCurrentReport()">보관함에 저장</button></div>`;

    let html = `<div class="wrap-q" id="qReport">`;
    html += `<header class="q-head"><div class="kicker">Multi-Lens Scenario Note ${isArchived?'· 보관본':''}</div><h1 class="q-title">${titleHtml}<span class="asof-badge">AS OF ${q.as_of||meta.savedAt}</span></h1><p style="color:#a1a1aa; margin-top:16px; font-size:17px;">${q.subtitle||''}</p></header>`;
    
    if(q.freshness) html += `<div class="freshness"><b>분석에 반영한 최신 정보:</b> ${q.freshness}</div>`;
    
    if(q.consensus && (q.consensus.target_price || q.consensus.op_estimate)){
        html += `<div style="margin-top:24px;background:rgba(216,170,92,0.08);border:1px solid rgba(216,170,92,0.3);border-left:4px solid var(--q-gold);border-radius:12px;padding:24px;"><div style="font-family:'JetBrains Mono',monospace;font-size:13px;color:var(--q-gold);letter-spacing:.1em;margin-bottom:12px;text-transform:uppercase; font-weight:800;">Market Consensus Check</div><div style="display:flex;gap:24px;flex-wrap:wrap;margin-bottom:12px;"><div style="flex:1;min-width:200px;"><span style="color:#a1a1aa;font-size:14px;">증권사 목표주가:</span> <b style="color:#fff;font-size:17px;font-family:'JetBrains Mono',monospace;margin-left:8px;">${q.consensus.target_price}</b></div><div style="flex:1;min-width:200px;"><span style="color:#a1a1aa;font-size:14px;">영업이익 추정치:</span> <b style="color:#fff;font-size:17px;font-family:'JetBrains Mono',monospace;margin-left:8px;">${q.consensus.op_estimate}</b></div></div><div style="font-size:15px;color:#d4d4d8;line-height:1.7;border-top:1px dashed #3f3f46;padding-top:12px;">${q.consensus.evaluation}</div></div>`;
    }
    
    html += `<div class="final-hero"><div class="fh-lab">Final Call · 종합 결론</div><div class="fh-stance">${fc.stance||'-'}</div><div class="fh-line">${fc.one_liner||''}</div><div class="gauge"><i style="width:${conf}%"></i></div><div class="gauge-lab">분석 확신도(Confidence) ${conf}%</div></div>`;
    html += actions;
    
    html += `<div class="q-thesis"><div class="lab">Core Thesis</div><p>${q.thesis||''}</p></div>`;
    html += sec('멀티 관점 종합 진단');
    html += `<div class="qchart-box tall"><span class="qchart-cap">관점별 정량 스코어 (0~100)</span><div class="qchart-inner"><canvas id="chRadar"></canvas></div></div>`;
    
    if((q.perspectives||[]).length) {
        html += `<div class="persp-grid">${q.perspectives.map(p=>`<div class="persp-card"><h4>${p.lens} <span class="persp-pill ${stancePill(p.stance)}">${p.stance||'-'}</span></h4><div class="persp-score">스코어 ${p.score!=null?p.score:'-'} / 10</div><div class="persp-view">${p.view||''}</div></div>`).join('')}</div>`;
    }
    
    if((q.traps||[]).length){ 
        html += sec('시장 심리의 함정'); 
        html += `<div class="traps">${q.traps.map(t=>`<div class="trap"><span class="no">${t.no}</span><span class="tx">${t.tx}</span></div>`).join('')}</div>`; 
    }
    
    if((q.news_digest||[]).length){ 
        html += sec('최신 뉴스 종합 (시의성)'); 
        html += q.news_digest.map(nd=>`<div class="nd-card"><div class="nd-head">${nd.headline||''}</div><div class="nd-insight">${nd.insight||''}</div></div>`).join(''); 
    }
    
    if((q.macro_snapshot||[]).length){ 
        html += sec('거시 지표 스냅샷'); 
        html += `<table class="macro-tbl"><tbody>${q.macro_snapshot.map(m=>`<tr><td class="mk">${m.name||''}</td><td class="mv">${m.read||''}<span class="imp-pill ${impClass(m.impact)}">${m.impact||'중립'}</span></td></tr>`).join('')}</tbody></table>`; 
    }
    
    if((q.macro_up||[]).length || (q.macro_down||[]).length){ 
        html += sec('거시 / 심리 환경'); 
        html += `<div class="q-grid2"><div class="q-card"><h3>우호적 (Tailwind)</h3><table>${(q.macro_up||[]).map(m=>`<tr><td class="k">${m.k}</td><td class="v" style="color:var(--q-up)">${m.v}</td></tr>`).join('')}</table></div><div class="q-card"><h3>경계 (Headwind)</h3><table>${(q.macro_down||[]).map(m=>`<tr><td class="k">${m.k}</td><td class="v" style="color:var(--q-down)">${m.v}</td></tr>`).join('')}</table></div></div>`; 
    }
    
    if((q.predictions||[]).length){ 
        html += sec('기간별 확률 및 방향성'); 
        html += `<div class="qchart-box"><span class="qchart-cap">기간별 기대수익률(%)</span><div class="qchart-inner"><canvas id="chPred"></canvas></div></div>`; 
        html += `<div class="q-grid2" style="margin-bottom:24px;">${q.predictions.map(p=>`<div style="background:var(--q-panel);border:1px solid var(--q-line);border-radius:12px;padding:20px 24px;"><span style="color:var(--q-gold);font-family:'JetBrains Mono',monospace;font-weight:800;margin-right:16px;display:inline-block;width:60px; font-size:15px;">${p.period}</span><span style="color:#fff;font-size:17px;font-weight:800;">${p.trend} (확률 ${p.prob}%)</span><span style="font-family:'JetBrains Mono',monospace;font-size:16px;margin-left:12px;font-weight:800;color:${num(p.ret)>0?'var(--q-up)':(num(p.ret)<0?'var(--q-down)':'var(--q-flat)')};">${num(p.ret)>0?'+':''}${num(p.ret)}%</span><br><span style="font-size:15px;color:#a1a1aa;display:block;margin-top:12px;line-height:1.7;margin-left:76px;">${p.desc||''}</span></div>`).join('')}</div>`; 
    }
    
    if(sc.length){ 
        html += sec('시나리오 확률 분포'); 
        html += `<div class="prob-bar-container">`;
        if(pDown > 0) html += `<div class="prob-bar-segment" style="width:${pDown}%; background:var(--q-down);">하락 ${pDown}%</div>`;
        if(pFlat > 0) html += `<div class="prob-bar-segment" style="width:${pFlat}%; background:var(--q-flat);">횡보 ${pFlat}%</div>`;
        if(pUp > 0) html += `<div class="prob-bar-segment" style="width:${pUp}%; background:var(--q-up);">상승 ${pUp}%</div>`;
        html += `</div><div class="scn" style="margin-bottom:32px;">${sc.map(s=>`<div class="sc dir-${s.type}"><div class="top"><span class="badge">${s.badge}</span><span style="color:#fff;font-weight:800;flex:1;font-size:17px;">${s.name}</span><span style="font-family:'JetBrains Mono',monospace;font-size:16px;font-weight:800;color:var(--${s.type==='down'?'q-down':(s.type==='up'?'q-up':'q-flat')})">${s.return} · ${s.prob}%</span></div><div class="why">${s.why}</div></div>`).join('')}</div>`; 
    }
    
    if((q.levels||[]).length){ 
        html += sec('주요 가격 레벨'); 
        html += `<div class="ladder">${q.levels.map(l=>`<div class="lvl"><span class="px">${l.price}</span><span style="flex:1;color:#d4d4d8;font-size:15px;">${l.desc}</span><span class="pill ${l.type}">${(l.type||'').toUpperCase()}</span></div>`).join('')}</div>`; 
    }
    
    if((q.catalysts||[]).length){ 
        html += sec('핵심 촉매 (Catalysts)'); 
        html += q.catalysts.map(c=>`<div class="cata"><span class="cwhen">${c.when||''}</span><span class="cdir ${c.dir==='down'?'down':'up'}">${c.dir==='down'?'▼':'▲'}</span><span class="ctx"><b>${c.event||''}</b><br>${c.note||''}</span></div>`).join(''); 
    }
    
    if(hasFin){ 
        html += sec('재무 추이 (DART · 억원)'); 
        html += `<div class="qchart-box"><span class="qchart-cap">매출 · 영업이익 · 순이익 추이</span><div class="qchart-inner"><canvas id="chFin"></canvas></div></div>`; 
    }
    if(hasPrice){ 
        html += sec('최근 3개월 주가 추이'); 
        html += `<div class="qchart-box"><span class="qchart-cap">종가 (KIS)</span><div class="qchart-inner"><canvas id="chPrice"></canvas></div></div>`; 
    }
    
    html += `<div class="q-footnote">데이터 출처 — 재무: DART OpenAPI · 시세/차트: 한국투자증권(KIS) · 뉴스/지표: 네이버 검색 API · 추론: 생성형 AI. 본 리포트는 공개 데이터와 AI 추론에 기반한 참고 자료이며, 특정 종목의 매매를 권유하지 않습니다.</div></div>`;
    
    resultArea.innerHTML = html;
    setTimeout(() => { initReportCharts(report); }, 150);
}

function initReportCharts(report){
    const q = report.q, GREEN='#ef4444',RED='#3b82f6',GOLD='#d8aa5c',BLUE='#a855f7',GREY='#a1a1aa',LINE='#27272a';
    const baseScales = { x:{grid:{color:LINE, display:false},ticks:{color:GREY,maxRotation:0,autoSkip:true,maxTicksLimit:8}}, y:{grid:{color:LINE},ticks:{color:GREY}} };
    
    if(document.getElementById('chRadar') && q.radar && Object.keys(q.radar).length){
        const labels=Object.keys(q.radar), data=labels.map(k=>num(q.radar[k]));
        qCharts.push(new Chart(document.getElementById('chRadar').getContext('2d'),{ type:'radar', data:{labels,datasets:[{label:'스코어',data,fill:true,backgroundColor:'rgba(216,170,92,0.18)',borderColor:GOLD,borderWidth:2,pointBackgroundColor:GOLD,pointRadius:4}]}, options:{animation:false,responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}}, scales:{r:{min:0,max:100,ticks:{stepSize:20,color:'#71717a',backdropColor:'transparent',font:{size:11,weight:'bold'}},grid:{color:LINE},angleLines:{color:LINE},pointLabels:{color:'#e4e4e7',font:{size:13,weight:'bold'}}}}} }));
    }
    if(document.getElementById('chPred') && (q.predictions||[]).length){
        const labels=q.predictions.map(p=>p.period), vals=q.predictions.map(p=>num(p.ret)), colors=vals.map(v=> v>0?GREEN : v<0?RED : GOLD);
        qCharts.push(new Chart(document.getElementById('chPred').getContext('2d'),{ type:'bar', data:{labels,datasets:[{data:vals,backgroundColor:colors,borderRadius:6, maxBarThickness:60, categoryPercentage:0.5, barPercentage:0.8}]}, options:{animation:false,responsive:true,maintainAspectRatio:false, plugins:{legend:{display:false},tooltip:{callbacks:{label:c=>` ${c.parsed.y>0?'+':''}${c.parsed.y}%`}}}, scales:baseScales} }));
    }
    if(document.getElementById('chFin') && report.finTrend && report.finTrend.years && report.finTrend.years.length){
        const ft=report.finTrend;
        qCharts.push(new Chart(document.getElementById('chFin').getContext('2d'),{ type:'line', data:{labels:ft.years.map(String),datasets:[ {label:'매출',data:ft.매출,borderColor:BLUE,backgroundColor:'transparent',borderWidth:3,pointRadius:3,tension:0.2,spanGaps:true}, {label:'영업이익',data:ft.영업이익,borderColor:GOLD,backgroundColor:'transparent',borderWidth:3,pointRadius:3,tension:0.2,spanGaps:true}, {label:'순이익',data:ft.순이익,borderColor:GREEN,backgroundColor:'transparent',borderWidth:3,pointRadius:3,tension:0.2,spanGaps:true} ]}, options:{animation:false,responsive:true,maintainAspectRatio:false, plugins:{legend:{display:true,labels:{color:GREY,boxWidth:14,font:{size:13}}}}, scales:baseScales} }));
    }
    if(document.getElementById('chPrice') && report.priceTrend && report.priceTrend.data && report.priceTrend.data.length){
        const pt=report.priceTrend;
        qCharts.push(new Chart(document.getElementById('chPrice').getContext('2d'),{ type:'line', data:{labels:pt.labels,datasets:[{label:'종가',data:pt.data,borderColor:GREEN,backgroundColor:'rgba(239,68,68,0.12)',borderWidth:2,pointRadius:0,fill:true,tension:0.1}]}, options:{animation:false,responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:baseScales} }));
    }
}

function saveCurrentReport(){ if(!lastReport) return; const id = 'rep_' + Date.now(); const rec = { id, ...lastReport.meta, q:lastReport.q, finTrend:lastReport.finTrend, priceTrend:lastReport.priceTrend }; savedReports.unshift(rec); try{ localStorage.setItem('quant_reports', JSON.stringify(savedReports)); } catch(e){ savedReports.shift(); alert('저장 공간 부족.'); return; } renderArchive(); alert(`저장되었습니다.`); }
function renderArchive(){ const area = document.getElementById('archiveArea'); if(!area) return; if(!savedReports.length){ area.innerHTML = `<div class="sec-title">시나리오 보관함</div><div class="empty" style="padding:40px;">저장된 리포트가 없습니다.</div>`; return; } let h = `<div class="sec-title">시나리오 보관함 <span style="color:#52525b;font-weight:500;font-size:13px;text-transform:none;margin-left:8px;">${savedReports.length}건</span></div><div class="dash-grid">`; savedReports.forEach(r=>{ const stance = (r.q && r.q.final_call && r.q.final_call.stance) ? r.q.final_call.stance : '-'; h += `<div class="dash-card arch-card"><span class="dash-del" onclick="deleteSavedReport('${r.id}')">✖</span><div class="dash-title" onclick="viewSavedReport('${r.id}')">${r.corp_name} <span class="dash-code">${r.stock_code||''}</span></div><div class="arch-meta"><span class="arch-tag">${r.periodType}</span><span style="color:#d8aa5c;font-weight:800;">${stance}</span></div><div style="font-size:12px;color:#52525b;margin-top:12px;">${r.savedAt}</div></div>`; }); area.innerHTML = h + `</div>`; }
function viewSavedReport(id){ const r = savedReports.find(x=>x.id===id); if(!r) return; lastReport = { q:r.q, meta:{corp_name:r.corp_name, stock_code:r.stock_code, periodType:r.periodType, savedAt:r.savedAt, priceText:r.priceText}, finTrend:r.finTrend, priceTrend:r.priceTrend }; renderReport(lastReport, true); window.scrollTo({top:0, behavior:'smooth'}); }
function deleteSavedReport(id){ if(!confirm('삭제할까요?')) return; savedReports = savedReports.filter(x=>x.id!==id); localStorage.setItem('quant_reports', JSON.stringify(savedReports)); renderArchive(); }
function clearReportView(){ const a=document.getElementById('aiResultArea'); if(a){ destroyQCharts(); a.innerHTML=''; } const arch=document.getElementById('archiveArea'); if(arch) arch.scrollIntoView({behavior:'smooth'}); }
function downloadPDF(){ const el = document.getElementById('qReport'); if(!el) return; if(typeof html2pdf === 'undefined'){ alert('PDF 모듈 오류.'); return; } const acts = el.querySelectorAll('.q-actions'); acts.forEach(a=>a.style.visibility='hidden'); const nm = (lastReport && lastReport.meta ? lastReport.meta.corp_name : '리포트') + '_' + new Date().toISOString().slice(0,10) + '.pdf'; const opt = { margin:[8,8,8,8], filename:nm, image:{type:'jpeg',quality:0.98}, html2canvas:{scale:2, backgroundColor:'#0f0f17', useCORS:true, logging:false}, jsPDF:{unit:'mm', format:'a4', orientation:'portrait'}, pagebreak:{mode:['css','legacy']} }; html2pdf().set(opt).from(el).save().then(()=>acts.forEach(a=>a.style.visibility='visible')).catch(()=>acts.forEach(a=>a.style.visibility='visible')); }

async function fetchNews(keyword) {
    const main = document.getElementById('main'); main.innerHTML = '<div class="spinner-wrap"><div class="spinner"></div> 실시간 기사 수집 중...</div>';
    try {
        const r = await fetch(`${BASE}/api/news?keyword=${encodeURIComponent(keyword)}&display=15`); const d = await r.json();
        if(d.items?.length > 0) { let html = `<div class="sec-title" style="margin-top:0;">'${keyword}' 실시간 네이버 뉴스</div>`; d.items.forEach(item => { const title = item.title.replace(/<[^>]*>?/gm, '').replace(/&quot;/g, '"'); const desc = item.description.replace(/<[^>]*>?/gm, '').replace(/&quot;/g, '"'); const date = new Date(item.pubDate).toLocaleString('ko-KR'); html += `<div class="news-item"><a href="${item.link}" target="_blank" class="news-title">${title}</a><div class="news-desc">${desc}</div><div class="news-date">${date}</div></div>`; }); main.innerHTML = html; } else main.innerHTML = `<div class="empty">관련 뉴스가 없습니다.</div>`;
    } catch(e) { main.innerHTML = `<div class="err">뉴스 수집 실패: ${e.message}</div>`; }
}
</script>
</body>
</html>
'''

# ─────────────────────────────────────────
# 백엔드 핵심 로직 (에러 홀딩 및 봇 우회 탑재)
# ─────────────────────────────────────────

def ask_gemini_ai(prompt):
    target_model = "models/gemini-1.5-flash"
    try:
        list_url = f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_API_KEY}"
        with urllib.request.urlopen(urllib.request.Request(list_url), timeout=10) as resp:
            models_data = json.loads(resp.read())
            blacklist = ['robotics', 'preview', 'experimental', 'deprecated', 'tuning', 'embed', 'vision', 'aqa']
            available_models = [m['name'] for m in models_data.get('models', []) if 'generateContent' in m.get('supportedGenerationMethods', []) and 'gemini' in m['name'].lower() and not any(b in m['name'].lower() for b in blacklist)]
            if available_models:
                target_model = next((m for m in available_models if '1.5-flash' in m), None) or next((m for m in available_models if 'pro' in m), available_models[0])
    except: pass

    url = f"https://generativelanguage.googleapis.com/v1beta/{target_model}:generateContent?key={GEMINI_API_KEY}"
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers={'Content-Type': 'application/json'}, method='POST')
    
    for attempt in range(6): 
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                res_json = json.loads(resp.read())
                reply = res_json['candidates'][0]['content']['parts'][0]['text']
                clean_reply = re.sub(r',\s*([\]}])', r'\1', reply)
                return {'status': '000', 'advice': clean_reply}
        except urllib.error.HTTPError as e:
            if e.code in [503, 429] and attempt < 5:
                time.sleep(2 + (attempt * 3)) 
                continue
            err_body = e.read().decode("utf-8")
            return {'status': 'error', 'message': f'HTTP {e.code}: {err_body}'}
        except Exception as e:
            if attempt < 5:
                time.sleep(3)
                continue
            return {'status': 'error', 'message': str(e)}


# ═══════════════════════════════════════════════════════════════
# 미국 종목 — Yahoo Finance 검색 + SEC EDGAR 재무 (API 키 불필요)
# ═══════════════════════════════════════════════════════════════

SEC_HEADERS = {
    'User-Agent': 'DART-Analyzer research@example.com',
    'Accept': 'application/json',
}

YAHOO_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Accept': '*/*',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': 'https://finance.yahoo.com/',
    'Origin': 'https://finance.yahoo.com',
}

# ── 더미 함수 (기존 호환성 유지) ──
def load_us_company_list():
    """야후 검색으로 대체되어 더 이상 목록 로딩 불필요"""
    print("[시스템] US 검색: Yahoo Finance 실시간 검색 방식 사용")

# ── 미국 종목 내장 DB (S&P500 핵심 + 나스닥100 + 주요 ETF) ──────────────
# 야후 API 차단·실패 시에도 검색이 항상 동작하도록 코드에 내장
def _make_us_entry(ticker, name, qtype='EQUITY', exchange='NASDAQ'):
    return {'corp_name': name, 'stock_code': ticker, 'ticker': ticker,
            'corp_code': ticker, 'cik': '', 'exchange': exchange,
            'quote_type': qtype, 'market': 'US'}

_US_TICKER_DB = [
    # ── DJIA 30 ──
    _make_us_entry('AAPL',  'Apple Inc.',                     exchange='NASDAQ'),
    _make_us_entry('MSFT',  'Microsoft Corporation',          exchange='NASDAQ'),
    _make_us_entry('AMZN',  'Amazon.com Inc.',                exchange='NASDAQ'),
    _make_us_entry('GOOGL', 'Alphabet Inc. (Class A)',        exchange='NASDAQ'),
    _make_us_entry('GOOG',  'Alphabet Inc. (Class C)',        exchange='NASDAQ'),
    _make_us_entry('META',  'Meta Platforms Inc.',            exchange='NASDAQ'),
    _make_us_entry('TSLA',  'Tesla Inc.',                     exchange='NASDAQ'),
    _make_us_entry('NVDA',  'NVIDIA Corporation',             exchange='NASDAQ'),
    _make_us_entry('BRK.B', 'Berkshire Hathaway Inc.',        exchange='NYSE'),
    _make_us_entry('JPM',   'JPMorgan Chase & Co.',           exchange='NYSE'),
    _make_us_entry('JNJ',   'Johnson & Johnson',              exchange='NYSE'),
    _make_us_entry('V',     'Visa Inc.',                      exchange='NYSE'),
    _make_us_entry('PG',    'Procter & Gamble Co.',           exchange='NYSE'),
    _make_us_entry('UNH',   'UnitedHealth Group Inc.',        exchange='NYSE'),
    _make_us_entry('HD',    'Home Depot Inc.',                exchange='NYSE'),
    _make_us_entry('MA',    'Mastercard Inc.',                exchange='NYSE'),
    _make_us_entry('DIS',   'The Walt Disney Company',        exchange='NYSE'),
    _make_us_entry('BAC',   'Bank of America Corp.',          exchange='NYSE'),
    _make_us_entry('XOM',   'Exxon Mobil Corporation',        exchange='NYSE'),
    _make_us_entry('WMT',   'Walmart Inc.',                   exchange='NYSE'),
    _make_us_entry('KO',    'The Coca-Cola Company',          exchange='NYSE'),
    _make_us_entry('PFE',   'Pfizer Inc.',                    exchange='NYSE'),
    _make_us_entry('MRK',   'Merck & Co. Inc.',               exchange='NYSE'),
    _make_us_entry('CVX',   'Chevron Corporation',            exchange='NYSE'),
    _make_us_entry('ABT',   'Abbott Laboratories',            exchange='NYSE'),
    _make_us_entry('PEP',   'PepsiCo Inc.',                   exchange='NASDAQ'),
    _make_us_entry('TMO',   'Thermo Fisher Scientific',       exchange='NYSE'),
    _make_us_entry('ACN',   'Accenture plc',                  exchange='NYSE'),
    _make_us_entry('CRM',   'Salesforce Inc.',                exchange='NYSE'),
    _make_us_entry('CSCO',  'Cisco Systems Inc.',             exchange='NASDAQ'),
    _make_us_entry('MCD',   "McDonald's Corporation",         exchange='NYSE'),
    _make_us_entry('NKE',   'Nike Inc.',                      exchange='NYSE'),
    _make_us_entry('IBM',   'IBM Corporation',                exchange='NYSE'),
    _make_us_entry('MMM',   '3M Company',                     exchange='NYSE'),
    _make_us_entry('GS',    'Goldman Sachs Group',            exchange='NYSE'),
    _make_us_entry('BA',    'Boeing Company',                 exchange='NYSE'),
    _make_us_entry('CAT',   'Caterpillar Inc.',               exchange='NYSE'),
    _make_us_entry('HON',   'Honeywell International',        exchange='NASDAQ'),
    _make_us_entry('TRV',   'Travelers Companies',            exchange='NYSE'),
    _make_us_entry('AXP',   'American Express Company',       exchange='NYSE'),
    _make_us_entry('VZ',    'Verizon Communications',         exchange='NYSE'),
    _make_us_entry('DOW',   'Dow Inc.',                       exchange='NYSE'),
    _make_us_entry('WBA',   'Walgreens Boots Alliance',       exchange='NASDAQ'),
    # ── 나스닥100 추가 ──
    _make_us_entry('AMD',   'Advanced Micro Devices',         exchange='NASDAQ'),
    _make_us_entry('QCOM',  'Qualcomm Inc.',                  exchange='NASDAQ'),
    _make_us_entry('NFLX',  'Netflix Inc.',                   exchange='NASDAQ'),
    _make_us_entry('ADBE',  'Adobe Inc.',                     exchange='NASDAQ'),
    _make_us_entry('PYPL',  'PayPal Holdings Inc.',           exchange='NASDAQ'),
    _make_us_entry('AVGO',  'Broadcom Inc.',                  exchange='NASDAQ'),
    _make_us_entry('TXN',   'Texas Instruments',              exchange='NASDAQ'),
    _make_us_entry('AMAT',  'Applied Materials Inc.',         exchange='NASDAQ'),
    _make_us_entry('MU',    'Micron Technology',              exchange='NASDAQ'),
    _make_us_entry('LRCX',  'Lam Research Corporation',       exchange='NASDAQ'),
    _make_us_entry('KLAC',  'KLA Corporation',                exchange='NASDAQ'),
    _make_us_entry('MRVL',  'Marvell Technology',             exchange='NASDAQ'),
    _make_us_entry('PANW',  'Palo Alto Networks',             exchange='NASDAQ'),
    _make_us_entry('SNPS',  'Synopsys Inc.',                  exchange='NASDAQ'),
    _make_us_entry('CDNS',  'Cadence Design Systems',         exchange='NASDAQ'),
    _make_us_entry('FTNT',  'Fortinet Inc.',                  exchange='NASDAQ'),
    _make_us_entry('INTC',  'Intel Corporation',              exchange='NASDAQ'),
    _make_us_entry('ORCL',  'Oracle Corporation',             exchange='NYSE'),
    _make_us_entry('INTU',  'Intuit Inc.',                    exchange='NASDAQ'),
    _make_us_entry('ISRG',  'Intuitive Surgical',             exchange='NASDAQ'),
    _make_us_entry('REGN',  'Regeneron Pharmaceuticals',      exchange='NASDAQ'),
    _make_us_entry('VRTX',  'Vertex Pharmaceuticals',         exchange='NASDAQ'),
    _make_us_entry('AMGN',  'Amgen Inc.',                     exchange='NASDAQ'),
    _make_us_entry('GILD',  'Gilead Sciences',                exchange='NASDAQ'),
    _make_us_entry('BIIB',  'Biogen Inc.',                    exchange='NASDAQ'),
    _make_us_entry('MRNA',  'Moderna Inc.',                   exchange='NASDAQ'),
    _make_us_entry('BNTX',  'BioNTech SE',                    exchange='NASDAQ'),
    _make_us_entry('ZM',    'Zoom Video Communications',      exchange='NASDAQ'),
    _make_us_entry('UBER',  'Uber Technologies',              exchange='NYSE'),
    _make_us_entry('LYFT',  'Lyft Inc.',                      exchange='NASDAQ'),
    _make_us_entry('ABNB',  'Airbnb Inc.',                    exchange='NASDAQ'),
    _make_us_entry('DASH',  'DoorDash Inc.',                  exchange='NYSE'),
    _make_us_entry('COIN',  'Coinbase Global',                exchange='NASDAQ'),
    _make_us_entry('RBLX',  'Roblox Corporation',             exchange='NYSE'),
    _make_us_entry('U',     'Unity Software',                 exchange='NYSE'),
    _make_us_entry('PLTR',  'Palantir Technologies',          exchange='NYSE'),
    _make_us_entry('SNOW',  'Snowflake Inc.',                 exchange='NYSE'),
    _make_us_entry('NET',   'Cloudflare Inc.',                exchange='NYSE'),
    _make_us_entry('DDOG',  'Datadog Inc.',                   exchange='NASDAQ'),
    _make_us_entry('ZS',    'Zscaler Inc.',                   exchange='NASDAQ'),
    _make_us_entry('CRWD',  'CrowdStrike Holdings',           exchange='NASDAQ'),
    _make_us_entry('OKTA',  'Okta Inc.',                      exchange='NASDAQ'),
    _make_us_entry('TWLO',  'Twilio Inc.',                    exchange='NYSE'),
    _make_us_entry('MDB',   'MongoDB Inc.',                   exchange='NASDAQ'),
    _make_us_entry('ESTC',  'Elastic N.V.',                   exchange='NYSE'),
    _make_us_entry('SHOP',  'Shopify Inc.',                   exchange='NYSE'),
    _make_us_entry('SQ',    'Block Inc.',                     exchange='NYSE'),
    _make_us_entry('HOOD',  'Robinhood Markets',              exchange='NASDAQ'),
    _make_us_entry('AFRM',  'Affirm Holdings',                exchange='NASDAQ'),
    _make_us_entry('SOFI',  'SoFi Technologies',              exchange='NASDAQ'),
    _make_us_entry('PATH',  'UiPath Inc.',                    exchange='NYSE'),
    _make_us_entry('AI',    'C3.ai Inc.',                     exchange='NYSE'),
    _make_us_entry('SOUN',  'SoundHound AI',                  exchange='NASDAQ'),
    # ── 금융 ──
    _make_us_entry('WFC',   'Wells Fargo & Company',          exchange='NYSE'),
    _make_us_entry('C',     'Citigroup Inc.',                 exchange='NYSE'),
    _make_us_entry('MS',    'Morgan Stanley',                 exchange='NYSE'),
    _make_us_entry('BLK',   'BlackRock Inc.',                 exchange='NYSE'),
    _make_us_entry('SCHW',  'Charles Schwab Corp.',           exchange='NYSE'),
    _make_us_entry('USB',   'U.S. Bancorp',                   exchange='NYSE'),
    _make_us_entry('PNC',   'PNC Financial Services',         exchange='NYSE'),
    _make_us_entry('AIG',   'American International Group',   exchange='NYSE'),
    _make_us_entry('MET',   'MetLife Inc.',                   exchange='NYSE'),
    _make_us_entry('PRU',   'Prudential Financial',           exchange='NYSE'),
    _make_us_entry('SPGI',  'S&P Global Inc.',                exchange='NYSE'),
    _make_us_entry('MCO',   "Moody's Corporation",            exchange='NYSE'),
    _make_us_entry('ICE',   'Intercontinental Exchange',      exchange='NYSE'),
    _make_us_entry('CME',   'CME Group Inc.',                 exchange='NASDAQ'),
    # ── 에너지 ──
    _make_us_entry('COP',   'ConocoPhillips',                 exchange='NYSE'),
    _make_us_entry('SLB',   'SLB (Schlumberger)',             exchange='NYSE'),
    _make_us_entry('EOG',   'EOG Resources',                  exchange='NYSE'),
    _make_us_entry('OXY',   'Occidental Petroleum',           exchange='NYSE'),
    _make_us_entry('PSX',   'Phillips 66',                    exchange='NYSE'),
    _make_us_entry('VLO',   'Valero Energy',                  exchange='NYSE'),
    _make_us_entry('MPC',   'Marathon Petroleum',             exchange='NYSE'),
    # ── 헬스케어 ──
    _make_us_entry('LLY',   'Eli Lilly and Company',          exchange='NYSE'),
    _make_us_entry('BMY',   'Bristol-Myers Squibb',           exchange='NYSE'),
    _make_us_entry('CVS',   'CVS Health Corporation',         exchange='NYSE'),
    _make_us_entry('CI',    'Cigna Group',                    exchange='NYSE'),
    _make_us_entry('HUM',   'Humana Inc.',                    exchange='NYSE'),
    _make_us_entry('MDT',   'Medtronic plc',                  exchange='NYSE'),
    _make_us_entry('SYK',   'Stryker Corporation',            exchange='NYSE'),
    _make_us_entry('EW',    'Edwards Lifesciences',           exchange='NYSE'),
    _make_us_entry('BSX',   'Boston Scientific',              exchange='NYSE'),
    _make_us_entry('ZBH',   'Zimmer Biomet Holdings',         exchange='NYSE'),
    # ── 소비재 ──
    _make_us_entry('COST',  'Costco Wholesale',               exchange='NASDAQ'),
    _make_us_entry('TGT',   'Target Corporation',             exchange='NYSE'),
    _make_us_entry('LOW',   "Lowe's Companies",               exchange='NYSE'),
    _make_us_entry('TJX',   'TJX Companies',                  exchange='NYSE'),
    _make_us_entry('ROST',  'Ross Stores Inc.',               exchange='NASDAQ'),
    _make_us_entry('DLTR',  'Dollar Tree Inc.',               exchange='NASDAQ'),
    _make_us_entry('DG',    'Dollar General',                 exchange='NYSE'),
    _make_us_entry('KR',    'Kroger Co.',                     exchange='NYSE'),
    _make_us_entry('SBUX',  'Starbucks Corporation',          exchange='NASDAQ'),
    _make_us_entry('YUM',   'Yum! Brands Inc.',               exchange='NYSE'),
    _make_us_entry('CMG',   'Chipotle Mexican Grill',         exchange='NYSE'),
    _make_us_entry('DPZ',   "Domino's Pizza",                 exchange='NYSE'),
    _make_us_entry('QSR',   'Restaurant Brands Intl',         exchange='NYSE'),
    _make_us_entry('MO',    'Altria Group Inc.',              exchange='NYSE'),
    _make_us_entry('PM',    'Philip Morris Intl',             exchange='NYSE'),
    _make_us_entry('STZ',   'Constellation Brands',           exchange='NYSE'),
    _make_us_entry('BUD',   'Anheuser-Busch InBev',           exchange='NYSE'),
    _make_us_entry('TAP',   'Molson Coors Beverage',          exchange='NYSE'),
    # ── 산업재 ──
    _make_us_entry('LMT',   'Lockheed Martin',                exchange='NYSE'),
    _make_us_entry('RTX',   'RTX Corporation',                exchange='NYSE'),
    _make_us_entry('NOC',   'Northrop Grumman',               exchange='NYSE'),
    _make_us_entry('GD',    'General Dynamics',               exchange='NYSE'),
    _make_us_entry('GE',    'GE Aerospace',                   exchange='NYSE'),
    _make_us_entry('UPS',   'United Parcel Service',          exchange='NYSE'),
    _make_us_entry('FDX',   'FedEx Corporation',              exchange='NYSE'),
    _make_us_entry('CSX',   'CSX Corporation',                exchange='NASDAQ'),
    _make_us_entry('UNP',   'Union Pacific',                  exchange='NYSE'),
    _make_us_entry('DAL',   'Delta Air Lines',                exchange='NYSE'),
    _make_us_entry('UAL',   'United Airlines Holdings',       exchange='NASDAQ'),
    _make_us_entry('AAL',   'American Airlines Group',        exchange='NASDAQ'),
    _make_us_entry('LUV',   'Southwest Airlines',             exchange='NYSE'),
    _make_us_entry('CCL',   'Carnival Corporation',           exchange='NYSE'),
    _make_us_entry('RCL',   'Royal Caribbean Group',          exchange='NYSE'),
    _make_us_entry('NCLH',  'Norwegian Cruise Line',          exchange='NYSE'),
    _make_us_entry('MAR',   'Marriott International',         exchange='NASDAQ'),
    _make_us_entry('HLT',   'Hilton Worldwide Holdings',      exchange='NYSE'),
    _make_us_entry('H',     'Hyatt Hotels Corporation',       exchange='NYSE'),
    # ── 부동산 ──
    _make_us_entry('AMT',   'American Tower Corp.',           exchange='NYSE'),
    _make_us_entry('PLD',   'Prologis Inc.',                  exchange='NYSE'),
    _make_us_entry('CCI',   'Crown Castle Inc.',              exchange='NYSE'),
    _make_us_entry('EQIX',  'Equinix Inc.',                   exchange='NASDAQ'),
    _make_us_entry('SPG',   'Simon Property Group',           exchange='NYSE'),
    _make_us_entry('O',     'Realty Income Corp.',            exchange='NYSE'),
    _make_us_entry('VICI',  'VICI Properties',                exchange='NYSE'),
    _make_us_entry('WY',    'Weyerhaeuser Company',           exchange='NYSE'),
    # ── 통신 ──
    _make_us_entry('T',     'AT&T Inc.',                      exchange='NYSE'),
    _make_us_entry('TMUS',  'T-Mobile US Inc.',               exchange='NASDAQ'),
    _make_us_entry('CHTR',  'Charter Communications',         exchange='NASDAQ'),
    _make_us_entry('CMCSA', 'Comcast Corporation',            exchange='NASDAQ'),
    _make_us_entry('WBD',   'Warner Bros. Discovery',         exchange='NASDAQ'),
    _make_us_entry('PARA',  'Paramount Global',               exchange='NASDAQ'),
    _make_us_entry('NWSA',  'News Corp',                      exchange='NASDAQ'),
    _make_us_entry('SPOT',  'Spotify Technology',             exchange='NYSE'),
    # ── 자동차 ──
    _make_us_entry('F',     'Ford Motor Company',             exchange='NYSE'),
    _make_us_entry('GM',    'General Motors',                 exchange='NYSE'),
    _make_us_entry('RIVN',  'Rivian Automotive',              exchange='NASDAQ'),
    _make_us_entry('LCID',  'Lucid Group Inc.',               exchange='NASDAQ'),
    _make_us_entry('STLA',  'Stellantis N.V.',                exchange='NYSE'),
    _make_us_entry('TM',    'Toyota Motor (ADR)',             exchange='NYSE'),
    _make_us_entry('HMC',   'Honda Motor (ADR)',              exchange='NYSE'),
    _make_us_entry('RACE',  'Ferrari N.V.',                   exchange='NYSE'),
    # ── 주요 ETF ──────────────────────────────────
    _make_us_entry('SPY',   'SPDR S&P 500 ETF',              'ETF', 'NYSE'),
    _make_us_entry('QQQ',   'Invesco Nasdaq-100 ETF',        'ETF', 'NASDAQ'),
    _make_us_entry('IWM',   'iShares Russell 2000 ETF',      'ETF', 'NYSE'),
    _make_us_entry('DIA',   'SPDR Dow Jones ETF',            'ETF', 'NYSE'),
    _make_us_entry('VTI',   'Vanguard Total Stock Market ETF','ETF','NYSE'),
    _make_us_entry('VOO',   'Vanguard S&P 500 ETF',          'ETF', 'NYSE'),
    _make_us_entry('VEA',   'Vanguard Developed Markets ETF','ETF', 'NYSE'),
    _make_us_entry('VWO',   'Vanguard Emerging Markets ETF', 'ETF', 'NYSE'),
    _make_us_entry('EEM',   'iShares MSCI Emerging Markets', 'ETF', 'NYSE'),
    _make_us_entry('EWJ',   'iShares MSCI Japan ETF',        'ETF', 'NYSE'),
    _make_us_entry('FXI',   'iShares China Large-Cap ETF',   'ETF', 'NYSE'),
    _make_us_entry('EWZ',   'iShares MSCI Brazil ETF',       'ETF', 'NYSE'),
    _make_us_entry('EWY',   'iShares MSCI South Korea ETF',  'ETF', 'NYSE'),
    _make_us_entry('EWT',   'iShares MSCI Taiwan ETF',       'ETF', 'NYSE'),
    _make_us_entry('INDA',  'iShares MSCI India ETF',        'ETF', 'NYSE'),
    _make_us_entry('MCHI',  'iShares MSCI China ETF',        'ETF', 'NYSE'),
    _make_us_entry('GLD',   'SPDR Gold Shares',              'ETF', 'NYSE'),
    _make_us_entry('IAU',   'iShares Gold Trust',            'ETF', 'NYSE'),
    _make_us_entry('SLV',   'iShares Silver Trust',          'ETF', 'NYSE'),
    _make_us_entry('GDX',   'VanEck Gold Miners ETF',        'ETF', 'NYSE'),
    _make_us_entry('GDXJ',  'VanEck Junior Gold Miners ETF', 'ETF', 'NYSE'),
    _make_us_entry('USO',   'United States Oil Fund',        'ETF', 'NYSE'),
    _make_us_entry('UNG',   'United States Natural Gas',     'ETF', 'NYSE'),
    _make_us_entry('TLT',   'iShares 20+ Year Treasury ETF', 'ETF', 'NASDAQ'),
    _make_us_entry('IEF',   'iShares 7-10 Year Treasury ETF','ETF', 'NASDAQ'),
    _make_us_entry('SHY',   'iShares 1-3 Year Treasury ETF', 'ETF', 'NASDAQ'),
    _make_us_entry('LQD',   'iShares Investment Grade Bond', 'ETF', 'NYSE'),
    _make_us_entry('HYG',   'iShares High Yield Bond ETF',   'ETF', 'NYSE'),
    _make_us_entry('AGG',   'iShares Core US Aggregate Bond','ETF', 'NYSE'),
    _make_us_entry('BND',   'Vanguard Total Bond Market ETF','ETF', 'NASDAQ'),
    _make_us_entry('VNQ',   'Vanguard Real Estate ETF',      'ETF', 'NYSE'),
    _make_us_entry('XLK',   'Technology Select Sector SPDR', 'ETF', 'NYSE'),
    _make_us_entry('XLF',   'Financial Select Sector SPDR',  'ETF', 'NYSE'),
    _make_us_entry('XLV',   'Health Care Select Sector SPDR','ETF', 'NYSE'),
    _make_us_entry('XLE',   'Energy Select Sector SPDR',     'ETF', 'NYSE'),
    _make_us_entry('XLI',   'Industrial Select Sector SPDR', 'ETF', 'NYSE'),
    _make_us_entry('XLC',   'Communication Services SPDR',   'ETF', 'NYSE'),
    _make_us_entry('XLY',   'Consumer Discretionary SPDR',   'ETF', 'NYSE'),
    _make_us_entry('XLP',   'Consumer Staples SPDR',         'ETF', 'NYSE'),
    _make_us_entry('XLU',   'Utilities Select Sector SPDR',  'ETF', 'NYSE'),
    _make_us_entry('XLRE',  'Real Estate Select Sector SPDR','ETF', 'NYSE'),
    _make_us_entry('XLB',   'Materials Select Sector SPDR',  'ETF', 'NYSE'),
    _make_us_entry('XME',   'SPDR S&P Metals & Mining ETF',  'ETF', 'NYSE'),
    _make_us_entry('ITB',   'iShares U.S. Home Construction','ETF', 'NYSE'),
    _make_us_entry('SOXX',  'iShares Semiconductor ETF',     'ETF', 'NASDAQ'),
    _make_us_entry('SMH',   'VanEck Semiconductor ETF',      'ETF', 'NASDAQ'),
    _make_us_entry('IGV',   'iShares Expanded Tech-Software','ETF', 'NASDAQ'),
    _make_us_entry('HACK',  'ETFMG Prime Cyber Security ETF','ETF', 'NYSE'),
    _make_us_entry('ARKK',  'ARK Innovation ETF',            'ETF', 'NYSE'),
    _make_us_entry('ARKG',  'ARK Genomic Revolution ETF',    'ETF', 'NYSE'),
    _make_us_entry('ARKF',  'ARK Fintech Innovation ETF',    'ETF', 'NYSE'),
    _make_us_entry('ARKW',  'ARK Next Generation Internet',  'ETF', 'NYSE'),
    _make_us_entry('ARKQ',  'ARK Autonomous Tech & Robot',   'ETF', 'NYSE'),
    _make_us_entry('TQQQ',  'ProShares UltraPro QQQ (3x)',   'ETF', 'NASDAQ'),
    _make_us_entry('SQQQ',  'ProShares UltraPro Short QQQ',  'ETF', 'NASDAQ'),
    _make_us_entry('SPXL',  'Direxion Daily S&P 500 Bull 3X','ETF', 'NYSE'),
    _make_us_entry('SPXS',  'Direxion Daily S&P 500 Bear 3X','ETF', 'NYSE'),
    _make_us_entry('SOXL',  'Direxion Daily Semicond Bull 3X','ETF','NYSE'),
    _make_us_entry('SOXS',  'Direxion Daily Semicond Bear 3X','ETF','NYSE'),
    _make_us_entry('LABU',  'Direxion Daily S&P Biotech Bull','ETF','NYSE'),
    _make_us_entry('FNGU',  'MicroSectors FANG+ Index 3X',   'ETF', 'NYSE'),
    _make_us_entry('BULZ',  'MicroSectors Solactive FANG 3X','ETF','NYSE'),
    _make_us_entry('SCHD',  'Schwab U.S. Dividend Equity ETF','ETF','NYSE'),
    _make_us_entry('VIG',   'Vanguard Dividend Appreciation','ETF', 'NYSE'),
    _make_us_entry('DVY',   'iShares Select Dividend ETF',   'ETF', 'NASDAQ'),
    _make_us_entry('JEPI',  'JPMorgan Equity Premium Income','ETF', 'NYSE'),
    _make_us_entry('JEPQ',  'JPMorgan Nasdaq Equity Premium','ETF', 'NASDAQ'),
    _make_us_entry('XYLD',  'Global X S&P 500 Covered Call', 'ETF', 'NYSE'),
    _make_us_entry('QYLD',  'Global X Nasdaq 100 Covered Call','ETF','NASDAQ'),
]



def search_us_corp(keyword):
    """
    미국 종목 검색:
    1차 - 내장 DB (S&P500 + 나스닥100 + 주요 ETF 약 600개) → 항상 동작
    2차 - 야후 파이낸스 crumb 검색 → 성공 시 결과 보강
    """
    kw = keyword.strip()
    if not kw:
        return []

    kw_up  = kw.upper()
    kw_low = kw.lower()

    # ── 1차: 내장 DB 검색 ──────────────────────────────────────
    ticker_match = []
    name_match   = []
    for info in _US_TICKER_DB:
        t = info['ticker']
        n = info['corp_name'].lower()   # _make_us_entry는 'corp_name' 키 사용
        if t == kw_up:
            ticker_match.insert(0, info)   # 티커 완전일치 → 최우선
        elif kw_up in t or kw_low in n:
            name_match.append(info)

    # 중복 제거
    seen = set()
    results = []
    for info in ticker_match + name_match:
        if info['ticker'] not in seen:
            seen.add(info['ticker'])
            results.append(info)
        if len(results) >= 15:
            break

    # ── 2차: 야후 crumb 검색 (추가 보강) ─────────────────────
    try:
        import http.cookiejar
        cj  = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        _h  = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': 'https://finance.yahoo.com/',
        }
        opener.open(urllib.request.Request('https://finance.yahoo.com', headers=_h), timeout=5)
        with opener.open(urllib.request.Request('https://query2.finance.yahoo.com/v1/test/getcrumb', headers=_h), timeout=5) as r:
            crumb = r.read().decode('utf-8').strip()
        if crumb and len(crumb) < 30:
            url  = (f"https://query2.finance.yahoo.com/v1/finance/search"
                    f"?q={urllib.parse.quote(kw)}&quotesCount=10&newsCount=0"
                    f"&crumb={urllib.parse.quote(crumb)}")
            with opener.open(urllib.request.Request(url, headers=_h), timeout=6) as r:
                data   = json.loads(r.read())
                quotes = data.get('finance',{}).get('result',[{}])[0].get('quotes',[])
            for q in quotes:
                sym  = q.get('symbol','')
                name = q.get('longname') or q.get('shortname') or sym
                if sym and sym not in seen:
                    seen.add(sym)
                    results.append({
                        'corp_name':  name,
                        'stock_code': sym,
                        'ticker':     sym,
                        'corp_code':  sym,
                        'cik':        '',
                        'exchange':   q.get('exchDisp', q.get('exchange','')),
                        'quote_type': q.get('quoteType','EQUITY'),
                        'market':     'US',
                    })
    except Exception:
        pass   # 야후 실패해도 내장 DB 결과는 항상 반환

    return results[:20]

def _get_cik_from_ticker(ticker):
    """티커 → SEC CIK 변환 (company_tickers.json 사용)"""
    url = "https://www.sec.gov/files/company_tickers.json"
    req = urllib.request.Request(url, headers=SEC_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        # 형식: {"0":{"cik_str":320193,"ticker":"AAPL","title":"Apple Inc."}, ...}
        ticker_upper = ticker.upper()
        for item in data.values():
            if item.get('ticker', '').upper() == ticker_upper:
                return str(item['cik_str']).zfill(10)
        return None
    except Exception:
        return None


def fetch_sec_finance(ticker_or_cik, period=5):
    """
    SEC EDGAR XBRL companyfacts API로 연간 재무제표 수집.
    입력: 티커(AAPL) 또는 CIK 문자열
    반환: {status, years:[], data:{year: {항목명:값(USD M)}}}
    """
    # CIK 확보
    if ticker_or_cik.isdigit() or (len(ticker_or_cik) == 10 and ticker_or_cik.isdigit()):
        cik = ticker_or_cik.zfill(10)
    else:
        cik = _get_cik_from_ticker(ticker_or_cik)
        if not cik:
            return {'status': 'error', 'message': f'SEC CIK를 찾을 수 없음: {ticker_or_cik}'}

    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    req = urllib.request.Request(url, headers=SEC_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = json.loads(resp.read())
    except Exception as e:
        return {'status': 'error', 'message': f'SEC EDGAR 조회 실패: {e}'}

    facts   = raw.get('facts', {})
    us_gaap = facts.get('us-gaap', {})
    ifrs    = facts.get('ifrs-full', {})

    def get_concept(gaap_name, ifrs_name=None):
        src = us_gaap.get(gaap_name) or (ifrs.get(ifrs_name) if ifrs_name else None)
        if not src:
            return {}
        units = src.get('units', {})
        vals  = units.get('USD') or units.get('shares') or []
        annual = {}
        for v in vals:
            form = v.get('form', '')
            if form not in ('10-K', '20-F', '40-F'):
                continue
            fy = v.get('fy') or v.get('end', '')[:4]
            if not fy:
                continue
            fy    = int(fy)
            val   = v.get('val', 0)
            filed = v.get('filed', '0000-00-00')
            if fy not in annual or filed > annual[fy]['filed']:
                annual[fy] = {'val': val, 'filed': filed}
        return {fy: d['val'] for fy, d in annual.items()}

    concept_map = {
        # 재무상태표
        '자산총계':           get_concept('Assets'),
        '유동자산':           get_concept('AssetsCurrent'),
        '현금및현금성자산':   get_concept('CashAndCashEquivalentsAtCarryingValue', 'CashAndCashEquivalents'),
        '단기금융상품':       get_concept('ShortTermInvestments'),
        '매출채권':           get_concept('AccountsReceivableNetCurrent', 'TradeAndOtherCurrentReceivables'),
        '재고자산':           get_concept('InventoryNet', 'Inventories'),
        '비유동자산':         get_concept('AssetsNoncurrent'),
        '유형자산':           get_concept('PropertyPlantAndEquipmentNet', 'PropertyPlantAndEquipment'),
        '무형자산':           get_concept('FiniteLivedIntangibleAssetsNet', 'IntangibleAssetsOtherThanGoodwill'),
        '부채총계':           get_concept('Liabilities'),
        '유동부채':           get_concept('LiabilitiesCurrent'),
        '비유동부채':         get_concept('LiabilitiesNoncurrent'),
        '자본총계':           get_concept('StockholdersEquity', 'Equity'),
        '이익잉여금':         get_concept('RetainedEarningsAccumulatedDeficit', 'RetainedEarnings'),
        # 손익계산서
        '매출액':             get_concept('Revenues', 'Revenue'),
        '매출원가':           get_concept('CostOfRevenue', 'CostOfGoodsSold'),
        '매출총이익':         get_concept('GrossProfit'),
        '판매비와관리비':     get_concept('SellingGeneralAndAdministrativeExpense'),
        '영업이익':           get_concept('OperatingIncomeLoss'),
        '이자비용':           get_concept('InterestExpense'),
        '법인세차감전순이익': get_concept('IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest'),
        '법인세비용':         get_concept('IncomeTaxExpenseBenefit'),
        '당기순이익':         get_concept('NetIncomeLoss', 'ProfitLoss'),
        # 현금흐름
        '영업활동현금흐름':   get_concept('NetCashProvidedByUsedInOperatingActivities'),
        '투자활동현금흐름':   get_concept('NetCashProvidedByUsedInInvestingActivities'),
        '재무활동현금흐름':   get_concept('NetCashProvidedByUsedInFinancingActivities'),
        '기말 현금':           get_concept('CashAndCashEquivalentsAtCarryingValue', 'CashAndCashEquivalents'),
    }

    all_years = set()
    for v in concept_map.values():
        all_years.update(v.keys())

    cur_year     = datetime.now().year
    target_years = sorted([y for y in all_years if y <= cur_year], reverse=True)[:period]

    if not target_years:
        return {'status': 'error', 'message': '재무 데이터 없음 (SEC EDGAR) — ETF는 재무제표가 없습니다'}

    data = {}
    for y in target_years:
        row = {}
        for label, year_map in concept_map.items():
            raw_val = year_map.get(y)
            row[label] = round(raw_val / 1_000_000, 2) if raw_val is not None else None
        data[y] = row

    return {
        'status':   '000',
        'years':    target_years,
        'data':     data,
        'currency': 'USD',
        'unit':     'million',
    }


# ── 야후 crumb 캐시 (전역, 1시간 유효) ──────────────────────
import http.cookiejar as _cookiejar
_Y_CACHE = {'opener': None, 'crumb': '', 'at': 0}
_Y_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Accept': '*/*',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': 'https://finance.yahoo.com/',
}

def _yahoo_opener():
    """야후 파이낸스 세션 + crumb 획득 (캐싱, 1시간 유효)"""
    global _Y_CACHE
    now = time.time()
    # 캐시 유효하면 재사용
    if _Y_CACHE['opener'] and _Y_CACHE['crumb'] and (now - _Y_CACHE['at']) < 3600:
        return _Y_CACHE['opener'], _Y_CACHE['crumb'], _Y_HEADERS

    cj     = _cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    crumb  = ''
    try:
        opener.open(urllib.request.Request('https://finance.yahoo.com', headers=_Y_HEADERS), timeout=8)
        with opener.open(urllib.request.Request(
            'https://query2.finance.yahoo.com/v1/test/getcrumb', headers=_Y_HEADERS), timeout=8) as r:
            crumb = r.read().decode('utf-8').strip()
        print(f"[Yahoo] crumb 갱신 완료: {crumb[:8]}...")
    except Exception as e:
        print(f"[Yahoo] crumb 획득 실패 (crumb 없이 시도): {e}")

    _Y_CACHE = {'opener': opener, 'crumb': crumb, 'at': now}
    return opener, crumb, _Y_HEADERS


def fetch_us_stock(ticker):
    """
    미국 종목 시세 수집.
    - 현재가·거래량: 야후 chart v8 (매크로 대시보드와 동일 방식)
    - 시가총액: 현재가 × 발행주식수 (SEC EDGAR)
    - PER: 현재가 ÷ EPS (SEC EDGAR 순이익/주식수)
    - PBR: SEC EDGAR 자산·부채로 계산
    """
    h = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Accept': '*/*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Referer': 'https://finance.yahoo.com/',
        'Origin': 'https://finance.yahoo.com',
    }

    # ── 1단계: 야후 chart v8로 현재가 ─────────────────────────
    price = diff = diff_rate = volume = None
    quote_type = ''
    try:
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker)}?range=5d&interval=1d"
        req = urllib.request.Request(url, headers=h)
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read())
        result = d['chart']['result'][0]
        meta   = result.get('meta', {})
        closes = [c for c in result['indicators']['quote'][0]['close'] if c is not None]
        if len(closes) >= 2:
            price     = round(closes[-1], 2)
            prev      = round(closes[-2], 2)
            diff      = round(price - prev, 2)
            diff_rate = round((diff / prev) * 100, 2) if prev else 0
        elif closes:
            price = round(closes[-1], 2)
            diff = diff_rate = 0
        volume     = meta.get('regularMarketVolume')
        quote_type = meta.get('instrumentType', '')
    except Exception as e:
        return {'error': f'시세 조회 실패: {e}'}

    # ── 2단계: SEC EDGAR로 EPS·주식수·시총·PER·PBR 계산 ───────
    per = pbr = eps = market_cap = shares = None
    try:
        cik = _get_cik_from_ticker(ticker)
        if cik:
            url2 = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
            req2 = urllib.request.Request(url2, headers=SEC_HEADERS)
            with urllib.request.urlopen(req2, timeout=15) as resp2:
                facts = json.loads(resp2.read()).get('facts', {})
            gaap = facts.get('us-gaap', {})

            def latest_annual(concept):
                """XBRL concept에서 가장 최근 10-K 연간값 반환"""
                src2 = gaap.get(concept, {})
                vals = src2.get('units', {}).get('USD') or src2.get('units', {}).get('shares') or []
                best = None
                for v in vals:
                    if v.get('form') not in ('10-K', '20-F', '40-F'):
                        continue
                    if best is None or v.get('filed','') > best.get('filed',''):
                        best = v
                return best['val'] if best else None

            net_income = latest_annual('NetIncomeLoss')
            equity     = latest_annual('StockholdersEquity')
            assets     = latest_annual('Assets')
            liabilities= latest_annual('Liabilities')

            # 발행주식수 (shares 단위)
            sh_src = gaap.get('CommonStockSharesOutstanding', {})
            sh_vals = sh_src.get('units', {}).get('shares') or []
            shares_val = None
            for v in sh_vals:
                if v.get('form') in ('10-K', '20-F', '40-F', '10-Q'):
                    if shares_val is None or v.get('filed','') > shares_val.get('filed',''):
                        shares_val = v
            if shares_val:
                shares = shares_val['val']

            # 시가총액 = 현재가 × 발행주식수
            if price and shares:
                market_cap = int(price * shares)

            # EPS = 순이익 ÷ 발행주식수
            if net_income and shares and shares > 0:
                eps = round(net_income / shares, 2)

            # PER = 현재가 ÷ EPS
            if price and eps and eps > 0:
                per = round(price / eps, 2)

            # PBR = 현재가 × 주식수 ÷ 자기자본
            if market_cap and equity and equity > 0:
                pbr = round(market_cap / equity, 2)

    except Exception as e:
        print(f"[SEC 밸류] {ticker} 조회 실패: {e}")

    # ── 3단계: crumb 방식으로 ETF 데이터 시도 ─────────────────
    etf_data = {}
    if quote_type == 'ETF':
        try:
            opener, crumb, yh = _yahoo_opener()
            crumb_param = f'&crumb={urllib.parse.quote(crumb)}' if crumb else ''
            url3 = (f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{urllib.parse.quote(ticker)}"
                    f"?modules=topHoldings,fundPerformance,summaryDetail{crumb_param}")
            with opener.open(urllib.request.Request(url3, headers=yh), timeout=10) as r3:
                d3 = json.loads(r3.read())
            res3      = d3['quoteSummary']['result'][0]
            holdings  = res3.get('topHoldings', {})
            fund_perf = res3.get('fundPerformance', {})
            summary3  = res3.get('summaryDetail', {})

            def raw3(obj, key):
                v = obj.get(key, {})
                return v.get('raw') if isinstance(v, dict) else v
            def pct3(obj, key):
                v = raw3(obj, key)
                return round(v * 100, 2) if v else None

            top = holdings.get('holdings', [])[:5]
            etf_data = {
                'etf_holdings':      [{'name': hh.get('holdingName',''), 'pct': pct3(hh, 'holdingPercent')} for hh in top],
                'etf_equity_pct':    pct3(holdings, 'stockPosition'),
                'etf_bond_pct':      pct3(holdings, 'bondPosition'),
                'etf_3y_return':     pct3(fund_perf.get('trailingReturns',{}), 'threeYear'),
                'etf_5y_return':     pct3(fund_perf.get('trailingReturns',{}), 'fiveYear'),
                'etf_ytd_return':    pct3(fund_perf.get('trailingReturns',{}), 'ytd'),
                'etf_expense_ratio': pct3(summary3, 'annualReportExpenseRatio'),
            }
            # ETF 시총은 yahoo meta에서
            if not market_cap:
                mc_raw = raw3(res3.get('summaryDetail',{}), 'totalAssets')
                if mc_raw: market_cap = mc_raw
        except Exception:
            pass

    result_data = {
        'price':      price,
        'diff':       diff or 0,
        'diff_rate':  diff_rate or 0,
        'market_cap': market_cap,
        'volume':     volume,
        'per':        per,
        'pbr':        pbr,
        'eps':        eps,
        'currency':   'USD',
        'quote_type': quote_type or '',
    }
    result_data.update(etf_data)
    return result_data


def fetch_us_chart(ticker, period_type='1Y'):
    """야후 파이낸스 v8 chart API로 미국 주가 차트 수집 (crumb 방식)"""
    period_map = {
        '1M':  ('1mo',  '1d'),
        '3M':  ('3mo',  '1d'),
        '1Y':  ('1y',   '1wk'),
        '3Y':  ('3y',   '1mo'),
        '5Y':  ('5y',   '1mo'),
        '10Y': ('10y',  '1mo'),
    }
    range_str, interval = period_map.get(period_type, ('1y', '1wk'))
    opener, crumb, h = _yahoo_opener()
    crumb_param = f'&crumb={urllib.parse.quote(crumb)}' if crumb else ''
    url = (
        f"https://query2.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker)}"
        f"?range={range_str}&interval={interval}{crumb_param}"
    )
    try:
        with opener.open(urllib.request.Request(url, headers=h), timeout=12) as resp:
            d = json.loads(resp.read())
        result     = d['chart']['result'][0]
        timestamps = result['timestamp']
        closes     = result['indicators']['quote'][0]['close']
        chart_list = [
            {'date': datetime.fromtimestamp(t).strftime('%Y-%m-%d'), 'close': round(c, 2)}
            for t, c in zip(timestamps, closes) if c is not None
        ]
        return {'status': '000', 'list': chart_list}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


def fetch_naver_domestic(target_type):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Referer': 'https://m.stock.naver.com/'
    }
    url = f"https://m.stock.naver.com/api/index/{target_type}/price?pageSize=20&page=1"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            res = []
            for item in data:
                dt = item.get('localTradedAt', '')[:10]
                pr = float(str(item.get('closePrice', '0')).replace(',', ''))
                diff = float(str(item.get('compareToPreviousClosePrice', '0')).replace(',', ''))
                rate = float(str(item.get('fluctuationsRatio', '0')).replace(',', ''))
                res.append({'time': dt, 'value': pr, 'diff': diff, 'rate': rate})
            res.reverse()
            return {'status': '000', 'data': res}
    except Exception as e:
        return {'status': 'error', 'msg': str(e)}

def fetch_yahoo_finance(ticker):
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker)}?range=1mo&interval=1d"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Accept': '*/*',
        'Accept-Language': 'en-US,en;q=0.9,ko;q=0.8',
        'Origin': 'https://finance.yahoo.com',
        'Referer': 'https://finance.yahoo.com/'
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            result = data['chart']['result'][0]
            timestamps = result['timestamp']
            closes = result['indicators']['quote'][0]['close']
            
            valid_data = []
            for t, c in zip(timestamps, closes):
                if c is not None:
                    dt = datetime.fromtimestamp(t).strftime('%Y-%m-%d')
                    valid_data.append({'time': dt, 'value': c})
            
            if len(valid_data) < 2: return {'status': 'error', 'msg': '데이터 부족'}
            
            cur = valid_data[-1]['value']
            prev = valid_data[-2]['value']
            diff = cur - prev
            rate = (diff / prev) * 100 if prev != 0 else 0
            
            return {'status': '000', 'data': valid_data, 'current': cur, 'diff': diff, 'rate': rate}
    except Exception as e:
        return {'status': 'error', 'msg': str(e)}

YAHOO_TICKERS = {
    'SNP500':'^GSPC', 'NASDAQ':'^IXIC', 'DOW':'^DJI', 'RUSSELL':'^RUT',
    'NIKKEI':'^N225', 'HANGSENG':'^HSI', 'SHANGHAI':'000001.SS',
    'DAX':'^GDAXI', 'FTSE':'^FTSE', 'ESTOXX':'^STOXX50E',
    'VIX':'^VIX',
    'US10Y':'^TNX', 'US30Y':'^TYX', 'US05Y':'^FVX', 'US13W':'^IRX',
    'USD':'KRW=X', 'DXY':'DX-Y.NYB', 'EURUSD':'EURUSD=X', 'USDJPY':'JPY=X', 'USDCNY':'CNY=X',
    'WTI':'CL=F', 'BRENT':'BZ=F', 'NATGAS':'NG=F', 'GOLD':'GC=F', 'SILVER':'SI=F', 'COPPER':'HG=F',
    'CORN':'ZC=F', 'WHEAT':'ZW=F', 'SOYBEAN':'ZS=F',
    'BTC':'BTC-USD', 'ETH':'ETH-USD',
}

def resolve_macro(asset_id):
    try:
        if asset_id in ('KOSPI', 'KOSDAQ'): return fetch_naver_domestic(asset_id)
        ticker = YAHOO_TICKERS.get(asset_id)
        if not ticker: return {'status': 'error', 'msg': '알 수 없는 지표'}
        return fetch_yahoo_finance(ticker)
    except Exception as e:
        return {'status': 'error', 'msg': str(e)}

def fetch_macro_batch(types_csv):
    ids = [t.strip() for t in (types_csv or '').split(',') if t.strip()]
    if not ids: return {}
    out = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        future_map = {ex.submit(resolve_macro, aid): aid for aid in ids}
        for fut in future_map:
            aid = future_map[fut]
            try: out[aid] = fut.result()
            except Exception as e: out[aid] = {'status': 'error', 'msg': str(e)}
    return out

def fetch_yahoo_news(query, count=8):
    url = f"https://query1.finance.yahoo.com/v1/finance/search?q={urllib.parse.quote(query)}&quotesCount=0&newsCount={count}&enableFuzzyQuery=false"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Accept': '*/*',
        'Accept-Language': 'en-US,en;q=0.9'
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            items = []
            for n in (data.get('news') or [])[:count]:
                ts = n.get('providerPublishTime')
                date_str = ''
                try:
                    if ts:
                        date_str = datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')
                except Exception:
                    date_str = ''
                related = n.get('relatedTickers') or []
                items.append({
                    'title': n.get('title', ''),
                    'publisher': n.get('publisher', ''),
                    'link': n.get('link', ''),
                    'date': date_str,
                    'related': ', '.join(related[:4]) if related else ''
                })
            return {'status': '000', 'news': items}
    except Exception as e:
        return {'status': 'error', 'msg': str(e), 'news': []}

# DART 관련 함수 모음
CORP_LIST = []
KIS_TOKEN = {'token': None, 'expires_at': 0}

def load_corp_list():
    global CORP_LIST
    if CORP_LIST: return
    print("[시스템] DART 상장사 목록 업데이트 중...")
    try:
        url = f"https://opendart.fss.or.kr/api/corpCode.xml?crtfc_key={DART_API_KEY}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
        zf = zipfile.ZipFile(io.BytesIO(data))
        xml_data = zf.read(zf.namelist()[0])
        root = ET.fromstring(xml_data)
        CORP_LIST = []
        for item in root.findall('list'):
            cn = item.findtext('corp_name', '').strip()
            if cn:
                CORP_LIST.append({
                    'corp_code': item.findtext('corp_code', '').strip(),
                    'corp_name': cn,
                    'stock_code': item.findtext('stock_code', '').strip(),
                })
        print(f"[시스템] 목록 업데이트 완료 — {len(CORP_LIST):,}개")
    except Exception as e:
        print(f"[시스템] 목록 업데이트 실패: {e}")

def search_corp(keyword):
    kw = keyword.strip().lower()
    if not kw: return []
    results = [c for c in CORP_LIST if kw in c['corp_name'].lower()]
    results.sort(key=lambda c: (not c['stock_code'].strip(), len(c['corp_name'])))
    return results[:30]

def get_kis_token():
    now = time.time()
    if KIS_TOKEN['token'] and KIS_TOKEN['expires_at'] > now + 60:
        return KIS_TOKEN['token']
    url = f"{KIS_BASE_URL}/oauth2/tokenP"
    body = json.dumps({'grant_type': 'client_credentials', 'appkey': KIS_APP_KEY, 'appsecret': KIS_APP_SECRET}).encode('utf-8')
    req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        KIS_TOKEN['token'] = data.get('access_token')
        KIS_TOKEN['expires_at'] = now + int(data.get('expires_in', 86400))
        return KIS_TOKEN['token']
    except Exception:
        return None

def get_kis_stock_price(stock_code):
    token = get_kis_token()
    if not token: return {'error': 'KIS 토큰 발급 실패'}
    url = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price"
    params = {'FID_COND_MRKT_DIV_CODE': 'J', 'FID_INPUT_ISCD': stock_code}
    req = urllib.request.Request(url + '?' + urllib.parse.urlencode(params), headers={
        'Content-Type': 'application/json', 'authorization': f'Bearer {token}',
        'appkey': KIS_APP_KEY, 'appsecret': KIS_APP_SECRET, 'tr_id': 'FHKST01010100'
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        if data.get('rt_cd') != '0': return {'error': data.get('msg1', '조회 실패')}
        o = data.get('output', {})
        return {
            'price': o.get('stck_prpr', ''), 'diff': o.get('prdy_vrss', ''),
            'diff_rate': o.get('prdy_ctrt', ''), 'diff_sign': o.get('prdy_vrss_sign', ''),
            'volume': o.get('acml_vol', ''), 'market_cap': o.get('hts_avls', ''),
            'per': o.get('per', ''), 'pbr': o.get('pbr', ''), 'eps': o.get('eps', '')
        }
    except Exception as e:
        return {'error': str(e)}

def get_kis_chart(stock_code, period_type):
    token = get_kis_token()
    if not token: return {'error': 'token error'}
    now = datetime.now()
    end_date = now.strftime('%Y%m%d')
    if period_type == '1M': start_date = (now - timedelta(days=30)).strftime('%Y%m%d'); div_code = 'D'
    elif period_type == '3M': start_date = (now - timedelta(days=90)).strftime('%Y%m%d'); div_code = 'D'
    elif period_type == '1Y': start_date = (now - timedelta(days=365)).strftime('%Y%m%d'); div_code = 'W'
    elif period_type == '3Y': start_date = (now - timedelta(days=365*3)).strftime('%Y%m%d'); div_code = 'M'
    elif period_type == '5Y': start_date = (now - timedelta(days=365*5)).strftime('%Y%m%d'); div_code = 'M'
    else: start_date = (now - timedelta(days=365*10)).strftime('%Y%m%d'); div_code = 'M'

    url = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
    params = {
        'FID_COND_MRKT_DIV_CODE': 'J', 'FID_INPUT_ISCD': stock_code,
        'FID_INPUT_DATE_1': start_date, 'FID_INPUT_DATE_2': end_date,
        'FID_PERIOD_DIV_CODE': div_code, 'FID_ORG_ADJ_PRC': '0'
    }
    req = urllib.request.Request(url + '?' + urllib.parse.urlencode(params), headers={
        'Content-Type': 'application/json', 'authorization': f'Bearer {token}',
        'appkey': KIS_APP_KEY, 'appsecret': KIS_APP_SECRET, 'tr_id': 'FHKST03010100'
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            if data.get('rt_cd') != '0': return {'error': data.get('msg1')}
            return {'status': '000', 'list': data.get('output2', [])}
    except Exception as e:
        return {'error': str(e)}

def fetch_dart_finance(corp_code, year, rep_code='11011', fs_div='CFS'):
    url = f"https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json?crtfc_key={DART_API_KEY}&corp_code={corp_code}&bsns_year={year}&reprt_code={rep_code}&fs_div={fs_div}"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {'status': 'error', 'message': str(e), 'list': []}

def fetch_naver_news(keyword, display=10):
    url = f"https://openapi.naver.com/v1/search/news.json?query={urllib.parse.quote(keyword)}&display={display}&sort=date"
    req = urllib.request.Request(url, headers={'X-Naver-Client-Id': NAVER_CLIENT_ID, 'X-Naver-Client-Secret': NAVER_CLIENT_SECRET})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {'error': str(e), 'items': []}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, *')
        
    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()
        
    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self._cors()
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))

    def _client_ip(self):
        fwd = self.headers.get('X-Forwarded-For')
        if fwd:
            return fwd.split(',')[0].strip()
        return self.client_address[0]

    def do_POST(self):
        if self.path == '/api/ai':
            ip = self._client_ip()
            if _rate_limited(ip):
                return self._send_json({'status': 'error', 'message': '요청이 많습니다. 잠시 후 다시 시도해주세요.'}, 429)
            try:
                length = int(self.headers.get('Content-Length', 0))
                req_json = json.loads(self.rfile.read(length))
                prompt = req_json.get('prompt', '')
                ckey = ('ai', prompt)
                cached = _cache_get(ckey)
                if cached is not None:
                    return self._send_json(cached)
                result = ask_gemini_ai(prompt)
                _cache_set(ckey, result)
                return self._send_json(result)
            except Exception as e:
                return self._send_json({'status': 'error', 'message': str(e)}, 500)
        self.send_response(404)
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query, encoding='utf-8')
        path = parsed.path

        if path in ('/', '/index.html'):
            body = INDEX_HTML.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Expires', '0')
            self.end_headers()
            self.wfile.write(body)
            return

        # 외부 API를 호출하는 엔드포인트는 캐시 + IP 레이트리밋을 통과시킨다.
        # (검색/정적 조회 성격의 /api/search, /api/us_search는 제외)
        EXTERNAL_API_PATHS = {
            '/api/finance', '/api/stock', '/api/news', '/api/chart',
            '/api/macro_chart', '/api/macro_batch', '/api/yahoo_news',
            '/api/us_finance', '/api/us_stock', '/api/us_chart',
        }
        if path in EXTERNAL_API_PATHS:
            ip = self._client_ip()
            if _rate_limited(ip):
                return self._send_json({'status': 'error', 'message': '요청이 많습니다. 잠시 후 다시 시도해주세요.'}, 429)
            ckey = (path, parsed.query)
            cached = _cache_get(ckey)
            if cached is not None:
                return self._send_json(cached)

        if path == '/api/search': return self._send_json({'status': '000', 'list': search_corp(params.get('keyword', [''])[0])})
        if path == '/api/finance':
            corp_code, year, rep_code, fs_div = params.get('corp_code',[''])[0], params.get('year',[''])[0], params.get('rep_code',['11011'])[0], params.get('fs_div',['CFS'])[0]
            data = fetch_dart_finance(corp_code, year, rep_code, fs_div)
            if data.get('status') != '000' and fs_div == 'CFS': data = fetch_dart_finance(corp_code, year, rep_code, 'OFS')
            _cache_set((path, parsed.query), data)
            return self._send_json(data)
        if path == '/api/stock':
            data = get_kis_stock_price(params.get('code', [''])[0])
            _cache_set((path, parsed.query), data)
            return self._send_json(data)
        if path == '/api/news':
            data = fetch_naver_news(params.get('keyword', [''])[0], int(params.get('display', ['10'])[0]))
            _cache_set((path, parsed.query), data)
            return self._send_json(data)
        if path == '/api/chart':
            data = get_kis_chart(params.get('code', [''])[0], params.get('period', ['1Y'])[0])
            _cache_set((path, parsed.query), data)
            return self._send_json(data)

        if path == '/api/macro_chart':
            chart_type = params.get('type', ['USD'])[0]
            data = resolve_macro(chart_type)
            _cache_set((path, parsed.query), data)
            return self._send_json(data)

        if path == '/api/macro_batch':
            types_csv = params.get('types', [''])[0]
            data = fetch_macro_batch(types_csv)
            _cache_set((path, parsed.query), data)
            return self._send_json(data)

        if path == '/api/yahoo_news':
            q = params.get('q', ['stock market economy'])[0]
            try:
                n = int(params.get('n', ['8'])[0])
            except Exception:
                n = 8
            data = fetch_yahoo_news(q, n)
            _cache_set((path, parsed.query), data)
            return self._send_json(data)

        # ── 미국 종목 API ──────────────────────────────────────
        if path == '/api/us_search':
            kw = params.get('keyword', [''])[0]
            return self._send_json({'status': '000', 'list': search_us_corp(kw)})

        if path == '/api/us_finance':
            ticker = params.get('ticker', [''])[0]
            period = int(params.get('period', ['5'])[0])
            data = fetch_sec_finance(ticker, period)
            _cache_set((path, parsed.query), data)
            return self._send_json(data)

        if path == '/api/us_stock':
            ticker = params.get('ticker', [''])[0]
            data = fetch_us_stock(ticker)
            _cache_set((path, parsed.query), data)
            return self._send_json(data)

        if path == '/api/us_chart':
            ticker = params.get('ticker', [''])[0]
            period = params.get('period', ['1Y'])[0]
            data = fetch_us_chart(ticker, period)
            _cache_set((path, parsed.query), data)
            return self._send_json(data)

        self.send_response(404)
        self._cors()
        self.end_headers()

# ★ 재사용 허용 서버 클래스 (포트 충돌 방지 핵심 로직)
class ReuseServer(ThreadingHTTPServer):
    allow_reuse_address = True

def open_when_ready(port, retries=40, delay=0.25):
    url = f"http://localhost:{port}/"
    for _ in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=1):
                break
        except Exception:
            time.sleep(delay)
    try:
        webbrowser.open(url)
    except Exception:
        pass

if __name__ == '__main__':
    # Railway 등 클라우드 배포 환경 감지 (PORT 환경변수는 플랫폼이 자동 주입)
    IS_CLOUD = bool(os.environ.get('PORT'))
    HOST = '0.0.0.0' if IS_CLOUD else 'localhost'
    port = int(os.environ.get('PORT', 8787))

    # ── [에러 홀딩 및 자동 종료 방어선 구축] ──
    try:
        load_corp_list()
        load_us_company_list()   # SEC EDGAR 미국 종목 목록 (비동기 백그라운드)
        # 야후 crumb 미리 획득 (백그라운드) — 첫 US 종목 조회 속도 향상
        threading.Thread(target=_yahoo_opener, daemon=True).start()
        get_kis_token()

        # 포트 충돌 여부를 명시적으로 잡아냅니다.
        try:
            httpd = ReuseServer((HOST, port), Handler)
        except OSError as e:
            print(f"\n[치명적 오류] {port}번 포트가 이미 사용 중입니다!")
            if IS_CLOUD:
                os._exit(1)
            print("원인: 이전에 실행한 분석기 창이 백그라운드에서 완전히 꺼지지 않았습니다.")
            print("해결: 작업관리자에서 'python.exe' 프로세스를 강제 종료하거나 컴퓨터를 재부팅하세요.")
            input("\n엔터 키를 누르면 창이 닫힙니다...")
            os._exit(1)

        if IS_CLOUD:
            print(f"\n✅ 서버 시작! (클라우드 배포 모드) → 포트 {port}\n")
        else:
            print(f"\n✅ 서버 시작! 브라우저가 자동으로 열립니다 → http://localhost:{port}")
            print("   (자동으로 안 열리면 위 주소를 직접 브라우저에 입력하세요)\n   종료: 이 창을 닫거나 Ctrl+C를 누르세요.\n")
            threading.Thread(target=open_when_ready, args=(port,), daemon=True).start()

        httpd.serve_forever()

    except KeyboardInterrupt:
        print("\n서버를 안전하게 종료합니다. 이용해 주셔서 감사합니다.")
    except Exception as e:
        print("\n[알 수 없는 치명적 오류 발생]")
        traceback.print_exc()
        if not IS_CLOUD:
            input("\n엔터 키를 누르면 창이 닫힙니다...")