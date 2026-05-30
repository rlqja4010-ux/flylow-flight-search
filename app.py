import os
import json
import uuid
from datetime import datetime
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv
import requests
from apscheduler.schedulers.background import BackgroundScheduler

load_dotenv(override=True)

app = Flask(__name__)

FLIGHT_API = os.getenv('FLIGHT_API', 'rapidapi')  # 'amadeus' or 'rapidapi'

DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
os.makedirs(DATA_DIR, exist_ok=True)

FAVORITES_FILE = os.path.join(DATA_DIR, 'favorites.json')
ALERTS_FILE    = os.path.join(DATA_DIR, 'alerts.json')
HISTORY_FILE   = os.path.join(DATA_DIR, 'price_history.json')

# ──────────────────────────────────────────────
# 유틸
# ──────────────────────────────────────────────
def load_json(path):
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []

def save_json(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def send_telegram(message):
    token   = os.getenv('TELEGRAM_BOT_TOKEN')
    chat_id = os.getenv('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        return False
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={'chat_id': chat_id, 'text': message, 'parse_mode': 'HTML'},
            timeout=10
        )
        return True
    except Exception:
        return False

def fmt_duration(d):
    if not d:
        return ''
    d = d.replace('PT', '')
    h = m = 0
    if 'H' in d:
        parts = d.split('H')
        h = int(parts[0])
        if 'M' in parts[1]:
            m = int(parts[1].replace('M', ''))
    elif 'M' in d:
        m = int(d.replace('M', ''))
    if h and m:
        return f"{h}시간 {m}분"
    if h:
        return f"{h}시간"
    return f"{m}분"

# ──────────────────────────────────────────────
# Amadeus
# ──────────────────────────────────────────────
def get_amadeus():
    from amadeus import Client
    return Client(
        client_id=os.getenv('AMADEUS_CLIENT_ID'),
        client_secret=os.getenv('AMADEUS_CLIENT_SECRET')
    )

def parse_amadeus_results(offers):
    results = []
    for offer in offers:
        legs = []
        for itin in offer.get('itineraries', []):
            segs = itin.get('segments', [])
            first, last = segs[0], segs[-1]
            stops = len(segs) - 1
            legs.append({
                'from':      first['departure']['iataCode'],
                'fromTime':  first['departure']['at'],
                'to':        last['arrival']['iataCode'],
                'toTime':    last['arrival']['at'],
                'duration':  fmt_duration(itin['duration']),
                'stops':     stops,
                'carrier':   first['carrierCode'],
                'stopCities':[s['arrival']['iataCode'] for s in segs[:-1]],
                'segments':  [{
                    'from':     s['departure']['iataCode'],
                    'fromTime': s['departure']['at'],
                    'to':       s['arrival']['iataCode'],
                    'toTime':   s['arrival']['at'],
                    'flightNo': s['carrierCode'] + s['number'],
                    'carrier':  s['carrierCode'],
                    'duration': fmt_duration(s['duration'])
                } for s in segs]
            })
        results.append({
            'id':               offer['id'],
            'price':            offer['price']['total'],
            'currency':         offer['price'].get('currency', 'KRW'),
            'legs':             legs,
            'validatingCarrier':offer.get('validatingAirlineCodes', [''])[0],
            'bookable':         offer.get('numberOfBookableSeats', 0)
        })
    results.sort(key=lambda x: float(x['price']))
    return results

def build_travelers(data):
    travelers, tid = [], 1
    for _ in range(int(data.get('adults', 1))):
        travelers.append({"id": str(tid), "travelerType": "ADULT"}); tid += 1
    for _ in range(int(data.get('children', 0))):
        travelers.append({"id": str(tid), "travelerType": "CHILD"}); tid += 1
    for _ in range(int(data.get('infants', 0))):
        travelers.append({"id": str(tid), "travelerType": "HELD_INFANT"}); tid += 1
    return travelers

def search_amadeus(data):
    from amadeus import ResponseError
    trip_type = data.get('tripType', 'oneway')
    amadeus = get_amadeus()
    if trip_type == 'multicity':
        ods = [{"id": str(i+1),
                "originLocationCode": leg['from'],
                "destinationLocationCode": leg['to'],
                "departureDateTimeRange": {"date": leg['date']}}
               for i, leg in enumerate(data.get('legs', []))]
        resp = amadeus.shopping.flight_offers_search.post({
            "currencyCode": "KRW",
            "originDestinations": ods,
            "travelers": build_travelers(data),
            "sources": ["GDS"],
            "searchCriteria": {
                "maxFlightOffers": 20,
                "cabinRestrictions": [{"cabin": data.get('cabin', 'ECONOMY'),
                                       "originDestinationIds": [str(i+1) for i in range(len(ods))]}]
            }
        })
    else:
        kwargs = {
            "originLocationCode":      data['from'],
            "destinationLocationCode": data['to'],
            "departureDate":           data['departDate'],
            "adults":                  int(data.get('adults', 1)),
            "currencyCode":            "KRW",
            "max":                     20,
            "travelClass":             data.get('cabin', 'ECONOMY')
        }
        if int(data.get('children', 0)) > 0: kwargs['children'] = int(data['children'])
        if int(data.get('infants',  0)) > 0: kwargs['infants']  = int(data['infants'])
        if trip_type == 'roundtrip' and data.get('returnDate'):
            kwargs['returnDate'] = data['returnDate']
        resp = amadeus.shopping.flight_offers_search.get(**kwargs)
    return parse_amadeus_results(resp.data)

def airports_amadeus(keyword):
    resp = get_amadeus().reference_data.locations.get(keyword=keyword, subType='AIRPORT,CITY')
    return [{
        'iata':    loc['iataCode'],
        'city':    loc.get('address', {}).get('cityName', loc['name']),
        'country': loc.get('address', {}).get('countryName', ''),
        'name':    loc['name'],
        'label':   f"{loc['iataCode']} — {loc.get('address',{}).get('cityName', loc['name'])} ({loc.get('address',{}).get('countryName','')})"
    } for loc in resp.data[:8]]

# ──────────────────────────────────────────────
# RapidAPI (Sky Scrapper)
# ──────────────────────────────────────────────
RAPID_HOST   = 'sky-scrapper.p.rapidapi.com'
RAPID_HEADERS = {
    'x-rapidapi-key':  os.getenv('RAPIDAPI_KEY', ''),
    'x-rapidapi-host': RAPID_HOST
}

def airports_rapidapi(keyword):
    try:
        r = requests.get(
            f'https://{RAPID_HOST}/api/v1/flights/searchAirport',
            params={'query': keyword, 'locale': 'ko-KR'},
            headers=RAPID_HEADERS, timeout=10
        )
        data = r.json()
        results = []
        for item in (data.get('data') or [])[:8]:
            presentation = item.get('presentation', {})
            nav = item.get('navigation', {})
            iata = nav.get('relevantFlightParams', {}).get('skyId', '') or item.get('skyId', '')
            city = presentation.get('suggestionTitle', '') or presentation.get('title', '')
            country = presentation.get('subtitle', '')
            results.append({
                'iata':    iata,
                'city':    city,
                'country': country,
                'name':    city,
                'label':   f"{iata} — {city} ({country})"
            })
        return results
    except Exception:
        return []

def parse_rapidapi_results(itineraries, currency='KRW'):
    results = []
    for i, item in enumerate(itineraries):
        legs_data = item.get('legs', [])
        price_info = item.get('price', {})
        price = price_info.get('raw') or price_info.get('formatted', '0')
        try:
            price = str(int(float(str(price).replace(',', '').replace('₩', '').strip())))
        except Exception:
            price = str(price)

        legs = []
        for leg in legs_data:
            segs = leg.get('segments', [])
            stops = leg.get('stopCount', len(segs) - 1)
            origin = leg.get('origin', {})
            dest = leg.get('destination', {})
            carrier_name = ''
            if leg.get('carriers', {}).get('marketing'):
                carrier_name = leg['carriers']['marketing'][0].get('alternateId', '')

            parsed_segs = []
            for s in segs:
                parsed_segs.append({
                    'from':     s.get('origin', {}).get('flightPlaceId', ''),
                    'fromTime': s.get('departure', ''),
                    'to':       s.get('destination', {}).get('flightPlaceId', ''),
                    'toTime':   s.get('arrival', ''),
                    'flightNo': s.get('flightNumber', ''),
                    'carrier':  s.get('operatingCarrier', {}).get('alternateId', ''),
                    'duration': f"{s.get('durationInMinutes', 0)//60}시간 {s.get('durationInMinutes', 0)%60}분"
                })

            dur_min = leg.get('durationInMinutes', 0)
            legs.append({
                'from':      origin.get('id', '') or origin.get('flightPlaceId', ''),
                'fromTime':  leg.get('departure', ''),
                'to':        dest.get('id', '') or dest.get('flightPlaceId', ''),
                'toTime':    leg.get('arrival', ''),
                'duration':  f"{dur_min//60}시간 {dur_min%60}분",
                'stops':     stops,
                'carrier':   carrier_name,
                'stopCities':[s.get('origin', {}).get('flightPlaceId', '') for s in segs[1:]],
                'segments':  parsed_segs
            })

        # 에이전트별 가격 (Skyscanner pricing_options)
        agents = []
        seen_agents = {}
        for opt in item.get('pricing_options', []):
            opt_price_raw = opt.get('price', {}).get('raw', 0)
            try:
                opt_price = int(float(opt_price_raw))
            except Exception:
                opt_price = 0
            for ag in opt.get('agents', []):
                name = ag.get('name', '') or ag.get('id', '')
                if not name:
                    continue
                if name not in seen_agents or opt_price < seen_agents[name]['price']:
                    seen_agents[name] = {
                        'name':  name,
                        'price': opt_price,
                        'url':   ag.get('url', '')
                    }
        agents = sorted(seen_agents.values(), key=lambda x: x['price'])[:10]

        results.append({
            'id':               str(i),
            'price':            price,
            'currency':         currency,
            'legs':             legs,
            'validatingCarrier':legs[0]['carrier'] if legs else '',
            'bookable':         1,
            'agents':           agents
        })
    try:
        results.sort(key=lambda x: float(x['price']))
    except Exception:
        pass
    return results

def search_rapidapi(data):
    trip_type = data.get('tripType', 'oneway')

    # 공항 skyId 조회
    def get_sky_id(iata):
        try:
            r = requests.get(
                f'https://{RAPID_HOST}/api/v1/flights/searchAirport',
                params={'query': iata, 'locale': 'ko-KR'},
                headers=RAPID_HEADERS, timeout=10
            )
            items = r.json().get('data') or []
            for item in items:
                sky = item.get('navigation', {}).get('relevantFlightParams', {}).get('skyId', '')
                entity = item.get('navigation', {}).get('relevantFlightParams', {}).get('entityId', '')
                if sky:
                    return sky, entity
        except Exception:
            pass
        return iata, ''

    origin_sky, origin_entity = get_sky_id(data['from'])
    dest_sky, dest_entity = get_sky_id(data['to'])

    cabin_map = {'ECONOMY': 'economy', 'PREMIUM_ECONOMY': 'premium_economy',
                 'BUSINESS': 'business', 'FIRST': 'first'}
    cabin = cabin_map.get(data.get('cabin', 'ECONOMY'), 'economy')

    params = {
        'originSkyId':           origin_sky,
        'destinationSkyId':      dest_sky,
        'originEntityId':        origin_entity,
        'destinationEntityId':   dest_entity,
        'date':                  data['departDate'],
        'adults':                int(data.get('adults', 1)),
        'childrens':             int(data.get('children', 0)),
        'infants':               int(data.get('infants', 0)),
        'cabinClass':            cabin,
        'currency':              'KRW',
        'locale':                'ko-KR',
        'market':                'KR',
        'countryCode':           'KR'
    }
    if trip_type == 'roundtrip' and data.get('returnDate'):
        params['returnDate'] = data['returnDate']

    endpoint = 'searchFlightsComplete' if trip_type == 'roundtrip' else 'searchFlights'
    r = requests.get(
        f'https://{RAPID_HOST}/api/v2/flights/{endpoint}',
        params=params, headers=RAPID_HEADERS, timeout=30
    )
    resp = r.json()

    itineraries = (resp.get('data') or {}).get('itineraries') or []
    return parse_rapidapi_results(itineraries)

# ──────────────────────────────────────────────
# 라우트
# ──────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/provider')
def get_provider():
    return jsonify({'provider': FLIGHT_API})

@app.route('/api/airports')
def search_airports():
    keyword = request.args.get('q', '').strip()
    if not keyword:
        return jsonify([])
    try:
        if FLIGHT_API == 'rapidapi':
            return jsonify(airports_rapidapi(keyword))
        else:
            return jsonify(airports_amadeus(keyword))
    except Exception:
        return jsonify([])

@app.route('/api/search', methods=['POST'])
def search_flights():
    data = request.json
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        results_by_source = {}

        def try_amadeus():
            try:
                r = search_amadeus(data)
                for item in r: item['source'] = 'Amadeus'
                return 'amadeus', r
            except Exception as e:
                return 'amadeus', []

        def try_rapidapi():
            try:
                r = search_rapidapi(data)
                for item in r: item['source'] = 'Skyscanner'
                return 'rapidapi', r
            except Exception as e:
                return 'rapidapi', []

        has_amadeus = bool(os.getenv('AMADEUS_CLIENT_ID') and os.getenv('AMADEUS_CLIENT_SECRET'))
        has_rapid   = bool(os.getenv('RAPIDAPI_KEY'))

        with ThreadPoolExecutor(max_workers=2) as ex:
            futures = []
            if has_amadeus: futures.append(ex.submit(try_amadeus))
            if has_rapid:   futures.append(ex.submit(try_rapidapi))
            for f in as_completed(futures):
                src, r = f.result()
                if r: results_by_source[src] = r

        # 두 소스 병합: 같은 항공편은 가격 비교 정보 추가
        all_results = []
        for src, items in results_by_source.items():
            all_results.extend(items)

        # 가격순 정렬
        try:
            all_results.sort(key=lambda x: float(x['price']))
        except Exception:
            pass

        if all_results:
            history = load_json(HISTORY_FILE)
            history.append({
                'from':       data.get('from', ''),
                'to':         data.get('to', ''),
                'departDate': data.get('departDate', ''),
                'tripType':   data.get('tripType', 'oneway'),
                'price':      all_results[0]['price'],
                'provider':   ','.join(results_by_source.keys()),
                'searchedAt': datetime.now().isoformat()
            })
            save_json(HISTORY_FILE, history[-200:])

        return jsonify({
            'success': True,
            'results': all_results,
            'sources': list(results_by_source.keys()),
            'counts':  {k: len(v) for k, v in results_by_source.items()}
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/flexible', methods=['POST'])
def search_flexible():
    data = request.json
    if FLIGHT_API != 'amadeus':
        return jsonify({'success': False, 'error': '날짜 유연 검색은 Amadeus에서만 지원됩니다.'}), 400
    try:
        kwargs = {'origin': data['from'], 'destination': data['to']}
        if data.get('month'):
            kwargs['departureDate'] = data['month']
        if data.get('tripType') == 'oneway':
            kwargs['oneWay'] = True
        resp = get_amadeus().shopping.flight_dates.get(**kwargs)
        results = sorted([{
            'departureDate': item['departureDate'],
            'returnDate':    item.get('returnDate', ''),
            'price':         item['price']['total']
        } for item in resp.data], key=lambda x: float(x['price']))
        return jsonify({'success': True, 'results': results[:31]})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# 즐겨찾기
@app.route('/api/favorites', methods=['GET'])
def get_favorites():
    return jsonify(load_json(FAVORITES_FILE))

@app.route('/api/favorites', methods=['POST'])
def add_favorite():
    data = request.json
    favorites = load_json(FAVORITES_FILE)
    if any(f['from'] == data['from'] and f['to'] == data['to'] for f in favorites):
        return jsonify({'success': False, 'error': '이미 저장된 노선입니다.'})
    data.update({'id': str(uuid.uuid4()), 'savedAt': datetime.now().isoformat()})
    favorites.append(data)
    save_json(FAVORITES_FILE, favorites)
    return jsonify({'success': True, 'favorite': data})

@app.route('/api/favorites/<fid>', methods=['DELETE'])
def delete_favorite(fid):
    favorites = [f for f in load_json(FAVORITES_FILE) if f['id'] != fid]
    save_json(FAVORITES_FILE, favorites)
    return jsonify({'success': True})

# 알림
@app.route('/api/alerts', methods=['GET'])
def get_alerts():
    return jsonify(load_json(ALERTS_FILE))

@app.route('/api/alerts', methods=['POST'])
def add_alert():
    data = request.json
    alerts = load_json(ALERTS_FILE)
    data.update({'id': str(uuid.uuid4()), 'createdAt': datetime.now().isoformat(),
                 'active': True, 'lastChecked': None})
    alerts.append(data)
    save_json(ALERTS_FILE, alerts)
    return jsonify({'success': True, 'alert': data})

@app.route('/api/alerts/<aid>', methods=['DELETE'])
def delete_alert(aid):
    alerts = [a for a in load_json(ALERTS_FILE) if a['id'] != aid]
    save_json(ALERTS_FILE, alerts)
    return jsonify({'success': True})

# 가격 히스토리
@app.route('/api/history')
def get_history():
    history = load_json(HISTORY_FILE)
    return jsonify(list(reversed(history[-20:])))

# ──────────────────────────────────────────────
# 가격 알림 스케줄러
# ──────────────────────────────────────────────
def check_alerts_job():
    alerts = load_json(ALERTS_FILE)
    if not alerts:
        return
    changed = False
    for alert in alerts:
        if not alert.get('active'):
            continue
        try:
            if FLIGHT_API == 'rapidapi':
                results = search_rapidapi({
                    'from': alert['from'], 'to': alert['to'],
                    'departDate': alert['departDate'],
                    'adults': 1, 'tripType': 'oneway'
                })
                cur = float(results[0]['price']) if results else None
            else:
                from amadeus import Client
                amadeus = get_amadeus()
                resp = amadeus.shopping.flight_offers_search.get(
                    originLocationCode=alert['from'],
                    destinationLocationCode=alert['to'],
                    departureDate=alert['departDate'],
                    adults=1, currencyCode='KRW', max=1
                )
                cur = float(resp.data[0]['price']['total']) if resp.data else None

            if cur is not None:
                target = float(alert['targetPrice'])
                if cur <= target:
                    date_c = alert['departDate'].replace('-', '')
                    link = f"https://flight.naver.com/flights/international/{alert['from']}-{alert['to']}-{date_c}?adult=1&fareType=Y"
                    send_telegram(
                        f"✈️ <b>항공권 가격 알림!</b>\n\n"
                        f"노선: {alert['from']} → {alert['to']}\n"
                        f"날짜: {alert['departDate']}\n"
                        f"현재가: <b>{int(cur):,}원</b> (목표: {int(target):,}원)\n\n"
                        f"🔗 <a href='{link}'>네이버 항공 바로가기</a>"
                    )
            alert['lastChecked'] = datetime.now().isoformat()
            changed = True
        except Exception:
            pass
    if changed:
        save_json(ALERTS_FILE, alerts)

scheduler = BackgroundScheduler()
scheduler.add_job(check_alerts_job, 'interval', hours=1)
scheduler.start()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5090, use_reloader=False)
