from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi import status
import os
import httpx
from dotenv import load_dotenv
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=['http://localhost:3000', 'https://mindthedash.netlify.app'],
    allow_credentials=True,
    allow_methods=['GET'],
    allow_headers=['*'],
)

RAILDATA_API_KEY = os.environ.get('RAILDATA_API_KEY') 
RAILDATA_BASE_URL = os.environ.get('RAILDATA_URL')

def _to_list(maybe_list):
    if not maybe_list:
        return []
    return maybe_list if isinstance(maybe_list, list) else [maybe_list]

def _extract_services(payload):
    if isinstance(payload, list):
        return payload

    if 'GetDepartureBoardResponse' in payload:
        result = (
            payload.get('GetDepartureBoardResponse', {})
            .get('GetDepartureBoardResult', {})
        )
        return _to_list(result.get('trainServices', {}).get('service', []))

    if 'GetDepartureBoardWithDetailsResponse' in payload:
        result = (
            payload.get('GetDepartureBoardWithDetailsResponse', {})
            .get('GetDepartureBoardWithDetailsResult', {})
        )
        return _to_list(result.get('trainServices', {}).get('service', []))

    if 'GetDepBoardWithDetailsResponse' in payload:
        result = (
            payload.get('GetDepBoardWithDetailsResponse', {})
            .get('GetDepBoardWithDetailsResult', {})
        )
        return _to_list(result.get('trainServices', {}).get('service', []))

    if 'trainServices' in payload:
        services = payload.get('trainServices', [])
        if isinstance(services, dict):
            services = services.get('service', [])
        return _to_list(services)

    return []

def normalize_departure_board(payload):
    """
    Normalise RailData LDBWS GetDepartureBoard response into a stable shape:
    { departures: { all: [ ... ] } }
    """
    services = _extract_services(payload)

    normalized = []
    for svc in services:
        if not isinstance(svc, dict):
            continue
        destinations = svc.get('destination', {})
        if isinstance(destinations, dict):
            destinations = destinations.get('location', [])
        destinations = _to_list(destinations)
        subsequent = svc.get('subsequentCallingPoints', {})
        if isinstance(subsequent, list):
            calling_points = subsequent
        else:
            calling_points = subsequent.get('callingPointList', [])
        calling_points = _to_list(calling_points)

        calling_at = []
        for group in calling_points:
            for cp in _to_list(group.get('callingPoint', [])):
                calling_at.append({
                    'station_code': cp.get('crs'),
                    'station_name': cp.get('locationName'),
                })

        normalized.append({
            'aimed_departure_time': svc.get('std'),
            'expected_departure_time': svc.get('etd'),
            'platform': svc.get('platform'),
            'operator_name': svc.get('operator'),
            'delay_reason': svc.get('delayReason'),
            'cancel_reason': svc.get('cancelReason'),
            'is_cancelled': svc.get('isCancelled'),
            'destination_name': destinations[0].get('locationName') if destinations else None,
            'station_detail': {
                'destination': {
                    'station_code': destinations[0].get('crs') if destinations else None,
                },
                'calling_at': calling_at,
            },
        })

    normalized = filter_departures_to_one_am(normalized)

    return {
        'departures': {
            'all': normalized
        }
    }

def filter_departures_to_one_am(departures):
    tz = ZoneInfo('Europe/London')
    now = datetime.now(tz)
    end = datetime.combine((now + timedelta(days=1)).date(), time(1, 0), tzinfo=tz)

    def _parse_departure_time(value):
        if not value or not isinstance(value, str):
            return None
        raw = value.strip().upper()
        is_pm = raw.endswith('PM')
        is_am = raw.endswith('AM')
        raw = raw.replace('AM', '').replace('PM', '').strip()
        try:
            hh, mm = raw.split(':', 1)
            hour = int(hh)
            minute = int(mm)
            if is_pm and hour < 12:
                hour += 12
            if is_am and hour == 12:
                hour = 0
            dep_time = time(hour, minute)
        except Exception:
            return None
        dep_dt = datetime.combine(now.date(), dep_time, tzinfo=tz)
        if dep_dt < now:
            dep_dt = dep_dt + timedelta(days=1)
        return dep_dt

    filtered = []
    for dep in departures:
        std = dep.get('aimed_departure_time')
        etd = dep.get('expected_departure_time')
        dep_dt = _parse_departure_time(etd) or _parse_departure_time(std)
        if etd in ('Delayed', 'On time', 'Cancelled'):
            dep_dt = _parse_departure_time(std)
        if dep_dt and now <= dep_dt <= end:
            filtered.append(dep)
    return filtered

@app.get('/')
async def get_station_board(
    station: str = Query(..., min_length=3, max_length=3),
    numRows: int = Query(100)
):
    url = f'{RAILDATA_BASE_URL}/{station.upper()}?numRows={numRows}'

    headers = {
        'x-apikey': RAILDATA_API_KEY,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, headers=headers)
            data = response.json()
            return normalize_departure_board(data)
    except Exception as e:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={'error': str(e)}
        )

@app.get('/debug')
async def debug_station_board(
    station: str = Query(..., min_length=3, max_length=3),
    numRows: int = Query(10)
):
    url = f'{RAILDATA_BASE_URL}/{station.upper()}?numRows={numRows}'

    headers = {
        'x-apikey': RAILDATA_API_KEY,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, headers=headers)
            try:
                data = response.json()
            except Exception:
                data = {}
            return {
                'status': response.status_code,
                'url': url,
                'text_preview': response.text[:500],
                'raw': data,
                'normalized': normalize_departure_board(data)
            }
    except Exception as e:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={'error': str(e)}
        )
