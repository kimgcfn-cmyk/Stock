"""
한국주식 종목 캐시 생성 (kr_symbols_cache.json)
================================================================
integrated_strategy.py가 사용할 한국주식 스캔 대상 리스트를
FinanceDataReader로 코스피+코스닥 전체 상장종목에서 만든다.

[필터링 이유]
  코스피+코스닥 전체(약 2,500종목)를 그대로 넣으면
  1종목당 API호출+0.15초 대기 × 2,500 ≈ 20~40분 소요.
  매일 자동 실행(cron)엔 너무 길어서, 아래 기준으로 걸러낸다.

  1) 우선주 제외 (이름에 "우" 패턴 — 예: "삼성전자우", "LG화학우B")
  2) 스팩(SPAC) 제외 (이름에 "스팩" 포함)
  3) 리츠/기타 특수종목 제외 (이름에 "리츠" 포함) — 급등 스캐너 취지상 제외
  4) 시가총액 하한 필터 (기본 500억원) — 너무 작은 종목은 유동성 낮아 매매 리스크 큼
  5) 종목코드가 6자리 숫자가 아닌 경우 제외 (데이터 이상치 방지)

실행:
  venv/bin/python build_kr_symbol_cache.py                    # 기본(시총 500억 이상)
  venv/bin/python build_kr_symbol_cache.py --min-marcap 1000  # 시총 1000억 이상으로 더 좁힘
  venv/bin/python build_kr_symbol_cache.py --min-marcap 0     # 필터 없이 전체(느림, 비추천)

필요 패키지 (EC2에 없으면 설치):
  venv/bin/pip install finance-datareader
"""

import sys, json
from pathlib import Path

def build_cache(min_marcap_eok: float = 3000.0) -> list[dict]:
    """
    min_marcap_eok: 시가총액 하한 (억원 단위). 3000 = 3,000억원.
    반환: [{"symbol": "005930", "name": "삼성전자"}, ...]
    """
    try:
        import FinanceDataReader as fdr
    except ImportError:
        print("❌ FinanceDataReader 미설치")
        print("   설치: venv/bin/pip install finance-datareader")
        sys.exit(1)

    import pandas as pd

    print("  코스피+코스닥 상장종목 리스트 수집 중 (FinanceDataReader)...")
    frames = []
    for market in ["KOSPI", "KOSDAQ"]:
        try:
            df = fdr.StockListing(market)
            df["__market"] = market
            frames.append(df)
            print(f"    {market}: {len(df)}종목 수집")
        except Exception as e:
            print(f"    ⚠️ {market} 수집 실패: {e}")

    if not frames:
        print("❌ 종목 리스트 수집 완전 실패 — 네트워크/FDR 상태 확인 필요")
        sys.exit(1)

    df = pd.concat(frames, ignore_index=True)

    # 컬럼명이 FDR 버전에 따라 다를 수 있어 유연하게 탐색
    code_col = next((c for c in ["Code", "Symbol", "code"] if c in df.columns), None)
    name_col = next((c for c in ["Name", "name"] if c in df.columns), None)
    marcap_col = next((c for c in ["Marcap", "MarketCap", "marcap"] if c in df.columns), None)

    if not code_col or not name_col:
        print(f"❌ 예상 컬럼을 못 찾음 (현재 컬럼: {list(df.columns)})")
        sys.exit(1)

    before = len(df)

    # 1) 종목코드 6자리 숫자만
    df = df[df[code_col].astype(str).str.match(r"^\d{6}$")]
    # 2) 우선주 제외 (이름이 "우"/"우B" 등으로 끝나는 패턴)
    df = df[~df[name_col].astype(str).str.contains(r"\d?우(?:[A-Z])?$", regex=True, na=False)]
    # 3) 스팩 제외
    df = df[~df[name_col].astype(str).str.contains("스팩", na=False)]
    # 4) 리츠 제외
    df = df[~df[name_col].astype(str).str.contains("리츠", na=False)]
    # 5) 시가총액 필터 (컬럼 있을 때만)
    if marcap_col and min_marcap_eok > 0:
        min_marcap_won = min_marcap_eok * 1e8   # 억원 → 원
        df = df[df[marcap_col].fillna(0) >= min_marcap_won]
    elif min_marcap_eok > 0:
        print(f"  ⚠️ 시가총액 컬럼을 못 찾아 시총 필터는 건너뜀 (컬럼: {list(df.columns)})")

    after = len(df)
    print(f"  필터링: {before}종목 → {after}종목 "
          f"(우선주/스팩/리츠 제외, 시총 {min_marcap_eok:.0f}억 이상)")

    result = [{"symbol": str(row[code_col]), "name": str(row[name_col])}
              for _, row in df.iterrows()]
    return result

if __name__ == "__main__":
    args = sys.argv[1:]
    min_marcap = 3000.0
    if "--min-marcap" in args:
        try:
            min_marcap = float(args[args.index("--min-marcap") + 1])
        except (IndexError, ValueError):
            print("  --min-marcap 뒤에 숫자(억원)를 입력하세요. 기본 3000 사용")

    print(f"\n{'='*56}")
    print(f"  한국주식 종목 캐시 생성 (시총 {min_marcap:.0f}억원 이상)")
    print(f"{'='*56}")

    symbols = build_cache(min_marcap)

    out_path = Path("kr_symbols_cache.json")
    out_path.write_text(json.dumps(symbols, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n  💾 저장 완료: {out_path.resolve()} ({len(symbols)}종목)")
    print(f"  예상 스캔 시간: 약 {len(symbols)*0.15/60:.1f}분 (API 응답시간 별도)")
    if symbols:
        print(f"\n  샘플 (앞 5개): {symbols[:5]}")
