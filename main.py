from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi import status
import os
import httpx
from dotenv import load_dotenv

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

def normalize_departure_board(payload):
    """
    Normalise RailData LDBWS GetDepartureBoard response into a stable shape:
    { departures: { all: [ ... ] } }
    """
    result = (
        payload.get('GetDepartureBoardResponse', {})
        .get('GetDepartureBoardResult', {})
    )
    services = _to_list(result.get('trainServices', {}).get('service', []))

    normalized = []
    for svc in services:
        destinations = _to_list(svc.get('destination', {}).get('location', []))
        calling_points = (
            svc.get('subsequentCallingPoints', {})
            .get('callingPointList', [])
        )
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
            'platform': svc.get('platform'),
            'operator_name': svc.get('operator'),
            'destination_name': destinations[0].get('locationName') if destinations else None,
            'station_detail': {
                'destination': {
                    'station_code': destinations[0].get('crs') if destinations else None,
                },
                'calling_at': calling_at,
            },
        })

    return {
        'departures': {
            'all': normalized
        }
    }

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
