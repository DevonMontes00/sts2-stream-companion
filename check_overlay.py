import asyncio
import websockets

async def test():
    try:
        async with websockets.connect('ws://localhost:5000/ws') as ws:
            print('Connected to WebSocket OK')
            await ws.send('{\"type\": \"startup\", \"channel\": \"test\", \"runs\": 999}')
            print('Event sent — did the overlay show anything?')
    except Exception as e:
        print(f'FAILED: {e}')

asyncio.run(test())