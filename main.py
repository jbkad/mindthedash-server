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
            return response.json()
    except Exception as e:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={'error': str(e)}
        )