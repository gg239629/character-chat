"""SSE carries no chat bodies, identities, secrets or hidden-channel metadata."""
import asyncio
from fastapi import APIRouter,Request
from starlette.responses import StreamingResponse
from backend.app.infrastructure import storage,events

router=APIRouter()

@router.get('/api/events')
async def stream_events(request:Request):
    user=storage.scope.get()[0]
    async def stream():
        async with events.subscribe(user) as queue:
            yield 'event: ready\ndata: {}\n\n'
            while not await request.is_disconnected():
                try:
                    await asyncio.wait_for(queue.get(),20)
                    # Coalesce one transaction burst; no message text crosses the bus.
                    await asyncio.sleep(.1)
                    while not queue.empty():queue.get_nowait()
                    yield 'event: changed\ndata: {}\n\n'
                except asyncio.TimeoutError:yield ': heartbeat\n\n'
    return StreamingResponse(stream(),media_type='text/event-stream',headers={'Cache-Control':'no-store','X-Accel-Buffering':'no'})
